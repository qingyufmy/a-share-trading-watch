"""Resolve company evidence to a fixed execution board, never to today's winner."""
import re
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

POLICY = 'company_board_identity_v2'
BROAD = {'深圳特区', '华为概念', '网红经济', '并购重组概念', '融资融券', '沪股通', '深股通'}
EXECUTION_THEMES = {'液冷服务器', 'CPO概念', 'PCB', '数字货币', '存储芯片', '商业航天',
                    '培育钻石', '创新药', '光纤概念', '人形机器人', '农化制品', '农业种植'}
ALIASES = {
    '液冷': '液冷服务器', 'CPO': 'CPO概念', '光模块': 'CPO概念',
    '印制电路板': 'PCB', '印制电路板制造': 'PCB', '光纤': '光纤概念',
    '种子': '种植业', '种业': '种植业', '农业': '种植业',
    '复合肥': '农化制品', '化肥农药': '农化制品', '化肥': '农化制品',
    '船舶制造': '船舶制造', '船舶与海洋装备': '船舶制造',
    '零售': '一般零售', '商贸零售': '商贸零售', '航空装备': '航空装备Ⅱ',
    '飞机制造': '航天航空', '航空发动机': '航天航空',
    '铜': '有色金属', '铜冶炼': '有色金属', '工业金属': '有色金属',
    '影视院线': '影视院线', '出版': '出版',
    '造纸': '造纸印刷', '旅游服务': '旅游酒店', '酒店': '旅游酒店',
    '化学原料': '化学原料', '调味发酵品Ⅱ': '食品饮料',
    '调味发酵品Ⅲ': '食品饮料', '制糖': '食品饮料',
    '电子元件': '元件', '文化传媒': '传媒', '平面媒体': '出版',
    '百货': '百货', '超市': '超市', '一般零售': '一般零售',
    '影视': '影视院线',
}

RESEARCH_THEMES = EXECUTION_THEMES | {'MLCC', '复合集流体', '绿色电力', '风能', '新材料'}


def research_candidates(stock, boards=None):
    """Alternative company-evidenced identities; never execution authorization."""
    active = set(resolve(stock, boards).get('names') or [])
    quote = stock.get('quote') or {}
    names = {canonical(x) for x in tokens(quote.get('verified_concepts', quote.get('concepts', quote.get('concept'))))}
    catalog = {}
    for board in boards or []:
        if (board.get('_coverage') or {}).get('complete') is False:
            return []
        catalog.setdefault(board.get('f14'), set()).add(board.get('f12'))
    return [{'name': name, 'id': next(iter(catalog[name])), 'status': 'RESEARCH_ONLY',
             'execution_authorized': False, 'source': 'company_concepts_and_board_catalog'}
            for name in sorted(names & RESEARCH_THEMES - active)
            if len(catalog.get(name, set())) == 1
            and re.fullmatch(r'BK\d+', str(next(iter(catalog[name]))))]


def tokens(value):
    if isinstance(value, (tuple, list, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in re.split(r'[、,，;；|]', str(value or '')) if x.strip() and x.strip() != '-']


def canonical(value):
    value = str(value).strip()
    return ALIASES.get(value, value)


def resolve(stock, boards=None):
    quote = stock.get('quote') or {}
    # Only company metadata participates. News keyword matches belong to research.
    concepts = tokens(quote.get('verified_concepts', quote.get('concepts', quote.get('concept'))))
    industry = str(quote.get('industry') or '').strip()
    industry_parts = [x for x in reversed(industry.split('-')) if x and x != '-']
    catalog = {}
    coverage = next((b.get('_coverage') for b in boards or [] if b.get('_coverage')), {})
    if coverage and coverage.get('complete') is not True:
        return {'status': 'pending_catalog', 'names': [], 'ids': [], 'reason': '板块目录响应不完整'}
    for board in boards or []:
        name, code = str(board.get('f14') or ''), str(board.get('f12') or '')
        if name and code:
            catalog.setdefault(name, set()).add(code)
    def mapped(value):
        return value if value in catalog else canonical(value)
    def legal(value):
        return (value and value not in BROAD and not value.endswith('板块')
                and not value.startswith(('昨日', '最近', '连板', '高送转', '转债', '次新', '趋势股'))
                and (not catalog or value in catalog))
    concept_names = sorted({mapped(x) for x in concepts if legal(mapped(x))})
    theme_names = [x for x in concept_names if x in EXECUTION_THEMES]
    industry_names = [mapped(x) for x in industry_parts if legal(mapped(x))]
    evidence = set(concept_names + industry_names)
    explicit = tokens(stock.get('resonance_boards'))
    requested = [mapped(x) for x in explicit]
    if explicit:
        # An old hierarchical industry is a label, not a signed board identity.
        requested = [next((mapped(p) for p in reversed(x.split('-'))
                           if mapped(p) in evidence), mapped(x)) for x in explicit]
        selected = sorted(set(requested))
        reason = 'validated_plan_identity'
        if any(x not in evidence for x in selected):
            return {'status': 'invalid_identity', 'names': [], 'ids': [],
                    'reason': '盘前主线与公司行业/概念证据不一致', 'requested': explicit}
    elif industry_names:
        # Preserve the primary business even when one incidental theme exists.
        # The authorized set is selected without any price/return information.
        selected = list(dict.fromkeys([industry_names[0]] + (theme_names if len(theme_names) == 1 else [])))
        reason = 'primary_industry_with_verified_theme'
    elif len(theme_names) == 1:
        selected, reason = theme_names, 'single_company_execution_theme'
    elif len(concept_names) == 1:
        selected, reason = concept_names, 'company_concept'
    else:
        selected, reason = [], '主线歧义待确认' if concept_names else '缺少可映射公司行业/概念'
    if not selected:
        return {'status': 'pending_identity', 'names': [], 'ids': [], 'reason': reason}
    if not catalog or any(len(catalog.get(x, set())) != 1 for x in selected):
        return {'status': 'pending_catalog', 'names': selected, 'ids': [],
                'reason': '板块目录缺失或名称不能唯一解析', 'evidence': reason}
    return {'status': 'ready', 'names': selected, 'ids': [next(iter(catalog[x])) for x in selected],
            'reason': reason, 'evidence': {'industry': industry, 'concepts': concepts}}


def load_profiles(base_dir, now=None):
    now = now or datetime.now()
    try:
        value = json.loads((Path(base_dir) / 'data/runtime/market_profiles.json').read_text())
        if not timedelta(0) <= now - datetime.fromisoformat(value['updated_at']) <= timedelta(days=7):
            return {}
        profiles = {}
        for code, row in value['profiles'].items():
            if not re.fullmatch(r'\d{6}', code):
                continue
            fields = {key: row[key] for key in ('industry', 'concepts')
                      if row.get(key) and str(row[key]).strip() not in {'-', '--', '暂无', 'None'}}
            if fields:
                profiles[code] = fields
        return profiles
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def apply(metrics, identity):
    metrics.update(resonance_policy=POLICY, resonance_boards=identity['names'],
                   resonance_board_ids=identity['ids'], resonance_identity=identity,
                   resonance_source=identity['reason'])


def ready(metrics):
    names = metrics.get('resonance_boards') or []
    codes = metrics.get('resonance_board_ids') or []
    return ((metrics.get('resonance_identity') or {}).get('status') == 'ready'
            and isinstance(names, list) and isinstance(codes, list)
            and bool(names) and len(names) == len(codes)
            and all(isinstance(x, str) and x.strip() for x in names)
            and all(isinstance(x, str) and re.fullmatch(r'BK\d+', x) for x in codes)
            and len(set(names)) == len(names) and len(set(codes)) == len(codes))


def load_catalog(base_dir, now=None, max_age_days=7):
    """Identity cache only: never store or return historical price/strength fields."""
    now = now or datetime.now()
    try:
        value = json.loads((Path(base_dir) / 'data/reference/eastmoney_boards.json').read_text())
        verified = datetime.fromisoformat(value['verified_at'])
        rows = value['rows']
        if not timedelta(0) <= now - verified <= timedelta(days=max_age_days):
            return []
        if (value.get('source') != 'eastmoney_industry_concept'
                or not rows or len({r['f12'] for r in rows}) != len(rows)
                or any(not re.fullmatch(r'BK\d+', r['f12']) or not r['f14'] for r in rows)):
            return []
        canonical_rows = [{k: row[k] for k in ('f12', 'f14')} for row in rows]
        if catalog_hash(canonical_rows) != value.get('sha256'):
            return []
        return canonical_rows
    except (OSError, ValueError, KeyError, TypeError):
        return []


def catalog_hash(rows):
    return hashlib.sha256(json.dumps(sorted(rows, key=lambda r: r['f12']),
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def update_catalog(base_dir, boards, now=None):
    """Promote complete provider responses atomically, retaining prior identities."""
    now = now or datetime.now()
    coverage = next((r.get('_coverage') for r in boards if r.get('_coverage')), {})
    rows = [{'f12': str(r.get('f12') or ''), 'f14': str(r.get('f14') or '')} for r in boards]
    expected = coverage.get('expected')
    if (coverage.get('complete') is not True or not expected or len(rows) != expected
            or len({r['f12'] for r in rows}) != len(rows)
            or any(not re.fullmatch(r'BK\d+', r['f12']) or not r['f14'] for r in rows)):
        return {'status': 'rejected_incomplete', 'count': len(rows)}
    directory = Path(base_dir) / 'data/reference'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'eastmoney_boards.json'
    digest = catalog_hash(rows)
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except (ValueError, OSError):
            pass
        if old and len(rows) < 0.9 * len(old.get('rows') or []):
            return {'status': 'rejected_universe_shrink', 'count': len(rows)}
        if old and old.get('sha256') == digest and str(old.get('verified_at', '')).startswith(now.strftime('%Y-%m-%d')):
            return {'status': 'unchanged', 'count': len(rows)}
        backup = directory / ('eastmoney_boards_before_' + hashlib.sha256(path.read_bytes()).hexdigest() + '.json')
        if not backup.exists():
            with backup.open('xb') as stream:
                stream.write(path.read_bytes())
    payload = {'source': 'eastmoney_industry_concept', 'verified_at': now.isoformat(timespec='seconds'),
               'sha256': digest, 'count': len(rows), 'rows': sorted(rows, key=lambda r: r['f12'])}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, suffix='.json', delete=False) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        temporary = stream.name
    os.replace(temporary, path)
    return {'status': 'updated', 'count': len(rows), 'sha256': digest}
