"""Read-only historical branch comparison and serialized-input round trip."""
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import intraday_timing_v2 as timing, local_market_data as market, method_entry
from core import emotion_leader_pool
import realtime_signal_engine as engine


def main():
    runtime = Path.home() / "Library/Application Support/a-share-trading-watch"
    previous = json.loads((ROOT / "output/review_20260909_10_155551/daily_method_recheck.json").read_text())
    con = sqlite3.connect(f"file:{runtime}/data/runtime/market_data.sqlite?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")
    config = timing.load_config()["daily_method_entry"]
    result = {"days": {}, "scope": "Historical daily-method branches only, not fills or complete leader replay"}
    for day in ("2026-09-09", "2026-09-10"):
        minutes = {}
        counts = Counter()
        for line in (runtime / f"data/runtime/signal_audit_{day.replace('-', '')}.jsonl").open():
            sample = json.loads(line)
            now = datetime.fromisoformat(sample["timestamp"])
            for row in sample["rows"]:
                if row.get("strategy_key") not in {"TREND_520", "TREND_MA5"} or not row.get("daily_qualified"):
                    continue
                code = row["symbol"]
                if code not in minutes:
                    minutes[code] = [dict(b) for b in con.execute(
                        "SELECT * FROM market_bars WHERE source='tencent' AND timeframe='1m' AND symbol=? AND bar_end>=? AND bar_end<=? ORDER BY bar_end",
                        (code, day + " 09:30:00", day + " 15:00:00"))]
                bars = timing.closed_bars(market._as_minute_rows([b for b in minutes[code] if b["bar_end"] <= str(now)]), 5, now)
                detail = method_entry.evaluate(row["strategy_contract"], bars, row["price"], now, config)
                frozen = json.loads(json.dumps(engine.json_safe({"contract": row["strategy_contract"], "bars": bars,
                                                               "price": row["price"], "now": now.isoformat(), "config": config})))
                repeated = method_entry.evaluate(frozen["contract"], frozen["bars"], frozen["price"],
                                                 datetime.fromisoformat(frozen["now"]), frozen["config"])
                counts["evaluations"] += 1
                counts["eligible"] += bool(detail.get("eligible"))
                counts["historical_disagreements"] += bool(row.get("candidate_pattern")) != bool(detail.get("eligible"))
                counts["serialized_input_disagreements"] += engine.json_safe(detail) != engine.json_safe(repeated)
        prior = previous["days"][day]
        assert counts["evaluations"] == prior["counts"]["evaluations"]
        assert counts["eligible"] == prior["counts"]["recomputed_candidates"]
        assert counts["historical_disagreements"] == len(prior["mismatches"])
        assert counts["serialized_input_disagreements"] == 0
        result["days"][day] = dict(counts)
    con.close()
    snapshot = json.loads((runtime / "data/runtime/emotion_leader_pool_20260910.json").read_text())
    selected = emotion_leader_pool.select_leader_candidates(snapshot)
    result["pool_recheck"] = {"source_date": snapshot["source_date"], "old_count": len(snapshot["candidates"]),
                              "new_count": len(selected), "removed": sorted({r["code"] for r in snapshot["candidates"]} - {r["code"] for r in selected})}
    assert "301689" not in {r["code"] for r in selected}
    path = ROOT / "output" / ("review_iteration_acceptance_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    with path.open("x") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(path)


if __name__ == "__main__":
    main()
