"""Read-only, close-ranked observation-pool audit; never invokes trading or delivery."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def stock_code(code):
    from core.tracking_registry import instrument_kind
    try:
        return instrument_kind(code, '') == 'equity'
    except ValueError:
        return False


def universe(snapshots, day):
    codes, excluded = set(), set()
    for snapshot in snapshots:
        if snapshot.get("report_date") != day:
            raise ValueError("观察池快照非目标交易日，禁止用最新名单替代历史池")
        refs = {r['code'] for r in snapshot.get('reference_instruments', [])}
        for row in snapshot['rows']:
            code = str(row['code'])
            if code in refs or not stock_code(code) or 'ETF' in str(row.get('name', '')).upper():
                excluded.add(code)
            else:
                codes.add(code)
    return sorted(codes - excluded), sorted(excluded)


def rank_closing_quotes(codes, quotes, day, limit=10):
    ranked, missing = [], []
    for code in codes:
        q = quotes.get(code) or {}
        stamp = re.sub(r"\D", "", str(q.get('datetime', '')))
        price, prev, pct = number(q.get('close')), number(q.get('prev_close')), number(q.get('pct'))
        valid = (len(stamp) >= 12 and stamp[:8] == day.replace('-', '') and
                 '1500' <= stamp[8:12] <= '1530' and not q.get('_stale') and
                 price is not None and price > 0 and prev is not None and prev > 0 and
                 pct is not None and abs((price / prev - 1) * 100 - pct) <= .06 and
                 q.get('code') == code and 'ETF' not in str(q.get('name', '')).upper())
        if not valid:
            missing.append({'symbol': code, 'quote_time': q.get('datetime'),
                            'reason': '缺少同日收盘报价或价格/涨幅/证券身份不一致'})
            continue
        ranked.append({'symbol': code, 'name': q.get('name', code), 'pct': pct,
                       'close': price, 'prev_close': prev, 'quote_time': q['datetime']})
    # Use the provider's displayed percentage; ties have a stable code order.
    ranked.sort(key=lambda r: (-r['pct'], r['symbol']))
    return ranked[:limit], missing, len(ranked)


def read_audit(path, day, selected, study_universe=None):
    from research.gap_validation import inspect_row
    study_universe = set(study_universe if study_universe is not None else selected)
    replay_counts, replay_symbols = Counter(), set()
    rows = {code: [] for code in selected}
    sha, issues, first_symbols = hashlib.sha256(), [], None
    contracts, count = {}, 0
    with path.open('rb') as stream:
        for line_no, line in enumerate(stream, 1):
            sha.update(line)
            try:
                sample = json.loads(line)
                if not str(sample['timestamp']).startswith(day):
                    raise ValueError('wrong_date')
                data = sample['rows']
                if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
                    raise ValueError('invalid_rows')
                if sample.get('input_sha256') != digest(data):
                    raise ValueError('audit_hash_missing_or_mismatch')
                symbols = [r['symbol'] for r in data]
                if len(set(symbols)) != len(symbols):
                    raise ValueError('duplicate_symbol')
                if first_symbols is None:
                    first_symbols = set(symbols)
                count += 1
                for r in data:
                    code = r['symbol']
                    replay = None
                    if code in study_universe:
                        replay = inspect_row(r)
                        replay_symbols.add(code)
                        for branch in ('method', 'shadow'):
                            replay_counts[branch + ':' + replay[branch]['status']] += 1
                        replay_counts['method:current_eligible'] += bool(replay['method'].get('current_eligible'))
                        replay_counts['method:continuation_shape'] += bool(replay['method'].get('continuation_shape'))
                    if code not in rows:
                        continue
                    contract = r.get('strategy_contract') or {}
                    contract_id = digest(contract)
                    contracts[contract_id] = contract
                    keys = ('price', 'pct', 'scenario', 'strategy_key', 'daily_qualified',
                            'plan_allowed', 'plan_complete', 'sector_ok', 'sector_reason',
                            'candidate_pattern', 'regime', 'path', 'setup', 'execution_5m',
                            'blockers', 'all_blockers', 'leader_routes', 'room_risk',
                            'source_bar_close', 'timing_config_hash', 'execution_gates',
                            'gap_research', 'contract_readiness')
                    node = {k: r.get(k) for k in keys}
                    node['replay_validation'] = replay
                    if isinstance(node.get('gap_research'), dict):
                        node['gap_research'] = {k: v for k, v in node['gap_research'].items() if k != 'replay'}
                    sector = r.get('sector_momentum') or {}
                    freshness = r.get('structure_freshness') or {}
                    node.update(at=sample['timestamp'], recorded_at=sample.get('decision_recorded_at'),
                                audit_line=line_no, contract_id=contract_id,
                                board_name=sector.get('board_name'), board_pct=sector.get('board_pct'),
                                board_emotion_ok=sector.get('emotion_ok'),
                                structure_status=freshness.get('status'),
                                structure_entry_ready=freshness.get('entry_ready'),
                                market_gate=freshness.get('market_gate'),
                                method_input_present=bool(r.get('method_replay_input')),
                                timing_input_present=bool(r.get('timing_replay_input')))
                    rows[code].append(node)
            except (ValueError, KeyError, TypeError) as exc:
                issues.append({'line': line_no, 'error': str(exc)})
    for values in rows.values():
        values.sort(key=lambda n: (n['at'], n.get('recorded_at') or ''))
    return rows, contracts, {'path': str(path), 'sha256': sha.hexdigest(), 'valid_snapshots': count,
                             'issues': issues, 'first_symbols': sorted(first_symbols or []),
                             'replay_counts': dict(replay_counts), 'replay_symbols': sorted(replay_symbols),
                             'replay_scope': '固定观察池全部样本含下跌票；方法分支与研究分支，不是完整交易回测'}


def query_readonly(path, sql, day):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as con:
        con.execute('PRAGMA query_only=ON')
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, (day,))]


def read_opening_research(path, day, codes):
    from core import strategy_gap_research as gaps
    if not path.exists():
        return {'status': 'missing_input', 'scope': '缺少早盘逐分钟研究记录，不能推断09:35前信号', 'rows': []}
    rows, counts, issues = [], Counter(), []
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for i, raw in enumerate(stream, 1):
            sha.update(raw)
            try:
                sample = json.loads(raw)
                if not sample['timestamp'].startswith(day) or sample['input_sha256'] != digest(sample['rows']):
                    raise ValueError('date_or_integrity_failed')
                for row in sample['rows']:
                    if row['symbol'] not in codes:
                        continue
                    frozen = row['gap_research']['replay']
                    if frozen['input']['now'] != sample['timestamp']:
                        raise ValueError('cycle_mismatch')
                    actual = gaps.replay(frozen)
                    counts['verified'] += 1
                    counts['exact_match'] += actual == frozen['expected']
                    rows.append({'at': sample['timestamp'], 'symbol': row['symbol'], 'result': actual})
            except (KeyError, ValueError, TypeError) as exc:
                issues.append({'line': i, 'error': str(exc)})
    return {'status': 'incomplete' if issues else 'complete', 'sha256': sha.hexdigest(),
            'counts': dict(counts), 'rows': rows, 'issues': issues}


def summarize(nodes, orders, events):
    flags = {
        'daily_qualified': lambda n: n.get('daily_qualified') is True,
        'plan_allowed': lambda n: n.get('plan_allowed') is True,
        'sector_ok': lambda n: n.get('sector_ok') is True,
        'board_emotion_ok': lambda n: n.get('board_emotion_ok') is True,
        'candidate': lambda n: bool(n.get('candidate_pattern')),
        'daily_plan_sector_candidate': lambda n: n.get('daily_qualified') is True and
            n.get('plan_allowed') is True and n.get('sector_ok') is True and bool(n.get('candidate_pattern')),
    }
    counts = {key: sum(test(n) for n in nodes) for key, test in flags.items()}
    counts['observed'] = len(nodes)
    buys = [o for o in orders if o['side'] == 'BUY']
    fills = [o for o in buys if o['status'] == 'FILLED']
    from core.strategy_discipline import BUY_SCENARIOS
    buy_events = [e for e in events if e.get('scenario') in BUY_SCENARIOS]
    if fills:
        stage = '已有模拟买入成交，不属于未识别'
    elif buys or buy_events:
        stage = '已有买入订单或信号事件，需核对成交/投递链路'
    elif not nodes:
        stage = '缺少盘中审计覆盖，不能判定策略是否执行'
    elif not counts['daily_qualified']:
        stage = '日线资格未通过，禁止把待分类/修复观察当成买入资格'
    elif not counts['plan_allowed']:
        stage = '日线有资格，但盘前开仓授权未通过'
    elif not counts['sector_ok']:
        stage = '已识别候选，但当日板块/路由检查未通过'
    elif not counts['candidate']:
        stage = '已识别且曾通过板块检查，但未形成策略买点'
    else:
        stage = '已有时机候选，需核对剩余执行门控，不能直接判为漏单'
    blocks = Counter(b for n in nodes for b in set(n.get('all_blockers') or []))
    first_pass = {key: next((n['at'] for n in nodes if test(n)), None) for key, test in flags.items()}
    return {'stage': stage, 'counts': counts, 'first_pass': first_pass,
            'top_blockers': blocks.most_common(10), 'buy_order_count': len(buys),
            'filled_buy_count': len(fills), 'buy_signal_event_count': len(buy_events),
            'orders': orders, 'events': events}


def review(base, day, top_n=10):
    if not isinstance(top_n, int) or not 1 <= top_n <= 100:
        raise ValueError('top_n must be between 1 and 100')
    runtime = base / 'data/runtime'
    compact = day.replace('-', '')
    snapshots, sources = [], []
    for name in ('watchlist_snapshot_', 'watchlist_observation_snapshot_'):
        data, source = read_json(runtime / f'{name}{compact}.json')
        snapshots.append(data)
        sources.append({**source, 'updated_at': data.get('updated_at')})
    codes, excluded = universe(snapshots, day)
    quotes, quote_source = read_json(runtime / 'quote_cache.json')
    ranking, missing, valid_count = rank_closing_quotes(codes, quotes, day, limit=len(codes))
    top = [dict(r) for r in ranking[:top_n]]
    nodes, contracts, audit = read_audit(runtime / f'signal_audit_{compact}.jsonl', day,
                                       [r['symbol'] for r in top], codes)
    opening = read_opening_research(runtime / f'gap_opening_{compact}.jsonl', day, set(codes))
    orders = query_readonly(runtime / 'paper_trading.sqlite',
                           'SELECT symbol,created_at,side,status,fill_price,reason FROM paper_orders WHERE trading_date=?', day)
    events = query_readonly(runtime / 'signal_state.sqlite',
                           'SELECT symbol,created_at,scenario,event_type,price FROM signal_events WHERE trading_date=?', day)
    for item in top:
        code = item['symbol']
        item['timeline'] = nodes[code]
        item.update(summarize(nodes[code], [r for r in orders if r['symbol'] == code],
                              [r for r in events if r['symbol'] == code]))
        item['present_in_first_audit'] = code in audit['first_symbols']
    return {'date': day, 'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'scope': '核心池与扩展观察池并集，排除ETF/基金/指数；不并入临时全市场候选',
            'universe_count': len(codes), 'universe_symbols': codes, 'excluded': excluded,
            'quotes_valid': valid_count, 'missing_quotes': missing,
            'ranking_complete': bool(codes) and not missing,
            'status': 'complete' if codes and not missing and not audit['issues'] and
                all(r['timeline'] for r in top) else 'incomplete',
            'sources': sources + [quote_source], 'ranking': ranking,
            'audit': audit, 'contracts': contracts, 'top_n': top_n, 'selected': top,
            'opening_research': opening,
            'limits': ['收盘排名仅用于选取复盘样本，绝不写回盘前计划或盘中指标。',
                       '审计为留存时点，不等于每个引擎轮次；事件/订单只读交叉核对，不代表已验证消息投递。',
                       '完整当时输入缺失时，只能做决策证据核对，不能宣称完成全策略无前视回测。',
                       '观察池快照可能盘后更新；另列首个盘中记录是否已包含该票，不把盘后新增当成早盘漏选。',
                       '仅选涨幅靠前股票有事后选择偏差，不可据此计算胜率或自动放宽阈值。']}


def render(report):
    selected = report.get('selected', report.get('top5', []))
    top_n = report.get('top_n', 5)
    lines = [f"# {report['date']} 观察池涨幅前{top_n}反向核查", '',
             report['scope'], '',
             f"股票 {report['universe_count']} 只；有效同日收盘报价 {report['quotes_valid']} 只；"
             f"排名完整：{report['ranking_complete']}；审计状态：{report['status']}。",
             f'按行情显示涨幅降序，同涨幅按代码排序；不足全池覆盖时只称已覆盖样本前{top_n}。', '',
             '|股票|涨幅|收盘|日线/板块/时机节点|模拟买入成交|定位|',
             '|---|---:|---:|---|---:|---|']
    for r in selected:
        c = r['counts']
        lines.append(f"|{r['name']} {r['symbol']}|{r['pct']:+.2f}%|{r['close']:.2f}|"
                     f"{c['daily_qualified']}/{c['sector_ok']}/{c['candidate']}，共{c['observed']}|"
                     f"{r['filled_buy_count']}|{r['stage']}|")
    for r in selected:
        lines += ['', f"## {r['name']} {r['symbol']}", '',
                  f"首个盘中快照已覆盖：{r['present_in_first_audit']}；买入信号事件 {r['buy_signal_event_count']}；"
                  f"买入订单 {r['buy_order_count']}；成交 {r['filled_buy_count']}。"]
        timeline = r['timeline']
        if timeline:
            contract = report['contracts'][timeline[0]['contract_id']]
            lines += [f"初始分类：{contract.get('name')}；{contract.get('daily_gate_reason')}",
                      f"首次板块检查通过：{r['first_pass']['sector_ok'] or '未出现'}。", '', '主要阻断（计数为审计节点，非独立机会数）：']
            lines += [f"- {reason}：{count}次" for reason, count in r['top_blockers'][:5]]
            lines += ['', '|时点|价格/涨幅|板块/涨幅|结构|首项时机阻断|', '|---|---|---|---|---|']
            for n in timeline:
                clock = n['at'][11:16]
                if clock <= '10:15' or clock in ('11:00', '13:05', '14:00', '15:00') or n['candidate_pattern']:
                    reason = (n['blockers'] or ['未记录'])[0].replace('|', '/')
                    lines.append(f"|{n['at'][11:]}|{n['price']} / {n['pct']}%|{n['board_name']} / "
                                 f"{n['board_pct']}%|{n['regime']}|{reason}|")
    lines += ['', '## 证据与边界', ''] + ['- ' + s for s in report['limits']]
    if 'replay_counts' in report['audit']:
        lines += ['- ' + report['audit']['replay_scope'],
                  '- 分支回放统计：' + json.dumps(report['audit']['replay_counts'], ensure_ascii=False),
                  '- 旧时点没有新研究输入时明确计为 missing_input，不补造历史买点。']
    if 'opening_research' in report:
        opening = report['opening_research']
        lines += ['- 早盘逐分钟研究记录：' + opening['status'] + '；' + str(opening.get('counts') or opening.get('scope') or '')]
    lines += [f"- 审计文件SHA256：{report['audit']['sha256']}；有效快照 {report['audit']['valid_snapshots']}；"
              f"校验问题 {len(report['audit']['issues'])}。", '- 完整时点、原合同、来源哈希见同目录 evidence.json。']
    return '\n'.join(lines) + '\n'


def write_report(report, parent):
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / (report['date'].replace('-', '') + '_' + datetime.now().strftime('%H%M%S_%f'))
    dest.mkdir(exist_ok=False)
    with (dest / 'evidence.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    with (dest / 'report.md').open('x', encoding='utf-8') as stream:
        stream.write(render(report))
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='auto')
    parser.add_argument('--top-n', type=int, default=10)
    parser.add_argument('--base-dir', type=Path, default=Path.home() / 'Library/Application Support/a-share-trading-watch')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output/top_gainers_review')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    day = now.date() if args.date == 'auto' else datetime.strptime(args.date, '%Y-%m-%d').date()
    from after_close_report import is_a_share_trading_day, A_SHARE_HOLIDAY_RANGES
    if day.year not in A_SHARE_HOLIDAY_RANGES:
        raise ValueError('项目交易日历未覆盖目标年份，需先更新日历')
    if not is_a_share_trading_day(day):
        print(json.dumps({'status': 'skipped_non_trading_day', 'date': str(day)}))
        return 0
    if day > now.date() or (day == now.date() and now.hour < 15):
        raise ValueError('只能在目标交易日收盘后做收盘排名')
    report = review(args.base_dir, str(day), args.top_n)
    dest = write_report(report, args.output_dir)
    print(json.dumps({'status': report['status'], 'output': str(dest),
                      'universe_count': report['universe_count'], 'quotes_valid': report['quotes_valid'],
                      'top_n': args.top_n,
                      'selected': [{k: r[k] for k in ('symbol', 'name', 'pct', 'stage')} for r in report['selected']]},
                     ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'complete' else 2


if __name__ == '__main__':
    sys.exit(main())
