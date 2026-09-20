"""Global technology risk context shared by reports and realtime signals."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from core import theme_validation


NORMAL = "green"
YELLOW = "yellow"
RED = "red"
LOCAL_ENTRY_POLICY_VERSION = "a_share_entry_evidence_20260914"


def external_entry_context(context: dict[str, Any]) -> dict[str, Any]:
    """An overseas/selected-watchlist warning is not an A-share circuit breaker.

    Retain the risk level and defensive position rules, but separate their
    origin from permission to open. Live domestic evidence is applied each tick.
    """
    result = deepcopy(context)
    policy = dict(result.get("policy") or {})
    policy.update(
        buy_gate="external_caution" if result.get("risk_level") != NORMAL else "normal",
        allow_core_attack_buy=True, allow_market_opportunity_buy=True,
        allow_tech_opportunity_buy=True, allow_non_tech_sector_buy=True,
        disable_attack_buy_before=None,
        notes="海外/自选竞价风险只提示，不代表A股全市场禁开；盘中依据当日A股、板块共振与策略买点逐股判断，保留仓位及防守纪律。",
    )
    result.update(policy=policy, entry_risk_source="external_only",
                  entry_policy_version=LOCAL_ENTRY_POLICY_VERSION)
    result.pop("a_share_entry_evidence", None)
    return result


def apply_a_share_entry_policy(
    context: dict[str, Any], local_regime: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """Recompute broad entry permission from fresh full-market domestic evidence.

    Reuse the existing risk-off/weak-breadth definitions. Both must hold with
    all three indices negative; a growth-only shock is still a scoped gate.
    Never carry a previous tick's broad ban into a recovered domestic market.
    """
    result = external_entry_context(context)
    result["a_share_market_regime"] = deepcopy(local_regime)
    breadth = local_regime.get("breadth") or {}
    result["market_breadth"] = deepcopy(breadth)

    def fresh(item):
        try:
            stamp = datetime.fromisoformat(str(item.get("updated_at") or ""))
            return stamp.date() == now.date() and 0 <= (now - stamp).total_seconds() <= 300
        except (ValueError, TypeError):
            return False

    indexes = local_regime.get("indexes") or {}
    values = [_number((indexes.get(key) or {}).get("pct"))
              for key in ("shanghai", "shenzhen", "growth")]
    ready = (fresh(local_regime) and fresh(breadth)
             and not local_regime.get("stale")
             and local_regime.get("state") != "data_stale"
             and breadth.get("state") != "data_stale"
             and breadth.get("coverage_ready") is True
             and all(value is not None and math.isfinite(value) for value in values)
             and not any((indexes.get(key) or {}).get("stale")
                         for key in ("shanghai", "shenzhen", "growth")))
    systemic = bool(ready and local_regime.get("state") == "risk_off"
                    and breadth.get("state") in {"broad_weak", "shrinking_weak"}
                    and all(value < 0 for value in values))
    source = "a_share_systemic" if systemic else "external_only" if ready else "a_share_data_unready"
    reason = ("当日A股风险偏好转弱、三大指数同跌且全市场弱广度，暂停新增仓"
              if systemic else "海外警报不禁止开仓；逐股核验当日板块共振和策略买点"
              if ready else "当日A股指数/全市场广度数据不完整或超过5分钟，暂停新增仓；不是海外风险禁开")
    result["entry_risk_source"] = source
    result["a_share_entry_evidence"] = {
        "evaluated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_ready": bool(ready), "systemic_confirmed": systemic,
        "local_state": local_regime.get("state"), "index_pct": values,
        "breadth_state": breadth.get("state"), "breadth_updated_at": breadth.get("updated_at"),
        "coverage_ready": breadth.get("coverage_ready"), "reason": reason,
    }
    if not ready or systemic:
        result["policy"].update(allow_core_attack_buy=False, allow_market_opportunity_buy=False,
                                buy_gate=source, notes=reason)
    return result


# 外部科技冲击首先影响科技/成长链，而不是天然等同于全部 A 股的
# 系统性风险。非科技方向若出现独立的板块共振，仍应交给本地量价和
# 三周期纪律判断，不能被科技因子一刀切关闭。
TECHNOLOGY_RISK_PATTERN = re.compile(
    r"半导体|芯片|存储|DRAM|NAND|HBM|电子|元件|消费电子|通信|光通信|CPO|"
    r"算力|服务器|数据中心|AI|人工智能|软件|计算机|互联网|集成电路|PCB|"
    r"英伟达|三星|海力士|美光|科技",
    re.I,
)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def candidate_risk_bucket(row: dict[str, Any] | None) -> str:
    """Classify a candidate for a technology-specific external shock."""
    row = row or {}
    explicit = str(row.get("risk_bucket") or row.get("sector_bucket") or "").strip().lower()
    if explicit in {"technology", "tech", "科技"}:
        return "technology"
    if explicit in {"other", "non_tech", "non-tech", "非科技"}:
        return "other"
    quote = row.get("quote") or {}
    momentum = row.get("sector_momentum") or {}
    framework_validation = row.get("framework_validation") or theme_validation.framework_evidence_from_text(
        row.get("focus") or row.get("框架依据")
    )
    theme_evidence = row.get("theme_evidence") or quote.get("theme_evidence") or {}
    # Once a candidate has framework evidence, raw provider concepts and a
    # persisted focus string must not override industry/name classification.
    # This keeps a mislabeled concept from switching the global-risk route.
    if theme_evidence or framework_validation.get("executable") is False:
        parts = [
            row.get("industry"), framework_validation.get("industry_text"), row.get("机会名称"),
            quote.get("industry"), quote.get("name"),
            theme_evidence.get("labels"), row.get("framework_theme_labels"),
            momentum.get("sector_name"), momentum.get("board_name"),
        ]
    else:
        parts = [
            row.get("industry"), row.get("concepts"), row.get("focus"), row.get("matched"),
            row.get("机会名称"), row.get("框架依据"),
            quote.get("industry"), quote.get("concepts"), quote.get("name"),
            momentum.get("sector_name"), momentum.get("board_name"),
        ]
    text = " ".join(str(part) for part in parts if part)
    return "technology" if TECHNOLOGY_RISK_PATTERN.search(text) else "other"


def sector_rotation_gate(row: dict[str, Any] | None) -> tuple[bool, str]:
    """Require independent non-tech sector emotion and live volume confirmation.

    This is only an eligibility gate under a technology-specific shock.  The
    regular radar/discipline gates still require VWAP, position, risk/reward,
    T+1 and simulated-fill checks before an order exists.
    """
    row = row or {}
    if candidate_risk_bucket(row) == "technology":
        return False, "科技链候选不适用非科技板块独立通道"
    momentum = row.get("sector_momentum") or {}
    board_pct = _number(momentum.get("board_pct"))
    emotion_ok = momentum.get("emotion_ok") is True
    board_name = str(momentum.get("board_name") or momentum.get("sector_name") or row.get("industry") or "所属板块")
    if not emotion_ok or board_pct is None or board_pct < 0.8:
        return False, f"非科技方向需板块情绪确认：{board_name} 未达到强势阈值"
    rotation = row.get("sector_rotation") or {}
    if not rotation.get("sustained"):
        return False, f"{board_name} 仅单次走强，未完成连续刷新确认"
    if not rotation.get("leader_healthy"):
        return False, f"{board_name} 领涨股承接未确认，不把板块脉冲当作入场信号"
    features = row.get("rt_features") or {}
    amount_1m = _number(features.get("amount_ratio_1m"))
    amount_5m = _number(features.get("amount_ratio_5m"))
    if amount_1m is None or amount_5m is None:
        return False, f"{board_name} 板块情绪已转强，但分钟量能未刷新"
    if amount_1m < 1.0 or amount_5m < 1.0:
        return False, f"{board_name} 板块情绪转强，但个股1m/5m量能 {amount_1m:.2f}/{amount_5m:.2f} 未同步"
    return True, (
        f"非科技板块独立确认：{board_name} {board_pct:.2f}% + 连续板块/领涨承接 + "
        f"个股1m/5m量能 {amount_1m:.2f}/{amount_5m:.2f}"
    )


def confirmed_strategy_sector(context: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """A verified planned sector may diverge from a weak growth-style index.

    Do not add another volume threshold here: the selected strategy owns its
    execution volume. Domestic systemic/data vetoes always take precedence.
    """
    row = row or {}
    contract = row.get("strategy_contract") or {}
    identity = (contract.get("daily_metrics") or {}).get("resonance_identity") or {}
    momentum = row.get("sector_momentum") or {}
    rotation = row.get("sector_rotation") or {}
    pct = _number(momentum.get("board_pct"))
    return bool(
        context.get("entry_risk_source") == "external_only"
        and (context.get("a_share_entry_evidence") or {}).get("data_ready") is True
        and not (context.get("a_share_entry_evidence") or {}).get("systemic_confirmed")
        and contract.get("key") in {"LEADER_EMOTION", "TREND_MA5", "TREND_520"}
        and contract.get("daily_qualified") is True
        and identity.get("status") == "ready"
        and momentum.get("board_id") in (identity.get("ids") or [])
        and momentum.get("board_name") in (identity.get("names") or [])
        and (momentum.get("theme_evidence") or {}).get("valid") is True
        and momentum.get("emotion_ok") is True
        and pct is not None and math.isfinite(pct) and pct >= 0.8
        and rotation.get("sustained") is True and rotation.get("leader_healthy") is True
        and rotation.get("board_name") == momentum.get("board_name")
    )


def market_opportunity_gate_status(
    context: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """Return the buy-gate result with an auditable blocking stage."""
    context = context or {}
    policy = context.get("policy") or {}
    level = str(context.get("risk_level") or "").lower()
    risk_scope = str(context.get("risk_scope") or "broad").lower()
    bucket = candidate_risk_bucket(row)
    current = now or datetime.now()
    local_regime = context.get("a_share_market_regime") or {}
    if context.get("entry_risk_source") == "a_share_data_unready":
        return False, "A_SHARE_REGIME_BLOCKED", str(policy.get("notes"))
    if str(local_regime.get("state") or "neutral") == "data_stale":
        return False, "A_SHARE_REGIME_BLOCKED", str(local_regime.get("action") or "A股指数数据未完成刷新，新增机会只观察")
    if context.get("entry_risk_source") == "a_share_systemic":
        return False, "GLOBAL_RISK_BLOCKED", str(policy.get("notes"))

    # A red technology shock is a sector gate, not an all-market circuit
    # breaker.  It blocks technology candidates, while independent non-tech
    # sectors may continue only with board emotion and live volume confirmation.
    if level == RED and risk_scope == "technology" and context.get("entry_risk_source") != "external_only":
        if bucket == "technology":
            return False, "TECH_RISK_BLOCKED", "科技链红色门控：科技/成长方向只观察，等待外部风险与本地结构修复"
        rotation_ok, rotation_reason = sector_rotation_gate(row)
        if not rotation_ok:
            return False, "SECTOR_ROTATION_BLOCKED", rotation_reason
        return True, "SECTOR_ROTATION_ALLOWED", rotation_reason

    if bool(policy.get("allow_market_opportunity_buy", True)):
        allowed, fade_reason = afternoon_external_fade_gate(context, row=row, now=now)
        if not allowed:
            return False, "EXTERNAL_FADE_BLOCKED", fade_reason
        local_state = str(local_regime.get("state") or "neutral")
        if local_state in {"transition", "risk_off", "rotation_defensive"}:
            if confirmed_strategy_sector(context, row):
                return True, "STRATEGY_SECTOR_ALLOWED", "当日盘前授权板块持续共振、领涨承接确认；允许按本策略继续核验买点，不由整体风格一刀切否决"
            # Growth/technology weakness is not enough to erase a verified
            # rotation into medicine, resources, energy, consumer and so on.
            if row is None:
                return False, "A_SHARE_REGIME_BLOCKED", str(local_regime.get("action") or "A股盘中状态转弱：新增机会只观察")
            if bucket != "technology":
                rotation_ok, rotation_reason = sector_rotation_gate(row)
                if rotation_ok:
                    return True, "SECTOR_ROTATION_ALLOWED", rotation_reason
                return False, "SECTOR_ROTATION_BLOCKED", rotation_reason
            return False, "A_SHARE_TECH_REGIME_BLOCKED", str(local_regime.get("action") or "A股成长风格转弱：科技链新增只观察")
        return True, "", ""
    source = str((row or {}).get("candidate_source") or "")
    if (
        level == YELLOW
        and source.startswith("auction:")
        and current.time() >= dtime(9, 35)
    ):
        return True, "", ""
    label = {RED: "红色", YELLOW: "黄色"}.get(level, "全局")
    return False, "GLOBAL_RISK_BLOCKED", f"全局{label}门控阻断：只观察"


def market_opportunity_buy_allowed(context: dict[str, Any] | None, row: dict[str, Any] | None = None, now: datetime | None = None) -> bool:
    """Return whether the market-radar execution gate is open."""
    allowed, _, _ = market_opportunity_gate_status(context, row=row, now=now)
    return allowed


def market_opportunity_gate_reason(context: dict[str, Any] | None, row: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """Return the user-facing reason for a blocked market-radar buy."""
    _, _, reason = market_opportunity_gate_status(context, row=row, now=now)
    return reason


def core_attack_gate_status(
    context: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """Apply the same scoped-risk policy to first entries in the core list."""
    context = context or {}
    policy = context.get("policy") or {}
    if context.get("entry_risk_source") == "a_share_data_unready":
        return False, "CORE_A_SHARE_DATA_STALE", str(policy.get("notes"))
    if policy.get("allow_core_attack_buy") is False:
        return False, "CORE_GLOBAL_RISK_BLOCKED", str(policy.get("notes") or "全市场系统性风险门控：核心池新增关闭")
    local_regime = context.get("a_share_market_regime") or {}
    local_state = str(local_regime.get("state") or "neutral")
    if local_state == "data_stale":
        return False, "CORE_A_SHARE_DATA_STALE", str(
            local_regime.get("action") or "A股指数数据未完成刷新，V2新增机会只观察"
        )
    if local_state in {"transition", "risk_off", "rotation_defensive"}:
        if confirmed_strategy_sector(context, row):
            return True, "CORE_STRATEGY_SECTOR_ALLOWED", "当日盘前授权板块持续共振、领涨承接确认；允许按本策略继续核验买点，不由整体风格一刀切否决"
        if candidate_risk_bucket(row) == "technology":
            return False, "CORE_A_SHARE_TECH_REGIME_BLOCKED", str(
                local_regime.get("action") or "A股成长风格转弱，科技链新增只观察"
            )
        rotation_ok, rotation_reason = sector_rotation_gate(row)
        if not rotation_ok:
            return False, "CORE_SECTOR_ROTATION_BLOCKED", rotation_reason
        return True, "CORE_SECTOR_ROTATION_ALLOWED", rotation_reason
    if (
        str(context.get("risk_level") or "").lower() == RED
        and str(context.get("risk_scope") or "broad").lower() == "technology"
        and context.get("entry_risk_source") != "external_only"
    ):
        if candidate_risk_bucket(row) == "technology":
            return False, "CORE_TECH_RISK_BLOCKED", "科技链红色门控：科技/成长核心票暂停新增，只保留防守与修复观察"
        rotation_ok, rotation_reason = sector_rotation_gate(row)
        if not rotation_ok:
            return False, "CORE_SECTOR_ROTATION_BLOCKED", rotation_reason
        return True, "CORE_SECTOR_ROTATION_ALLOWED", rotation_reason
    return True, "", ""


def core_attack_buy_allowed(context: dict[str, Any] | None, row: dict[str, Any] | None = None, now: datetime | None = None) -> bool:
    allowed, _, _ = core_attack_gate_status(context, row=row, now=now)
    return allowed


def afternoon_external_fade_gate(
    context: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Block new opportunity buys after lunch when overseas tech momentum fades.

    A green closing quote is insufficient when the Korean/Japanese tech path has
    already retraced materially from its intraday high. The gate is deliberately
    time-bound and only affects new market-radar buys; risk exits remain active.
    """
    context = context or {}
    if candidate_risk_bucket(row) != "technology":
        return True, ""
    current = now or datetime.now()
    if current.time() < dtime(13, 0):
        return True, ""
    path = context.get("intraday_path") or {}
    markets = path.get("markets") or {}
    fades = []
    for key, label, drawdown_limit, current_limit in (
        ("kospi", "韩国科技链", -3.0, 2.5),
        ("nikkei", "日经科技链", -1.8, 1.5),
    ):
        metrics = markets.get(key) or {}
        drawdown = metrics.get("drawdown_from_high_pct")
        quote = metrics.get("current_pct")
        if (
            isinstance(drawdown, (int, float))
            and isinstance(quote, (int, float))
            and drawdown <= drawdown_limit
            and quote <= current_limit
        ):
            fades.append(f"{label}较盘中高点回撤{drawdown:.2f}%（当前{quote:.2f}%）")
    if not fades:
        return True, ""
    return False, "午后外部科技动能衰减：" + "；".join(fades) + "，新增机会只观察"


def runtime_path(base_dir: Path | str) -> Path:
    return Path(base_dir) / "data" / "runtime" / "global_risk_context.json"


def intraday_path_file(base_dir: Path | str) -> Path:
    """Persistent intraday path used to detect shocks and V-shaped repairs."""
    return Path(base_dir) / "data" / "runtime" / "global_risk_path.json"


PATH_ALIASES = {
    "nasdaq": ("纳斯达克",),
    "spx": ("标普",),
    "kospi": ("韩国",),
    "nikkei": ("日经",),
    "hsi": ("恒生", "香港"),
}

# A primary quote occasionally carries a stale prior-session percentage while
# its cross-check is already current.  A single such point must never become
# the intraday high/low used by the recovery gate.
PATH_CROSSCHECK_MAX_DELTA_PCT = 0.75
PATH_OPENING_OUTLIER_DELTA_PCT = 3.0


def _path_quote_values(global_quotes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for key, aliases in PATH_ALIASES.items():
        row = _find_quote(global_quotes, *aliases)
        value = _pct(row)
        if value is None:
            continue
        cross_check = (row or {}).get("cross_check") or {}
        cross_value = _pct(cross_check)
        source = row.get("source")
        quality = "primary"
        if (
            isinstance(cross_value, (int, float))
            and abs(value - cross_value) > PATH_CROSSCHECK_MAX_DELTA_PCT
        ):
            value = cross_value
            source = cross_check.get("source") or source
            quality = "cross_check_override"
        values[key] = {
            "pct": value,
            "price": row.get("price"),
            "time": row.get("time"),
            "source": source,
            "quality": quality,
        }
    return values


def _drop_opening_path_outliers(path: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove a lone opening quote that is incompatible with the session path.

    The first sample is vulnerable to a stale cache because no same-day point
    exists yet.  Only remove it when later observations establish a clearly
    different regime; genuine persistent moves remain untouched.
    """
    samples = path.get("samples") or []
    if len(samples) < 3:
        return []
    first_quotes = samples[0].get("quotes") or {}
    issues: list[dict[str, Any]] = []
    for key in PATH_ALIASES:
        first = (first_quotes.get(key) or {}).get("pct")
        later = [
            float(((sample.get("quotes") or {}).get(key) or {}).get("pct"))
            for sample in samples[1:]
            if isinstance(((sample.get("quotes") or {}).get(key) or {}).get("pct"), (int, float))
        ]
        if not isinstance(first, (int, float)) or len(later) < 2:
            continue
        ordered = sorted(later)
        median = ordered[len(ordered) // 2]
        if abs(float(first) - median) <= PATH_OPENING_OUTLIER_DELTA_PCT:
            continue
        first_quotes.pop(key, None)
        issues.append({
            "timestamp": samples[0].get("timestamp"),
            "market": key,
            "discarded_pct": round(float(first), 2),
            "reference_pct": round(median, 2),
            "reason": "opening_quote_inconsistent_with_session_path",
        })
    return issues


def read_intraday_path(base_dir: Path | str, report_date: str) -> dict[str, Any]:
    path = intraday_path_file(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if str(data.get("date") or "") != str(report_date):
        previous_summary = data.get("summary")
        if not isinstance(previous_summary, dict) and isinstance(data.get("samples"), list):
            previous_summary = summarize_intraday_path(data)
        return {
            "version": "global_tech_path_v1",
            "date": report_date,
            "samples": [],
            "previous_date": data.get("date"),
            "previous_summary": previous_summary if isinstance(previous_summary, dict) else {},
        }
    if not isinstance(data.get("samples"), list):
        data["samples"] = []
    # Work from a detached structure so a malformed cached file cannot mutate
    # the caller's raw JSON object while we compute its audit trail.
    data = deepcopy(data)
    issues = _drop_opening_path_outliers(data)
    if issues:
        data.setdefault("quality_issues", []).extend(issues)
        data["quality_issues"] = data["quality_issues"][-20:]
    return data


def _path_metrics(samples: list[dict[str, Any]], key: str) -> dict[str, Any]:
    points = []
    for sample in samples:
        value = ((sample.get("quotes") or {}).get(key) or {}).get("pct")
        if isinstance(value, (int, float)):
            points.append(float(value))
    if not points:
        return {
            "samples": 0,
            "first_pct": None,
            "current_pct": None,
            "min_pct": None,
            "max_pct": None,
            "rebound_from_low_pct": None,
            "drawdown_from_high_pct": None,
        }
    current = points[-1]
    low = min(points)
    high = max(points)
    return {
        "samples": len(points),
        "first_pct": round(points[0], 2),
        "current_pct": round(current, 2),
        "min_pct": round(low, 2),
        "max_pct": round(high, 2),
        "rebound_from_low_pct": round(current - low, 2),
        "drawdown_from_high_pct": round(current - high, 2),
    }


def summarize_intraday_path(path: dict[str, Any]) -> dict[str, Any]:
    samples = path.get("samples") or []
    metrics = {key: _path_metrics(samples, key) for key in PATH_ALIASES}
    kospi = metrics["kospi"]
    current = kospi.get("current_pct")
    low = kospi.get("min_pct")
    rebound = kospi.get("rebound_from_low_pct")
    previous_summary = path.get("previous_summary") or {}
    previous_kospi = (previous_summary.get("markets") or {}).get("kospi") or {}
    previous_current = previous_kospi.get("current_pct")
    recent = []
    for sample in samples[-3:]:
        value = ((sample.get("quotes") or {}).get("kospi") or {}).get("pct")
        if isinstance(value, (int, float)):
            recent.append(float(value))

    # A V-shaped repair requires both a material selloff and a confirmed
    # recovery. One green tick after a crash is not enough.
    strong_v = (
        isinstance(low, (int, float))
        and isinstance(current, (int, float))
        and isinstance(rebound, (int, float))
        and low <= -3.0
        and current >= 0.0
        and rebound >= 3.0
        and len(recent) >= 2
        and sum(value >= 0 for value in recent[-2:]) >= 2
    )
    partial_v = (
        isinstance(low, (int, float))
        and isinstance(current, (int, float))
        and isinstance(rebound, (int, float))
        and low <= -2.0
        and current >= 0.0
        and rebound >= 2.5
    )
    # When the shock happens before the A-share session, the first 09:30
    # sample may already be green. Preserve the previous session's close and
    # classify the overnight-to-open repair without fabricating a same-day V.
    preopen_repair = (
        isinstance(previous_current, (int, float))
        and previous_current <= -3.0
        and isinstance(current, (int, float))
        and current >= 0.0
        and any(
            isinstance(value, (int, float)) and value >= 0.5
            for value in (metrics["nikkei"].get("current_pct"), metrics["hsi"].get("current_pct"))
        )
    )
    shock_unrepaired = (
        isinstance(low, (int, float))
        and low <= -3.0
        and (not isinstance(current, (int, float)) or current < 0.0 or (rebound or 0) < 1.5)
    )
    if strong_v:
        event_state = "deep_v_recovery"
    elif partial_v:
        event_state = "partial_recovery"
    elif preopen_repair:
        event_state = "preopen_recovery"
    elif shock_unrepaired:
        event_state = "shock_unrepaired"
    else:
        event_state = "normal"

    recovery_score = 0
    path_reason: list[str] = []
    if strong_v:
        recovery_score += 3
        path_reason.append(
            f"韩国盘中深V：最低{low:.2f}%，当前{current:.2f}%，较低点反弹{rebound:.2f}%"
        )
    elif partial_v:
        recovery_score += 2
        path_reason.append(
            f"韩国盘中修复：最低{low:.2f}%，当前{current:.2f}%，较低点反弹{rebound:.2f}%"
        )
    elif preopen_repair:
        recovery_score += 2
        path_reason.append(
            f"开盘前外部冲击已修复：前一交易日韩国收盘{previous_current:.2f}%，"
            f"当前{current:.2f}%，日韩/港股出现同步转强"
        )
    elif shock_unrepaired:
        path_reason.append(f"韩国盘中冲击尚未修复：最低{low:.2f}%，当前{current:.2f}%")

    nikkei = metrics["nikkei"].get("current_pct")
    hsi = metrics["hsi"].get("current_pct")
    if (strong_v or partial_v or preopen_repair) and any(isinstance(value, (int, float)) and value >= 0.5 for value in (nikkei, hsi)):
        recovery_score += 1
        path_reason.append("日韩/港股至少一个主要市场同步转强")

    return {
        "version": "global_tech_path_v1",
        "date": path.get("date"),
        "sample_count": len(samples),
        "first_sample_at": (samples[0].get("timestamp") if samples else None),
        "last_sample_at": (samples[-1].get("timestamp") if samples else None),
        "event_state": event_state,
        "recovery_confirmed": bool(strong_v or partial_v or preopen_repair),
        "preopen_repair": bool(preopen_repair),
        "previous_date": path.get("previous_date"),
        "previous_kospi_current_pct": previous_current,
        "recovery_score": recovery_score,
        "path_reason": path_reason[:5],
        "quality_issues": (path.get("quality_issues") or [])[-5:],
        "markets": metrics,
    }


def record_intraday_path(
    base_dir: Path | str,
    report_date: str,
    global_quotes: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    path = read_intraday_path(base_dir, report_date)
    values = _path_quote_values(global_quotes)
    if values:
        timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        last = path.get("samples")[-1] if path.get("samples") else None
        if not last or last.get("timestamp") != timestamp:
            path.setdefault("samples", []).append({"timestamp": timestamp, "quotes": values})
        # Keep a full trading session at the current five-minute refresh rate
        # without allowing an accidentally long-running process to grow forever.
        path["samples"] = path["samples"][-720:]
        path["updated_at"] = timestamp
        path["summary"] = summarize_intraday_path(path)
        target = intraday_path_file(base_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(path, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.get("summary") or summarize_intraday_path(path)


def _pct(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    try:
        return float(row.get("pct"))
    except Exception:
        return None


def _find_quote(global_quotes: list[dict[str, Any]], *names: str) -> dict[str, Any] | None:
    aliases = {
        "标普": ("标普", "标准普尔", "S&P", "SPX"),
        "韩国": ("韩国", "KOSPI", "KS11"),
        "纳斯达克": ("纳斯达克", "NASDAQ", "IXIC"),
        "日经": ("日经", "N225", "NKY"),
        "恒生": ("恒生", "香港", "HSI"),
    }
    needles: list[str] = []
    for name in names:
        needles.extend(aliases.get(name, (name,)))
    for row in global_quotes or []:
        text = str(row.get("name") or "")
        if any(name in text for name in needles):
            return row
    return None


def _quote_meta(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "name": row.get("name"),
        "source": row.get("source"),
        "time": row.get("time"),
        "cross_check": row.get("cross_check"),
    }


def _focus_text(ths_focus: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if not isinstance(ths_focus, dict):
        return ""
    for obj in ths_focus.values():
        if isinstance(obj, dict):
            parts.append(json.dumps(obj, ensure_ascii=False)[:6000])
        elif isinstance(obj, list):
            parts.append(json.dumps(obj[:20], ensure_ascii=False))
        else:
            parts.append(str(obj))
    return " ".join(parts)


def policy_for_level(level: str, risk_scope: str = "broad") -> dict[str, Any]:
    if level == RED and risk_scope == "technology":
        return {
            "buy_gate": "technology_defensive",
            "allow_core_attack_buy": True,
            "disable_attack_buy_before": None,
            "allow_market_opportunity_buy": True,
            "allow_tech_opportunity_buy": False,
            "allow_non_tech_sector_buy": True,
            "allow_repair_continuation": False,
            "repair_continuation_start": "09:45",
            "repair_continuation_max_vwap_distance_pct": 0.025,
            "profit_guard_min_pnl_pct": 15.0,
            "profit_guard_day_loss_pct": -3.5,
            "profit_erosion_max_pnl_pct": 12.0,
            "profit_erosion_day_loss_pct": -5.5,
            "portfolio_risk_position_pct": 70.0,
            "portfolio_risk_day_loss_pct": -1.5,
            "portfolio_risk_soft_break_ratio": 0.25,
            "overnight_position_cap_pct": 3.0,
            "overnight_de_risk_start": "14:30",
            "opening_carry_de_risk_start": "09:35",
            "opening_carry_de_risk_end": "09:50",
            "opening_carry_de_risk_fraction": 0.34,
            "notes": "科技链红灯：科技/成长新增关闭；非科技仅在板块情绪转强、分钟量能同步和三周期确认后允许首笔试错。",
        }
    if level == RED:
        return {
            "buy_gate": "defensive_day",
            "allow_core_attack_buy": False,
            "disable_attack_buy_before": "15:00",
            "allow_market_opportunity_buy": False,
            "allow_repair_continuation": False,
            "repair_continuation_start": "09:45",
            "repair_continuation_max_vwap_distance_pct": 0.025,
            "profit_guard_min_pnl_pct": 15.0,
            "profit_guard_day_loss_pct": -3.5,
            "profit_erosion_max_pnl_pct": 12.0,
            "profit_erosion_day_loss_pct": -5.5,
            "portfolio_risk_position_pct": 70.0,
            "portfolio_risk_day_loss_pct": -1.5,
            "portfolio_risk_soft_break_ratio": 0.25,
            "overnight_position_cap_pct": 3.0,
            "overnight_de_risk_start": "14:30",
            "opening_carry_de_risk_start": "09:35",
            "opening_carry_de_risk_end": "09:50",
            "opening_carry_de_risk_fraction": 0.34,
            "notes": "全球科技负反馈：全天禁新增进攻，提前利润保护，组合风控前置。",
        }
    if level == YELLOW:
        return {
            "buy_gate": "strict_confirmation",
            "allow_core_attack_buy": True,
            "disable_attack_buy_before": None,
            "allow_market_opportunity_buy": True,
            "allow_repair_continuation": True,
            "repair_continuation_start": "09:45",
            "repair_continuation_max_vwap_distance_pct": 0.025,
            "profit_guard_min_pnl_pct": 18.0,
            "profit_guard_day_loss_pct": -4.0,
            "profit_erosion_max_pnl_pct": 12.0,
            "profit_erosion_day_loss_pct": -6.0,
            "portfolio_risk_position_pct": 80.0,
            "portfolio_risk_day_loss_pct": -2.0,
            "portfolio_risk_soft_break_ratio": 0.35,
            "overnight_position_cap_pct": 6.0,
            "overnight_de_risk_start": "14:30",
            "opening_carry_de_risk_start": None,
            "opening_carry_de_risk_end": None,
            "opening_carry_de_risk_fraction": 0.0,
            "notes": "外部风险黄灯：不关闭新增买入；仅允许价格、量能、板块承接共同确认。",
        }
    return {
        "buy_gate": "normal",
        "allow_core_attack_buy": True,
        "disable_attack_buy_before": None,
        "allow_market_opportunity_buy": True,
        "allow_repair_continuation": True,
        "repair_continuation_start": "09:45",
        "repair_continuation_max_vwap_distance_pct": 0.025,
        "profit_guard_min_pnl_pct": 20.0,
        "profit_guard_day_loss_pct": -5.0,
        "profit_erosion_max_pnl_pct": 10.0,
        "profit_erosion_day_loss_pct": -7.0,
        "portfolio_risk_position_pct": 90.0,
        "portfolio_risk_day_loss_pct": -2.5,
        "portfolio_risk_soft_break_ratio": 0.50,
        "overnight_position_cap_pct": 10.0,
        "overnight_de_risk_start": "14:45",
        "opening_carry_de_risk_start": None,
        "opening_carry_de_risk_end": None,
        "opening_carry_de_risk_fraction": 0.0,
        "notes": "全球风险正常：沿用常规信号纪律。",
    }


def apply_intraday_recovery_policy(policy: dict[str, Any] | None, path_state: dict[str, Any] | None) -> dict[str, Any]:
    """Keep a sharp overseas V-recovery tradable without treating it as stable overnight risk.

    A same-day rebound can reopen local opportunity screening, but A-share T+1
    makes a late entry an overnight decision.  The overlay therefore narrows
    only late-session *new* exposure; it does not downgrade a red hard block
    or create a buy signal on its own.
    """
    merged = dict(policy or {})
    path_state = path_state or {}
    if str(path_state.get("event_state") or "") not in {"deep_v_recovery", "partial_recovery"}:
        return merged
    merged.update({
        "recovery_overnight_guard": True,
        "recovery_overnight_entry_cutoff": "13:45",
        "recovery_overnight_entry_position_cap_pct": 2.0,
        "recovery_overnight_t1_cap_pct": 2.5,
    })
    note = str(merged.get("notes") or "")
    suffix = "深V修复仅代表风险缓解；13:45后新增仓按2%试错并受T+1隔夜约束。"
    if suffix not in note:
        merged["notes"] = f"{note}；{suffix}" if note else suffix
    return merged


def infer_premarket_context(
    report_date: str,
    global_quotes: list[dict[str, Any]],
    macro_bias: str = "",
    ths_focus: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    nas = _pct(_find_quote(global_quotes, "纳斯达克"))
    spx = _pct(_find_quote(global_quotes, "标普"))
    kospi = _pct(_find_quote(global_quotes, "韩国"))
    nikkei = _pct(_find_quote(global_quotes, "日经"))
    hsi = _pct(_find_quote(global_quotes, "恒生", "香港"))
    text = f"{macro_bias} {_focus_text(ths_focus)}"

    score = 0
    tech_score = 0
    broad_score = 0
    reasons: list[str] = []
    if nas is not None:
        if nas <= -2.0:
            score += 4
            tech_score += 4
            reasons.append(f"纳斯达克隔夜下跌{nas:.2f}%")
        elif nas <= -1.0:
            score += 2
            tech_score += 2
            reasons.append(f"纳斯达克隔夜偏弱{nas:.2f}%")
    if spx is not None and spx <= -1.2:
        score += 1
        broad_score += 1
        reasons.append(f"标普500下跌{spx:.2f}%")
        if spx <= -2.0:
            broad_score += 2
    if hsi is not None and hsi <= -1.2:
        score += 1
        broad_score += 1
        reasons.append(f"恒生指数走弱{hsi:.2f}%")
        if hsi <= -2.0:
            broad_score += 1
    if kospi is not None:
        if kospi <= -4.0:
            score += 5
            tech_score += 5
            reasons.append(f"韩国科技链重挫{kospi:.2f}%")
        elif kospi <= -2.5:
            score += 3
            tech_score += 3
            reasons.append(f"韩国市场明显走弱{kospi:.2f}%")
        elif kospi <= -1.2:
            score += 1
            tech_score += 1
            reasons.append(f"韩国市场偏弱{kospi:.2f}%")
    if nikkei is not None and nikkei <= -1.5:
        score += 1
        tech_score += 1
        reasons.append(f"日经走弱{nikkei:.2f}%")
    # Nasdaq is a technology-sensitive index by itself, but a simultaneous
    # S&P selloff is evidence of a US broad-risk event rather than a sector
    # adjustment.  Combine it with Asia breadth below before closing all buys.
    if (
        isinstance(nas, (int, float)) and nas <= -1.8
        and isinstance(spx, (int, float)) and spx <= -1.2
    ):
        broad_score += 2
        reasons.append("纳指与标普同步大跌，确认美股广谱风险")
    tech_pattern = r"费半|半导体|芯片|AI|英伟达|科技股|三星|SK海力士|KOSPI|韩国|存储|DRAM|NAND|HBM|美光"
    negative_pattern = r"大跌|重挫|杀跌|熔断|跳水|负反馈|跌超|下跌|走弱|拖累|开盘大跌|暴跌|急跌|大幅下挫"
    text_tech_risk = bool(re.search(tech_pattern, text, re.I) and re.search(negative_pattern, text))
    # The THS discovery stream mixes current quotes with stale/repeated headlines.
    # When the key technology proxies are all green, a headline alone must not
    # close the buy gate or override the quote evidence.
    tech_quotes = [value for value in (nas, kospi) if value is not None]
    tech_quotes_strong = bool(tech_quotes) and all(value >= 0 for value in tech_quotes)
    if text_tech_risk and not tech_quotes_strong:
        score += 2 if any(value < 0 for value in tech_quotes) else 1
        tech_score += 2 if any(value < 0 for value in tech_quotes) else 1
        reasons.append("同花顺/资讯文本出现科技链负反馈关键词，且指数未同步确认修复")
    elif text_tech_risk and tech_quotes_strong:
        reasons.append("同花顺资讯含科技负面标题，但纳指/韩国指数同步走强，按滞后或混合信息处理")
    oil_geopolitical_pattern = r"美伊|伊朗|霍尔木兹|袭击|爆炸|制裁|布油|美油|原油|油价|黄金|避险"
    oil_risk_pattern = r"大涨|飙升|暴涨|冲击|突变|升级|发动|风险|撤销|重启|袭击|爆炸"
    if re.search(oil_geopolitical_pattern, text, re.I) and re.search(oil_risk_pattern, text):
        score += 1
        broad_score += 1
        reasons.append("中东/油价冲击抬升避险情绪")
    if "偏空科技" in macro_bias:
        score += 1
        tech_score += 1
        reasons.append("盘前宏观偏向科技成长承压")

    level = RED if score >= 5 else YELLOW if score >= 2 else NORMAL
    # Only a broad US/commodity shock earns the all-market scope.  A Korean or
    # semiconductor-led drawdown remains a technology channel even when its
    # aggregate score reaches red.
    if broad_score >= 3 or (broad_score >= 2 and isinstance(nas, (int, float)) and nas <= -1.5):
        risk_scope = "broad"
    elif tech_score > 0:
        risk_scope = "technology"
    else:
        risk_scope = "normal"
    if not reasons:
        reasons.append("未检测到显著隔夜全球科技负反馈")
    return external_entry_context({
        "version": "global_tech_risk_v2",
        "date": report_date,
        "updated_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "premarket",
        "risk_level": level,
        "risk_scope": risk_scope,
        "score": score,
        "risk_channels": {"technology_score": tech_score, "broad_score": broad_score},
        "reason": reasons[:8],
        "quotes": {"nasdaq": nas, "spx": spx, "kospi": kospi, "nikkei": nikkei, "hsi": hsi},
        "quote_meta": {
            "nasdaq": _quote_meta(_find_quote(global_quotes, "纳斯达克")),
            "spx": _quote_meta(_find_quote(global_quotes, "标普")),
            "kospi": _quote_meta(_find_quote(global_quotes, "韩国")),
            "nikkei": _quote_meta(_find_quote(global_quotes, "日经")),
            "hsi": _quote_meta(_find_quote(global_quotes, "恒生", "香港")),
        },
        "policy": policy_for_level(level, risk_scope=risk_scope),
    })


def _level_index(level: str | None) -> int:
    return {NORMAL: 0, YELLOW: 1, RED: 2}.get(str(level or NORMAL), 0)


def _level_from_index(index: int) -> str:
    if index >= 2:
        return RED
    if index == 1:
        return YELLOW
    return NORMAL


def infer_intraday_context(
    base_context: dict[str, Any] | None,
    report_date: str,
    global_quotes: list[dict[str, Any]],
    path_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh global risk during A-share trading hours.

    Overseas and Asia-Pacific markets can reverse sharply after the premarket
    report. This function only adjusts the risk gate; trading still needs A-share
    price, volume, VWAP, and timeframe confirmation.
    """
    fresh = infer_premarket_context(report_date, global_quotes, now=now)
    base = dict(base_context or fresh)
    base_level = str(base.get("risk_level") or NORMAL)
    base_scope = str(base.get("risk_scope") or "broad")
    fresh_level = str(fresh.get("risk_level") or NORMAL)
    fresh_scope = str(fresh.get("risk_scope") or "normal")
    quotes = fresh.get("quotes") or {}
    kospi = quotes.get("kospi")
    nikkei = quotes.get("nikkei")
    hsi = quotes.get("hsi")
    nas = quotes.get("nasdaq")
    path_state = path_state or {}
    path_recovery_score = int(path_state.get("recovery_score") or 0)
    path_event = str(path_state.get("event_state") or "normal")

    relief_reasons: list[str] = []
    relief_score = 0
    if isinstance(kospi, (int, float)):
        if kospi >= 5.0:
            relief_score += 3
            relief_reasons.append(f"韩国市场熔断级修复{kospi:.2f}%")
        elif kospi >= 3.0:
            relief_score += 2
            relief_reasons.append(f"韩国市场强修复{kospi:.2f}%")
    if isinstance(nikkei, (int, float)) and nikkei >= 1.0:
        relief_score += 1
        relief_reasons.append(f"日经转强{nikkei:.2f}%")
    if isinstance(hsi, (int, float)) and hsi >= 1.0:
        relief_score += 1
        relief_reasons.append(f"恒生转强{hsi:.2f}%")
    if path_recovery_score:
        relief_score += path_recovery_score
        relief_reasons.extend(path_state.get("path_reason") or [])
    elif path_event == "shock_unrepaired":
        relief_reasons.extend(path_state.get("path_reason") or [])

    fresh_idx = _level_index(fresh_level)
    base_idx = _level_index(base_level)
    current_time = (now or datetime.now()).time()
    opening_protection = current_time < datetime.strptime("09:45", "%H:%M").time()
    early_risk_window = current_time < datetime.strptime("10:00", "%H:%M").time()
    level_idx = fresh_idx
    if base_idx >= 2:
        level_idx = max(level_idx, 1)
    elif base_idx == 1 and opening_protection:
        level_idx = max(level_idx, 1)

    # Asia-Pacific intraday relief can remove part of the external-risk penalty,
    # but it must not override a severe Nasdaq shock on its own.
    severe_us_tech = isinstance(nas, (int, float)) and nas <= -2.0
    if relief_score >= 3 and not severe_us_tech:
        if base_idx >= 2:
            level_idx = min(level_idx, 1)
        else:
            level_idx = min(level_idx, max(0, base_idx - 1), fresh_idx)
    elif relief_score >= 5 and severe_us_tech:
        level_idx = min(level_idx, max(1, base_idx - 1))

    # A confirmed V-shaped repair can remove the panic premium, but it cannot
    # turn a still-negative Nasdaq session into a normal-risk day. The result
    # is capped at yellow so local A-share price/volume confirmation remains
    # mandatory before any simulated buy.
    if path_state.get("recovery_confirmed"):
        level_idx = min(level_idx, 1)
        if isinstance(nas, (int, float)) and nas <= -1.0:
            level_idx = max(level_idx, 1)
    elif path_event == "shock_unrepaired":
        level_idx = max(level_idx, 2)

    inherited_base_reason = list(base.get("base_reason") or base.get("reason") or [])
    base_reasons = " ".join(str(x) for x in inherited_base_reason)
    persistent_news_risk = any(
        key in base_reasons
        for key in (
            "科技链负反馈",
            "中东/油价冲击",
            "盘前宏观偏向科技成长承压",
        )
    )
    if base_idx >= 2 and early_risk_window and persistent_news_risk and relief_score < 3:
        level_idx = max(level_idx, 2)
        relief_reasons.append("盘前同花顺新闻确认的外部冲击仍处开盘前段，未满足强修复解除条件")

    level = _level_from_index(level_idx)
    risk_scope = "broad" if "broad" in {base_scope, fresh_scope} else "technology" if "technology" in {base_scope, fresh_scope} else "normal"
    # A confirmed external recovery may retire yesterday/preopen's wider scope.
    # An unrepaired shock or a still-broad live assessment can never use this.
    if path_state.get('recovery_confirmed') and fresh_scope != 'broad':
        risk_scope = fresh_scope
    reasons = list(fresh.get("reason") or [])
    if relief_reasons:
        reasons = relief_reasons + ["盘中外部风险复核：只解除压制，不单独触发买入"] + reasons
    if not reasons:
        reasons = ["盘中全球市场未检测到新增外部风险"]

    fresh.update({
        "source": "intraday_global_refresh",
        "base_risk_level": base_level,
        "base_risk_scope": base_scope,
        "base_reason": inherited_base_reason[:10],
        "risk_level": level,
        "risk_scope": risk_scope,
        "relief_score": relief_score,
        "intraday_path": path_state,
        "reason": reasons[:10],
        "policy": apply_intraday_recovery_policy(policy_for_level(level, risk_scope=risk_scope), path_state),
    })
    return external_entry_context(fresh)


def quote_gap(q: dict[str, Any]) -> float | None:
    try:
        prev = float(q.get("prev_close") or 0)
        open_price = float(q.get("open") or q.get("close") or 0)
        if prev <= 0:
            return None
        return (open_price - prev) / prev * 100
    except Exception:
        return None


def infer_auction_context(
    base_context: dict[str, Any] | None,
    report_date: str,
    watch_quotes: dict[str, dict[str, Any]],
    indexes: dict[str, Any] | list[dict[str, Any]] | None = None,
    opportunities: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ctx = dict(base_context or infer_premarket_context(report_date, [], now=now))
    gaps = [gap for q in (watch_quotes or {}).values() if (gap := quote_gap(q)) is not None]
    low_open = [gap for gap in gaps if gap <= -1.5]
    deep_low_open = [gap for gap in gaps if gap <= -3.0]
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    tech_codes = [
        code for code, q in (watch_quotes or {}).items()
        if str(code).startswith(("300", "301", "688")) or any(k in str(q.get("name") or "") for k in ("科技", "芯", "光", "微", "电"))
    ]
    tech_low = [
        quote_gap(watch_quotes[code]) for code in tech_codes
        if quote_gap(watch_quotes[code]) is not None and quote_gap(watch_quotes[code]) <= -1.5
    ]
    low_ratio = len(low_open) / len(gaps) if gaps else 0.0
    tech_low_ratio = len(tech_low) / len(tech_codes) if tech_codes else 0.0

    auction_reasons: list[str] = []
    auction_score = 0
    if gaps:
        if low_ratio >= 0.35:
            auction_score += 2
            auction_reasons.append(f"自选/持仓竞价低开比例{low_ratio:.0%}")
        if avg_gap is not None and avg_gap <= -1.2:
            auction_score += 1
            auction_reasons.append(f"自选/持仓平均竞价{avg_gap:.2f}%")
        if len(deep_low_open) >= max(2, len(gaps) * 0.15):
            auction_score += 2
            auction_reasons.append(f"深低开股票{len(deep_low_open)}只")
    if tech_codes:
        if tech_low_ratio >= 0.35:
            auction_score += 2
            auction_reasons.append(f"科技相关竞价低开比例{tech_low_ratio:.0%}")
    if opportunities is not None and len(opportunities) <= 2:
        auction_score += 1
        auction_reasons.append("集合竞价严格机会池较少，风险偏好偏弱")

    base_level = ctx.get("risk_level") or NORMAL
    level = base_level
    if auction_score >= 4 or (base_level == RED and auction_score >= 1):
        level = RED
    elif auction_score >= 2 and base_level != RED:
        level = YELLOW
    elif base_level == RED and auction_score == 0:
        level = YELLOW

    risk_scope = str(ctx.get("risk_scope") or "broad")
    if level == RED:
        # A tech-heavy weak auction is a technology shock unless broad watchlist
        # breadth or average opening weakness independently confirms systemic risk.
        broad_auction_risk = low_ratio >= 0.35 or (avg_gap is not None and avg_gap <= -1.2)
        if tech_low_ratio >= 0.35 and not broad_auction_risk:
            risk_scope = "technology"
        elif broad_auction_risk:
            risk_scope = "broad"

    reasons = list(ctx.get("reason") or [])
    if auction_reasons:
        reasons = reasons + ["竞价确认：" + "；".join(auction_reasons)]
    ctx.update({
        "date": report_date,
        "updated_at": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "auction",
        "risk_level": level,
        "risk_scope": risk_scope,
        "auction_score": auction_score,
        "auction": {
            "watch_count": len(gaps),
            "avg_gap_pct": avg_gap,
            "low_open_count": len(low_open),
            "deep_low_open_count": len(deep_low_open),
            "tech_count": len(tech_codes),
            "tech_low_open_count": len(tech_low),
            "opportunity_count": len(opportunities or []),
        },
        "reason": reasons[:10],
        "policy": policy_for_level(level, risk_scope=risk_scope),
    })
    return external_entry_context(ctx)


def write_context(base_dir: Path | str, context: dict[str, Any]) -> Path:
    path = runtime_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_context(base_dir: Path | str, report_date: str | None = None) -> dict[str, Any]:
    path = runtime_path(base_dir)
    try:
        ctx = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        ctx = infer_premarket_context(report_date or "", [])
    if report_date and str(ctx.get("date") or "") != str(report_date):
        ctx = infer_premarket_context(report_date, [])
    # Reports may be rendered after the realtime process has stopped. Rebuild
    # the embedded path from the audited file so a stale opening quote cannot
    # survive in post-close explanations.
    active_date = str(report_date or ctx.get("date") or "")
    if active_date:
        path = read_intraday_path(base_dir, active_date)
        if path.get("samples"):
            ctx["intraday_path"] = summarize_intraday_path(path)
    return ctx


def summary_line(context: dict[str, Any]) -> str:
    label = {"green": "🟢 正常", "yellow": "🟡 隔夜偏弱", "red": "🔴 全球科技负反馈"}.get(
        context.get("risk_level"), "🟢 正常"
    )
    reasons = "；".join((context.get("reason") or [])[:3])
    scope = str(context.get("risk_scope") or "broad")
    scope_text = "科技链专项" if scope == "technology" else "全市场系统性" if scope == "broad" else "常规"
    if context.get("entry_policy_version") == LOCAL_ENTRY_POLICY_VERSION:
        scope_text = "海外影响范围，非A股禁开依据"
    return f"全球科技风险：{label}（{scope_text}）。{reasons}。策略门控：{context.get('policy', {}).get('notes', '-')}"
