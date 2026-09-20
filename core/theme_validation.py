"""Evidence rules for mapping a stock to an intraday sector/theme.

An upstream quote's concept field is useful for research, but is not reliable
enough to prove that a candidate belongs to today's leading board.  Execution
and risk routing therefore use the industry/name evidence below.
"""

from __future__ import annotations

import re
from typing import Any


THEME_FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "technology": ("科技", (
        "半导体", "芯片", "存储", "DRAM", "NAND", "HBM", "集成电路", "电子", "元件",
        "PCB", "印制电路板", "通信", "光通信", "CPO", "算力", "服务器", "数据中心",
        "液冷", "热管理", "人工智能", "AI", "软件", "计算机", "消费电子", "光刻", "封装",
    )),
    "medical": ("医疗", (
        "医疗", "医药", "生物", "创新药", "CRO", "CXO", "医疗服务", "基因", "CAR-T", "疫苗",
    )),
    "energy": ("能源", (
        "电力", "储能", "新能源", "光伏", "风电", "煤炭", "石油", "油气", "油服", "航运",
    )),
    "materials": ("资源材料", (
        "有色", "黄金", "稀土", "铜", "铝", "锂", "化工", "新材料", "玻纤",
    )),
    "defense": ("军工", ("军工", "商业航天", "航空", "航天", "国防")),
    "consumer": ("消费", ("消费", "零售", "食品", "白酒", "旅游", "家电", "纺织")),
    "media": ("传媒文娱", (
        "传媒", "媒体", "影视", "院线", "出版", "短剧", "游戏", "广告", "营销", "文娱", "AIGC",
    )),
    "agriculture": ("农业", ("农业", "种植", "种业", "粮食", "农牧", "养殖", "饲料")),
    "finance": ("金融地产", ("券商", "银行", "保险", "地产", "房地产")),
    # “设备/工程”过于宽泛，不能仅凭行业名就否定其属于油服、医疗设备等
    # 具体产业链；这类情况应标为证据不足，而不是硬判跨主题冲突。
    "infrastructure": ("基建制造", ("基建", "建筑", "工程机械", "电网")),
}


def _text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts)


def families_for_text(*parts: Any) -> set[str]:
    text = _text(*parts)
    return {
        family
        for family, (_, tokens) in THEME_FAMILIES.items()
        if any(token.lower() in text.lower() for token in tokens)
    }


def labels_for_families(families: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    return [THEME_FAMILIES[family][0] for family in THEME_FAMILIES if family in families]


def board_theme_context(boards: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    context: dict[str, list[str]] = {}
    for board in boards or []:
        name = str(board.get("f14") or board.get("name") or "").strip()
        if not name:
            continue
        for family in families_for_text(name):
            context.setdefault(family, []).append(name)
    return context


def candidate_theme_evidence(
    quote: dict[str, Any] | None,
    boards: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return only reproducible sector evidence for a market-radar candidate."""
    quote = quote or {}
    industry = str(quote.get("industry") or "").strip()
    name = str(quote.get("name") or "").strip()
    concepts = quote.get("concepts") or quote.get("concept") or ""
    framework_labels = quote.get("framework_theme_labels") or []
    industry_families = families_for_text(industry, name)
    contextual_families = families_for_text(concepts, framework_labels)
    # Provider concept tags are supporting evidence.  They may fill an
    # otherwise unmapped industry (for example auto-parts companies doing
    # liquid cooling), but may not contradict a specific mapped industry.
    candidate_families = industry_families or contextual_families
    board_context = board_theme_context(boards)
    board_families = set(board_context)
    shared = candidate_families & board_families
    identity_text = _text(industry) if industry_families else _text(industry, concepts, framework_labels)
    exact_board_names = [
        board_name
        for names in board_context.values()
        for board_name in names
        if identity_text and any(
            token and (token == board_name or token in board_name or board_name in token)
            for token in re.split(r"[\s、,/，]+", identity_text)
        )
    ]
    valid = bool(shared or exact_board_names)
    labels = labels_for_families(shared)
    if exact_board_names:
        labels.extend(name for name in exact_board_names if name not in labels)
    reason = (
        f"行业/题材与当日板块一致：{'、'.join(labels)}"
        if valid else
        "行业/题材未与当日强势板块形成可验证交集"
    )
    return {
        "valid": valid,
        "candidate_families": sorted(candidate_families),
        "board_families": sorted(board_families),
        "shared_families": sorted(shared),
        "labels": labels,
        "reason": reason,
        "source": "industry_concepts_vs_board",
    }


def framework_evidence_from_text(focus: Any) -> dict[str, Any]:
    """Validate a persisted framework label before it can be replayed live."""
    text = str(focus or "")
    industry_match = re.search(r"全市场框架筛选[:：]([^｜\n]+)", text)
    theme_match = re.search(r"(?:题材匹配|主线匹配)[:：]([^｜\n]+)", text)
    industry_text = industry_match.group(1).strip() if industry_match else ""
    theme_text = theme_match.group(1).strip() if theme_match else ""
    industry_families = families_for_text(industry_text)
    theme_families = families_for_text(theme_text)
    conflict = bool(industry_families and theme_families and not (industry_families & theme_families))
    unverified = bool(theme_text and not industry_families)
    return {
        "valid": not conflict,
        "executable": not conflict and not unverified,
        "reason": (
            f"框架行业“{industry_text}”与主题“{theme_text}”归属冲突"
            if conflict else (
                f"框架行业“{industry_text}”无法从名称独立验证主题“{theme_text}”"
                if unverified else "框架主题证据可用"
            )
        ),
        "industry_families": sorted(industry_families),
        "theme_families": sorted(theme_families),
        "industry_text": industry_text,
        "theme_text": theme_text,
    }
