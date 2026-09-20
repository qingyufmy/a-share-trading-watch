#!/usr/bin/env python3
import json
import math
import os
import sqlite3
from datetime import datetime, time as dtime
from pathlib import Path

from core import sim_broker
from core import intraday_timing_v2
from core import strategy_discipline
from core import ledger_reconciliation
from core import paper_costs


TICK = 0.01
DEFAULT_BUY_QTY = 100
DEFAULT_SELL_QTY = 100
MAX_BUY_PARTICIPATION_1M = 0.03
DEFAULT_INITIAL_CASH = 1_000_000.0
DEFAULT_RADAR_BUY_PCT = 0.03
DEFAULT_P1_BUY_PCT = 0.06
# 仓位采用“首次试错、确认加仓、再次确认”的阶梯，不用固定100股替代仓位管理。
DEFAULT_INITIAL_POSITION_PCT = 0.03
DEFAULT_ADD1_TARGET_POSITION_PCT = 0.06
DEFAULT_ADD2_TARGET_POSITION_PCT = 0.08
DEFAULT_MAX_SINGLE_POSITION_PCT = 0.085
DEFAULT_DEFENSIVE_MAX_SINGLE_POSITION_PCT = 0.055
DEFAULT_MAX_SINGLE_ORDER_PCT = 0.15  # 兼容旧环境变量；实际单票上限由 position ladder 控制。
DEFAULT_HARD_RISK_SELL_PCT = 1.00
DEFAULT_SOFT_RISK_SELL_PCT = 0.33
DEFAULT_BUY_WARN_POSITION_PCT = 90.0
DEFAULT_BUY_HARD_POSITION_PCT = 95.0
DEFAULT_HIGH_EXPOSURE_AMOUNT_1M = 1.60
DEFAULT_HIGH_EXPOSURE_AMOUNT_5M = 1.25
DEFAULT_HIGH_EXPOSURE_MAX_VWAP_DISTANCE_PCT = 0.60
# A 股 T+1 买入当日不可卖，新增风险暴露按总资产单独设上限。
DEFAULT_T1_ENTRY_RISK_PCT = 0.06
DEFAULT_MARKET_MAX_ACTIVE_BUYS = 2
DEFAULT_MARKET_MAX_BUYS_PER_60M = 2


def runtime_paths(base_dir):
    base = Path(base_dir)
    runtime = base / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return {
        "db": runtime / "paper_trading.sqlite",
        "events": runtime / "paper_trades.jsonl",
        "latest": base / "web_dashboard" / "data" / "runtime" / "paper_trading.json",
    }


def ceil_to_tick(value):
    return math.ceil(float(value) / TICK) * TICK


def floor_to_tick(value):
    return math.floor(float(value) / TICK) * TICK


def f2(value):
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def paper_initial_cash():
    try:
        return float(os.environ.get("A_SHARE_PAPER_INITIAL_CASH") or DEFAULT_INITIAL_CASH)
    except Exception:
        return DEFAULT_INITIAL_CASH


def cash_from_filled_orders(con, initial_cash=None, cutoff_at=None):
    cash = float(initial_cash if initial_cash is not None else paper_initial_cash())
    sql = """
        SELECT side, qty, fill_price, COALESCE(fees_total, 0)
        FROM paper_orders
        WHERE status IN ('FILLED', 'PARTIAL_FILLED')
    """
    params = []
    if cutoff_at:
        sql += " AND created_at <= ?"
        params.append(str(cutoff_at))
    rows = con.execute(sql, params).fetchall()
    for side, qty, fill_price, fees in rows:
        amount = float(qty or 0) * float(fill_price or 0)
        if side == "BUY":
            cash -= amount
        elif side == "SELL":
            cash += amount
        cash -= float(fees)
    return cash


def previous_account_snapshot(con, trading_date, initial_cash=None):
    """Return the latest account snapshot before ``trading_date``.

    Position snapshots are written whenever the workbench refreshes. Combining
    the prior day's latest marked market value with cash up to that snapshot
    gives an account-level daily P/L baseline that includes positions sold today.
    """
    if not trading_date:
        return None
    snapshot_floor = ledger_reconciliation.latest_cutoff(con) or ''
    # Account snapshots are written even when the account is flat. Position
    # snapshots alone cannot provide a valid prior-close baseline on empty days.
    account_row = con.execute(
        """
        SELECT trading_date, snapshot_at, cash, market_value, total_assets
        FROM paper_account_snapshots
        WHERE trading_date < ? AND snapshot_at >= ?
        ORDER BY trading_date DESC, snapshot_at DESC
        LIMIT 1
        """,
        (trading_date, snapshot_floor),
    ).fetchone()
    if account_row:
        return {
            "trading_date": account_row[0],
            "snapshot_at": account_row[1],
            "cash": float(account_row[2] or 0),
            "market_value": float(account_row[3] or 0),
            "total_assets": float(account_row[4] or 0),
        }
    row = con.execute(
        """
        SELECT trading_date, snapshot_at
        FROM paper_position_snapshots
        WHERE trading_date < ? AND snapshot_at >= ?
        ORDER BY trading_date DESC, snapshot_at DESC
        LIMIT 1
        """,
        (trading_date, snapshot_floor),
    ).fetchone()
    if not row:
        return None
    prev_date, snapshot_at = row[0], row[1]
    value_row = con.execute(
        """
        SELECT COALESCE(SUM(market_value), 0)
        FROM paper_position_snapshots
        WHERE trading_date=? AND snapshot_at=?
        """,
        (prev_date, snapshot_at),
    ).fetchone()
    market_value = float((value_row or [0])[0] or 0)
    cash = cash_from_filled_orders(con, initial_cash=initial_cash, cutoff_at=snapshot_at)
    return {
        "trading_date": prev_date,
        "snapshot_at": snapshot_at,
        "cash": cash,
        "market_value": market_value,
        "total_assets": cash + market_value,
    }


def account_exposure_snapshot(con, initial_cash=None, price_map=None, trading_date=None, now=None):
    """Estimate current paper account exposure before the next simulated order.

    Prefer live marks from ``price_map`` so buy gates and portfolio risk controls
    use real market value. Fall back to avg_cost only when a quote is missing.
    """
    initial_cash = float(initial_cash if initial_cash is not None else paper_initial_cash())
    cash = cash_from_filled_orders(con, initial_cash)
    prices = normalize_price_map(price_map)
    rows = con.execute(
        """
        SELECT symbol, name, quantity, sellable, avg_cost, source, updated_at
        FROM paper_positions
        WHERE quantity > 0
        """
    ).fetchall()
    exposure = 0.0
    total_cost = 0.0
    total_day_pnl = 0.0
    total_day_base = 0.0
    has_day_pnl = False
    position_count = 0
    unmarked_positions = 0
    now_value = now or datetime.now()
    for symbol, name, qty, sellable, avg_cost, source, updated_at in rows:
        try:
            qty = int(qty or 0)
            cost = float(avg_cost or 0)
        except Exception:
            continue
        if qty <= 0:
            continue
        symbol = str(symbol)
        quote = prices.get(symbol) or {}
        mark = quote.get("last_price")
        try:
            mark = float(mark) if mark is not None and mark > 0 else None
        except Exception:
            mark = None
        if mark is None:
            mark = cost
            unmarked_positions += 1
        pos = {
            "symbol": symbol,
            "name": name,
            "quantity": qty,
            "sellable": sellable or 0,
            "avg_cost": cost,
            "source": source,
            "updated_at": updated_at,
        }
        position_count += 1
        exposure += max(0.0, qty * mark)
        total_cost += max(0.0, qty * cost)
        if trading_date and quote.get("last_price") is not None:
            day_pnl, _, _ = calc_position_day_pnl(con, pos, quote, trading_date)
            if day_pnl is not None:
                has_day_pnl = True
                total_day_pnl += day_pnl
                total_day_base += max(0.0, qty * mark - day_pnl)
    total_assets = cash + exposure
    total_pnl = total_assets - initial_cash
    previous = previous_account_snapshot(con, trading_date, initial_cash) if trading_date else None
    position_day_pnl = total_day_pnl if has_day_pnl else None
    position_day_pnl_pct = (total_day_pnl / total_day_base * 100) if has_day_pnl and total_day_base else None
    asset_day_pnl = None
    asset_day_pnl_pct = None
    if previous and previous.get("total_assets"):
        asset_day_pnl = total_assets - float(previous["total_assets"])
        asset_day_pnl_pct = asset_day_pnl / float(previous["total_assets"]) * 100
    return annotate_account_quality(con, {
        "initial_cash": initial_cash,
        "cash": cash,
        "market_value_estimate": exposure,
        "total_assets_estimate": total_assets,
        "position_pct": (exposure / total_assets * 100) if total_assets else 0.0,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / initial_cash * 100) if initial_cash else None,
        "day_pnl": asset_day_pnl if asset_day_pnl is not None else position_day_pnl,
        "day_pnl_pct": asset_day_pnl_pct if asset_day_pnl_pct is not None else position_day_pnl_pct,
        "day_pnl_source": "account_total_assets" if asset_day_pnl is not None else "open_positions",
        "day_pnl_basis": "total_assets_vs_previous_close" if asset_day_pnl is not None else "open_positions_fallback",
        "day_pnl_basis_note": "总资产较上一交易日收盘变化" if asset_day_pnl is not None else "无上一交易日账户快照，按持仓表现估算",
        "previous_total_assets": previous.get("total_assets") if previous else None,
        "previous_snapshot_at": previous.get("snapshot_at") if previous else None,
        "previous_trading_date": previous.get("trading_date") if previous else None,
        "position_day_pnl": position_day_pnl,
        "position_day_pnl_pct": position_day_pnl_pct,
        "position_count": position_count,
        "unmarked_positions": unmarked_positions,
        "mark_source": "price_map" if prices and unmarked_positions < position_count else "avg_cost_fallback",
        "updated_at": now_value.strftime("%Y-%m-%d %H:%M:%S"),
    })


def account_exposure_for_base(base_dir, trading_date=None, price_map=None, now=None, initial_cash=None):
    con = init_db(base_dir)
    if trading_date:
        settle_t1_buys(con, trading_date)
    return account_exposure_snapshot(
        con,
        initial_cash=initial_cash,
        price_map=price_map,
        trading_date=trading_date,
        now=now,
    )


def parse_datetime_text(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    candidates = [
        raw,
        raw[:19],
        raw[:10],
        raw[:14],
        raw[:8],
    ]
    for text in candidates:
        if not text:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
    return None


def holding_days_for_position(con, pos, now):
    symbol = pos.get("symbol")
    start_raw = None
    if symbol:
        row = con.execute(
            """
            SELECT created_at, side, qty
            FROM paper_orders
            WHERE symbol=? AND side IN ('BUY', 'SELL') AND status IN ('FILLED', 'PARTIAL_FILLED')
            ORDER BY created_at, order_id
            """,
            (symbol,),
        ).fetchall()
        lots = []
        for created_at, side, qty in row:
            try:
                lot_qty = int(qty or 0)
            except Exception:
                lot_qty = 0
            if lot_qty <= 0:
                continue
            if side == "BUY":
                lots.append([lot_qty, created_at])
            elif side == "SELL":
                remaining = lot_qty
                while remaining > 0 and lots:
                    take = min(lots[0][0], remaining)
                    lots[0][0] -= take
                    remaining -= take
                    if lots[0][0] <= 0:
                        lots.pop(0)
        start_raw = lots[0][1] if lots else None
    start = parse_datetime_text(start_raw) or parse_datetime_text(pos.get("updated_at"))
    if not start:
        return None
    current = now or datetime.now()
    return max(1, (current.date() - start.date()).days + 1)


def init_db(base_dir):
    paths = runtime_paths(base_dir)
    con = sqlite3.connect(paths["db"])
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_orders (
          order_id TEXT PRIMARY KEY,
          trading_date TEXT,
          created_at TEXT,
          symbol TEXT,
          name TEXT,
          scenario TEXT,
          side TEXT,
          qty INTEGER,
          signal_price REAL,
          limit_price REAL,
          fill_price REAL,
          status TEXT,
          reason TEXT,
          signal_fingerprint TEXT,
          signal_payload_json TEXT,
          strategy_rationale_json TEXT,
          discipline_check_json TEXT
        )
        """
    )
    ensure_order_columns(con)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
          symbol TEXT PRIMARY KEY,
          name TEXT,
          quantity INTEGER,
          sellable INTEGER,
          avg_cost REAL,
          source TEXT,
          updated_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_buy_lots (
          order_id TEXT PRIMARY KEY,
          trading_date TEXT,
          symbol TEXT,
          qty INTEGER,
          settled INTEGER DEFAULT 0
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_position_snapshots (
          snapshot_at TEXT,
          trading_date TEXT,
          symbol TEXT,
          name TEXT,
          quantity INTEGER,
          sellable INTEGER,
          avg_cost REAL,
          last_price REAL,
          market_value REAL,
          cost_value REAL,
          unrealized_pnl REAL,
          unrealized_pnl_pct REAL,
          source TEXT,
          PRIMARY KEY(snapshot_at, symbol)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_account_snapshots (
          snapshot_at TEXT PRIMARY KEY,
          trading_date TEXT,
          initial_cash REAL,
          cash REAL,
          market_value REAL,
          total_assets REAL,
          total_pnl REAL,
          total_pnl_pct REAL,
          position_pct REAL,
          source TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_order_marks (
          order_id TEXT,
          mark_at TEXT,
          trading_date TEXT,
          symbol TEXT,
          last_price REAL,
          source TEXT,
          PRIMARY KEY(order_id, mark_at)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_intent_marks (
          order_id TEXT,
          mark_at TEXT,
          trading_date TEXT,
          symbol TEXT,
          order_status TEXT,
          signal_price REAL,
          last_price REAL,
          return_from_signal_pct REAL,
          source TEXT,
          PRIMARY KEY(order_id, mark_at)
        )
        """
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS paper_seed_registry (
          symbol TEXT PRIMARY KEY, quantity INTEGER, avg_cost REAL,
          imported_at TEXT, status TEXT, capital_value REAL DEFAULT 0,
          note TEXT
        )
    """)
    con.execute("""
        INSERT OR IGNORE INTO paper_seed_registry
        SELECT symbol, quantity, avg_cost, updated_at, 'legacy_review', 0,
               'Legacy imported inventory: reconciliation required before trading.'
        FROM paper_positions WHERE source='positions_file'
    """)
    ledger_reconciliation.ensure_schema(con)
    con.execute('''CREATE TABLE IF NOT EXISTS paper_fill_outbox (
        order_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0
    )''')
    con.commit()
    return con


def ensure_order_columns(con):
    cols = {row[1] for row in con.execute("PRAGMA table_info(paper_orders)").fetchall()}
    migrations = {
        "strategy_rationale_json": "ALTER TABLE paper_orders ADD COLUMN strategy_rationale_json TEXT",
        "discipline_check_json": "ALTER TABLE paper_orders ADD COLUMN discipline_check_json TEXT",
        "fees_total": "ALTER TABLE paper_orders ADD COLUMN fees_total REAL NOT NULL DEFAULT 0",
        "fees_json": "ALTER TABLE paper_orders ADD COLUMN fees_json TEXT",
    }
    for col, sql in migrations.items():
        if col not in cols:
            con.execute(sql)


def order_session_allows(now, side):
    t = now.time()
    continuous = dtime(9, 30) <= t < dtime(11, 30) or dtime(13, 0) <= t < dtime(14, 57)
    return continuous and side in ("BUY", "SELL")


def signal_side(signal):
    scenario = signal.get("scenario") or ""
    if scenario in intraday_timing_v2.ENTRY_SCENARIOS:
        return "BUY"
    if scenario in {"V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT"}:
        return "SELL"
    return None


def normalize_buy_qty(value=None):
    raw = int(value or os.environ.get("A_SHARE_PAPER_BUY_QTY") or DEFAULT_BUY_QTY)
    return max(100, raw // 100 * 100)


def normalize_sell_qty(sellable, value=None):
    if sellable is None:
        return 0
    raw = int(value or os.environ.get("A_SHARE_PAPER_SELL_QTY") or DEFAULT_SELL_QTY)
    if sellable < 100:
        return max(0, int(sellable))
    return max(0, min(int(sellable), raw // 100 * 100 or 100))


def env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except Exception:
        return float(default)


def high_exposure_buy_gate(signal, account):
    pct = float(account.get("position_pct") or 0.0)
    warn_pct = env_float("A_SHARE_PAPER_BUY_WARN_POSITION_PCT", DEFAULT_BUY_WARN_POSITION_PCT)
    hard_pct = env_float("A_SHARE_PAPER_BUY_HARD_POSITION_PCT", DEFAULT_BUY_HARD_POSITION_PCT)
    if pct >= hard_pct:
        return False, (
            f"组合仓位 {pct:.2f}% >= {hard_pct:.2f}%，禁止净买入；"
            "需先减仓/离场腾出仓位后，才允许新的模拟入场。"
        )
    if pct < warn_pct:
        return True, f"组合仓位 {pct:.2f}% < {warn_pct:.2f}%，未触发高仓位买入门控。"

    if signal.get("strategy_family") == "THREE_METHOD":
        return True, (
            f"组合仓位 {pct:.2f}% 已达到强化检查区；三策略合同已完成120m/15m/5m时机证据、"
            "动态Room/RR与执行区间校验，仍仅按盘前仓位计划的一档试错执行。"
        )

    if signal.get("portfolio_replacement_ok"):
        return True, f"组合仓位 {pct:.2f}% 已偏高，但信号标记为替换式买入，允许继续校验。"

    amount_1m = float(signal.get("amount_ratio_1m") or 0)
    amount_5m = float(signal.get("amount_ratio_5m") or 0)
    min_1m = env_float("A_SHARE_PAPER_HIGH_EXPOSURE_AMOUNT_1M", DEFAULT_HIGH_EXPOSURE_AMOUNT_1M)
    min_5m = env_float("A_SHARE_PAPER_HIGH_EXPOSURE_AMOUNT_5M", DEFAULT_HIGH_EXPOSURE_AMOUNT_5M)
    max_vwap_dist = env_float(
        "A_SHARE_PAPER_HIGH_EXPOSURE_MAX_VWAP_DISTANCE_PCT",
        DEFAULT_HIGH_EXPOSURE_MAX_VWAP_DISTANCE_PCT,
    )
    quality_gate = str(signal.get("signal_quality_gate") or "")
    strict_gate = quality_gate in {"vwap_pullback_reclaim", "or15_or_repair_reclaim"}
    vwap_dist = signal.get("vwap_distance_pct")
    try:
        vwap_dist_abs = abs(float(vwap_dist))
    except Exception:
        vwap_dist_abs = None
    not_chasing = not signal.get("rapid_rise_blocked") and not signal.get("over_extension_blocked")
    in_vwap_band = vwap_dist_abs is not None and vwap_dist_abs <= max_vwap_dist
    ok = strict_gate and amount_1m >= min_1m and amount_5m >= min_5m and not_chasing and in_vwap_band
    detail = (
        f"组合仓位 {pct:.2f}% 已超过 {warn_pct:.2f}%，买入需升级确认："
        f"形态={quality_gate or '-'}，1m/5m量能={amount_1m:.2f}/{amount_5m:.2f}，"
        f"距VWAP={vwap_dist_abs if vwap_dist_abs is not None else '-'}%，"
        f"急拉/远离阻断={'否' if not_chasing else '是'}。"
    )
    if ok:
        return True, detail + " 高仓位强化门槛通过，仅允许计划内一档。"
    return False, detail + " 高仓位强化门槛未通过，拒绝新增净买入。"


def t1_entry_buy_gate(con, signal, trading_date, now, planned_qty, account):
    """Limit same-day buy exposure and prevent clustered market-radar entries.

    A-share ordinary stocks cannot be sold on the buy date.  The cap therefore
    applies to today's filled BUY notional plus the proposed order, rather than
    only to the current marked position percentage.
    """
    price = float(signal.get("current_price") or signal.get("add_price") or signal.get("trigger_price") or 0)
    planned_notional = max(0.0, price * int(planned_qty or 0))
    rows = con.execute(
        """
        SELECT symbol, scenario, created_at, qty, fill_price
        FROM paper_orders
        WHERE trading_date=? AND side='BUY' AND status IN ('FILLED', 'PARTIAL_FILLED')
        """,
        (trading_date,),
    ).fetchall()
    today_t1_notional = 0.0
    for _symbol, _scenario, _created_at, qty, fill_price in rows:
        try:
            today_t1_notional += max(0.0, float(qty or 0) * float(fill_price or 0))
        except Exception:
            continue
    total_assets = float(account.get("total_assets_estimate") or account.get("initial_cash") or paper_initial_cash())
    configured_t1_pct = env_float("A_SHARE_PAPER_T1_ENTRY_RISK_PCT", DEFAULT_T1_ENTRY_RISK_PCT)
    signal_t1_cap = signal.get("t1_entry_risk_cap_pct")
    try:
        configured_t1_pct = min(configured_t1_pct, max(0.0, float(signal_t1_cap)))
    except (TypeError, ValueError):
        pass
    max_t1_notional = total_assets * configured_t1_pct
    projected_t1_notional = today_t1_notional + planned_notional
    t1_ok = projected_t1_notional <= max_t1_notional + 1e-9
    t1_reason = (
        f"T+1新增暴露 {projected_t1_notional / total_assets * 100:.2f}%"
        f"（当日已有 {today_t1_notional / total_assets * 100:.2f}% + 本单 {planned_notional / total_assets * 100:.2f}%），"
        f"上限 {max_t1_notional / total_assets * 100:.2f}%"
    )

    market_ok = True
    market_reason = "非全市场机会池，不触发机会池集中度门控。"
    if signal.get("scenario") == "MARKET_OPPORTUNITY_ACTIONABLE":
        market_rows = [row for row in rows if row[0] and row[1] == "MARKET_OPPORTUNITY_ACTIONABLE"]
        active_symbols = {str(row[0]) for row in market_rows}
        projected_active = len(active_symbols | {str(signal.get("symbol") or "")})
        cutoff = now.timestamp() - 60 * 60
        recent_count = 0
        for _symbol, _scenario, created_at, _qty, _fill_price in market_rows:
            try:
                created = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                recent_count += int(created >= cutoff)
            except Exception:
                continue
        max_active = int(env_float("A_SHARE_PAPER_MARKET_MAX_ACTIVE_BUYS", DEFAULT_MARKET_MAX_ACTIVE_BUYS))
        max_recent = int(env_float("A_SHARE_PAPER_MARKET_MAX_BUYS_PER_60M", DEFAULT_MARKET_MAX_BUYS_PER_60M))
        market_ok = projected_active <= max_active and recent_count + 1 <= max_recent
        market_reason = (
            f"机会池集中度：预计活跃标的 {projected_active}/{max_active}，"
            f"近60分钟买入 {recent_count + 1}/{max_recent}"
        )

    return {
        "passed": bool(t1_ok and market_ok),
        "t1_passed": bool(t1_ok),
        "market_passed": bool(market_ok),
        "today_t1_notional": today_t1_notional,
        "planned_notional": planned_notional,
        "projected_t1_notional": projected_t1_notional,
        "max_t1_notional": max_t1_notional,
        "t1_entry_risk_pct": projected_t1_notional / total_assets * 100 if total_assets else 0.0,
        "t1_entry_cap_pct": configured_t1_pct * 100,
        "reason": t1_reason + "；" + market_reason,
        "t1_reason": t1_reason,
        "market_reason": market_reason,
    }


def buy_pct_for_signal(signal):
    return env_float("A_SHARE_PAPER_INITIAL_POSITION_PCT", DEFAULT_INITIAL_POSITION_PCT)


def active_position_buy_count(con, symbol):
    """Count filled buy lots that belong to the currently open position.

    Historical buys before a full exit must not make the next position jump to
    the second add tier. Seeded external holdings correctly return zero and
    can still receive their first confirmed add.
    """
    rows = con.execute(
        """
        SELECT side, qty
        FROM paper_orders
        WHERE symbol=? AND status IN ('FILLED', 'PARTIAL_FILLED')
        ORDER BY created_at, rowid
        """,
        (str(symbol),),
    ).fetchall()
    running_qty = 0
    active_buy_count = 0
    for side, qty in rows:
        amount = max(0, int(qty or 0))
        if side == "BUY":
            if running_qty <= 0:
                active_buy_count = 0
            running_qty += amount
            if amount:
                active_buy_count += 1
        elif side == "SELL":
            running_qty -= amount
            if running_qty <= 0:
                running_qty = 0
                active_buy_count = 0
    return active_buy_count


def buy_position_plan(con, signal, account=None, position=None):
    """Build a target-position buy plan with A-share lot-size constraints."""
    scenario = signal.get("scenario") or ""
    position = position or get_position(con, signal.get("symbol"))
    current_qty = max(0, int(position.get("quantity") or 0))
    try:
        price = float(signal.get("current_price") or signal.get("add_price") or signal.get("trigger_price") or 0)
    except Exception:
        price = 0.0
    initial_cash = paper_initial_cash()
    account = account or account_exposure_snapshot(con, initial_cash=initial_cash)
    total_assets = max(0.0, float(account.get("total_assets_estimate") or initial_cash))
    cash = max(0.0, float(account.get("cash") or cash_from_filled_orders(con, initial_cash)))
    current_value = max(0.0, current_qty * price)
    current_pct = current_value / initial_cash * 100 if initial_cash else 0.0
    is_strategy_contract = str(signal.get("strategy_family") or "") == "THREE_METHOD"
    is_observation_strategy = is_strategy_contract
    risk_level = "strategy" if is_strategy_contract else str(signal.get("global_risk_level") or "green").lower()
    max_pct = env_float("A_SHARE_PAPER_MAX_SINGLE_POSITION_PCT", DEFAULT_MAX_SINGLE_POSITION_PCT)
    if risk_level == "yellow":
        max_pct = min(max_pct, env_float(
            "A_SHARE_PAPER_DEFENSIVE_MAX_SINGLE_POSITION_PCT",
            DEFAULT_DEFENSIVE_MAX_SINGLE_POSITION_PCT,
        ))
    elif risk_level == "red":
        max_pct = 0.0
    # The premarket contract owns the daily allocation envelope. Shared
    # timing evidence only chooses a valid clip inside that envelope.
    plan_max_pct = signal.get("planned_max_position_pct")
    plan_target_pct = signal.get("planned_target_position_pct")
    plan_probe_pct = signal.get("planned_v2_probe_position_pct")
    try:
        if plan_max_pct is not None:
            max_pct = min(max_pct, max(0.0, float(plan_max_pct)))
    except (TypeError, ValueError):
        plan_max_pct = None
    # A same-day overseas deep-V can reopen local screening, but an A-share
    # buy made late in the session cannot be sold until tomorrow.  Express the
    # recovery overlay as a position cap so a qualified setup is scaled down
    # to a valid lot instead of becoming an opaque all-or-nothing rejection.
    entry_cap = signal.get("entry_position_cap_pct")
    try:
        if entry_cap is not None:
            max_pct = min(max_pct, max(0.0, float(entry_cap)))
    except (TypeError, ValueError):
        pass
    max_pct = max(0.0, max_pct)

    plan = {
        "passed": False,
        "qty": 0,
        "step": "BLOCKED",
        "filled_buy_lots": active_position_buy_count(con, signal.get("symbol")),
        "current_quantity": current_qty,
        "current_market_value": current_value,
        "current_position_pct": current_pct,
        "target_position_pct": 0.0,
        "projected_position_pct": current_pct,
        "max_position_pct": max_pct * 100,
        "planned_notional": 0.0,
        "risk_level": risk_level,
    }
    if price <= 0:
        plan["reason"] = "价格无效，无法计算仓位"
        return plan
    if scenario in intraday_timing_v2.TIMING_PATTERNS or str(signal.get("strategy_family") or "") == "LEGACY_BLOCKED":
        plan["reason"] = "内部时机证据或历史合同不能直接下单，必须先映射为三策略正式买入信号"
        return plan
    probe_multiplier = float(signal.get("position_multiplier") or 0.0) if is_strategy_contract else 1.0
    if is_strategy_contract and not 0 < probe_multiplier <= 1.0:
        plan["reason"] = "策略试仓系数无效，拒绝模拟买入"
        return plan
    if risk_level == "red":
        plan["reason"] = "全球风险红灯，禁止新增净买入"
        return plan

    if is_strategy_contract and signal.get("premarket_plan_allows_entry") is False:
        plan["reason"] = f"盘前仓位计划={signal.get('premarket_plan_action') or '防守'}，不允许新增试仓"
        return plan

    if is_strategy_contract and not bool(signal.get("premarket_plan_complete")):
        plan["reason"] = "盘前仓位计划缺失或不完整，策略合同禁止使用默认仓位兜底"
        return plan

    planned_probe = None
    planned_target = None
    if is_strategy_contract:
        try:
            planned_probe = float(plan_probe_pct) if plan_probe_pct is not None else None
            if planned_probe is not None and not 0 < planned_probe <= 1:
                planned_probe = None
        except (TypeError, ValueError):
            planned_probe = None
        try:
            planned_target = float(plan_target_pct) if plan_target_pct is not None else None
            if planned_target is not None and not 0 < planned_target <= 1:
                planned_target = None
        except (TypeError, ValueError):
            planned_target = None

    if current_qty <= 0:
        target_pct = planned_probe if is_strategy_contract and planned_probe is not None else env_float(
            "A_SHARE_PAPER_INITIAL_POSITION_PCT", DEFAULT_INITIAL_POSITION_PCT
        ) * probe_multiplier if is_strategy_contract else env_float("A_SHARE_PAPER_INITIAL_POSITION_PCT", DEFAULT_INITIAL_POSITION_PCT)
        step = "STRATEGY_PROBE_INITIAL" if is_observation_strategy else "INITIAL"
    else:
        if is_strategy_contract:
            increment = planned_probe if planned_probe is not None else env_float(
                "A_SHARE_PAPER_INITIAL_POSITION_PCT", DEFAULT_INITIAL_POSITION_PCT
            ) * probe_multiplier
            target_pct = current_pct / 100 + increment
            if planned_target is not None:
                target_pct = min(target_pct, planned_target)
            step = "STRATEGY_PROBE_ADD"
        else:
            filled_buy_lots = int(plan["filled_buy_lots"] or 0)
            if "ADD" not in scenario:
                plan["reason"] = "已有持仓只能由明确加仓场景触发增持"
                return plan
            if filled_buy_lots <= 1:
                target_pct = env_float("A_SHARE_PAPER_ADD1_TARGET_POSITION_PCT", DEFAULT_ADD1_TARGET_POSITION_PCT)
                step = "ADD1"
            elif filled_buy_lots == 2:
                target_pct = env_float("A_SHARE_PAPER_ADD2_TARGET_POSITION_PCT", DEFAULT_ADD2_TARGET_POSITION_PCT)
                step = "ADD2"
            else:
                plan["reason"] = "当前仓位已完成两次加仓确认，不再继续增加"
                return plan
    target_pct = min(max(0.0, target_pct), max_pct)
    target_amount = initial_cash * target_pct
    max_amount = initial_cash * max_pct
    remaining_amount = max(0.0, target_amount - current_value)
    min_lot_amount = price * 100
    cash_budget = cash * 0.95
    qty = int(min(remaining_amount, cash_budget) // price // 100 * 100)
    if qty < 100 and cash_budget >= min_lot_amount and current_value + min_lot_amount <= max_amount + 1e-9:
        qty = 100
    projected_value = current_value + qty * price
    projected_pct = projected_value / initial_cash * 100 if initial_cash else 0.0
    plan.update({
        "passed": qty >= 100 and projected_value <= max_amount + 1e-9,
        "qty": qty,
        "step": step,
        "target_position_pct": target_pct * 100,
        "projected_position_pct": projected_pct,
        "planned_notional": qty * price,
    })
    if plan["passed"]:
        recovery_note = "；深V修复晚段仅保留隔夜试错仓" if signal.get("recovery_overnight_guard") else ""
        plan["reason"] = (
            f"{step}仓位阶梯：当前 {current_pct:.2f}% -> 目标 {target_pct * 100:.2f}%，"
            f"计划 {qty} 股，成交后约 {projected_pct:.2f}%；"
            f"单票上限 {max_pct * 100:.2f}%，全球风险 {risk_level}；"
            f"盘前计划={signal.get('premarket_plan_action') or '未加载'}{recovery_note}"
        )
    else:
        plan["reason"] = (
            f"仓位阶梯无法形成不少于100股且不超单票上限的订单："
            f"当前 {current_pct:.2f}%，目标 {target_pct * 100:.2f}%，上限 {max_pct * 100:.2f}%"
        )
    return plan


def planned_buy_qty(con, signal, account=None, position=None):
    fixed = os.environ.get("A_SHARE_PAPER_BUY_QTY")
    if fixed:
        return normalize_buy_qty(fixed)
    return int(buy_position_plan(con, signal, account=account, position=position).get("qty") or 0)


def sell_pct_for_signal(signal):
    scenario = signal.get("scenario") or ""
    if scenario == "V2_STRUCTURAL_EXIT":
        return 1.0
    if scenario == "V2_TAKE_PROFIT":
        return 0.50
    if scenario == "V2_REDUCE":
        return 0.50
    if scenario == "P0_HARD_RISK_REDUCE":
        return env_float("A_SHARE_PAPER_HARD_RISK_SELL_PCT", DEFAULT_HARD_RISK_SELL_PCT)
    signal_fraction = signal.get("paper_reduce_fraction")
    if signal_fraction is not None:
        try:
            return max(0.0, min(1.0, float(signal_fraction)))
        except Exception:
            pass
    if scenario in ("SOFT_VWAP_BREAK", "PAPER_OPEN_RED_CARRY_DE_RISK", "P0_SOFT_BREAK"):
        return env_float("A_SHARE_PAPER_SOFT_RISK_SELL_PCT", DEFAULT_SOFT_RISK_SELL_PCT)
    return env_float("A_SHARE_PAPER_SELL_PCT", DEFAULT_HARD_RISK_SELL_PCT)


def planned_sell_qty(signal, position):
    sellable = int((position or {}).get("sellable") or 0)
    if sellable <= 0:
        return 0
    fixed = os.environ.get("A_SHARE_PAPER_SELL_QTY")
    if fixed:
        return normalize_sell_qty(sellable, fixed)
    if sellable < 100:
        return sellable
    qty = int(sellable * sell_pct_for_signal(signal) // 100 * 100)
    return max(100, min(sellable, qty))


def filled_sell_count(con, trading_date, symbol, scenario):
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM paper_orders
        WHERE trading_date=? AND symbol=? AND scenario=?
          AND side='SELL' AND status IN ('FILLED', 'PARTIAL_FILLED')
        """,
        (trading_date, symbol, scenario),
    ).fetchone()
    return int((row or [0])[0] or 0)


def risk_sell_followup_allowed(con, signal, position, existing, now):
    scenario = signal.get("scenario")
    if scenario not in ("P0_HARD_RISK_REDUCE", "SOFT_VWAP_BREAK"):
        return False
    if signal.get("external_status") != "立即处理":
        return False
    if int((position or {}).get("sellable") or 0) <= 0:
        return False
    default_max = 3 if scenario == "P0_HARD_RISK_REDUCE" else 2
    max_fills = int(env_float("A_SHARE_PAPER_MAX_RISK_SELLS_PER_DAY", default_max))
    if filled_sell_count(con, signal.get("trading_date"), signal.get("symbol"), scenario) >= max_fills:
        return False
    try:
        current = float(signal.get("current_price") or 0)
        trigger = float(signal.get("trigger_price") or 0)
    except Exception:
        return False
    if current <= 0:
        return False
    if scenario == "P0_HARD_RISK_REDUCE":
        return trigger > 0 and current <= trigger

    try:
        previous_fill = float((existing or {}).get("fill_price") or 0)
    except Exception:
        previous_fill = 0.0

    try:
        last_time = datetime.strptime((existing or {}).get("created_at") or "", "%Y-%m-%d %H:%M:%S")
    except Exception:
        last_time = None
    stale_seconds = env_float("A_SHARE_PAPER_SOFT_FOLLOWUP_SECONDS", 15 * 60)
    enough_time = bool(last_time and (now - last_time).total_seconds() >= stale_seconds)
    try:
        vwap = float(signal.get("vwap") or 0)
    except Exception:
        vwap = 0.0
    try:
        l30 = float(signal.get("l30") or 0)
    except Exception:
        l30 = 0.0
    try:
        amount_1m = float(signal.get("amount_ratio_1m") or 0)
        amount_5m = float(signal.get("amount_ratio_5m") or 0)
    except Exception:
        amount_1m = 0.0
        amount_5m = 0.0
    eps = max(2 * TICK, current * 0.0008)
    volume_confirms = amount_1m >= env_float("A_SHARE_PAPER_SOFT_FOLLOWUP_AMOUNT_1M", 1.15) or amount_5m >= env_float("A_SHARE_PAPER_SOFT_FOLLOWUP_AMOUNT_5M", 1.05)
    worsen_pct = env_float("A_SHARE_PAPER_SOFT_FOLLOWUP_WORSEN_PCT", 0.015)
    broke_next_low = (
        (previous_fill > 0 and current <= previous_fill * (1 - worsen_pct))
        or (l30 > 0 and current <= l30 - eps)
        or (trigger > 0 and current <= trigger - eps)
    )
    prices = []
    for value in signal.get("last3_prices") or []:
        try:
            prices.append(float(value))
        except Exception:
            pass
    prior_sample_low = min(prices[:-1]) if len(prices) >= 2 else None
    broke_sample_low = bool(prior_sample_low and current <= prior_sample_low - eps)
    stayed_below_vwap = bool(vwap > 0 and current <= vwap - eps and prices and all(p <= vwap - eps for p in prices[-3:]))
    failed_reclaim = bool(enough_time and stayed_below_vwap and previous_fill > 0 and current <= previous_fill * 0.997)
    followup_confirmation = broke_next_low or broke_sample_low or failed_reclaim
    return bool(volume_confirms and followup_confirmation)


def seed_positions(con, positions, names=None, now=None):
    names = names or {}
    now_text = now.strftime("%Y-%m-%d %H:%M:%S") if now else ""
    for code, row in (positions or {}).items():
        qty = row.get("quantity")
        sellable = row.get("sellable")
        if qty is None and sellable is None:
            continue
        qty = int(qty if qty is not None else sellable or 0)
        sellable = int(sellable if sellable is not None else qty)
        if qty <= 0:
            continue
        if con.execute("SELECT 1 FROM paper_seed_registry WHERE symbol=?", (code,)).fetchone():
            continue
        existing = con.execute("SELECT source FROM paper_positions WHERE symbol=?", (code,)).fetchone()
        traded = con.execute(
            "SELECT 1 FROM paper_orders WHERE symbol=? AND status IN ('FILLED','PARTIAL_FILLED') LIMIT 1", (code,)
        ).fetchone()
        # Existing ledgers have no immutable import event. Never recreate sold
        # inventory or overwrite a position from a stale external file.
        if existing or traded:
            status = "legacy_review" if not existing or existing[0] == "positions_file" else "paper_owned"
            con.execute("INSERT INTO paper_seed_registry VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (code, qty, row.get("cost"), now_text, status, 0,
                         "Existing inventory/history preserved; reconcile external transfers before correction."))
            continue
        cost = float(row.get("cost") or 0)
        con.execute("INSERT INTO paper_seed_registry VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (code, qty, row.get("cost"), now_text,
                     "verified" if cost > 0 else "legacy_review", qty * cost,
                     "One-time security contribution; not trading profit."))
        con.execute(
            """
            INSERT INTO paper_positions(symbol, name, quantity, sellable, avg_cost, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              name=excluded.name,
              quantity=excluded.quantity,
              sellable=excluded.sellable,
              avg_cost=COALESCE(excluded.avg_cost, paper_positions.avg_cost),
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (code, names.get(code) or code, qty, sellable, row.get("cost"), "positions_file", now_text),
        )
    con.commit()


def ledger_quality(con):
    """Surface unresolved imports without rewriting historical positions."""
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='paper_seed_registry'").fetchone()
    if not exists:
        return {"ready": True, "review_symbols": [], "capital_value": 0.0}
    review = [r[0] for r in con.execute("SELECT symbol FROM paper_seed_registry WHERE status='legacy_review'")]
    capital = con.execute("SELECT COALESCE(SUM(capital_value),0) FROM paper_seed_registry WHERE status='verified'").fetchone()[0]
    return {"ready": not review, "review_symbols": review, "capital_value": float(capital),
            "reason": "模拟账本导入/清仓历史待对账，收益不可用" if review else "账本导入记录已核验"}


def annotate_account_quality(con, account):
    quality = ledger_quality(con)
    account["recorded_fees_total"] = float(con.execute(
        "SELECT COALESCE(SUM(fees_total),0) FROM paper_orders WHERE status IN ('FILLED','PARTIAL_FILLED')"
    ).fetchone()[0])
    account["cost_basis_note"] = "2026-09-11起新增模拟成交计费用；此前历史保持原口径，佣金为研究假设"
    account["ledger_quality"] = quality
    account["security_contributions"] = quality["capital_value"]
    correction_at = ledger_reconciliation.latest_cutoff(con)
    account['last_reconciliation_at'] = correction_at
    if quality["ready"]:
        if account.get("total_pnl") is not None:
            account["total_pnl"] -= quality["capital_value"]
            basis = float(account.get("initial_cash") or 0) + quality["capital_value"]
            account["total_pnl_pct"] = account["total_pnl"] / basis * 100 if basis else None
        if account.get("previous_snapshot_at") and account.get("day_pnl") is not None:
            flow = con.execute("SELECT COALESCE(SUM(capital_value),0) FROM paper_seed_registry "
                               "WHERE status='verified' AND imported_at>? AND imported_at<=?",
                               (account['previous_snapshot_at'], account['updated_at'])).fetchone()[0]
            account['day_pnl'] -= float(flow)
            prior = float(account.get('previous_total_assets') or 0)
            account['day_pnl_pct'] = account['day_pnl'] / prior * 100 if prior else None
        if correction_at and (not account.get('previous_snapshot_at') or account['previous_snapshot_at'] < correction_at):
            account['day_pnl'] = account['day_pnl_pct'] = None
            account['day_pnl_source'] = 'unavailable_after_reconciliation'
            account['day_pnl_basis'] = 'awaiting_clean_previous_close'
            account['day_pnl_basis_note'] = '历史重复导入已对账；旧快照不可比，等待纠正后的上一交易日账户快照'
    else:
        for key in ("total_pnl", "total_pnl_pct", "day_pnl", "day_pnl_pct",
                    "unrealized_pnl", "unrealized_pnl_pct", "position_day_pnl", "position_day_pnl_pct"):
            account[key] = None
        account["day_pnl_basis_note"] = quality["reason"]
    return account


def settle_t1_buys(con, today):
    """Release simulated buy lots only after their trading date has passed."""
    rows = con.execute(
        """
        SELECT symbol, SUM(qty)
        FROM paper_buy_lots
        WHERE settled=0 AND trading_date < ?
        GROUP BY symbol
        """,
        (today,),
    ).fetchall()
    for symbol, qty in rows:
        pos = get_position(con, symbol)
        quantity = int(pos.get("quantity") or 0)
        sellable = min(quantity, int(pos.get("sellable") or 0) + int(qty or 0))
        con.execute(
            "UPDATE paper_positions SET sellable=? WHERE symbol=?",
            (sellable, symbol),
        )
    con.execute("UPDATE paper_buy_lots SET settled=1 WHERE settled=0 AND trading_date < ?", (today,))
    con.commit()


def get_position(con, symbol):
    row = con.execute(
        "SELECT symbol, name, quantity, sellable, avg_cost, source, updated_at FROM paper_positions WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if not row:
        return {"symbol": symbol, "quantity": 0, "sellable": 0, "avg_cost": None}
    return {
        "symbol": row[0],
        "name": row[1],
        "quantity": row[2] or 0,
        "sellable": row[3] or 0,
        "avg_cost": row[4],
        "source": row[5],
        "updated_at": row[6],
    }


def load_positions(base_dir):
    con = init_db(base_dir)
    rows = con.execute(
        "SELECT symbol, name, quantity, sellable, avg_cost, source, updated_at FROM paper_positions WHERE quantity > 0 ORDER BY symbol"
    ).fetchall()
    return [
        {
            "symbol": row[0],
            "name": row[1],
            "quantity": row[2] or 0,
            "sellable": row[3] or 0,
            "avg_cost": row[4],
            "source": row[5],
            "updated_at": row[6],
        }
        for row in rows
    ]


def already_filled(con, trading_date, symbol, scenario):
    row = con.execute(
        """
        SELECT order_id, side, qty, fill_price, created_at, fees_total, fees_json
        FROM paper_orders
        WHERE trading_date=? AND symbol=? AND scenario=? AND status IN ('FILLED', 'PARTIAL_FILLED')
        ORDER BY created_at DESC LIMIT 1
        """,
        (trading_date, symbol, scenario),
    ).fetchone()
    if not row:
        return None
    return {"order_id": row[0], "side": row[1], "qty": row[2], "fill_price": row[3], "created_at": row[4],
            "fees_total": float(row[5] or 0), "fees": json.loads(row[6] or "{}")}


def risk_sell_filled_today(con, trading_date, symbol):
    row = con.execute(
        """
        SELECT order_id, scenario, qty, fill_price, created_at
        FROM paper_orders
        WHERE trading_date=? AND symbol=? AND side='SELL'
          AND scenario IN ('P0_HARD_RISK_REDUCE', 'P0_HARD_BREAK', 'P0_SOFT_BREAK', 'SOFT_VWAP_BREAK', 'PAPER_OPEN_RED_CARRY_DE_RISK', 'OVERNIGHT_RISK_REDUCE')
          AND status IN ('FILLED', 'PARTIAL_FILLED')
        ORDER BY created_at DESC LIMIT 1
        """,
        (trading_date, symbol),
    ).fetchone()
    if not row:
        return None
    return {
        "order_id": row[0],
        "scenario": row[1],
        "qty": row[2],
        "fill_price": row[3],
        "created_at": row[4],
    }


def already_recorded(con, order_id):
    row = con.execute(
        """
        SELECT order_id, side, qty, fill_price, created_at, status, reason, fees_total, fees_json
        FROM paper_orders
        WHERE order_id=?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "order_id": row[0],
        "side": row[1],
        "qty": row[2],
        "fill_price": row[3],
        "created_at": row[4],
        "status": row[5],
        "reason": row[6],
        "fees_total": float(row[7] or 0),
        "fees": json.loads(row[8] or "{}"),
    }


def stable_reject_order_id(trading_date, symbol, scenario, side, reason_key):
    return f"{trading_date}:{symbol}:{scenario}:{side}:REJECTED:{reason_key}"


def count_filled_orders(base_dir, trading_date=None, scenario=None, side=None):
    con = init_db(base_dir)
    sql = "SELECT COUNT(*) FROM paper_orders WHERE status IN ('FILLED', 'PARTIAL_FILLED')"
    params = []
    if trading_date:
        sql += " AND trading_date=?"
        params.append(trading_date)
    if scenario:
        sql += " AND scenario=?"
        params.append(scenario)
    if side:
        sql += " AND side=?"
        params.append(side)
    return int(con.execute(sql, params).fetchone()[0] or 0)


def fill_price_path(base_dir, trading_date, symbol, fill_at, offsets=(60, 180, 300, 900)):
    """Return the first available marked price at each post-fill horizon."""
    try:
        fill_dt = datetime.strptime(str(fill_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return {str(offset): None for offset in offsets}
    con = init_db(base_dir)
    rows = con.execute(
        """
        SELECT snapshot_at, last_price
        FROM paper_position_snapshots
        WHERE trading_date=? AND symbol=? AND snapshot_at>=?
        ORDER BY snapshot_at
        """,
        (str(trading_date), str(symbol), fill_dt.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    mark_rows = con.execute(
        """
        SELECT mark_at, last_price
        FROM paper_order_marks
        WHERE trading_date=? AND symbol=? AND mark_at>=?
          AND order_id IN (
            SELECT order_id FROM paper_orders
            WHERE trading_date=? AND symbol=? AND created_at=?
              AND status IN ('FILLED', 'PARTIAL_FILLED')
          )
        ORDER BY mark_at
        """,
        (
            str(trading_date),
            str(symbol),
            fill_dt.strftime("%Y-%m-%d %H:%M:%S"),
            str(trading_date),
            str(symbol),
            fill_dt.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchall()
    rows.extend(mark_rows)
    parsed = []
    for snapshot_at, last_price in rows:
        try:
            timestamp = datetime.strptime(str(snapshot_at)[:19], "%Y-%m-%d %H:%M:%S")
            price = float(last_price)
        except (TypeError, ValueError):
            continue
        parsed.append((timestamp, price))
    result = {}
    for offset in offsets:
        target = fill_dt.timestamp() + int(offset)
        selected = next(((stamp, price) for stamp, price in parsed if stamp.timestamp() >= target), None)
        result[str(offset)] = selected[1] if selected else None
    return result


def write_order(con, base_dir, payload, *, commit=True):
    previous = con.execute('SELECT status FROM paper_orders WHERE order_id=?', (payload['order_id'],)).fetchone()
    if previous and previous[0] == 'VOID_RECONCILED':
        raise ValueError('Reconciled order ids cannot be overwritten')
    con.execute(
        """
        INSERT OR REPLACE INTO paper_orders (
          order_id, trading_date, created_at, symbol, name, scenario, side, qty,
          signal_price, limit_price, fill_price, status, reason, signal_fingerprint, signal_payload_json,
          strategy_rationale_json, discipline_check_json, fees_total, fees_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["order_id"],
            payload["trading_date"],
            payload["created_at"],
            payload["symbol"],
            payload.get("name"),
            payload["scenario"],
            payload["side"],
            payload.get("qty"),
            payload.get("signal_price"),
            payload.get("limit_price"),
            payload.get("fill_price"),
            payload["status"],
            payload.get("reason"),
            payload.get("signal_fingerprint"),
            json.dumps(payload.get("signal") or {}, ensure_ascii=False),
            json.dumps(payload.get("strategy_rationale") or {}, ensure_ascii=False),
            json.dumps(payload.get("discipline_check") or {}, ensure_ascii=False),
            float((payload.get("fees") or {}).get("total") or 0),
            json.dumps(payload.get("fees") or {}, ensure_ascii=False),
        ),
    )
    if not commit:
        return payload
    con.commit()
    paths = runtime_paths(base_dir)
    with paths["events"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def nonfill_order_id(trading_date, symbol, scenario, side, status, signal):
    key = signal.get("contract_hash") or signal.get("fingerprint") or "-"
    return f"{trading_date}:{symbol}:{scenario}:{side}:{status}:{key}"


def update_position_after_fill(con, signal, side, qty, fill_price, now, *, fees=0, commit=True, order_id=None):
    symbol = signal["symbol"]
    pos = get_position(con, symbol)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    if side == "BUY":
        old_qty = pos.get("quantity") or 0
        old_cost = pos.get("avg_cost") or fill_price
        new_qty = old_qty + qty
        avg_cost = ((old_qty * old_cost) + qty * fill_price + fees) / new_qty if new_qty else fill_price
        con.execute(
            """
            INSERT INTO paper_positions(symbol, name, quantity, sellable, avg_cost, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              name=excluded.name,
              quantity=excluded.quantity,
              sellable=paper_positions.sellable,
              avg_cost=excluded.avg_cost,
              source='paper',
              updated_at=excluded.updated_at
            """,
            (symbol, signal.get("name") or symbol, new_qty, pos.get("sellable") or 0, avg_cost, "paper", now_text),
        )
        con.execute(
            """
            INSERT OR REPLACE INTO paper_buy_lots(order_id, trading_date, symbol, qty, settled)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                order_id or f"{signal['trading_date']}:{symbol}:{signal.get('scenario')}:BUY",
                signal["trading_date"],
                symbol,
                qty,
            ),
        )
    elif side == "SELL":
        new_qty = max(0, (pos.get("quantity") or 0) - qty)
        new_sellable = max(0, (pos.get("sellable") or 0) - qty)
        if new_qty <= 0:
            con.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
            if commit:
                con.commit()
            return
        con.execute(
            """
            INSERT INTO paper_positions(symbol, name, quantity, sellable, avg_cost, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              name=excluded.name,
              quantity=excluded.quantity,
              sellable=excluded.sellable,
              avg_cost=paper_positions.avg_cost,
              source='paper',
              updated_at=excluded.updated_at
            """,
            (symbol, signal.get("name") or symbol, new_qty, new_sellable, pos.get("avg_cost"), "paper", now_text),
        )
    if commit:
        con.commit()


def flush_fill_events(con, base_dir):
    """SQLite is authoritative; the JSONL mirror is retryable and at-least-once."""
    rows = con.execute('SELECT order_id, payload_json FROM paper_fill_outbox WHERE published=0 ORDER BY rowid').fetchall()
    if not rows:
        return None
    try:
        with runtime_paths(base_dir)["events"].open("a", encoding="utf-8") as stream:
            for order_id, encoded in rows:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                con.execute('UPDATE paper_fill_outbox SET published=1 WHERE order_id=?', (order_id,))
                con.commit()
    except (OSError, sqlite3.Error) as exc:
        con.rollback()
        return "成交已入账，事件副本待重试：" + type(exc).__name__
    return None


def record_fill(con, base_dir, payload, now):
    """Commit one fill, its cost, inventory and retryable event atomically."""
    if con.in_transaction:
        raise ValueError("Fill requires a standalone transaction")
    if payload.get("status") not in {"FILLED", "PARTIAL_FILLED"}:
        raise ValueError("Expected a filled order")
    if any(payload.get(key) != (payload.get("signal") or {}).get(key)
           for key in ("symbol", "trading_date", "scenario")):
        raise ValueError("Fill and signal identity differ")
    con.execute("BEGIN IMMEDIATE")
    try:
        existing = already_recorded(con, payload["order_id"])
        if existing and existing.get("status") in {"FILLED", "PARTIAL_FILLED", "VOID_RECONCILED"}:
            con.rollback()
            return {**existing, "status": "FILLED_ALREADY" if existing["status"] != "VOID_RECONCILED" else "NO_ORDER"}
        side, qty, price = payload["side"], payload["qty"], payload["fill_price"]
        fees = paper_costs.estimate(payload["symbol"], side, qty, price, payload["trading_date"])
        if side == "BUY" and qty * price + fees["total"] > cash_from_filled_orders(con) + 1e-8:
            con.rollback()
            return {"status": "REJECTED", "side": side, "qty": 0, "reason": "含成交费用后可用现金不足"}
        position = get_position(con, payload["symbol"])
        if side == "SELL" and qty > min(position.get("quantity") or 0, position.get("sellable") or 0):
            con.rollback()
            return {"status": "REJECTED", "side": side, "qty": 0, "reason": "成交前复核可卖数量不足（T+1）"}
        payload = {**payload, "fees": fees, "fees_total": fees["total"]}
        update_position_after_fill(con, payload["signal"], side, qty, price, now,
                                   fees=fees["total"], commit=False, order_id=payload["order_id"])
        write_order(con, base_dir, payload, commit=False)
        con.execute("INSERT INTO paper_fill_outbox(order_id,payload_json) VALUES (?,?)",
                    (payload["order_id"], json.dumps(payload, ensure_ascii=False)))
        con.commit()
    except Exception:
        con.rollback()
        raise
    warning = flush_fill_events(con, base_dir)
    if warning:
        payload["event_warning"] = warning
    return payload


def buy_limit_price(signal):
    price = float(signal.get("current_price") or 0)
    base = float(signal.get("add_price") or signal.get("trigger_price") or price)
    band_high = signal.get("execution_band_high")
    if band_high:
        return ceil_to_tick(band_high)
    tolerance = max(2 * TICK, price * 0.0015)
    return ceil_to_tick(base + tolerance)


def sell_limit_price(signal):
    price = float(signal.get("current_price") or 0)
    tolerance = max(2 * TICK, price * 0.0015)
    return floor_to_tick(price - tolerance)


def limit_locked(signal, side, current_price):
    limit_up = signal.get("limit_up")
    limit_down = signal.get("limit_down")
    if side == "BUY" and limit_up and current_price >= float(limit_up) - TICK:
        return "涨停附近，模拟盘不假设可以买到"
    if side == "SELL" and limit_down and current_price <= float(limit_down) + TICK:
        return "跌停附近，模拟盘不假设可以卖出"
    return None


def estimated_slippage(signal, side, current_price):
    eps = float(signal.get("epsilon") or max(2 * TICK, current_price * 0.0008))
    atr1 = float(signal.get("atr1m") or 0)
    amount_ratio = float(signal.get("amount_ratio_1m") or 0)
    if side == "BUY":
        bps = 8 if amount_ratio >= 1.5 else 12
    else:
        bps = 10 if amount_ratio >= 1.0 else 16
    raw = max(2 * TICK, current_price * bps / 10000, 0.2 * eps, 0.1 * atr1)
    return ceil_to_tick(raw)


def simulated_fill_price(signal, side, current_price):
    slip = estimated_slippage(signal, side, current_price)
    if side == "BUY":
        return ceil_to_tick(current_price + slip), slip
    return floor_to_tick(current_price - slip), slip


def buy_liquidity_allows(signal, qty):
    amount_ratio = float(signal.get("amount_ratio_1m") or 0)
    last_volume = float(signal.get("last_volume") or 0)
    if amount_ratio < 1.05:
        return False, "1m量能未放大，模拟盘不假设买入可顺利成交"
    comparable_volume = last_volume if last_volume > 10000 else last_volume * 100
    if comparable_volume and qty > max(100, int(comparable_volume * MAX_BUY_PARTICIPATION_1M)):
        return False, "计划数量超过1m成交量参与上限，模拟盘不成交"
    return True, None


def maybe_execute_signal(base_dir, signal, positions, now, names=None, price_map=None):
    con = init_db(base_dir)
    flush_fill_events(con, base_dir)
    seed_positions(con, positions, names=names, now=now)
    settle_t1_buys(con, signal["trading_date"])
    quality = ledger_quality(con)
    if not quality["ready"] and (
        signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS
        or signal.get("symbol") in quality["review_symbols"]
    ):
        con.close()
        return {"status": "NO_ORDER", "symbol": signal.get("symbol"),
                "reason": quality["reason"], "ledger_quality": quality}
    timing_v2 = signal.get("timing_v2") or {}
    if signal.get("scenario") in intraday_timing_v2.ENTRY_SCENARIOS and not (
        isinstance(timing_v2, dict) and timing_v2 and timing_v2.get("entry_allowed")
    ):
        return {
            "status": "NO_ORDER",
            "reason": "V2多周期门控未通过：" + "；".join(str(x) for x in (timing_v2.get("blockers") or [])[:3]),
            "symbol": signal.get("symbol"),
            "scenario": signal.get("scenario"),
            "side": "BUY",
        }
    side = signal_side(signal)
    if not side:
        return {"status": "NO_ORDER", "reason": "信号不对应模拟下单场景"}

    contract = signal.get("signal_contract") or {}
    if side == "BUY" and (
        signal.get("external_status") != "立即处理" or not contract.get("sim_allowed")
    ):
        return {
            "status": "NO_ORDER",
            "reason": contract.get("sim_reason") or signal.get("sim_reason") or "V2仍在跟踪，执行契约未授权",
            "symbol": signal.get("symbol"),
            "scenario": signal.get("scenario"),
            "side": side,
        }

    if side == "BUY" and now.time() >= dtime(14, 45):
        return {"status": "NO_ORDER", "reason": "V2 14:45后只做风险管理，不新开仓", "side": side}

    trading_date = signal["trading_date"]
    symbol = signal["symbol"]
    scenario = signal["scenario"]
    pos = get_position(con, symbol)
    existing = already_filled(con, trading_date, symbol, scenario)
    followup_allowed = bool(
        existing and side == "SELL" and risk_sell_followup_allowed(con, signal, pos, existing, now)
    )
    if existing:
        if not followup_allowed:
            return {"status": "FILLED_ALREADY", "reason": "今日同一信号已模拟成交，后续不重复提醒", **existing}

    if side == "SELL" and int(pos.get("quantity") or 0) <= 0:
        return {
            "status": "NO_ORDER",
            "reason": "非模拟盘持仓，仅记录风险观察，不生成模拟卖出订单",
            "symbol": symbol,
            "scenario": scenario,
            "side": side,
        }

    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    order_id = f"{trading_date}:{symbol}:{scenario}:{side}"
    if followup_allowed:
        order_id = f"{order_id}:FOLLOWUP:{now.strftime('%H%M%S')}"
    recorded = already_recorded(con, order_id)
    if recorded and recorded.get('status') == 'VOID_RECONCILED':
        con.close()
        return {'status':'NO_ORDER', 'symbol':symbol, 'side':side,
                'reason':'该历史模拟订单已对账作废，不允许重放恢复成交'}

    if side == "CANCEL":
        rationale, discipline = strategy_discipline.order_notes(signal, side, position={}, now_session_allows=False)
        payload = {
            "order_id": order_id,
            "trading_date": trading_date,
            "created_at": now_text,
            "symbol": symbol,
            "name": signal.get("name"),
            "scenario": scenario,
            "side": "CANCEL",
            "qty": 0,
            "signal_price": signal.get("current_price"),
            "limit_price": None,
            "fill_price": None,
            "status": "CANCELLED",
            "reason": "取消进攻计划，本地模拟盘不产生买卖成交",
            "signal_fingerprint": signal.get("fingerprint"),
            "signal": signal,
            "strategy_rationale": rationale,
            "discipline_check": discipline,
        }
        return write_order(con, base_dir, payload)

    now_allows = order_session_allows(now, side)
    rationale, discipline = strategy_discipline.order_notes(signal, side, position=pos, now_session_allows=now_allows)
    if side == "BUY":
        prior_risk_sell = risk_sell_filled_today(con, trading_date, symbol)
        if prior_risk_sell and not signal.get("post_p0_reentry_ok"):
            reason = (
                "当日已触发风险卖出，未重新站回OR15/修复位并完成确认，禁止同日重新买入；"
                f"最近风险成交 {prior_risk_sell.get('scenario')} @ {f2(prior_risk_sell.get('fill_price'))}"
            )
            order_id = stable_reject_order_id(trading_date, symbol, scenario, side, "POST_P0_REENTRY")
            existing_order = already_recorded(con, order_id)
            if existing_order:
                return {
                    **existing_order,
                    "status": "REJECTED_ALREADY",
                    "reason": existing_order.get("reason") or reason,
                    "strategy_rationale": rationale,
                    "discipline_check": discipline,
                }
            payload = {
                "order_id": order_id,
                "trading_date": trading_date,
                "created_at": now_text,
                "symbol": symbol,
                "name": signal.get("name"),
                "scenario": scenario,
                "side": side,
                "qty": 0,
                "signal_price": signal.get("current_price"),
                "limit_price": None,
                "fill_price": None,
                "status": "REJECTED",
                "reason": reason,
                "signal_fingerprint": signal.get("fingerprint"),
                "signal": signal,
                "strategy_rationale": rationale,
                "discipline_check": discipline,
            }
            write_order(con, base_dir, payload)
            return {"status": "REJECTED", "reason": reason, "side": side, "qty": 0, "strategy_rationale": rationale, "discipline_check": discipline}
    if not discipline.get("passed"):
        status = "REJECTED"
        reason = discipline.get("summary") or "策略纪律未通过"
        order_id = stable_reject_order_id(trading_date, symbol, scenario, side, "DISCIPLINE")
        existing_order = already_recorded(con, order_id)
        if existing_order:
            return {
                **existing_order,
                "status": "REJECTED_ALREADY",
                "reason": existing_order.get("reason") or reason,
                "strategy_rationale": rationale,
                "discipline_check": discipline,
            }
        payload = {
            "order_id": order_id,
            "trading_date": trading_date,
            "created_at": now_text,
            "symbol": symbol,
            "name": signal.get("name"),
            "scenario": scenario,
            "side": side,
            "qty": 0,
            "signal_price": signal.get("current_price"),
            "limit_price": None,
            "fill_price": None,
            "status": status,
            "reason": reason,
            "signal_fingerprint": signal.get("fingerprint"),
            "signal": signal,
            "strategy_rationale": rationale,
            "discipline_check": discipline,
        }
        write_order(con, base_dir, payload)
        return {"status": status, "reason": reason, "side": side, "qty": 0, "strategy_rationale": rationale, "discipline_check": discipline}
    if side == "SELL" and int(pos.get("quantity") or 0) <= 0:
        return {
            "status": "NO_ORDER",
            "reason": "非模拟盘持仓，仅记录风险提醒，不生成模拟卖出订单",
            "side": side,
            "qty": 0,
        }

    account_gate = None
    position_plan = None
    if side == "BUY":
        account_gate = account_exposure_snapshot(
            con,
            price_map=price_map,
            trading_date=trading_date,
            now=now,
        )
        position_plan = buy_position_plan(con, signal, account=account_gate, position=pos)
        buy_qty = int(position_plan.get("qty") or 0)
    else:
        buy_qty = DEFAULT_BUY_QTY
    sell_qty = planned_sell_qty(signal, pos) if side == "SELL" else DEFAULT_SELL_QTY

    if side == "BUY":
        exposure_ok, exposure_reason = high_exposure_buy_gate(signal, account_gate)
        t1_gate = t1_entry_buy_gate(con, signal, trading_date, now, buy_qty, account_gate)
        position_ok = bool(position_plan and position_plan.get("passed"))
        position_reason = (position_plan or {}).get("reason") or "仓位计划不可用"
        portfolio_ok = exposure_ok and t1_gate["passed"] and position_ok
        portfolio_reason = exposure_reason + "；" + t1_gate["reason"] + "；" + position_reason
        portfolio_check = {
            "name": "T+1、仓位阶梯与机会池集中度门控",
            "passed": portfolio_ok,
            "detail": portfolio_reason,
            "severity": "required",
        }
        discipline = {
            **discipline,
            "checks": list(discipline.get("checks") or []) + [portfolio_check],
        }
        blocking = [x for x in discipline["checks"] if not x.get("passed") and x.get("severity", "required") in ("required", "blocking")]
        discipline.update({
            "passed": not blocking,
            "failed_checks": [x["name"] for x in blocking],
            "summary": "纪律通过" if not blocking else "纪律未通过：" + "、".join(x["name"] for x in blocking[:5]),
        })
        rationale = {
            **rationale,
            "portfolio_gate": {
                **account_gate,
                **t1_gate,
                "passed": portfolio_ok,
                "reason": portfolio_reason,
                "buy_position_plan": position_plan,
                "warn_position_pct": env_float("A_SHARE_PAPER_BUY_WARN_POSITION_PCT", DEFAULT_BUY_WARN_POSITION_PCT),
                "hard_position_pct": env_float("A_SHARE_PAPER_BUY_HARD_POSITION_PCT", DEFAULT_BUY_HARD_POSITION_PCT),
            },
            "action_basis": list(rationale.get("action_basis") or []) + [portfolio_reason],
        }
        if not portfolio_ok:
            if not t1_gate["t1_passed"]:
                reject_key = "T1_ENTRY_RISK"
            elif not position_ok:
                ladder_level = str((position_plan or {}).get("risk_level") or "green").upper()
                ladder_step = str((position_plan or {}).get("step") or "BLOCKED").upper()
                reject_key = f"POSITION_LADDER_{ladder_level}_{ladder_step}"
            else:
                reject_key = "PORTFOLIO_EXPOSURE"
            reason = portfolio_reason
            order_id = stable_reject_order_id(trading_date, symbol, scenario, side, reject_key)
            existing_order = already_recorded(con, order_id)
            if existing_order:
                return {
                    **existing_order,
                    "status": "REJECTED_ALREADY",
                    "reason": existing_order.get("reason") or reason,
                    "strategy_rationale": rationale,
                    "discipline_check": discipline,
                }
            payload = {
                "order_id": order_id,
                "trading_date": trading_date,
                "created_at": now_text,
                "symbol": symbol,
                "name": signal.get("name"),
                "scenario": scenario,
                "side": side,
                "qty": 0,
                "signal_price": signal.get("current_price"),
                "limit_price": None,
                "fill_price": None,
                "status": "REJECTED",
                "reason": reason,
                "signal_fingerprint": signal.get("fingerprint"),
                "signal": signal,
                "strategy_rationale": rationale,
                "discipline_check": discipline,
            }
            write_order(con, base_dir, payload)
            return {
                "status": "REJECTED",
                "reason": reason,
                "side": side,
                "qty": 0,
                "strategy_rationale": rationale,
                "discipline_check": discipline,
            }

    if side == "BUY" and buy_qty < 100:
        return {
            "status": "REJECTED",
            "reason": "可用现金或单笔仓位上限不足，模拟盘不买入",
            "side": side,
            "qty": 0,
        }
    if side == "SELL" and sell_qty <= 0:
        reason = "无可卖数量，T+1限制"
        order_id = stable_reject_order_id(trading_date, symbol, scenario, side, "NO_SELLABLE")
        existing_order = already_recorded(con, order_id)
        if existing_order:
            return {**existing_order, "status": "REJECTED_ALREADY", "reason": existing_order.get("reason") or reason}
        payload = {
            "order_id": order_id,
            "trading_date": trading_date,
            "created_at": now_text,
            "symbol": symbol,
            "name": signal.get("name"),
            "scenario": scenario,
            "side": side,
            "qty": 0,
            "signal_price": signal.get("current_price"),
            "limit_price": None,
            "fill_price": None,
            "status": "REJECTED",
            "reason": reason,
            "signal_fingerprint": signal.get("fingerprint"),
            "signal": signal,
            "strategy_rationale": rationale,
            "discipline_check": discipline,
        }
        write_order(con, base_dir, payload)
        return {"status": "REJECTED", "reason": reason, "side": side, "qty": 0, "strategy_rationale": rationale, "discipline_check": discipline}

    current_price = float(signal.get("current_price") or 0)
    sim = sim_broker.simulate_signal(
        signal,
        pos,
        now,
        buy_qty=buy_qty,
        sell_qty=sell_qty,
    )
    sim_payload = sim.to_dict()
    if sim.status in ("NO_ORDER",):
        return sim_payload

    qty = int(sim.qty or 0)
    limit_price = sim.limit_price
    fill_price = sim.fill_price
    slippage = sim.slippage
    status = sim.status
    reason = sim.reason
    if side == "BUY" and status in ("FILLED", "PARTIAL_FILLED"):
        cost_check = paper_costs.entry_check(symbol, qty, fill_price, trading_date, contract)
        sim_payload["cost_check"] = cost_check
        if not cost_check["passed"]:
            status, reason = "REJECTED", cost_check["reason"]
    if status not in ("FILLED", "PARTIAL_FILLED"):
        order_id = nonfill_order_id(trading_date, symbol, scenario, side, status, signal)
        existing_order = already_recorded(con, order_id)
        if existing_order:
            return {**existing_order, "status": f"{status}_ALREADY", "reason": existing_order.get("reason") or reason}
        payload = {
            "order_id": order_id,
            "trading_date": trading_date,
            "created_at": now_text,
            "symbol": symbol,
            "name": signal.get("name"),
            "scenario": scenario,
            "side": side,
            "qty": qty,
            "signal_price": signal.get("current_price"),
            "limit_price": limit_price,
            "fill_price": None,
            "status": status,
            "reason": reason,
            "signal_fingerprint": signal.get("fingerprint"),
            "signal": {**signal, "sim_result": sim_payload},
            "strategy_rationale": rationale,
            "discipline_check": discipline,
        }
        write_order(con, base_dir, payload)
        return {
            "status": status,
            "reason": reason,
            "side": sim.side,
            "qty": qty,
            "limit_price": limit_price,
            "estimated_fill_price": sim.theoretical_price,
            "slippage": slippage,
            "contract_hash": sim.contract_hash,
            "rule_context": sim.rule_context,
            "strategy_rationale": rationale,
            "discipline_check": discipline,
        }

    payload = {
        "order_id": order_id,
        "trading_date": trading_date,
        "created_at": now_text,
        "symbol": symbol,
        "name": signal.get("name"),
        "scenario": scenario,
        "side": side,
        "qty": qty,
        "signal_price": signal.get("current_price"),
        "limit_price": limit_price,
        "fill_price": fill_price,
        "status": status,
        "reason": reason,
        "signal_fingerprint": signal.get("fingerprint"),
        "signal": {**signal, "sim_result": sim_payload},
        "strategy_rationale": rationale,
        "discipline_check": discipline,
    }
    return record_fill(con, base_dir, payload, now)


def load_orders(base_dir, trading_date=None, filled_only=False):
    con = init_db(base_dir)
    sql = "SELECT order_id, trading_date, created_at, symbol, name, scenario, side, qty, signal_price, limit_price, fill_price, status, reason, signal_fingerprint, signal_payload_json, strategy_rationale_json, discipline_check_json, fees_total, fees_json FROM paper_orders"
    params = []
    clauses = []
    if trading_date:
        clauses.append("trading_date=?")
        params.append(trading_date)
    if filled_only:
        clauses.append("status IN ('FILLED', 'PARTIAL_FILLED')")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at"
    rows = con.execute(sql, params).fetchall()
    out = []
    for row in rows:
        try:
            signal = json.loads(row[14] or "{}")
        except Exception:
            signal = {}
        try:
            strategy_rationale = json.loads(row[15] or "{}")
        except Exception:
            strategy_rationale = {}
        try:
            discipline_check = json.loads(row[16] or "{}")
        except Exception:
            discipline_check = {}
        out.append({
            "order_id": row[0],
            "trading_date": row[1],
            "created_at": row[2],
            "symbol": row[3],
            "name": row[4],
            "scenario": row[5],
            "side": row[6],
            "qty": row[7],
            "signal_price": row[8],
            "limit_price": row[9],
            "fill_price": row[10],
            "status": row[11],
            "reason": row[12],
            "signal_fingerprint": row[13],
            "fees_total": float(row[17] or 0),
            "fees": json.loads(row[18] or "{}"),
            "signal": signal,
            "strategy_rationale": strategy_rationale,
            "discipline_check": discipline_check,
        })
    # A sell on a later trading day must include cost lots bought earlier;
    # derive attribution from the complete order history, then attach only
    # the rows requested by this view.
    realized_source = load_orders(base_dir) if trading_date else out
    realized = realized_pnl_by_order(realized_source)
    for order in out:
        order.update(realized.get(order.get("order_id"), {}))
    return out


def realized_pnl_by_order(orders):
    """Attribute filled sell results to FIFO cost lots without changing cash math."""
    lots = {}
    result = {}
    ordered = sorted(orders or [], key=lambda item: (item.get("created_at") or "", item.get("order_id") or ""))
    for order in ordered:
        if order.get("status") not in ("FILLED", "PARTIAL_FILLED"):
            continue
        symbol = str(order.get("symbol") or "")
        try:
            qty = int(order.get("qty") or 0)
            price = float(order.get("fill_price") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty <= 0 or price <= 0:
            continue
        if order.get("side") == "BUY":
            lots.setdefault(symbol, []).append([qty, price + float(order.get("fees_total") or 0) / qty])
            continue
        if order.get("side") != "SELL":
            continue
        remaining = qty
        cost_basis = 0.0
        realized_qty = 0
        symbol_lots = lots.setdefault(symbol, [])
        while remaining > 0 and symbol_lots:
            lot_qty, lot_price = symbol_lots[0]
            matched = min(remaining, lot_qty)
            realized_qty += matched
            cost_basis += matched * lot_price
            remaining -= matched
            lot_qty -= matched
            if lot_qty:
                symbol_lots[0][0] = lot_qty
            else:
                symbol_lots.pop(0)
        if realized_qty <= 0:
            continue
        realized_pnl = realized_qty * price - cost_basis - float(order.get("fees_total") or 0) * realized_qty / qty
        result[order.get("order_id")] = {
            "realized_qty": realized_qty,
            "realized_cost_basis": cost_basis,
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl / cost_basis * 100 if cost_basis else None,
            "pnl_basis": "FIFO成本",
        }
    return result


def normalize_price_map(price_map=None):
    out = {}
    for code, row in (price_map or {}).items():
        if isinstance(row, dict):
            price = row.get("last_price", row.get("price", row.get("close")))
            name = row.get("name")
            pct = row.get("pct")
            prev_close = row.get("prev_close", row.get("pre_close"))
        else:
            price = row
            name = None
            pct = None
            prev_close = None
        try:
            price = float(price)
        except Exception:
            price = None
        try:
            pct = float(str(pct).replace("%", "")) if pct is not None and pct != "" else None
        except Exception:
            pct = None
        try:
            prev_close = float(prev_close) if prev_close is not None and prev_close != "" else None
        except Exception:
            prev_close = None
        out[str(code)] = {"last_price": price, "name": name, "pct": pct, "prev_close": prev_close}
    return out


def day_buy_stats(con, symbol, trading_date):
    if not symbol or not trading_date:
        return {"qty": 0, "avg_price": None}
    rows = con.execute(
        """
        SELECT qty, fill_price, COALESCE(fees_total, 0)
        FROM paper_orders
        WHERE symbol=?
          AND side='BUY'
          AND status IN ('FILLED', 'PARTIAL_FILLED')
          AND substr(created_at, 1, 10)=?
        """,
        (symbol, trading_date),
    ).fetchall()
    qty = 0
    amount = 0.0
    for row_qty, fill_price, fees in rows:
        try:
            q = int(row_qty or 0)
            p = float(fill_price or 0)
        except Exception:
            continue
        if q <= 0 or p <= 0:
            continue
        qty += q
        amount += q * p + float(fees)
    return {"qty": qty, "avg_price": amount / qty if qty else None}


def calc_position_day_pnl(con, pos, quote, trading_date):
    qty = int(pos.get("quantity") or 0)
    if qty <= 0:
        return None, None, None
    last_price = quote.get("last_price")
    if last_price is None:
        return None, None, None

    sellable = max(0, int(pos.get("sellable") or 0))
    today_buy = day_buy_stats(con, pos.get("symbol"), trading_date)
    today_qty = min(max(qty - sellable, 0), int(today_buy.get("qty") or 0))
    legacy_qty = max(qty - today_qty, 0)

    prev_close = quote.get("prev_close")
    pct = quote.get("pct")
    if prev_close is None and pct is not None and pct > -99.999:
        prev_close = float(last_price) / (1 + float(pct) / 100)

    day_pnl = 0.0
    base_value = 0.0
    has_component = False
    if legacy_qty and prev_close is not None:
        day_pnl += (float(last_price) - prev_close) * legacy_qty
        base_value += prev_close * legacy_qty
        has_component = True
    today_avg = today_buy.get("avg_price")
    if today_qty and today_avg is not None:
        day_pnl += (float(last_price) - float(today_avg)) * today_qty
        base_value += float(today_avg) * today_qty
        has_component = True
    if not has_component:
        return None, None, prev_close
    return day_pnl, (day_pnl / base_value * 100 if base_value else None), prev_close


def position_review_status(holding_days, unrealized_pnl_pct, day_pnl_pct=None):
    """Flag stale positions for a planned review without inventing a sell signal."""
    days = int(holding_days or 0)
    total = float(unrealized_pnl_pct or 0)
    day = float(day_pnl_pct or 0)
    if days >= 20 and total <= -3:
        return {
            "level": "REVIEW_REQUIRED",
            "reason": f"持有{days}天且累计浮亏{total:.2f}%，需复核原始逻辑、相对强弱与V2结构",
        }
    if days >= 12 and total <= -1 and day <= 0:
        return {
            "level": "REVIEW_WATCH",
            "reason": f"持有{days}天仍未修复，暂不加仓并等待结构/相对强弱复核",
        }
    return {"level": "NORMAL", "reason": "持仓时间与收益状态未触发额外复核"}


def enrich_positions_with_marks(con, positions, trading_date=None, price_map=None, now=None):
    prices = normalize_price_map(price_map)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S") if now else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    enriched = []
    total_cost = 0.0
    total_value = 0.0
    total_pnl = 0.0
    total_day_pnl = 0.0
    total_day_base = 0.0
    has_day_pnl = False
    for pos in positions:
        symbol = pos["symbol"]
        quote = prices.get(symbol) or {}
        qty = int(pos.get("quantity") or 0)
        cost = pos.get("avg_cost")
        last_price = quote.get("last_price")
        holding_days = holding_days_for_position(con, pos, now or datetime.now())
        if quote.get("name") and (not pos.get("name") or pos.get("name") == symbol):
            pos["name"] = quote.get("name")
        cost_value = float(cost or 0) * qty if cost is not None else None
        market_value = float(last_price or 0) * qty if last_price is not None else None
        pnl = market_value - cost_value if market_value is not None and cost_value is not None else None
        pnl_pct = pnl / cost_value * 100 if pnl is not None and cost_value else None
        day_pnl, day_pnl_pct, prev_close = calc_position_day_pnl(con, pos, quote, trading_date)
        row = {
            **pos,
            "last_price": last_price,
            "pct": quote.get("pct"),
            "prev_close": prev_close,
            "market_value": market_value,
            "cost_value": cost_value,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "holding_days": holding_days,
            "review": position_review_status(holding_days, pnl_pct, day_pnl_pct),
            "updated_at": now_text,
        }
        enriched.append(row)
        if cost_value is not None:
            total_cost += cost_value
        if market_value is not None:
            total_value += market_value
        if pnl is not None:
            total_pnl += pnl
        if day_pnl is not None:
            has_day_pnl = True
            total_day_pnl += day_pnl
            if market_value is not None:
                total_day_base += market_value - day_pnl
        if trading_date and last_price is not None and qty > 0:
            con.execute(
                """
                INSERT OR REPLACE INTO paper_position_snapshots (
                  snapshot_at, trading_date, symbol, name, quantity, sellable, avg_cost,
                  last_price, market_value, cost_value, unrealized_pnl, unrealized_pnl_pct, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_text,
                    trading_date,
                    symbol,
                    row.get("name"),
                    qty,
                    row.get("sellable") or 0,
                    cost,
                    last_price,
                    market_value,
                    cost_value,
                    pnl,
                    pnl_pct,
                    row.get("source"),
                ),
            )
    con.commit()
    initial_cash = paper_initial_cash()
    cash = cash_from_filled_orders(con, initial_cash)
    total_assets = cash + total_value
    previous = previous_account_snapshot(con, trading_date, initial_cash) if trading_date else None
    position_day_pnl = total_day_pnl if has_day_pnl else None
    position_day_pnl_pct = (total_day_pnl / total_day_base * 100) if has_day_pnl and total_day_base else None
    asset_day_pnl = None
    asset_day_pnl_pct = None
    if previous and previous.get("total_assets"):
        asset_day_pnl = total_assets - float(previous["total_assets"])
        asset_day_pnl_pct = asset_day_pnl / float(previous["total_assets"]) * 100
    account = {
        "initial_cash": initial_cash,
        "cash": cash,
        "total_cost": total_cost if total_cost else 0,
        "total_amount": total_assets,
        "total_assets": total_assets,
        "market_value": total_value if total_value else 0,
        "position_pct": (total_value / total_assets * 100) if total_assets else None,
        "day_pnl": asset_day_pnl if asset_day_pnl is not None else position_day_pnl,
        "day_pnl_pct": asset_day_pnl_pct if asset_day_pnl_pct is not None else position_day_pnl_pct,
        "day_pnl_source": "account_total_assets" if asset_day_pnl is not None else "open_positions",
        "day_pnl_basis": "total_assets_vs_previous_close" if asset_day_pnl is not None else "open_positions_fallback",
        "day_pnl_basis_note": "总资产较上一交易日收盘变化" if asset_day_pnl is not None else "无上一交易日账户快照，按持仓表现估算",
        "previous_total_assets": previous.get("total_assets") if previous else None,
        "previous_snapshot_at": previous.get("snapshot_at") if previous else None,
        "previous_trading_date": previous.get("trading_date") if previous else None,
        "position_day_pnl": position_day_pnl,
        "position_day_pnl_pct": position_day_pnl_pct,
        "total_pnl": total_assets - initial_cash,
        "total_pnl_pct": ((total_assets - initial_cash) / initial_cash * 100) if initial_cash else None,
        "unrealized_pnl": total_pnl if enriched else 0,
        "unrealized_pnl_pct": (total_pnl / total_cost * 100) if total_cost else None,
        "position_count": len([x for x in enriched if (x.get("quantity") or 0) > 0]),
        "updated_at": now_text,
    }
    annotate_account_quality(con, account)
    con.execute(
        """
        INSERT OR REPLACE INTO paper_account_snapshots (
          snapshot_at, trading_date, initial_cash, cash, market_value,
          total_assets, total_pnl, total_pnl_pct, position_pct, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_text,
            trading_date,
            initial_cash,
            cash,
            total_value if total_value else 0,
            total_assets,
            account.get("total_pnl"),
            account.get("total_pnl_pct"),
            account.get("position_pct"),
            "paper_trading",
        ),
    )
    con.commit()
    return enriched, account


def record_filled_order_marks(con, trading_date, price_map=None, now=None):
    """Persist marks for filled orders after a full exit removes the position.

    Position snapshots intentionally contain only open positions because they
    feed account valuation. This separate stream preserves execution-quality
    evidence for a position that was fully sold during the session.
    """
    if not trading_date:
        return 0
    prices = normalize_price_map(price_map)
    now_dt = now or datetime.now()
    now_text = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    open_symbols = {
        str(row[0])
        for row in con.execute("SELECT symbol FROM paper_positions WHERE quantity > 0").fetchall()
    }
    rows = con.execute(
        """
        SELECT order_id, created_at, symbol
        FROM paper_orders
        WHERE trading_date=? AND status IN ('FILLED', 'PARTIAL_FILLED')
        ORDER BY created_at, order_id
        """,
        (str(trading_date),),
    ).fetchall()
    written = 0
    for order_id, created_at, symbol in rows:
        symbol = str(symbol or "")
        if not symbol or symbol in open_symbols:
            continue
        quote = prices.get(symbol) or {}
        price = quote.get("last_price")
        if price is None:
            continue
        try:
            fill_dt = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0 or now_dt < fill_dt:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO paper_order_marks (
              order_id, mark_at, trading_date, symbol, last_price, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (order_id, now_text, trading_date, symbol, price, "paper_trade_path"),
        )
        written += 1
    if written:
        con.commit()
    return written


def record_execution_intent_marks(con, trading_date, price_map=None, now=None):
    """Mark every executable intent so non-fills retain opportunity-cost evidence."""
    if not trading_date:
        return 0
    prices = normalize_price_map(price_map)
    current = now or datetime.now()
    mark_at = current.strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        """SELECT order_id, created_at, symbol, status, signal_price
           FROM paper_orders WHERE trading_date=? AND side IN ('BUY','SELL')
             AND status != 'VOID_RECONCILED'""",
        (str(trading_date),),
    ).fetchall()
    written = 0
    for order_id, created_at, symbol, status, signal_price in rows:
        quote = prices.get(str(symbol)) or {}
        last_price = quote.get("last_price")
        try:
            created = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
            signal_price = float(signal_price)
            last_price = float(last_price)
        except (TypeError, ValueError):
            continue
        if current < created or signal_price <= 0 or last_price <= 0:
            continue
        return_pct = (last_price - signal_price) / signal_price * 100
        con.execute(
            """INSERT OR REPLACE INTO paper_intent_marks
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                order_id, mark_at, trading_date, symbol, status, signal_price,
                last_price, return_pct, "execution_intent_path",
            ),
        )
        written += 1
    if written:
        con.commit()
    return written


def write_latest_snapshot(base_dir, trading_date=None, price_map=None, names=None, now=None):
    paths = runtime_paths(base_dir)
    paths["latest"].parent.mkdir(parents=True, exist_ok=True)
    con = init_db(base_dir)
    if trading_date:
        settle_t1_buys(con, trading_date)
    orders = load_orders(base_dir, trading_date=trading_date)
    positions = [
        {
            "symbol": row[0],
            "name": row[1],
            "quantity": row[2] or 0,
            "sellable": row[3] or 0,
            "avg_cost": row[4],
            "source": row[5],
            "updated_at": row[6],
        }
        for row in con.execute(
            "SELECT symbol, name, quantity, sellable, avg_cost, source, updated_at FROM paper_positions ORDER BY symbol"
        ).fetchall()
    ]
    positions = [pos for pos in positions if int(pos.get("quantity") or 0) > 0]
    if names:
        for pos in positions:
            if names.get(pos["symbol"]) and (not pos.get("name") or pos["name"] == pos["symbol"]):
                pos["name"] = names[pos["symbol"]]
    positions, account = enrich_positions_with_marks(con, positions, trading_date=trading_date, price_map=price_map, now=now)
    record_filled_order_marks(con, trading_date, price_map=price_map, now=now)
    record_execution_intent_marks(con, trading_date, price_map=price_map, now=now)
    status_counts = {}
    for order in orders:
        status = order.get("status") or "-"
        status_counts[status] = status_counts.get(status, 0) + 1
    payload = {
        "trading_date": trading_date,
        "orders": orders[-30:],
        "positions": positions,
        "account": account,
        "filled_count": sum(1 for order in orders if order.get("status") in ("FILLED", "PARTIAL_FILLED")),
        "status_counts": status_counts,
        "t1_note": "A股普通股票按T+1处理：今日模拟买入不增加当日可卖数量，下一交易日才结转为可卖。",
    }
    paths["latest"].write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload
