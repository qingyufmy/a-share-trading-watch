from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


OUT = Path(__file__).with_name("2026-08-11_四只股票技术分析与操作建议报告.docx")

NAVY = "0B2545"
BLUE = "2E74B5"
DARK_BLUE = "1F4D78"
MUTED = "5B6573"
GRID = "C9D3E0"
LIGHT_BLUE = "E8EEF5"
LIGHT_GRAY = "F2F4F7"
PALE_RED = "FCE8E6"
PALE_GOLD = "FFF4D6"
PALE_GREEN = "EAF4EA"
RED = "9B1C1C"
GOLD = "7A5A00"
GREEN = "1F5F3A"


def set_font(run, size=None, bold=None, color=None, name="Hiragino Sans GB"):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.rFonts
    fonts.set(qn("w:ascii"), name)
    fonts.set(qn("w:hAnsi"), name)
    fonts.set(qn("w:eastAsia"), "Hiragino Sans GB")
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def shade(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=80, start=120, bottom=80, end=120):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_width(cell, width_dxa):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def set_table_geometry(table, widths, indent=120):
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.first_child_found_in("w:tblW")
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(sum(widths)))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.first_child_found_in("w:tblInd")
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), str(indent))
    tbl_ind.set(qn("w:type"), "dxa")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            set_cell_width(cell, width)
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    elem = OxmlElement("w:tblHeader")
    elem.set(qn("w:val"), "true")
    tr_pr.append(elem)


def add_cell_text(cell, text, *, size=9.2, bold=False, color=NAVY, align=WD_ALIGN_PARAGRAPH.LEFT):
    p = cell.paragraphs[0]
    p.alignment = align
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.08
    run = p.add_run(text)
    set_font(run, size=size, bold=bold, color=color)


def add_heading(doc, text, level=1):
    p = doc.add_paragraph(style=f"Heading {level}")
    p.paragraph_format.keep_with_next = True
    p.add_run(text)
    return p


def add_body(doc, text, *, after=6, color=NAVY, bold_prefix=None):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.line_spacing = 1.12
    if bold_prefix and text.startswith(bold_prefix):
        r = p.add_run(bold_prefix)
        set_font(r, 10.6, True, color)
        r = p.add_run(text[len(bold_prefix):])
        set_font(r, 10.6, False, color)
    else:
        r = p.add_run(text)
        set_font(r, 10.6, False, color)
    return p


def add_bullet(doc, text, *, color=NAVY):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.12
    r = p.add_run(text)
    set_font(r, 10.4, False, color)
    return p


def add_callout(doc, label, text, fill=LIGHT_BLUE, label_color=DARK_BLUE):
    table = doc.add_table(rows=1, cols=1)
    set_table_geometry(table, [9360])
    cell = table.cell(0, 0)
    shade(cell, fill)
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.08
    r = p.add_run(label + "  ")
    set_font(r, 10.5, True, label_color)
    r = p.add_run(text)
    set_font(r, 10.5, False, NAVY)
    doc.add_paragraph().paragraph_format.space_after = Pt(1)


def add_comparison_table(doc):
    headers = ["标的", "当前形态", "关键防守", "上方压力", "操作结论"]
    rows = [
        ["300693\n盛弘股份", "放量突破后偏离过大\n强修复，未完成MA60确认", "41.73\n39.84", "44.14\n45.70", "不追高；等回踩缩量确认"],
        ["300953\n震裕科技", "弱势大周期中的修复反弹\n观察优先", "102.17\n97.00", "107.36\n116.17", "四只中更适合等回踩"],
        ["301566\n达利凯普", "高换手强拉、短线过热\n筹码稳定性最弱", "26.46\n25.19", "29.88\n30.08", "高风险；不接加速段"],
        ["600353\n旭光电子", "弱化520金叉\n中期修复而非主升", "27.32\n26.88", "30.03\n31.68", "仅观察后续确认，不追涨"],
    ]
    table = doc.add_table(rows=1, cols=len(headers))
    set_table_geometry(table, [1350, 2600, 1350, 1350, 2710])
    for cell, text in zip(table.rows[0].cells, headers):
        shade(cell, LIGHT_BLUE)
        add_cell_text(cell, text, size=9.4, bold=True, color=DARK_BLUE, align=WD_ALIGN_PARAGRAPH.CENTER)
    set_repeat_table_header(table.rows[0])
    for row in rows:
        cells = table.add_row().cells
        for idx, (cell, text) in enumerate(zip(cells, row)):
            add_cell_text(cell, text, size=9.1, align=WD_ALIGN_PARAGRAPH.CENTER if idx in [0, 2, 3] else WD_ALIGN_PARAGRAPH.LEFT)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def add_price_table(doc, entries):
    table = doc.add_table(rows=1, cols=3)
    set_table_geometry(table, [1700, 3100, 4560])
    for cell, text in zip(table.rows[0].cells, ["情景", "触发条件", "策略动作"]):
        shade(cell, LIGHT_GRAY)
        add_cell_text(cell, text, size=9.2, bold=True, color=DARK_BLUE, align=WD_ALIGN_PARAGRAPH.CENTER)
    set_repeat_table_header(table.rows[0])
    for name, trigger, action, tone in entries:
        cells = table.add_row().cells
        if tone:
            shade(cells[0], tone)
        add_cell_text(cells[0], name, size=9.1, bold=True, color=NAVY, align=WD_ALIGN_PARAGRAPH.CENTER)
        add_cell_text(cells[1], trigger, size=9.0)
        add_cell_text(cells[2], action, size=9.0)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def build():
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.78)
    section.bottom_margin = Inches(0.72)
    section.left_margin = Inches(0.78)
    section.right_margin = Inches(0.78)
    section.header_distance = Inches(0.35)
    section.footer_distance = Inches(0.35)

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Hiragino Sans GB"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Hiragino Sans GB")
    normal.font.size = Pt(10.6)
    normal.font.color.rgb = RGBColor.from_string(NAVY)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.12
    for level, size, color, before, after in [(1, 16, BLUE, 14, 7), (2, 12.5, DARK_BLUE, 10, 5), (3, 11.2, DARK_BLUE, 7, 3)]:
        style = styles[f"Heading {level}"]
        style.font.name = "Hiragino Sans GB"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Hiragino Sans GB")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.font.bold = True
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    # Quiet running header/footer for a formal research brief.
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = header.add_run("策略研究｜技术形态与操作计划")
    set_font(r, 8.5, False, MUTED)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = footer.add_run("仅作策略研究，不构成投资建议")
    set_font(r, 8.3, False, MUTED)

    title = doc.add_paragraph()
    title.paragraph_format.space_before = Pt(6)
    title.paragraph_format.space_after = Pt(3)
    r = title.add_run("四只股票技术分析与操作建议报告")
    set_font(r, 22, True, NAVY)
    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(12)
    r = subtitle.add_run("盛弘股份（300693）｜震裕科技（300953）｜达利凯普（301566）｜旭光电子（600353）")
    set_font(r, 10.5, False, MUTED)

    meta = doc.add_table(rows=3, cols=2)
    set_table_geometry(meta, [1800, 7560])
    metadata = [
        ("数据口径", "以 2026-08-11 收盘后腾讯行情快照为准；指标、价位均按该口径整理。"),
        ("市场背景", "上证 -0.82%、深成指 -0.40%、创业板 +0.34%。属于局部强势与快速轮动，单日拉升不等同于大级别趋势反转。"),
        ("框架依据", "情绪、筹码、时间 + 月线/日线/60分钟三周期；顺大势、逆小势，不以题材叙事替代价格与量能确认。"),
    ]
    for row, (label, value) in zip(meta.rows, metadata):
        shade(row.cells[0], LIGHT_BLUE)
        add_cell_text(row.cells[0], label, size=9.2, bold=True, color=DARK_BLUE, align=WD_ALIGN_PARAGRAPH.CENTER)
        add_cell_text(row.cells[1], value, size=9.2)

    add_heading(doc, "一、执行摘要", 1)
    add_callout(doc, "总判断", "四只都出现强反弹，但没有一只完成“周线/MA60趋势确认 + 日线承接 + 60分钟买点”三项闭环。明日的优先事项是验证回踩承接，不是在强拉日后的高位追买。", PALE_GOLD, GOLD)
    add_comparison_table(doc)
    add_body(doc, "排序不是开仓指令：300953适合观察回踩修复；600353等待弱化520的后续确认；300693、301566只适合已有仓位做防守管理。任何新增必须同时满足板块持续性、量能不衰竭、60分钟不再破低和回踩后重新破前高。", after=8)

    add_heading(doc, "二、统一策略纪律", 1)
    add_bullet(doc, "情绪：当指数弱、热点快速轮动时，题材强度只能决定观察优先级，不能直接生成买点。")
    add_bullet(doc, "筹码：放量阳线后的高换手，先看作筹码交换；次日若放量跌破关键价位且不能修复，不用“洗盘”解释。")
    add_bullet(doc, "时间：慢J高位只提高承接门槛；不因高J机械卖出，也不因MACD金叉在零轴下就追涨。")
    add_bullet(doc, "三周期：月线决定是否具备大趋势，日线决定结构是否修复，60分钟负责真正的执行点。任一环未过，只能观察或轻仓试错。")
    add_bullet(doc, "执行：新增仓位必须等“回踩承接后再突破”，而不是开盘冲高、偏离VWAP或首次触及压力位时追入。")

    stocks = [
        {
            "title": "三、300693 盛弘股份｜强修复，爆发后先看承接",
            "state": "收盘43.87，上涨12.92%，换手15.44%，成交量约为近20日3.47倍。MA5/MA20转强，但仍略低于MA60 43.92；MACD金叉位于零轴下，60日收益仍为负。策略上定义为“520后第4日确认”的强修复，而不是标准趋势主升。",
            "emotion": "超充、储能出海和算力电源给了情绪催化，但市场当日属于局部强势。公司已有减持计划带来的潜在供给扰动，必须由价格和量能决定是否持续。",
            "cycles": "月线/中周期尚未得到趋势反转确认；日线突破强，但收盘接近MA60与44.14高点压力；60分钟只用于明日确认回踩后是否重获主动，不能据当天大阳直接追。",
            "levels": [
                ("空仓等待", "不在44附近追；回踩41.73-39.84区间时，量能应明显回落并出现止跌。", "仅在回踩承接后重新接近或突破日内高点时评估；否则继续等。", PALE_GOLD),
                ("已有持仓", "盘中失守41.73且反抽不能收回。", "降低风险；日线跌破39.84，视为本轮突破结构基本失效。", PALE_RED),
                ("转强确认", "回踩不破关键带，随后重新放量站上44.14。", "转强后仍以分段跟随为主，不用单笔追满。", PALE_GREEN),
            ],
            "source": "参考：盛弘股份年报与业务、减持计划报道。https://stock.10jqka.com.cn/20260410/c675888814.shtml  https://stock.10jqka.com.cn/20260511/c676582526.shtml",
        },
        {
            "title": "四、300953 震裕科技｜弱势大周期中的修复反弹",
            "state": "收盘105.68，上涨5.00%；MA5 102.17、MA20 97.00，MACD修复较强，但仍显著低于MA60 128.73。上方先后面临107.36和116.17两层压力，因此不是已经反转。",
            "emotion": "机器人产业链预期可维持关注度，但主题预期不能代替确认。相对另外三只，它更适合用“回踩确认”方式观察，而不是抢首日突破。",
            "cycles": "中大周期仍弱，日线为反弹修复，60分钟的任务是验证101-102附近能否缩量企稳并重新站回105.68。若没有这个过程，107.36附近容易变成短线压力。",
            "levels": [
                ("空仓等待", "等待101-102附近缩量企稳，后续重新站回105.68。", "比直接追107.36更符合逆小势交易；未确认前只观察。", PALE_GOLD),
                ("已有持仓", "放量跌破MA5 102.17。", "先收缩仓位；日线失守97.00，520修复逻辑失效。", PALE_RED),
                ("转强确认", "站稳107.36，并且回踩不破、板块仍有承接。", "再看116.17的套牢盘压力；未消化前不定义为趋势主升。", PALE_GREEN),
            ],
            "source": "参考：公司主营信息。https://stock.10jqka.com.cn/newstock/300953/",
        },
        {
            "title": "五、301566 达利凯普｜高换手强拉，风险优先于空间预测",
            "state": "收盘28.19，上涨13.21%，换手31.54%，成交量约为近20日2.79倍。MA5/MA20转多、MACD金叉，但收盘仍低于MA60 30.08；单日振幅21.20%，筹码稳定性四只中最弱。",
            "emotion": "MLCC景气与AI服务器需求形成题材助推，但高弹性涨幅与高换手同时出现时，重点不是预判还能走多远，而是防止强分歧回落。",
            "cycles": "中周期未修复，日线属于强势爆发，60分钟需要先消化29.88前高的获利与解套压力。未经过缩量整理的再次上冲，均不属于策略性低风险买点。",
            "levels": [
                ("空仓等待", "不接29.88附近加速段；优先看回踩26.46附近能否不破、量能明显缩下去。", "没有缩量承接，不做追高试错。", PALE_GOLD),
                ("已有持仓", "29.88不能有效站稳，且日线跌回26.46下方。", "明显降低仓位；24.60为本轮反弹的硬失效参考。", PALE_RED),
                ("转强确认", "经过整理后再次站上29.88，且换手下降、板块不掉队。", "只允许小步跟随；不以强题材替代筹码稳定验证。", PALE_GREEN),
            ],
            "source": "观察重点：高换手与压力位变化，不以题材叙事替代价量确认。",
        },
        {
            "title": "六、600353 旭光电子｜弱化520金叉，先完成修复验证",
            "state": "收盘29.40，上涨7.69%，当日出现MA5上穿MA20的520信号，量比1.42。但DIF/DEA仍在零轴下，股价低于MA60 31.68，因此系统结论应为“弱化520、轻仓观察”，而不是核心入场。",
            "emotion": "公司具备多题材催化，但催化较多反而要警惕资金快速轮动；只有题材持续、价格站稳和量能健康三者同时成立，才具备跟随条件。",
            "cycles": "月线/中周期是回落后的修复，日线520刚出现，60分钟需验证27.32-28.03回踩是否有承接。若先冲30.03但不能站稳，依然是压力测试而非突破确认。",
            "levels": [
                ("空仓等待", "不追29.40附近；观察27.32-28.03回踩承接，或有效站上30.03后再验证。", "先完成520后续确认，再观察能否挑战31.68。", PALE_GOLD),
                ("已有持仓", "放量跌破27.32。", "先减仓；日线跌破26.88，视为本次金叉失败。", PALE_RED),
                ("转强确认", "突破30.03且回踩不破，随后挑战并站稳31.68。", "只有完成MA60确认，才可由修复观察升级为趋势跟随。", PALE_GREEN),
            ],
            "source": "参考：旭光电子年报解读。https://stock.10jqka.com.cn/20260320/c675430211.shtml",
        },
    ]

    for stock in stocks:
        add_heading(doc, stock["title"], 1)
        add_body(doc, "形态判断：" + stock["state"], bold_prefix="形态判断：")
        add_body(doc, "情绪与筹码：" + stock["emotion"], bold_prefix="情绪与筹码：")
        add_body(doc, "三周期：" + stock["cycles"], bold_prefix="三周期：")
        add_heading(doc, "情景化执行", 2)
        add_price_table(doc, stock["levels"])
        p = add_body(doc, stock["source"], after=7, color=MUTED)
        for run in p.runs:
            run.font.size = Pt(8.5)

    add_heading(doc, "七、下一交易日执行清单", 1)
    add_callout(doc, "新增仓位门槛", "必须同时满足：板块仍有持续性；价格回踩关键带后不再破低；重新站回VWAP或日内关键位；突破前高时有成交确认。少任一项，均不新增。", LIGHT_BLUE, DARK_BLUE)
    add_bullet(doc, "300953：优先看101-102附近缩量承接，随后是否重新站回105.68。")
    add_bullet(doc, "600353：优先看27.32-28.03是否守住，30.03突破后是否能有效承接。")
    add_bullet(doc, "300693：已有仓位防守41.73；空仓不追44附近，等回踩。")
    add_bullet(doc, "301566：不接29.88加速，只有26.46附近缩量承接才重新纳入观察。")
    add_body(doc, "风险声明：本报告基于指定时点行情快照与技术分析框架，不构成任何证券投资建议或收益承诺。市场跳空、消息、流动性和板块轮动均可能使既定价位失效，执行时应以实时行情和个人风险承受能力为准。", after=0, color=RED, bold_prefix="风险声明：")

    doc.core_properties.title = "2026-08-11 四只股票技术分析与操作建议报告"
    doc.core_properties.subject = "技术形态、关键价位与情景化操作计划"
    doc.core_properties.author = "Codex"
    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    build()
