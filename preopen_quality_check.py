#!/usr/bin/env python3
"""Non-trading pre-open readiness checks for the A-share runtime."""

import argparse
import hashlib
import importlib
import json
import os
import py_compile
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import after_close_report as base
import render_report_dashboard as dashboard


BASE_DIR = Path(os.environ.get("A_SHARE_BASE_DIR", Path(__file__).resolve().parent))
LOG_DIR = Path(os.environ.get("A_SHARE_LOG_DIR", BASE_DIR / "logs"))
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
REPORTS_DIR = BASE_DIR / "web_dashboard" / "data" / "reports"
WEB_RUNTIME_DIR = BASE_DIR / "web_dashboard" / "data" / "runtime"
RESULT_DIR = RUNTIME_DIR / "preopen_quality"
QUALITY_LATEST_PATH = WEB_RUNTIME_DIR / "preopen_quality" / "latest.json"
QUALITY_PUSH_STATE_PATH = RESULT_DIR / "feishu_push_state.json"
HEALTH_PATH = BASE_DIR / "web_dashboard" / "data" / "runtime" / "signal_health.json"
REALTIME_LABEL = "com.tonyyu.a-share-realtime-signal-engine"
REQUIRED_FILES = (
    "intraday_report.py",
    "realtime_signal_engine.py",
    "paper_trading.py",
    "core/strategy_discipline.py",
    "core/intraday_timing_v2.py",
    "core/local_market_data.py",
)
ENTRY_BLOCKING_CHECKS = {
    "发布清单一致性",
    "计划源",
    "核心股票池同步",
    "全自选观察池计划同步",
    "观察池策略分类与盘中合同",
    "盘前仓位计划完整性",
    "订盘标的加载",
    "订盘覆盖",
    "实时引擎导入",
    "三策略择时配置",
    "模拟盘数据库",
    "实时守护进程",
    "实时健康状态",
    "实时健康时效",
}
# 688825 is a recently listed/short-history focus stock.  Its missing 120m
# structure is visible to V2 on the symbol itself, but must not degrade the
# whole pre-open system readiness card.
QUALITY_IGNORE_STRUCTURAL_PENDING_CODES = {"688825"}


def report_now(value):
    if not value:
        return datetime.now()
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def add_check(checks, name, passed, detail, severity="required"):
    checks.append({"name": name, "passed": bool(passed), "detail": str(detail), "severity": severity})


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_manifest_status(base_dir):
    manifest_path = Path(base_dir) / "release_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"发布清单缺失或不可读：{exc}"
    mismatches = []
    for rel, expected in (manifest.get("files") or {}).items():
        path = Path(base_dir) / rel
        if not path.is_file():
            mismatches.append(f"缺失:{rel}")
        elif file_sha256(path) != expected:
            mismatches.append(f"漂移:{rel}")
    if mismatches:
        return False, f"{manifest.get('release_id') or '-'}；" + "、".join(mismatches[:8])
    return True, f"{manifest.get('release_id') or '-'}；{len(manifest.get('files') or {})}个文件一致"


def latest_scheduler_mode_result(log_path, mode, trading_date):
    """Return the final scheduler outcome for one mode/date from JSONL logs."""
    path = Path(log_path)
    if not path.is_file():
        return None
    target_date = trading_date.strftime("%Y-%m-%d") if hasattr(trading_date, "strftime") else str(trading_date)
    reversed_lines = list(reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()))
    for index, raw in enumerate(reversed_lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("mode") != mode or event.get("event") not in {"success", "failure"}:
            continue
        event_date = str(event.get("now") or event.get("ts") or "")[:10]
        if event_date == target_date:
            if event.get("event") == "failure":
                for older_raw in reversed_lines[index + 1:]:
                    try:
                        older = json.loads(older_raw)
                    except json.JSONDecodeError:
                        continue
                    older_date = str(older.get("now") or older.get("ts") or "")[:10]
                    if older_date != target_date or older.get("mode") != mode:
                        continue
                    if older.get("event") == "child_finished" and older.get("returncode"):
                        stderr = str(older.get("stderr") or "").strip()
                        if stderr:
                            event = {**event, "error_detail": stderr.splitlines()[-1][-800:]}
                        break
            return event
    return None


def launchd_running():
    domain = f"gui/{os.getuid()}/{REALTIME_LABEL}"
    result = subprocess.run(["launchctl", "print", domain], text=True, capture_output=True)
    output = (result.stdout or "") + (result.stderr or "")
    # macOS versions report a live daemon as either `running` or `active`.
    # Both mean the job is loaded and has an active execution context.
    state_markers = ("state = running", "state = active")
    return result.returncode == 0 and any(marker in output for marker in state_markers), output[-500:]


def check(now):
    checks = []
    release_ok, release_detail = release_manifest_status(BASE_DIR)
    add_check(checks, "发布清单一致性", release_ok, release_detail, severity="blocking")
    for rel in REQUIRED_FILES:
        path = BASE_DIR / rel
        present = path.is_file()
        add_check(checks, f"运行文件：{rel}", present, path if present else f"缺失：{path}")
        if present:
            try:
                py_compile.compile(str(path), doraise=True)
                add_check(checks, f"语法：{rel}", True, "ok")
            except py_compile.PyCompileError as exc:
                add_check(checks, f"语法：{rel}", False, exc)

    premarket_path = REPORTS_DIR / f"premarket_{now:%Y%m%d}.json"
    afterclose_path = REPORTS_DIR / f"afterclose_{now:%Y%m%d}.json"
    plan_path = premarket_path if premarket_path.is_file() else afterclose_path
    plan_source = "premarket" if plan_path == premarket_path else "afterclose_next_day_plan"
    add_check(
        checks,
        "计划源",
        plan_path.is_file(),
        f"{plan_source}: {plan_path}" if plan_path.is_file() else f"缺少 {premarket_path.name} 与 {afterclose_path.name}",
    )
    premarket_job = latest_scheduler_mode_result(LOG_DIR / "trading_scheduler.log", "premarket", now.date())
    premarket_delivered = bool(
        premarket_job
        and premarket_job.get("event") == "success"
        and not premarket_job.get("skip_feishu")
    )
    add_check(
        checks,
        "盘前任务与飞书投递",
        premarket_delivered,
        (
            "08:30盘前任务完成且飞书投递成功"
            if premarket_delivered
            else (
                "盘前报告已本地重建，但本次明确跳过飞书，不能视为投递成功"
                if (premarket_job or {}).get("event") == "success" and (premarket_job or {}).get("skip_feishu")
                else str(
                    (premarket_job or {}).get("error_detail")
                    or (premarket_job or {}).get("error")
                    or "未找到当日盘前任务成功记录"
                )
            )
        ),
        severity="required",
    )

    levels = {}
    try:
        watchlist_sync = base.read_watchlist_details()
        universe_ready, universe_detail = base.watchlist_sync_entry_gate(watchlist_sync, now=now)
        add_check(checks, "核心股票池同步", universe_ready, universe_detail, severity="blocking")
    except Exception as exc:
        add_check(checks, "核心股票池同步", False, repr(exc), severity="blocking")
    try:
        observation_plan = base.premarket_plan_observation_details()
        actual_groups = {str(item.get("name") or "") for item in observation_plan.get("groups") or []}
        planned_rows = observation_plan.get("rows") or []
        all_groups = base.premarket_plan_all_groups_enabled()
        expected_groups = set(base.PREMARKET_PLAN_OBSERVATION_GROUPS)
        plan_ready = bool(planned_rows) and (bool(actual_groups) if all_groups else expected_groups.issubset(actual_groups))
        detail = (
            f"{'全量' if all_groups else ','.join(sorted(actual_groups)) or '无'}：{len(planned_rows)}只，分组 {','.join(sorted(actual_groups)) or '无'}"
            if plan_ready
            else f"期望 {'全部同花顺非核心分组' if all_groups else ','.join(sorted(expected_groups)) or '无'}；实际 {','.join(sorted(actual_groups)) or '无'}；"
            f"状态 {observation_plan.get('status') or 'unknown'}"
        )
        add_check(checks, "全自选观察池计划同步", plan_ready, detail, severity="blocking")
    except Exception as exc:
        add_check(checks, "全自选观察池计划同步", False, repr(exc), severity="blocking")
    try:
        intra = importlib.import_module("intraday_report")
        levels = intra.read_premarket_levels()
        watchlist = intra.current_watchlist_map()
        missing = sorted(set(watchlist) - set(levels))
        add_check(checks, "订盘标的加载", bool(levels), f"加载 {len(levels)} 只")
        add_check(checks, "订盘覆盖", not missing, "覆盖全部自选" if not missing else f"缺失：{','.join(missing[:8])}")
        def complete_entry_plan(level):
            try:
                values = (
                    float(level.get("planned_target_position_pct")),
                    float(level.get("planned_max_position_pct")),
                    float(level.get("planned_v2_probe_position_pct")),
                )
                return bool(level.get("premarket_plan_loaded")) and bool(level.get("premarket_plan_action")) and all(0 < value <= 1 for value in values)
            except (TypeError, ValueError):
                return False

        incomplete_plans = [
            code for code, level in levels.items()
            if level.get("premarket_plan_allows_entry") and not complete_entry_plan(level)
        ]
        add_check(
            checks,
            "盘前仓位计划完整性",
            not incomplete_plans,
            "所有允许新增的订盘标的均有目标/上限/V2单次仓位" if not incomplete_plans else f"缺少完整仓位计划：{','.join(incomplete_plans[:8])}",
        )
        observation_levels = {
            code: level for code, level in levels.items()
            if level.get("observation_plan_group")
        }
        strategy_counts = {}
        malformed_contracts = []
        for code, level in observation_levels.items():
            key = str(level.get("strategy_key") or "").strip()
            strategy_counts[key or "MISSING"] = strategy_counts.get(key or "MISSING", 0) + 1
            patterns = [str(item) for item in level.get("strategy_allowed_patterns") or [] if str(item).strip()]
            if not key or not level.get("strategy_name"):
                malformed_contracts.append(code)
            elif key != "OBSERVE_UNCLASSIFIED" and not patterns:
                malformed_contracts.append(code)
        authorized_count = sum(
            1 for level in observation_levels.values()
            if level.get("strategy_daily_qualified") and level.get("premarket_plan_allows_entry")
        )
        count_text = "、".join(f"{key}={value}" for key, value in sorted(strategy_counts.items())) or "无观察池"
        add_check(
            checks,
            "观察池策略分类与盘中合同",
            bool(observation_levels) and not malformed_contracts,
            (
                f"分类 {count_text}；当日可进入盘中触发链 {authorized_count}只"
                if not malformed_contracts
                else f"策略键/名称/允许形态缺失：{','.join(malformed_contracts[:8])}；分类 {count_text}"
            ),
            severity="blocking",
        )
        add_check(checks, "三策略动态位计算", True, "支撑、阻力、失效与执行区间由大周期及15m/5m已收盘K线实时计算，不使用盘前固定关键位")
        from core import sector_identity
        catalog = sector_identity.load_catalog(BASE_DIR, now=now) or intra.fetch_fast_boards()
        identity_issues = {}
        for code, level in levels.items():
            if not level.get('strategy_daily_qualified'):
                continue
            identity = sector_identity.resolve({'quote': level,
                'resonance_boards': (level.get('strategy_daily_metrics') or {}).get('resonance_boards')}, catalog)
            if identity['status'] != 'ready':
                identity_issues[code] = identity['reason']
        add_check(checks, '日线合格股主线可执行性', not identity_issues and bool(catalog),
                  '公司归属及唯一板块代码已验证' if not identity_issues and catalog else
                  '计划不可执行：' + ('；'.join(f'{code}:{why}' for code, why in identity_issues.items()) or '板块目录缺失'),
                  severity='blocking')
        json.dumps(levels, ensure_ascii=False)
        add_check(checks, "订盘标的序列化", True, "ok")
    except Exception as exc:
        add_check(checks, "订盘标的加载", False, repr(exc))

    try:
        importlib.import_module("realtime_signal_engine")
        add_check(checks, "实时引擎导入", True, "ok")
    except Exception as exc:
        add_check(checks, "实时引擎导入", False, repr(exc))

    try:
        timing = importlib.import_module("core.intraday_timing_v2")
        config = timing.load_config()
        required = {"strategy_version", "history", "location", "room_risk", "execution", "path", "time", "position"}
        add_check(checks, "三策略择时配置", required.issubset(config), f"{config.get('strategy_version')} #{timing.config_hash(config)}")
    except Exception as exc:
        add_check(checks, "三策略择时配置", False, repr(exc))

    try:
        market_data = importlib.import_module("core.local_market_data")
        readiness = market_data.preopen_readiness(BASE_DIR, sorted(levels), now=now)
        pending = readiness.get("structural_history_pending") or {}
        ignored_pending = sorted(set(pending) & QUALITY_IGNORE_STRUCTURAL_PENDING_CODES)
        active_pending = sorted(set(pending) - QUALITY_IGNORE_STRUCTURAL_PENDING_CODES)
        ignored_note = f"；例外忽略：{','.join(ignored_pending)}" if ignored_pending else ""
        add_check(
            checks,
            "本地K线数据源",
            readiness.get("status") == "ok",
            f"可用 {readiness.get('ready', 0)}/{readiness.get('checked', 0)}；"
            f"结构历史积累中 {len(active_pending)}；"
            + ("无数据阻断" if not readiness.get("blocked") else f"阻断：{','.join(sorted(readiness['blocked'])[:5])}")
            + ignored_note,
            # Quote/K-line disagreements are isolated by V2 to the affected
            # symbol.  They are visible pre-open warnings, not a broken
            # portfolio-level execution dependency.
            severity="warning",
        )
    except Exception as exc:
        add_check(checks, "本地K线数据源", False, repr(exc), severity="warning")

    try:
        paper = importlib.import_module("paper_trading")
        con = paper.init_db(BASE_DIR)
        con.execute("SELECT 1").fetchone()
        con.close()
        add_check(checks, "模拟盘数据库", True, "可读写")
    except Exception as exc:
        add_check(checks, "模拟盘数据库", False, repr(exc))

    running, launchd_detail = launchd_running()
    add_check(checks, "实时守护进程", running, "running" if running else launchd_detail)

    try:
        health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        updated_at = datetime.strptime(str(health.get("updated_at") or ""), "%Y-%m-%d %H:%M:%S")
        freshness_sec = (now - updated_at).total_seconds()
        healthy = health.get("engine_status") != "engine_error" and not health.get("last_error")
        add_check(checks, "实时健康状态", healthy, f"状态：{health.get('engine_status')}；错误：{health.get('last_error') or '无'}")
        add_check(checks, "实时健康时效", freshness_sec <= 300, f"健康记录滞后 {max(0, freshness_sec):.0f} 秒")
    except Exception as exc:
        add_check(checks, "实时健康状态", False, repr(exc))

    blockers = [item for item in checks if not item["passed"] and item["severity"] in ("required", "blocking")]
    warnings = [item for item in checks if not item["passed"] and item["severity"] == "warning"]
    entry_blockers = [
        item for item in checks
        if not item["passed"] and (
            item["name"] in ENTRY_BLOCKING_CHECKS
            or item["name"].startswith("运行文件：")
            or item["name"].startswith("语法：")
        )
    ]
    return {
        "kind": "preopen_quality_check",
        "trading_date": now.strftime("%Y-%m-%d"),
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok" if not blockers else "failed",
        "plan_source": plan_source,
        "levels": len(levels),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        # A quote/history issue is handled per symbol by V2.  Only a broken
        # plan, engine, or execution dependency closes new entries globally.
        "entry_blockers": entry_blockers,
        "entry_blocked": bool(entry_blockers),
    }


def lark_text(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def quality_markdown(result):
    status = "通过" if result.get("status") == "ok" else "失败"
    lines = [f"# A股盘前系统质检｜{result.get('trading_date')}", ""]
    lines.append(f"- 检查时间：{result.get('checked_at')}（Asia/Shanghai）")
    lines.append(f"- 结论：**{status}**；计划源：{result.get('plan_source') or '-'}；订盘标的：{result.get('levels', 0)}只。")
    lines.append("- 口径：本卡只验证盘前分类计划、三策略运行合同、本地行情与模拟盘可用性；不生成交易指令。")
    lines.append("")
    lines.append("## 检查结果")
    lines.append("| 项目 | 结果 | 严重度 | 说明 |")
    lines.append("|---|---|---|---|")
    for item in result.get("checks") or []:
        lines.append(f"| {item.get('name')} | {'通过' if item.get('passed') else '失败'} | {item.get('severity') or '-'} | {item.get('detail') or '-'} |")
    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    lines.append("")
    lines.append("## 开盘前处理")
    entry_blocked = bool(result.get("entry_blocked")) or any(
        item.get("name") in ENTRY_BLOCKING_CHECKS for item in blockers
    )
    if blockers:
        lines.extend(f"- **{item.get('name')}**：{item.get('detail')}" for item in blockers[:8])
        lines.append(
            "- " + (
                "关键执行依赖未恢复前，系统保留观察和风险退出，不允许新增模拟盘买入。"
                if entry_blocked
                else "行情数据问题按标的阻断；未受影响标的仍须通过所属策略的完整周期确认。"
            )
        )
    else:
        lines.append("- 盘前计划、数据与执行环境通过；09:25继续核对竞价，三策略仅在当日板块共振和盘中结构确认后执行。")
    if warnings:
        lines.append("- **单标的行情告警**：" + "；".join(f"{item.get('name')}（{item.get('detail')}）" for item in warnings[:5]))
        lines.append("- 告警标的逐一禁开仓，其他标的仍按所属策略的完整周期确认执行。")
    return "\n".join(lines)


def quality_push_key(result):
    blockers = result.get("blockers") or []
    return json.dumps({
        "date": result.get("trading_date"),
        "status": result.get("status"),
        "blockers": [(item.get("name"), item.get("detail")) for item in blockers],
    }, ensure_ascii=False, sort_keys=True)


def should_push_quality(result, now):
    try:
        state = json.loads(QUALITY_PUSH_STATE_PATH.read_text(encoding="utf-8"))
        previous_at = datetime.strptime(str(state.get("pushed_at") or ""), "%Y-%m-%d %H:%M:%S")
        same = state.get("key") == quality_push_key(result)
        if same and (now - previous_at).total_seconds() < 900:
            return False
    except Exception:
        pass
    return True


def mark_quality_pushed(result, now):
    QUALITY_PUSH_STATE_PATH.write_text(json.dumps({
        "pushed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "key": quality_push_key(result),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def send_feishu(result, now=None):
    if os.environ.get("A_SHARE_SKIP_FEISHU") == "1":
        return json.dumps({"code": 0, "msg": "skipped by A_SHARE_SKIP_FEISHU"}, ensure_ascii=False)
    now = now or datetime.now()
    if not should_push_quality(result, now):
        return json.dumps({"code": 0, "msg": "duplicate preopen quality card suppressed"}, ensure_ascii=False)

    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    entry_blocked = bool(result.get("entry_blocked")) or any(
        item.get("name") in ENTRY_BLOCKING_CHECKS for item in blockers
    )
    failed = [item for item in result.get("checks") or [] if not item.get("passed")]
    template = "red" if blockers else ("yellow" if warnings else "green")
    status = (
        f"失败，阻断 {len(blockers)} 项"
        if blockers else (f"通过，单标的行情告警 {len(warnings)} 项" if warnings else "通过，可进入竞价复核")
    )
    blocker_text = "\n".join(
        f"- **{item.get('name')}**：{item.get('detail')}" for item in blockers[:8]
    ) or "- 无阻断项。"
    detail_text = "\n".join(
        f"- {'通过' if item.get('passed') else '失败'}｜{item.get('name')}：{item.get('detail')}"
        for item in failed[:8]
    ) or "- 所有检查项通过。"
    warning_text = "\n".join(
        f"- **{item.get('name')}**：{item.get('detail')}" for item in warnings[:8]
    ) or "- 无单标的行情告警。"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": f"A股盘前系统质检｜{now:%H:%M}"}},
            "elements": [
                lark_text(f"**结论：{status}**\n计划源：{result.get('plan_source') or '-'}｜订盘标的：{result.get('levels', 0)}只"),
                {"tag": "hr"},
                lark_text("**阻断项**\n" + blocker_text),
                lark_text("**单标的行情告警**\n" + warning_text),
                lark_text("**检查明细**\n" + detail_text),
                {"tag": "hr"},
                lark_text(
                    "**执行边界**\n"
                    "盘前框架决定当日分类、策略与仓位；盘中先确认当日板块共振，再按三策略合同择时。"
                    + (
                        "关键执行依赖失败，新增模拟盘买入保持关闭，风险退出仍可执行。"
                        if entry_blocked
                        else "数据项异常按标的拦截，不进行全局一刀切关闭。"
                    )
                ),
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(base.FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", "ignore")
            payload = json.loads(text)
            code = payload.get("code", payload.get("StatusCode", 0))
            if code in (0, "0", None):
                mark_quality_pushed(result, now)
                return text
            raise RuntimeError(text)
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"飞书盘前质检推送失败：{last_error}")


def main():
    parser = argparse.ArgumentParser(description="A-share pre-open runtime quality check")
    parser.add_argument("--now", help="YYYY-MM-DD HH:MM:SS; used by scheduled dry-runs")
    args = parser.parse_args()
    now = report_now(args.now or os.environ.get("A_SHARE_RUN_NOW"))
    result = check(now)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    QUALITY_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    output = RESULT_DIR / f"preopen_quality_{now:%Y%m%d}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    QUALITY_LATEST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = BASE_DIR / f"A股盘前系统质检_{now:%Y-%m-%d}.md"
    markdown_path.write_text(quality_markdown(result), encoding="utf-8")
    try:
        dashboard_result = dashboard.publish_report(markdown_path)
    except Exception as exc:
        dashboard_result = {"error": str(exc)}
    feishu_result = send_feishu(result, now)
    print(json.dumps({
        "status": result["status"],
        "output": str(output),
        "dashboard": dashboard_result,
        "feishu_result": feishu_result,
        "blockers": len(result["blockers"]),
    }, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
