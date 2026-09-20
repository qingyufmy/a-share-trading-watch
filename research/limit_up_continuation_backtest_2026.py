#!/usr/bin/env python3
"""Simple 2026 backtest for non-ST A-share limit-up continuation.

Daily-bar proxy rules, with A-share T+1 selling constraint:
- Signal day T: non-ST stock closes at/near limit-up.
- Exclude T+1 one-price limit-up cases as not reliably buyable.
- Compare T+1 open entry, T+1 intraday continuation proxy entry, and
  T+1 close-confirm/T+2 follow entry.
- Prices are unadjusted daily bars from Eastmoney public endpoints.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from limit_down_reversal_study import (  # noqa: E402
    OUT_DIR,
    add_market_features,
    fetch_all_bars,
    fetch_stock_list,
    is_st_name,
)


BACKTEST_DIR = ROOT / "data" / "limit_up_continuation" / "backtest"


@dataclass(frozen=True)
class CostModel:
    buy_commission: float = 0.0003
    sell_commission: float = 0.0003
    stamp_duty: float = 0.0005
    slippage_each_side: float = 0.0005

    @property
    def round_trip(self) -> float:
        return self.buy_commission + self.sell_commission + self.stamp_duty + self.slippage_each_side * 2


def limit_up_price(prev_close: pd.Series, limit_pct: pd.Series) -> pd.Series:
    return (prev_close * (1 + limit_pct / 100)).round(2)


def prepare_2026_events(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop_duplicates(["code", "date"], keep="last")
    df = add_market_features(df)
    df = df.sort_values(["code", "date"]).copy()
    g = df.groupby("code", group_keys=False)
    for prefix, offset in [("t1", -1), ("t2", -2), ("t3", -3), ("t4", -4)]:
        for col in ["date", "open", "close", "high", "low", "pct_chg", "amount", "turnover"]:
            df[f"{prefix}_{col}"] = g[col].shift(offset)

    threshold = df["limit_pct"] - 0.25
    df["limit_up"] = (~df["is_st"]) & (df["pct_chg"] >= threshold) & (df["close"] >= df["high"] * 0.999)
    events = df[(df["date"] >= pd.Timestamp("2026-01-01")) & df["limit_up"] & (~df["is_st"])].copy()
    events = events.dropna(subset=["t1_open", "t2_open", "t2_close", "t3_close", "t4_close"])
    events = events[
        (pd.to_datetime(events["date"]) < pd.to_datetime(events["t1_date"]))
        & (pd.to_datetime(events["t1_date"]) < pd.to_datetime(events["t2_date"]))
        & (pd.to_datetime(events["t2_date"]) < pd.to_datetime(events["t3_date"]))
        & (pd.to_datetime(events["t3_date"]) < pd.to_datetime(events["t4_date"]))
    ].copy()
    events["t1_gap_from_lu_close"] = events["t1_open"] / events["close"] - 1
    events["t_amount_yuan"] = events["amount"]
    events["t1_upper_limit"] = limit_up_price(events["close"], events["limit_pct"])
    events["t1_one_price_up"] = (
        (events["t1_open"] >= events["t1_upper_limit"] * 0.998)
        & (events["t1_low"] >= events["t1_upper_limit"] * 0.998)
    )
    events["industry_mood"] = pd.cut(
        events["industry_red_rate"],
        bins=[-0.01, 0.35, 0.55, 1.01],
        labels=["板块弱", "板块中性", "板块强"],
    ).astype(str)
    return events


def add_common_filters(ev: pd.DataFrame) -> pd.DataFrame:
    ev = ev.copy()
    max_gap = ev["limit_pct"].map(lambda x: 0.08 if x <= 10.5 else 0.14)
    ev["t1_buyable"] = ~ev["t1_one_price_up"]
    ev["not_chase_open"] = ev["t1_gap_from_lu_close"] <= max_gap
    ev["liquid_enough"] = (ev["t_amount_yuan"] >= 80_000_000) | (ev["turnover"] >= 3.0)
    ev["market_not_weak"] = (ev["red_rate"] >= 0.45) | (ev["limit_down_count"] <= 30)
    ev["market_strong"] = ev["red_rate"] >= 0.55
    ev["industry_strong"] = ev["industry_red_rate"] >= 0.55
    ev["industry_very_strong"] = ev["industry_red_rate"] >= 0.65
    ev["t1_bullish_close"] = ev["t1_close"] >= ev["t1_open"]
    ev["t1_close_near_high"] = (ev["t1_high"] > ev["t1_low"]) & (
        (ev["t1_close"] - ev["t1_low"]) / (ev["t1_high"] - ev["t1_low"]) >= 0.65
    )
    ev["t1_continuation_close"] = ev["t1_close"] >= ev["close"] * 1.02
    ev["t1_not_overextended"] = ev["t1_close"] <= ev["close"] * 1.12
    return ev


def select_t1_open(events: pd.DataFrame, variant: str) -> pd.DataFrame:
    ev = add_common_filters(events)
    base = ev["t1_buyable"] & ev["not_chase_open"]
    if variant == "limit_up_t1_open_base":
        mask = base
    elif variant == "limit_up_t1_open_strong_resonance":
        mask = base & ev["market_not_weak"] & ev["industry_strong"] & ev["liquid_enough"]
    elif variant == "limit_up_t1_open_very_strong":
        mask = base & ev["market_strong"] & ev["industry_very_strong"] & ev["liquid_enough"]
    else:
        raise ValueError(f"unknown variant: {variant}")
    out = ev[mask].copy()
    out["entry_price"] = out["t1_open"]
    out["entry_date"] = pd.to_datetime(out["t1_date"])
    out["variant"] = variant
    return out


def select_t1_continuation_proxy(events: pd.DataFrame, variant: str) -> pd.DataFrame:
    ev = add_common_filters(events)
    trigger = pd.concat(
        [
            ev["close"] * 1.02,
            ev["t1_open"] * 1.005,
        ],
        axis=1,
    ).max(axis=1)
    ev["proxy_trigger_price"] = trigger
    ev["proxy_entry_price"] = trigger
    ev["proxy_touched"] = ev["t1_high"] >= trigger
    ev["proxy_not_far"] = trigger <= ev["t1_open"] * 1.06
    base = ev["t1_buyable"] & ev["not_chase_open"] & ev["proxy_touched"] & ev["proxy_not_far"]
    if variant == "limit_up_t1_proxy_base":
        mask = base
    elif variant == "limit_up_t1_proxy_strong_resonance":
        mask = base & ev["market_not_weak"] & ev["industry_strong"] & ev["liquid_enough"]
    else:
        raise ValueError(f"unknown variant: {variant}")
    out = ev[mask].copy()
    out["entry_price"] = out["proxy_entry_price"]
    out["entry_date"] = pd.to_datetime(out["t1_date"])
    out["variant"] = variant
    return out


def select_t2_follow(events: pd.DataFrame, variant: str) -> pd.DataFrame:
    ev = add_common_filters(events)
    if variant == "limit_up_t1_close_follow_t2_entry":
        mask = (
            ev["t1_buyable"]
            & ev["market_not_weak"]
            & ev["industry_strong"]
            & ev["liquid_enough"]
            & ev["t1_bullish_close"]
            & ev["t1_close_near_high"]
            & ev["t1_continuation_close"]
            & ev["t1_not_overextended"]
        )
    else:
        raise ValueError(f"unknown variant: {variant}")
    out = ev[mask].copy()
    out["entry_price"] = out["t2_open"]
    out["entry_date"] = pd.to_datetime(out["t2_date"])
    out["variant"] = variant
    return out


def simulate_exits(
    trades: pd.DataFrame,
    costs: CostModel,
    stop_loss: float,
    take_profit: float,
    entry_day: str,
) -> pd.DataFrame:
    out = trades.copy()
    entry = out["entry_price"]
    stop_price = entry * (1 - stop_loss)
    target_price = entry * (1 + take_profit)
    out["stop_price"] = stop_price
    out["target_price"] = target_price
    if entry_day == "t1":
        first_sell_prefix = "t2"
        time_exit_prefix = "t3"
    elif entry_day == "t2":
        first_sell_prefix = "t3"
        time_exit_prefix = "t4"
    else:
        raise ValueError(entry_day)
    out["earliest_sell_date"] = pd.to_datetime(out[f"{first_sell_prefix}_date"])

    exit_date = []
    exit_price = []
    exit_reason = []
    for row in out.itertuples(index=False):
        first_open = getattr(row, f"{first_sell_prefix}_open")
        first_low = getattr(row, f"{first_sell_prefix}_low")
        first_high = getattr(row, f"{first_sell_prefix}_high")
        first_date = getattr(row, f"{first_sell_prefix}_date")
        time_date = getattr(row, f"{time_exit_prefix}_date")
        time_close = getattr(row, f"{time_exit_prefix}_close")
        if first_open <= row.stop_price:
            exit_date.append(first_date)
            exit_price.append(first_open)
            exit_reason.append(f"{first_sell_prefix.upper()}开盘止损")
        elif first_low <= row.stop_price:
            exit_date.append(first_date)
            exit_price.append(row.stop_price)
            exit_reason.append(f"{first_sell_prefix.upper()}盘中止损")
        elif first_high >= row.target_price:
            exit_date.append(first_date)
            exit_price.append(row.target_price)
            exit_reason.append(f"{first_sell_prefix.upper()}盘中止盈")
        else:
            exit_date.append(time_date)
            exit_price.append(time_close)
            exit_reason.append(f"{time_exit_prefix.upper()}收盘时间止盈止损")
    out["exit_date"] = pd.to_datetime(exit_date)
    out["exit_price"] = exit_price
    out["exit_reason"] = exit_reason
    out["gross_ret"] = out["exit_price"] / out["entry_price"] - 1
    out["net_ret"] = out["gross_ret"] - costs.round_trip
    out["hold_days"] = (out["exit_date"] - out["entry_date"]).dt.days
    return out


def summarize_trades(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"n": 0}
    r = trades["net_ret"].dropna()
    return {
        "n": int(len(trades)),
        "stock_count": int(trades["code"].nunique()),
        "mean_pct": round(float(r.mean() * 100), 3),
        "median_pct": round(float(r.median() * 100), 3),
        "win_rate_pct": round(float((r > 0).mean() * 100), 2),
        "p25_pct": round(float(r.quantile(0.25) * 100), 3),
        "p75_pct": round(float(r.quantile(0.75) * 100), 3),
        "avg_hold_days": round(float(trades["hold_days"].mean()), 2),
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
    }


def run_portfolio(
    trades: pd.DataFrame,
    initial_cash: float,
    max_positions: int,
    position_pct: float,
    costs: CostModel,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame()
    candidates = trades.sort_values(
        ["entry_date", "industry_red_rate", "red_rate", "t_amount_yuan"],
        ascending=[True, False, False, False],
    )
    cash = initial_cash
    open_positions: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    dates = sorted(set(candidates["entry_date"]).union(set(candidates["exit_date"])))
    for current_date in dates:
        remaining = []
        for pos in open_positions:
            if pos["exit_date"] <= current_date:
                gross_proceeds = pos["shares"] * pos["exit_price"]
                sell_cost = gross_proceeds * (costs.sell_commission + costs.stamp_duty + costs.slippage_each_side)
                net_proceeds = gross_proceeds - sell_cost
                cash += net_proceeds
                pos["sell_cost"] = sell_cost
                pos["net_proceeds"] = net_proceeds
                pos["realized_pnl"] = net_proceeds - pos["cash_outlay"]
                fills.append({**pos, "side": "SELL", "date": current_date})
            else:
                remaining.append(pos)
        open_positions = remaining
        todays = candidates[candidates["entry_date"] == current_date]
        for row in todays.itertuples(index=False):
            if len(open_positions) >= max_positions:
                continue
            budget = min(initial_cash * position_pct, cash)
            buy_cost_rate = costs.buy_commission + costs.slippage_each_side
            shares = math.floor(budget / (row.entry_price * (1 + buy_cost_rate)) / 100) * 100
            if shares <= 0:
                continue
            gross_cost = shares * row.entry_price
            buy_cost = gross_cost * buy_cost_rate
            cash_outlay = gross_cost + buy_cost
            cash -= cash_outlay
            pos = {
                "code": row.code,
                "name": row.name,
                "industry": row.industry,
                "signal_date": row.date,
                "entry_date": row.entry_date,
                "entry_price": row.entry_price,
                "exit_date": row.exit_date,
                "exit_price": row.exit_price,
                "exit_reason": row.exit_reason,
                "shares": shares,
                "capital": gross_cost,
                "buy_cost": buy_cost,
                "cash_outlay": cash_outlay,
                "net_ret": row.net_ret,
            }
            open_positions.append(pos)
            fills.append({**pos, "side": "BUY", "date": current_date})
        marked_value = sum(pos["shares"] * pos["entry_price"] * (1 + pos["net_ret"]) for pos in open_positions)
        equity_rows.append(
            {
                "date": current_date,
                "cash": cash,
                "open_positions": len(open_positions),
                "marked_value_rough": marked_value,
                "equity_rough": cash + marked_value,
            }
        )
    return pd.DataFrame(fills), pd.DataFrame(equity_rows)


def portfolio_summary(fills: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
    if fills.empty:
        return {"trades": 0}
    sells = fills[fills["side"] == "SELL"].copy()
    realized = float(sells.get("realized_pnl", pd.Series(dtype=float)).sum())
    deployed = float(fills[fills["side"] == "BUY"]["capital"].sum())
    return {
        "round_trips": int(len(sells)),
        "realized_pnl": round(realized, 2),
        "realized_return_on_initial_pct": round(realized / initial_cash * 100, 3),
        "capital_deployed": round(deployed, 2),
        "turnover_on_initial_x": round(deployed / initial_cash, 3),
        "win_rate_pct": round(float((sells["realized_pnl"] > 0).mean() * 100), 2) if len(sells) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kline-lmt", type=int, default=150)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--refresh-list", action="store_true")
    parser.add_argument("--refresh-kline", action="store_true")
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--position-pct", type=float, default=0.10)
    parser.add_argument("--stop-loss", type=float, default=0.05)
    parser.add_argument("--take-profit", type=float, default=0.08)
    args = parser.parse_args()

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stocks = fetch_stock_list(refresh=args.refresh_list)
    stocks = [s for s in stocks if not s.code.startswith(("8", "4", "9")) and not is_st_name(s.name)]
    print(f"stock universe={len(stocks)}", file=sys.stderr)
    df = fetch_all_bars(stocks, lmt=args.kline_lmt, refresh=args.refresh_kline, workers=args.workers)
    events = prepare_2026_events(df)
    costs = CostModel()
    variants: list[tuple[str, str, Any]] = [
        ("limit_up_t1_open_base", "t1", select_t1_open),
        ("limit_up_t1_open_strong_resonance", "t1", select_t1_open),
        ("limit_up_t1_open_very_strong", "t1", select_t1_open),
        ("limit_up_t1_proxy_base", "t1", select_t1_continuation_proxy),
        ("limit_up_t1_proxy_strong_resonance", "t1", select_t1_continuation_proxy),
        ("limit_up_t1_close_follow_t2_entry", "t2", select_t2_follow),
    ]
    summaries: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "assumptions": {
            "signal": "T day non-ST limit-up close",
            "buyability": "T+1 one-price limit-up excluded as not reliably buyable",
            "exit": f"After earliest sell date: stop {args.stop_loss:.1%}, take-profit {args.take_profit:.1%}; if both touched in one daily bar, assume stop first",
            "cost_round_trip_pct": round(costs.round_trip * 100, 3),
            "position": f"{args.initial_cash:.0f} initial cash, {args.position_pct:.1%} per trade, max {args.max_positions} concurrent positions, 100-share lots",
        },
        "event_count_2026_with_future": int(len(events)),
        "event_date_range": {
            "start": events["date"].min().strftime("%Y-%m-%d") if not events.empty else None,
            "end": events["date"].max().strftime("%Y-%m-%d") if not events.empty else None,
        },
        "variants": {},
    }
    all_trades = []
    for variant, entry_day, selector in variants:
        selected = selector(events, variant)
        trades = simulate_exits(selected, costs, args.stop_loss, args.take_profit, entry_day)
        fills, equity = run_portfolio(trades, args.initial_cash, args.max_positions, args.position_pct, costs)
        summaries["variants"][variant] = {
            "trade_stats": summarize_trades(trades),
            "portfolio": portfolio_summary(fills, args.initial_cash),
        }
        trades.to_csv(BACKTEST_DIR / f"trades_2026_{variant}.csv", index=False, encoding="utf-8-sig")
        fills.to_csv(BACKTEST_DIR / f"portfolio_fills_2026_{variant}.csv", index=False, encoding="utf-8-sig")
        equity.to_csv(BACKTEST_DIR / f"portfolio_equity_2026_{variant}.csv", index=False, encoding="utf-8-sig")
        all_trades.append(trades)
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(BACKTEST_DIR / "trades_2026_all_variants.csv", index=False, encoding="utf-8-sig")
    (BACKTEST_DIR / "summary_2026.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
