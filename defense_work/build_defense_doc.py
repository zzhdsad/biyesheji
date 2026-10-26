# -*- coding: utf-8 -*-
from pathlib import Path
from docx import Document
from docx.shared import Pt, Cm, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT = Path(r"C:\biyesheji-3.0.0")
WORK = ROOT / "defense_work"
OUT = ROOT / "答辩准备手册_中医药知识资源管理与智能问答系统.docx"

NAVY = "0B3A53"
TEAL = "1F8FB8"
LIGHT = "EAF7FF"
PALE = "F6FBFE"
GOLD = "D98E04"
GRAY = "667085"
WHITE = "FFFFFF"

doc = Document()

# ---------- page / base styles ----------
sec = doc.sections[0]
sec.page_width = Cm(21.0)
sec.page_height = Cm(29.7)
sec.top_margin = Cm(2.1)
sec.bottom_margin = Cm(2.0)
sec.left_margin = Cm(2.8)
sec.right_margin = Cm(2.8)

def set_font(run, name="宋体", size=10.5, bold=None, color=None):
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)

normal = doc.styles["Normal"]
normal.font.name = "宋体"
normal._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
normal.font.size = Pt(10.5)
normal.paragraph_format.line_spacing = 1.38
normal.paragraph_format.space_after = Pt(4)

for name, size, color, before, after in [
    ("Heading 1", 17, NAVY, 8, 8),
    ("Heading 2", 13.5, TEAL, 10, 5),
    ("Heading 3", 11.5, NAVY, 7, 3),
]:
    st = doc.styles[name]
    st.font.name = "微软雅黑"
    st._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "微软雅黑")
    st.font.size = Pt(size)
    st.font.bold = True
    st.font.color.rgb = RGBColor.from_string(color)
    st.paragraph_format.space_before = Pt(before)
    st.paragraph_format.space_after = Pt(after)
    if name == "Heading 1":
        # 用标题自带的分页，避免“内容刚好填满上一页 + 手动分页”产生空白页
        st.paragraph_format.page_break_before = True

# ---------- helpers ----------
def p(text="", style=None, size=10.5, bold=False, color=None, align=None, space_after=4):
    para = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    if align == "center":
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_after = Pt(space_after)
    if text:
        r = para.add_run(text)
        set_font(r, size=size, bold=bold, color=color)
    return para

def rich(text, style=None, size=10.5, color=None, space_after=4):
    """**bold** parsing."""
    para = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    para.paragraph_format.space_after = Pt(space_after)
    parts = text.split("**")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = para.add_run(part)
        set_font(r, size=size, bold=(i % 2 == 1), color=color)
    return para

def bullet(text, level=0, size=10.5):
    style = "List Bullet" if level == 0 else "List Bullet 2"
    para = doc.add_paragraph(style=style)
    para.paragraph_format.space_after = Pt(2)
    parts = text.split("**")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = para.add_run(part)
        set_font(r, size=size, bold=(i % 2 == 1))
    return para

def shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)

def cell_text(cell, text, size=9.2, bold=False, color=None, align=None):
    cell.text = ""
    para = cell.paragraphs[0]
    if align == "center":
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_after = Pt(2)
    parts = str(text).split("**")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = para.add_run(part)
        set_font(r, size=size, bold=(bold or i % 2 == 1), color=color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

def table(headers, rows, widths=None, font_size=9.2, header_fill=NAVY):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    # 固定版心宽度，避免表格/长英文列把页面撑出右边界
    max_width = 15.2
    if widths:
        total = sum(widths)
        if total > max_width:
            widths = [w * max_width / total for w in widths]
        else:
            max_width = total
    for i, h in enumerate(headers):
        c = t.rows[0].cells[i]
        shade(c, header_fill)
        cell_text(c, h, size=font_size, bold=True, color=WHITE, align="center")
        if widths:
            c.width = Cm(widths[i])
    # repeat header
    trPr = t.rows[0]._tr.get_or_add_trPr()
    tblHeader = OxmlElement("w:tblHeader")
    tblHeader.set(qn("w:val"), "true")
    trPr.append(tblHeader)
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cell_text(cells[i], val, size=font_size)
            if widths:
                cells[i].width = Cm(widths[i])
    # python-docx 只改 tcW 不会改 tblGrid；必须同时改列宽，否则 LibreOffice/Word
    # 会按默认等宽渲染，导致中文表格右侧被裁切。
    if widths:
        for i, w in enumerate(widths):
            t.columns[i].width = Cm(w)
        for old in t._tbl.tblPr.findall(qn("w:tblW")):
            t._tbl.tblPr.remove(old)
        tblW = OxmlElement("w:tblW")
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(int(sum(widths) * 567)))
        t._tbl.tblPr.append(tblW)
    # 行不跨页，避免表头在下一页孤立出现
    for row in t.rows:
        trPr = row._tr.get_or_add_trPr()
        trPr.append(OxmlElement("w:cantSplit"))
    return t

def callout(title, body, fill=LIGHT, border=TEAL):
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    c = t.rows[0].cells[0]
    c.width = Cm(15.2)
    t.columns[0].width = Cm(15.2)
    shade(c, fill)
    c.text = ""
    para = c.paragraphs[0]
    para.paragraph_format.space_after = Pt(3)
    r = para.add_run(title)
    set_font(r, "微软雅黑", 10.5, True, border)
    para2 = c.add_paragraph()
    para2.paragraph_format.space_after = Pt(2)
    parts = body.split("**")
    for i, part in enumerate(parts):
        if not part:
            continue
        rr = para2.add_run(part)
        set_font(rr, size=9.8, bold=(i % 2 == 1))

def caption(text):
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_after = Pt(8)
    r = para.add_run(text)
    set_font(r, size=8.5, color=GRAY)

def picture(path, width=6.1):
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_after = Pt(2)
    run = para.add_run()
    run.add_picture(str(path), width=Inches(width))

def page_break():
    doc.add_page_break()

def chapter(title, subtitle=None):
    doc.add_heading(title, level=1)
    if subtitle:
        rich(subtitle, size=10, color=GRAY, space_after=8)

def add_page_field(paragraph):
    run = paragraph.add_run()
    fldChar = OxmlElement("w:fldChar")
    fldChar.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = " PAGE "
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar)
    run._r.append(instrText)
    run._r.append(fldChar2)
    set_font(run, size=9, color=GRAY)

# header/footer
header = sec.header
hp = header.paragraphs[0]
hp.text = ""
r = hp.add_run("答辩准备手册 ｜ 中医药知识问答系统")
set_font(r, size=8, color=GRAY)
hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT

footer = sec.footer
fp = footer.paragraphs[0]
fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = fp.add_run("第 ")
set_font(r, size=9, color=GRAY)
add_page_field(fp)
r = fp.add_run(" 页")
set_font(r, size=9, color=GRAY)

# ---------- cover ----------
cover = doc.add_table(rows=1, cols=1)
cover.autofit = False
cover.alignment = WD_TABLE_ALIGNMENT.CENTER
cc = cover.rows[0].cells[0]
cc.width = Cm(15.2)
cover.columns[0].width = Cm(15.2)
shade(cc, NAVY)
cc.text = ""
cp = cc.paragraphs[0]
cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
cp.paragraph_format.space_before = Pt(22)
cp.paragraph_format.space_after = Pt(6)
r = cp.add_run("毕业设计答辩准备手册")
set_font(r, "微软雅黑", 15, True, WHITE)
cp2 = cc.add_paragraph()
cp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
cp2.paragraph_format.space_after = Pt(22)
r = cp2.add_run("应用型系统 · RAG · 可溯源问答")
set_font(r, "微软雅黑", 9, False, "B9DDEE")

p("", space_after=26)
tp = doc.add_paragraph()
tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
tp.paragraph_format.space_after = Pt(10)
r = tp.add_run("中医药知识资源管理\n与智能问答系统")
set_font(r, "微软雅黑", 22, True, NAVY)

sp = doc.add_paragraph()
sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
sp.paragraph_format.space_after = Pt(4)
r = sp.add_run("答辩人：张灏天")
set_font(r, "宋体", 12, False, NAVY)
sp2 = doc.add_paragraph()
sp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
sp2.paragraph_format.space_after = Pt(4)
r = sp2.add_run("学号：202512224B414 ｜ 专业：计算机科学与技术 ｜ 25级计科专升本1班")
set_font(r, "宋体", 9.5, False, GRAY)
sp3 = doc.add_paragraph()
sp3.alignment = WD_ALIGN_PARAGRAPH.CENTER
sp3.paragraph_format.space_after = Pt(4)
r = sp3.add_run("指导教师：胡皎月")
set_font(r, "宋体", 10, False, GRAY)

p("", space_after=18)
callout(
    "这份文档怎么用",
    "这不是论文，也不是让你背代码。它只解决三件事：**知道系统是什么**、**能讲清关键链路**、**被追问时有诚实且不塌的回答**。"
    "如果时间很少，先读第 1、2、3、5、9 章和最后的“一页纸速记”。",
    fill=PALE,
    border=TEAL,
)
p("", space_after=8)
rich("说明：本手册依据开题报告、当前代码、运行数据与测试结果整理；涉及“当前实现”的地方以代码为准。", size=9, color=GRAY)

# ---------- reading route ----------
doc.add_heading("阅读路线：先看什么，后看什么", level=1)
rich("你的目标不是变成全栈工程师，而是做到：**系统定位不跑偏、核心链路讲得顺、被追问时不编、知道自己边界**。", size=10.5)
table(
    ["顺序", "章节", "你要得到什么"],
    [
        ["1", "第 1 章 开题报告 vs 当前实现", "知道哪些话能讲、哪些话不能讲"],
        ["2", "第 2 章 5 分钟/10 分钟答辩稿", "有一份能直接练的讲稿"],
        ["3", "第 3—5 章 架构、数据、RAG", "能画图讲清系统怎么跑"],
        ["4", "第 6—7 章 数据库、权限、工程", "能回答“为什么这样设计”"],
        ["5", "第 8—9 章 亮点、不足、AI 写代码", "知道怎么把弱点讲成边界"],
        ["6", "第 10 章 高频问答 43 题", "覆盖大多数追问"],
        ["7", "第 11—12 章 演示、三天计划、速记", "答辩前最后一遍"],
    ],
    widths=[1.2, 6.5, 8.7],
    font_size=9.5,
)

callout(
    "先记住一句话",
    "这是一个**以中医药知识资源管理为主体、用 RAG 增强问答**的应用系统；我重点解决知识怎么组织、怎么检索、怎么让回答有依据，而不是训练大模型。",
    fill=LIGHT,
)
p("", space_after=4)
rich("如果老师问“你的创新点是什么”，先不要背“首创/领先”。更稳的说法是：**在应用层把多来源证据、动态检索路由、证据门控、引用溯源和评测串成了一条可运行的闭环**。", size=10)

# ---------- chapter 1 ----------
chapter("第 1 章 开题报告 vs 当前实现", "这一章最重要。答辩时最容易翻车的不是不会，而是把开题报告里的“计划”说成“已完成”，或者把工程实现说成算法创新。")

doc.add_heading("1.1 一句话对齐", level=2)
rich("开题报告写的是“中医药知识资源管理 + 知识检索 + 智能问答 + 权限管理”的综合系统；当前代码的主线也是这个，只是实现时把 RAG 做得比开题更细，同时收缩了部分文档格式。")

doc.add_heading("1.2 对照表", level=2)
table(
    ["开题报告里的要求", "当前实现", "答辩时怎么说"],
    [
        ["中医药知识资源分类管理：中药、方剂、理论、文献", "四类资源 CRUD、分类、标签、关键词检索、知识库挂载", "按四类资源组织，前端有独立管理页；服务端做权限校验"],
        ["个人文档管理：PDF、Word、TXT、CSV 等上传、解析、切片", "文档上传支持 PDF/DOCX/TXT/MD；CSV/JSON/Parquet 在“数据导入中心”处理", "我把“个人文档”和“结构化数据导入”拆成两个入口，避免把 CSV 当文档硬解析"],
        ["知识库管理与批量导入", "知识库、成员、资源挂载、导入批次、测试数据清理", "知识库是权限和检索边界；导入中心负责结构化数据"],
        ["智能问答：问题分析、关键词 + 向量、融合、重排、生成、引用、多轮、流式", "Query Analyzer、Entity Resolver、Dynamic Router、HyDE、Dense+Sparse、RRF、Reranker、Gate、LLM、Citation、Reflection、SSE", "完整 RAG 链路已落地；其中 Gate/Reflection/路由是开题之后的增强"],
        ["知识图谱/知识关联", "从四类资源构建轻量 KG（节点/边），KG 作为补充证据参与检索", "不是通用知识图谱，是从已有资源关系构建的领域子图，用来补充证据"],
        ["权限与系统管理", "系统角色 admin/member/viewer；知识库角色 owner/admin/editor/viewer；审计日志", "服务端鉴权，前端隐藏按钮只是体验，不是安全边界"],
        ["系统测试与评估", "pytest/前端测试；评测模块、40 条种子用例、RAGAS 风格指标", "评测流程已搭好，但标准答案只确认了 5 条，不能声称已经充分验证"],
    ],
    widths=[4.0, 7.0, 5.4],
    font_size=8.6,
)

doc.add_heading("1.3 三个必须主动说明的差异", level=2)
rich("**差异一：文档格式收缩了。** 开题写 CSV，但当前“文档上传”只支持 PDF/DOCX/TXT/MD；CSV/JSON/Parquet 放到“数据导入中心”。这不是漏做，而是把“文档解析”和“结构化数据导入”分开，避免 CSV 被当成长文本切片。")
rich("**差异二：评测还没有到“可以下结论”的程度。** 种子评测集 40 条、6 种题型，但只有 5 条有已确认标准答案，35 条仍是待审核。答辩时可以说“搭建了评测闭环、做了初步验证”，不能说“准确率 90%/显著优于基线”。")
rich("**差异三：研究增强是工程实现，不是算法创新。** Query Analyzer、Dynamic Router、KG、Evidence Gate、Self Reflection 都是可运行模块，但没有做大规模消融实验，不能声称“某策略显著提升”。")

callout(
    "答辩时的安全句式",
    "“开题时我计划做 X；实现时我发现 Y 更符合实际，所以调整为 Z。这个调整的代价是……，目前还没完全解决的是……。”"
    "这比硬说“都完成了”更可信。",
    fill="FFF7E6",
    border=GOLD,
)

doc.add_heading("1.4 不要说的六句话", level=2)
table(
    ["不要这样说", "为什么", "改成这样说"],
    [
        ["“我训练了 BGE-M3 / 大模型”", "你使用的是预训练模型和外部 LLM 接口", "“我使用 BGE-M3 做向量化、使用外部 LLM 生成，没有训练模型”"],
        ["“系统准确率 95%”", "没有足够的人工确认评测集", "“评测流程已搭好，目前只有 5 条确认答案，结论是初步的”"],
        ["“所有代码都是我手写的”", "AI 辅助痕迹可追溯，容易翻车", "“AI 辅助生成，我负责需求、数据、验收和迭代，核心链路我能解释”"],
        ["“RAG 不会幻觉”", "RAG 只能降低，不能消除", "“RAG 用证据约束生成，但仍可能出现引用错配/概括过度，所以有 Gate 和 Reflection”"],
        ["“知识图谱是通用图谱”", "实际是从资源关系构建的轻量图", "“是从中药/方剂/理论/文献关系构建的领域子图”"],
        ["“支持所有格式”", "当前文档上传只有 4 种", "“文档上传 4 种；结构化数据导入支持 CSV/JSON/Parquet 等”"],
    ],
    widths=[5.0, 5.6, 5.8],
    font_size=8.7,
)

# ---------- chapter 2 ----------
chapter("第 2 章 5 分钟 / 10 分钟答辩稿", "不要逐字背。先背“结构”和每段的第一个句子，现场可以自然展开。")

doc.add_heading("2.1 5 分钟版（推荐）", level=2)
table(
    ["时间", "内容", "你要说什么"],
    [
        ["0:00—0:30", "定位", "各位老师好，我是张灏天，题目是中医药知识资源管理与智能问答系统。它不是纯聊天机器人，而是以知识资源管理为主体、用 RAG 增强问答的应用系统。"],
        ["0:30—1:20", "问题", "中医药资料分散在教材、古籍、方剂和文献里，关键词检索难以覆盖语义，直接问大模型又缺少依据。我做的事情是把资料组织起来，检索出证据，再让模型基于证据回答。"],
        ["1:20—2:20", "架构", "系统是前后端分离：Next.js 前端、FastAPI 后端、PostgreSQL 存业务数据、Milvus 存向量、Redis 存会话，外部 LLM 只负责生成。"],
        ["2:20—3:40", "RAG 链路", "提问后先做问题分析/实体识别/路由，再 HyDE 改写，BGE-M3 双路召回，RRF 融合，Reranker 精排，Evidence Gate 判断证据是否足够，最后生成并做引用和复核。"],
        ["3:40—4:30", "演示/结果", "我现场演示一个有资料的问题、一个无资料拒答，以及引用定位。系统现在能跑通知识管理、检索、问答、引用、权限和评测。"],
        ["4:30—5:00", "边界", "我知道它还不是研究级系统：评测集标准答案不足、依赖外部模型、复杂问题的多子问题检索还不够。我的重点是把应用闭环做完整，后续再补实验。"],
    ],
    widths=[2.0, 3.0, 11.4],
    font_size=8.8,
)

doc.add_heading("2.2 10 分钟版的结构", level=2)
bullet("0—1 分钟：问题与定位（不要先讲技术栈）。")
bullet("1—3 分钟：系统演示或架构图（先让老师看到东西）。")
bullet("3—6 分钟：RAG 主链路（重点讲“为什么不是直接问模型”）。")
bullet("6—8 分钟：知识管理、权限、数据持久化（证明这是系统，不是 demo）。")
bullet("8—9 分钟：测试与评估（讲边界，不吹指标）。")
bullet("9—10 分钟：不足与下一步（主动交底，减少追问）。")

doc.add_heading("2.3 开场 30 秒逐字稿", level=2)
callout(
    "可以直接练",
    "各位老师好，我是张灏天，我的题目是《中医药知识资源管理与智能问答系统的设计与实现》。"
    "这个系统主要解决两个问题：一是中医药资料类型多、来源分散，需要统一管理；二是直接问大模型缺少依据，需要先检索知识、再生成回答。"
    "所以它首先是一个知识资源管理系统，RAG 是增强模块。下面我从系统定位、RAG 链路和实际效果三个方面汇报。",
    fill=PALE,
)

doc.add_heading("2.4 如果老师打断你", level=2)
table(
    ["老师说", "你不要", "你可以接"],
    [
        ["“直接讲 RAG”", "不要从头讲登录和页面", "“好，我直接讲从问题到答案的链路：分析 → 检索 → 融合 → 重排 → 门控 → 生成 → 引用。”"],
        ["“这有什么用”", "不要讲技术细节", "“它把分散资料变成可检索、可引用、可维护的知识库，适合学习和资料核对，不做诊断。”"],
        ["“创新点呢”", "不要硬说首创", "“应用层闭环：多来源证据、动态路由、门控/复核、引用溯源和评测串起来。”"],
        ["“你做的还是 AI 做的”", "不要回避", "“AI 辅助生成代码，我负责需求、数据、验收和迭代；核心链路的输入输出和失败模式我能解释。”"],
    ],
    widths=[4.0, 4.2, 8.2],
    font_size=8.8,
)

# ---------- chapter 3 ----------
chapter("第 3 章 系统是什么：一张图讲清", "老师最怕听到“用了很多技术”，最喜欢听到“每一层负责什么”。")
picture(WORK / "arch.png", 6.55)
caption("图 1  系统总体架构（管理是主体，RAG 是增强）")

doc.add_heading("3.1 三层就够", level=2)
table(
    ["层", "负责什么", "一句话"],
    [
        ["前端", "页面、表单、聊天、证据面板、权限按钮", "把用户操作变成 API 请求，把结果展示出来"],
        ["后端", "鉴权、业务规则、文档/资源、RAG 编排、SSE", "系统的“大脑和门禁”，所有权限最终在这里校验"],
        ["数据层", "PostgreSQL、Redis、Milvus、上传文件", "PG 存事实，Redis 存会话，Milvus 存向量索引"],
    ],
    widths=[2.4, 7.0, 7.0],
    font_size=9.2,
)

doc.add_heading("3.2 每个组件一句话", level=2)
bullet("**Next.js**：前端框架，负责页面路由和交互；不是“模型”。")
bullet("**FastAPI**：后端 Web 框架，接收请求、校验参数、调用业务服务。")
bullet("**PostgreSQL**：存用户、知识库、文档、切片、资源、会话、评测等结构化数据，是事实源。")
bullet("**Milvus**：存向量，负责相似度检索；它不存业务真相。")
bullet("**Redis**：存会话历史缓存，减少每次问答都查数据库。")
bullet("**BGE-M3**：把文本变成向量，同时给稠密和稀疏表示。")
bullet("**BGE-Reranker**：对候选片段做更精细的相关性排序。")
bullet("**外部 LLM**：只负责“根据给定证据组织语言”，不负责提供事实来源。")

doc.add_heading("3.3 为什么选这套技术栈", level=2)
rich("不要答“因为流行”。按这个逻辑答：**前端要快速做管理页面和聊天界面 → Next.js + Ant Design；后端要异步、接口清晰 → FastAPI + Pydantic；业务数据要事务和关联 → PostgreSQL；向量检索要专门引擎 → Milvus；会话要低延迟 → Redis；部署要可复现 → Docker Compose。**")
rich("如果老师问“为什么不用 Spring Boot / Vue / LangChain”：可以说“这套栈是我能掌控、能调试、能解释的最小方案；我没有为了技术栈而技术栈。LangChain 会隐藏部分编排细节，我选择自己写服务层，便于定位问题。”")

doc.add_heading("3.4 一张图怎么讲", level=2)
for s in [
    "先指用户：用户只面对浏览器，不直接接触模型。",
    "再指前端：前端把操作变成 API 请求，把结果和引用展示出来。",
    "再指后端：鉴权、权限、文档/资源、RAG 编排都在这里，它是系统的“门禁和大脑”。",
    "最后指数据层：PostgreSQL 存事实，Milvus 存向量，Redis 存会话，外部 LLM 只负责生成。",
]:
    bullet(s)

# ---------- chapter 4 ----------
chapter("第 4 章 数据怎么进去：知识管理链路", "这一章证明“系统”不是只有聊天窗口。")
picture(WORK / "ingest.png", 6.55)
caption("图 2  文档进入系统的过程")

doc.add_heading("4.1 上传后发生了什么", level=2)
table(
    ["步骤", "做什么", "关键点"],
    [
        ["1. 上传", "保存原文件，创建 documents 记录，状态 pending", "先落文件再解析；支持 PDF/DOCX/TXT/MD"],
        ["2. 解析", "抽取文本/结构；解析失败写 failed 和错误信息", "扫描件 PDF 可能没有文本，需要可复制版本"],
        ["3. 切片", "按标题/段落/窗口切分，默认 768 tokens、overlap 100、min 32", "Chunk 保留 doc_id、title_path、page_num"],
        ["4. 向量化", "BGE-M3 生成 dense + sparse 向量", "写入 Milvus；文档状态变 completed"],
        ["5. 可检索", "问答时按 KB 和权限检索这些 Chunk", "PG 是事实源，Milvus 是索引"],
    ],
    widths=[2.2, 8.2, 5.8],
    font_size=9.0,
)

doc.add_heading("4.2 文档和结构化资源是两条线", level=2)
rich("**文档上传**处理 PDF/DOCX/TXT/MD：先解析成文本，再切片、向量化。**数据导入中心**处理中药/方剂/理论/文献的结构化数据，支持 CSV/TSV/JSON/Parquet 等，导入时做字段映射、批次记录和测试数据清理。")
rich("答辩时如果老师问“CSV 为什么不在文档上传里”，就答：“CSV 是结构化数据，不适合当长文本切片；我把它放到导入中心，按字段映射进资源表，再决定是否向量化。”")

doc.add_heading("4.3 删除不是只删一行", level=2)
rich("文档/资源删除优先软删除，进入回收站；检索侧会过滤已删除文档/资源，彻底删除时清理 Milvus 向量、Chunk、文件、KB 挂载、KG 节点等派生数据。这样避免“页面删了，问答还能引用”的脏数据。")

# ---------- chapter 5 ----------
chapter("第 5 章 RAG 怎么答：核心链路", "这是答辩的主战场。你不需要会推导公式，但要知道每一步解决什么问题。")
picture(WORK / "rag.png", 6.55)
caption("图 3  RAG 问答链路")

doc.add_heading("5.1 先记住 7 个动作", level=2)
table(
    ["动作", "解决什么问题", "一句话解释"],
    [
        ["问题分析", "问题类型/实体/意图不清楚", "先判断问的是中药、方剂、理论还是文献"],
        ["实体消歧", "同名/别名/简称", "把“桂枝汤”这类实体对齐到资源"],
        ["动态路由", "不同问题适合不同检索策略", "选 baseline / herb / prescription / KG / multi-source 等策略"],
        ["HyDE", "问题和文档用词不一致", "让模型先写一个“假设答案”，用它去检索"],
        ["双路召回", "只靠关键词或只靠语义都不够", "BGE-M3 dense + sparse 各召回一批"],
        ["RRF + Reranker", "两路分数不可比、候选太多", "先按排名融合，再用 cross-encoder 精排"],
        ["Gate + 生成 + 引用", "证据不足会瞎编，答案不可追溯", "证据不足拒答；生成后只保留实际引用来源"],
    ],
    widths=[2.6, 6.4, 7.2],
    font_size=9.0,
)

doc.add_heading("5.2 用“人话”解释关键概念", level=2)
rich("**Embedding（向量化）**：把一段文字变成一串数字。意思相近的文字，数字也相近；这样就能“按意思”找资料，而不是只按字面找。")
rich("**Dense（稠密）**：看整体语义。“感冒”和“外感风寒”字面不同，但向量可能很近。")
rich("**Sparse（稀疏）**：看关键词/词权重。适合“桂枝汤”“太阳中风”这种术语精确匹配。")
rich("**RRF**：两路检索各自给排名，不比较分数，只按“第几名”算贡献：`score = Σ 1/(k + rank)`，常用 `k=60`。排名越靠前贡献越大，避免一路分数虚高。")
rich("**Reranker**：把问题和候选片段放在一起做更细的判断，比向量相似度更准，但更慢；所以只对 Top 候选做。")
rich("**HyDE**：先让模型写一个“假设答案”，再用它检索；好处是补足问题太短、术语不一致，坏处是假设答案可能跑偏，所以失败时回退原问题。")
rich("**Gate**：不是“有没有检索到”，而是“检索到的东西够不够支撑回答”。不够就拒答或换策略重试。")
rich("**Reflection**：答案生成后检查是否超出证据、引用是否完整/越界、有无无依据句子；可以接受、改写或重试。")

doc.add_heading("5.3 为什么“检索到 6 条，答案只引用 1 条”", level=2)
rich("`citations` 事件下发的是**生成前候选证据**，用于实时展示；`done.citations` 才是**正文实际引用的来源**。前端最终以 `done` 收敛，所以引用列表会从 6 条变成 1 条。这不是丢数据，而是把“候选”与“引用”分开。")
rich("如果老师追问“那候选还有意义吗”：有，生成前让用户看到检索范围；生成后用实际引用保证可追溯。")

doc.add_heading("5.4 为什么有时直接拒答", level=2)
rich("三道闸：**相关性阈值**（dense 相似度低于阈值，不送 LLM）、**Evidence Gate**（证据等级/来源类型/覆盖不足）、**生成约束**（证据不足时只输出保守回答）。这样宁可少答，也不要用模型通用知识补答。")
rich("拒答不是失败，是系统边界。答辩时可以把拒答当成“防幻觉设计”的证据。")

# ---------- chapter 6 ----------
chapter("第 6 章 数据库、缓存、向量库：为什么三个都要", "这题很常见。答“各司其职”，不要答“多放几个库显得高级”。")
table(
    ["存储", "存什么", "为什么不能用另一个替代"],
    [
        ["PostgreSQL", "用户、知识库、文档、Chunk、四类资源、会话、评测、审计", "需要事务、外键、关联查询、权限过滤；Milvus 不擅长这些"],
        ["Milvus", "Chunk/资源向量、doc_id、kb_id、page_num 等", "需要 ANN 相似度检索；PG 做向量检索不适合这个规模"],
        ["Redis", "会话历史缓存", "低延迟、可裁剪；PG 每次查历史会慢，且缓存丢了可回源"],
        ["上传目录/对象存储", "原始 PDF/DOCX 等文件", "数据库不适合存大文件；保留原文用于溯源和重新解析"],
    ],
    widths=[3.0, 7.4, 6.0],
    font_size=8.9,
)

doc.add_heading("6.1 核心表怎么记", level=2)
bullet("**users**：用户、角色、启用/禁用、软删除。")
bullet("**knowledge_bases / kb_members / knowledge_base_resources**：知识库、成员角色、资源挂载。")
bullet("**documents / chunks**：文档与切片；chunk 是检索和引用的最小单位。")
bullet("**herbs / prescriptions / theories / literatures**：四类结构化资源。")
bullet("**conversations / messages**：会话与消息；assistant 消息里存 citations JSON。")
bullet("**test_cases / evaluation_runs / evaluation_results**：评测集、运行、逐条结果。")
bullet("**kg_nodes / kg_edges**：知识图谱节点和边。")

doc.add_heading("6.2 数据量怎么答", level=2)
rich("当前演示库（含历史导入/测试数据）大约有：**3.5 万文档、16.7 万切片、1920 个知识库、532 个用户**。如果老师问“为什么知识库这么多”，要诚实说“包含历史导入批次和测试库，正式演示会选一个干净的知识库”。不要把测试数据说成真实业务量。")

doc.add_heading("6.3 数据持久化", level=2)
rich("Docker Compose 把 PostgreSQL、Redis、etcd、MinIO、Milvus 和 uploads 绑定到宿主机 `docker-data/`。删除容器或重建镜像不会丢数据，只有删除 `docker-data/` 才会丢。系统设置页有“数据持久化”卡片，显示宿主机目录。")

# ---------- chapter 7 ----------
chapter("第 7 章 权限、安全与工程化", "这章回答“这是不是玩具”。")
doc.add_heading("7.1 权限模型", level=2)
table(
    ["层级", "角色", "能做什么"],
    [
        ["系统角色", "admin / member / viewer", "admin 管用户、导入、审计、模型配置；member 正常使用；viewer 只读"],
        ["知识库角色", "owner / admin / editor / viewer", "owner 管成员和元信息；editor 可写文档/评测；viewer 只读"],
        ["服务端", "get_current_user + require_kb_write", "所有接口最终在服务端校验，不信任前端隐藏按钮"],
    ],
    widths=[3.0, 6.0, 7.4],
    font_size=9.0,
)

doc.add_heading("7.2 安全措施", level=2)
bullet("JWT 鉴权；密码哈希；首次登录强制改密；禁用账号登录阶段拒绝。")
bullet("Pydantic 校验输入；SQLAlchemy 参数化查询，降低 SQL 注入风险。")
bullet("API Key 只回显脱敏值；模型测试 SSRF 防护；生产环境拒绝弱 SECRET_KEY。")
bullet("审计日志记录关键管理操作；软删除 + 回收站；删除后检索侧过滤。")
bullet("Docker 数据绑定到本地目录；生产需限制 PG/Redis/Milvus 端口。")

doc.add_heading("7.3 系统边界", level=2)
rich("系统只用于中医药知识学习与查询辅助，**不做疾病诊断、不自动处方、不针对具体患者制定治疗方案**。这不是“功能没做”，而是产品边界。")

# ---------- chapter 8 ----------
chapter("第 8 章 项目亮点：只讲 4 个，不要贪多", "老师记不住十个亮点，只能记住三个。")
doc.add_heading("8.1 应用层闭环", level=2)
rich("不是“文档上传 + 聊天框”的拼接，而是从知识库、文档/资源、切片、向量、检索、证据、引用、评测、权限、审计到数据持久化的完整闭环。")
doc.add_heading("8.2 多来源证据", level=2)
rich("文档、中药、方剂、理论、文献、KG 关系可以进入同一套 Evidence/Citation 结构，前端按来源分组展示，而不是把所有片段混成一锅。")
doc.add_heading("8.3 动态检索 + 门控", level=2)
rich("问题先分析再路由，避免所有问题都走同一套检索；Evidence Gate 把“检索到”与“足以回答”分开；Reflection 再检查答案是否超出证据。")
doc.add_heading("8.4 可追溯与可评估", level=2)
rich("引用只保留正文实际使用的来源；引用卡片可以定位原始文档/资源；评测模块记录策略、Gate、Reflection 和逐条指标。")

callout(
    "创新点的稳说法",
    "“我的创新点不在模型训练，而在应用层：把多来源证据、动态检索、证据门控、引用溯源和评测做成可运行闭环，并且在中医药知识管理场景里落地。”",
    fill="FFF7E6",
    border=GOLD,
)

# ---------- chapter 9 ----------
chapter("第 9 章 已知不足：主动交底，比被问出来好", "不要把所有不足说成“未来工作”就完事；要说出影响和下一步。")
table(
    ["不足", "证据/影响", "答辩时怎么说"],
    [
        ["评测不充分", "40 条种子用例，只有 5 条确认标准答案，35 条待审核", "“评测流程搭好了，但人工标准答案不足，结论是初步的；我不会拿它声称高准确率。”"],
        ["依赖外部 LLM", "模型服务不可用/超时会直接影响回答", "“RAG 能约束证据，但不能消除模型服务依赖；系统有超时、重试和错误提示，不能保证 100% 可用。”"],
        ["复杂问题拆分不够", "多子问题目前主要是 Prompt 约束，没有逐子问题分别检索", "“简单多问题能部分回答，复杂多跳还需要问题分解和逐子问题检索。”"],
        ["格式/页码不完美", "文档上传 4 种；PDF 页码依赖 pypdf，扫描件可能无文本", "“格式做了收敛；扫描件和复杂版式仍是解析难点。”"],
        ["延迟/成本", "BGE-M3 + Reranker 在 CPU 上较慢，LLM 外部调用有延迟", "“本地 CPU 推理和外部 LLM 是主要耗时；后续可做缓存、批处理、GPU 或更小模型。”"],
        ["数据质量", "演示库含历史导入/测试数据", "“正式演示会选干净知识库；数据清洗和批次管理还要加强。”"],
        ["安全边界", "JWT 存在 localStorage，API Key 在数据库未加密", "“这是毕设级部署，不是公网生产；生产需要 HttpOnly、密钥托管、限流和 HTTPS。”"],
    ],
    widths=[3.2, 6.2, 7.0],
    font_size=8.4,
)

doc.add_heading("9.1 “如果重做，你会先改什么”", level=2)
rich("示范回答：**先把评测集和人工标准答案补起来**，因为没有评测就无法判断策略是否真的有用；**再做多子问题分解和逐子问题检索**；最后才是性能优化。技术栈我不会大改，因为当前瓶颈不在框架，而在数据和评测。")

# ---------- chapter 10 Q&A ----------
chapter("第 10 章 高频问答 43 题", "先看问题，遮住答案，用自己的话讲一遍；讲不顺的做标记，第二遍只看标记。")

qa = [
("为什么选这个题目？",
 "我是数据分析方向，想挑战一个完整的应用系统。中医药资料类型多、来源分散，RAG 又涉及数据组织、检索和评估，正好能把数据分析和系统开发结合起来。"),
("一句话介绍你的系统。",
 "它是一个以中医药知识资源管理为主体、用 RAG 增强问答的应用系统：先把资料组织成知识库，再检索证据，让大模型基于证据回答并给出引用。"),
("它和直接问 ChatGPT 有什么区别？",
 "ChatGPT 主要依赖模型参数里的知识；我的系统先从自己的知识库检索证据，再让模型基于证据回答，并展示来源。证据不足时拒答，不做通用知识补答。"),
("你的创新点是什么？",
 "应用层闭环：多来源证据、动态检索路由、Evidence Gate、引用溯源和评测串起来，并在中医药知识管理场景落地。不是模型训练创新。"),
("为什么用 RAG，不微调？",
 "微调需要高质量标注数据和训练资源，知识更新还要重训；RAG 可以随知识库更新，引用可追溯，更适合毕设和专业知识场景。"),
("系统整体架构？",
 "前后端分离：Next.js 前端、FastAPI 后端、PostgreSQL 存业务、Milvus 存向量、Redis 存会话，外部 LLM 负责生成。"),
("前端和后端分别做什么？",
 "前端负责页面、表单、聊天和证据展示；后端负责鉴权、权限、业务规则、文档/资源、RAG 编排和 SSE。所有权限最终在服务端校验。"),
("为什么用 Next.js / FastAPI？",
 "Next.js 适合快速做管理页面和聊天界面；FastAPI 异步、类型清晰、和 Pydantic/SQLAlchemy 配合好。核心原因是这套栈我能调试、能解释。"),
("文档上传后经历什么？",
 "保存文件 → 解析文本 → 切片 → BGE-M3 向量化 → 写 PostgreSQL 和 Milvus → 状态 completed，之后才能被检索。"),
("切片多大？为什么？",
 "默认 768 tokens、overlap 100、min 32。太大检索不精准，太小上下文不完整；这是折中值，不是理论最优。"),
("为什么 PostgreSQL 和 Milvus 都要？",
 "PG 存事实和关系，适合事务、权限、关联查询；Milvus 存向量，适合相似度检索。一个做事实源，一个做索引，不能互相替代。"),
("Redis 做什么？",
 "存会话历史缓存，减少每次问答都查 PG；缓存丢了可以回源，不影响事实数据。"),
("Embedding 是什么？",
 "把文本变成向量，让语义相近的文本在向量空间里接近；这样能按意思检索，不只按字面匹配。"),
("Dense 和 Sparse 的区别？",
 "Dense 看整体语义，适合同义表达；Sparse 看关键词权重，适合术语精确匹配。两路互补，所以都保留。"),
("RRF 是什么？",
 "Reciprocal Rank Fusion，按排名融合两路结果：score = Σ 1/(k+rank)，常用 k=60。它不需要两路分数在同一量纲，比较稳。"),
("Reranker 和 RRF 有什么区别？",
 "RRF 只融合排名，速度快；Reranker 把问题和候选一起做交叉编码，判断更细，但更慢。所以先 RRF 召回，再 Reranker 精排 Top-N。"),
("HyDE 是什么？为什么用？",
 "先让模型写一个假设答案，再用假设答案去检索，缓解问题太短/术语不一致；风险是假设答案跑偏，失败时会回退原问题。"),
("Query Analyzer / Dynamic Router 做什么？",
 "分析问题类型、实体和意图，再选合适检索策略（如中药、方剂、理论、文献、KG、多来源），避免所有问题走同一套检索。"),
("知识图谱在系统里做什么？",
 "从四类资源的关系构建轻量 KG，检索时作为补充证据；它不是通用图谱，也不替代向量检索。"),
("Evidence Gate 是什么？",
 "在生成前判断证据是否足够支撑回答。不够就拒答或换策略重试，避免模型凭常识补答。"),
("Self Reflection 是什么？",
 "答案生成后检查是否超出证据、引用是否完整/越界、有无无依据句子；可以接受、改写或重试。它是兜底，不是保证。"),
("引用怎么保证真实？",
 "模型输出 [citation: 编号, 页码]，后端只按检索命中的编号映射来源；越界/无标记不生成假引用，最终 done.citations 只保留正文实际引用。"),
("为什么有时会拒答？",
 "相关性低于阈值、Evidence Gate 判定不足、或生成时证据不够。宁可少答，也不编造。"),
("多轮对话怎么实现？",
 "会话 id 关联 messages；Redis 缓存最近历史并裁剪，PG 作为回退事实源；Prompt 带最近几轮历史。"),
("SSE 怎么实现？",
 "前端 fetch POST 读取 ReadableStream，后端按 start → citations → delta×N → done 推送；done.answer 是最终权威答案，异常用 error 替代 done。"),
("系统权限怎么设计？",
 "系统角色 admin/member/viewer，知识库角色 owner/admin/editor/viewer；接口用 get_current_user 和 KB 写权限校验，前端隐藏按钮只是体验。"),
("API Key 怎么保护？",
 "只回显脱敏值；测试连通时如提交空/脱敏值，回退数据库已保存 Key；生产拒绝弱 SECRET_KEY，模型 URL 做 SSRF 防护。"),
("怎么防 SQL 注入 / XSS？",
 "后端用 SQLAlchemy 参数化查询、Pydantic 校验；前端 React 默认转义，不直接插 HTML；但安全没有银弹，生产还要 CSP/限流/HTTPS。"),
("你做了哪些测试？",
 "后端 pytest 覆盖 RAG、引用、Gate、Reflection、权限、生命周期；前端覆盖 SSE 解析、chatStore、证据分组等；当前核心回归通过。"),
("怎么评估问答效果？",
 "用 RAGAS 风格指标：Context Relevancy 和 Answer Correctness；有评测模块、实验归档和 0.75 质量门禁。但标准答案不足，结论是初步的。"),
("评测集多大？",
 "40 条种子用例，覆盖中药/方剂/理论/文献/多来源/拒答 6 类；但只有 5 条有已确认标准答案，35 条待人工审核。不能声称高准确率。"),
("为什么标准答案这么少？",
 "中医标准答案需要专业审核，我没有中医资质，宁可标 needs_review，也不凭空编标准答案污染评测。"),
("当前数据量多大？",
 "演示库约 3.5 万文档、16.7 万切片、1920 个知识库、532 个用户；其中包含历史导入/测试数据，正式演示会选干净知识库。"),
("系统最难的地方？",
 "不是页面，而是“检索到的东西是否足以回答”和“引用是否真实”。所以做了 Gate、Reflection、引用过滤和拒答。"),
("你觉得最大的不足？",
 "评测不充分：标准答案少、没有用户研究；其次是复杂多子问题检索和外部 LLM 依赖。"),
("如果重做，先改什么？",
 "先补评测集和人工标准答案，再做多子问题分解和逐子问题检索，最后做性能和部署优化；技术栈不大改。"),
("这些代码是你写的吗？",
 "代码是 AI 辅助生成的。我负责需求拆解、数据准备、架构取舍、测试验收和迭代；核心链路的输入输出、失败模式和关键文件我能解释。我不把 AI 生成说成逐行手写，但我对提交的系统负责。"),
("那你具体做了什么？",
 "确定题目和功能边界、整理数据、设计表结构和模块、验收每个功能、定位并修复问题、跑测试和演示；AI 负责生成初版代码，我负责判断对不对、能不能用。"),
("如果现场让你改一个功能？",
 "我先定位入口、服务、数据表，再做小步修改并跑测试。比如改拒答阈值，系统设置页就能改，因为阈值走运行时配置；加字段则要动模型、迁移、API 和前端，不是一分钟能完成。"),
("模型服务挂了怎么办？",
 "会报错/超时，系统不会伪造答案；可以换模型配置或稍后重试。RAG 不能消除外部服务依赖。"),
("为什么不做诊断/处方？",
 "这是产品边界。系统只做知识学习与查询辅助，不做疾病诊断、自动处方或具体患者治疗方案。"),
("论文和系统怎么对应？",
 "论文按需求、设计、实现、测试与评估展开；不把计划写成已完成，不写没有数据支撑的“显著提升”。"),
("如果演示失败怎么办？",
 "先保系统：检查容器/health、数据目录、模型配置；讲架构和测试结果，不现场造假。演示前我会准备截图和录屏兜底。"),
]

for i, (q, a) in enumerate(qa, 1):
    doc.add_heading(f"Q{i}. {q}", level=3)
    rich(f"**短答：** {a}", size=10.2, space_after=2)
    if i % 10 == 0:
        callout("休息一下", f"到这里先停。把 Q{max(1, i - 9)}—Q{i} 用自己的话复述一遍，再继续。不要逐字背答案。", fill=PALE, border=TEAL)

# ---------- chapter 11 ----------
chapter("第 11 章 演示脚本与应急预案", "演示的目标不是“功能全”，而是“链路清、结果稳、出错不慌”。")
doc.add_heading("11.1 8 分钟演示顺序", level=2)
table(
    ["时间", "操作", "讲解重点"],
    [
        ["0:00—1:00", "登录，展示知识库/侧边栏", "这是知识资源管理系统，不是纯聊天"],
        ["1:00—2:30", "中药/方剂列表，搜索“丹参/桂枝汤”，打开详情", "四类资源、分类标签、权限按钮"],
        ["2:30—4:30", "提问一个有资料的问题（如“丹参有什么功效”）", "看回答、引用卡片、定位来源"],
        ["4:30—5:30", "提问一个无资料/超范围问题", "看拒答，讲防幻觉边界"],
        ["5:30—6:30", "（可选）多子问题/停止/编辑重问", "证明交互闭环"],
        ["6:30—7:30", "设置页数据持久化、模型配置/评测页", "证明工程化，不是单页 demo"],
        ["7:30—8:00", "收尾", "一句定位 + 一句不足"],
    ],
    widths=[2.2, 7.2, 7.0],
    font_size=8.8,
)

doc.add_heading("11.2 演示前检查清单", level=2)
bullet("容器：`docker ps` 七个服务都在；`/health` 三个组件 ok。")
bullet("数据：知识库列表非空；选一个干净 KB，不要展示测试库。")
bullet("模型：设置页测试连通；准备一个短问题，不要现场上传大文件。")
bullet("页面：登录、聊天、引用定位、设置页各点一次，确认无 401/空白。")
bullet("兜底：截图/录屏 + 架构图 + 测试输出；演示失败时不造假。")

doc.add_heading("11.3 如果出问题", level=2)
table(
    ["现象", "先做什么", "怎么讲"],
    [
        ["输入框禁用/没有知识库", "检查 `/api/v1/kb` 和数据库；确认 docker-data 未丢", "“数据服务刚重启，我先用截图/已有会话继续讲链路。”"],
        ["模型超时/无输出", "点“停止”；检查模型配置", "“外部模型不可用；系统有超时和中断，不会伪造答案。”"],
        ["回答内容不对", "不要争辩；指出引用和证据", "“这是检索/生成边界问题，我会看引用来源和 Gate 记录，后续补评测。”"],
        ["Milvus 不可用", "看 `/health`；重启服务", "“向量库是独立组件；不可用时系统会降级/报错，不会假装检索成功。”"],
    ],
    widths=[4.0, 6.0, 6.4],
    font_size=8.8,
)

# ---------- chapter 12 ----------
chapter("第 12 章 三天准备计划与一页纸速记", "不要把时间花在背代码上。把时间花在“讲顺”和“不被问倒”。")
doc.add_heading("12.1 三天计划", level=2)
table(
    ["时间", "任务", "验收"],
    [
        ["第 1 天上午", "读第 1—5 章；打开系统跑一遍", "能不看稿讲 3 分钟"],
        ["第 1 天下午", "画架构图、RAG 图、数据流图各 1 次", "能边画边讲"],
        ["第 2 天上午", "背 Q1—Q20；标记讲不顺的", "每个答案 30 秒内说完"],
        ["第 2 天下午", "背 Q21—Q43；准备 AI 写代码的回答", "不回避、不硬撑"],
        ["第 3 天上午", "完整模拟 2 次（5 分钟 + 10 分钟）", "录音回听，删口头禅"],
        ["第 3 天下午", "演示彩排 + 兜底截图", "断网/模型挂也能讲 5 分钟"],
    ],
    widths=[2.4, 8.4, 5.6],
    font_size=9.0,
)

doc.add_heading("12.2 必须背的 10 句话", level=2)
for s in [
    "这是一个以知识资源管理为主体、用 RAG 增强问答的应用系统。",
    "知识先进入知识库，检索出证据，再让模型基于证据回答。",
    "PostgreSQL 存事实，Milvus 存向量，Redis 存会话。",
    "BGE-M3 做稠密 + 稀疏双路召回，RRF 融合，Reranker 精排。",
    "Evidence Gate 判断证据够不够，不够就拒答。",
    "引用只保留正文实际使用的来源，候选不等于引用。",
    "系统有系统角色和知识库角色两层权限，服务端最终校验。",
    "评测流程已搭好，但标准答案不足，结论是初步的。",
    "代码由 AI 辅助生成，我负责需求、数据、验收和迭代。",
    "系统只做知识学习与查询辅助，不做诊断和处方。",
]:
    bullet(s)

doc.add_heading("12.3 五个数字", level=2)
table(
    ["数字", "含义"],
    [
        ["4", "文档上传支持 PDF/DOCX/TXT/MD 四种格式"],
        ["768 / 100 / 32", "切片 tokens / overlap / 最小长度"],
        ["0.35", "默认相关性阈值"],
        ["7", "动态路由的 7 种检索策略"],
        ["40 / 5", "评测种子 40 条，已确认标准答案 5 条"],
    ],
    widths=[3.0, 13.4],
    font_size=9.5,
)

doc.add_heading("12.4 可以承认不会的 5 类问题", level=2)
for s in [
    "Transformer/BGE-M3 的内部网络结构和训练细节。",
    "Milvus 索引的底层实现和参数调优。",
    "大规模分布式部署、Kubernetes、高并发。",
    "中医临床证型的专业判断（这不是系统目标）。",
    "现场从零手写一个完整模块（我可以讲思路和改小功能）。",
]:
    bullet(s)
rich("承认不会时加一句：“这块我没有深入，但我知道它在系统里的位置和边界。”比硬编好。", size=10)

doc.add_heading("12.5 最后一晚只做三件事", level=2)
for s in [
    "把 5 分钟稿完整讲 3 遍，录音回听，删掉“然后、就是、那个”。",
    "把架构图、RAG 图、数据流图各默画 1 遍，能边画边讲。",
    "把演示流程走 1 遍，准备好断网/模型挂掉时的截图兜底。",
]:
    bullet(s)

# ---------- appendix ----------
chapter("附录 A 关键文件地图：老师让你打开代码时看哪里", "不要从几千行 rag_service 开始逐行读；先打开入口和 3 个核心服务。")
table(
    ["功能", "关键文件", "你要能说出"],
    [
        ["问答入口", "backend/src/api/routes/chat.py", "ask / ask-stream，鉴权、KB 校验、SSE"],
        ["RAG 编排", "backend/src/application/rag_service.py", "检索 → 重排 → Gate → Prompt → LLM → Citation"],
        ["问题分析/路由", "backend/src/application/query_analyzer.py、dynamic_router.py、retrieval_strategies.py", "问题类型、实体、策略"],
        ["证据/引用", "backend/src/application/evidence.py、evidence_gate.py、self_reflection.py", "Evidence、Gate、复核"],
        ["知识图谱", "backend/src/application/kg_service.py、kg_retrieval.py", "节点/边、KG 补充证据"],
        ["文档处理", "backend/src/application/parse_service.py、indexing_service.py、document_service.py", "解析、切片、向量化"],
        ["数据模型", "backend/src/domain/models.py", "表、关系、软删除"],
        ["前端聊天", "frontend/src/app/chat/page.tsx、stores/chatStore.ts、hooks/useSSE.ts", "输入、SSE、状态"],
        ["引用面板", "frontend/src/components/chat/EvidencePanel.tsx、MessageItem.tsx", "分组、引用 chip、定位来源"],
        ["权限", "backend/src/core/deps.py、api/routes/auth.py", "JWT、角色、KB 权限"],
        ["导入", "backend/src/application/dataset_scanner.py、upload_import_service.py", "扫描、字段映射、批次"],
    ],
    widths=[3.4, 8.8, 4.2],
    font_size=8.2,
)

doc.add_heading("附录 B 术语表", level=2)
table(
    ["术语", "一句话解释"],
    [
        ["RAG", "先检索外部知识，再把知识放进 Prompt 让模型回答"],
        ["Embedding", "把文本变成向量，便于按语义检索"],
        ["Dense / Sparse", "稠密语义向量 / 稀疏关键词权重"],
        ["RRF", "按排名融合多路检索结果"],
        ["Reranker", "对候选片段做更精细的相关性重排"],
        ["HyDE", "用假设答案去检索，缓解问题与文档表述不一致"],
        ["Evidence Gate", "判断证据是否足以支撑回答"],
        ["Reflection", "生成后检查答案是否忠于证据"],
        ["Citation", "答案与知识来源的关联"],
        ["SSE", "服务器单向流式推送事件"],
        ["RBAC", "基于角色的权限控制"],
        ["Soft delete", "标记删除，进入回收站，可恢复"],
    ],
    widths=[4.0, 12.4],
    font_size=9.0,
)

doc.add_heading("附录 C 答辩前最后检查", level=2)
for s in [
    "能否不看稿讲 3 分钟？",
    "能否在白纸上画出架构图和 RAG 链路？",
    "能否解释 PostgreSQL / Milvus / Redis 的分工？",
    "能否诚实回答 AI 写代码和评测不足？",
    "演示是否准备了截图/录屏兜底？",
    "是否确认不会说“准确率 95%”“所有代码手写”？",
]:
    bullet(s)

callout(
    "最后一句",
    "答辩不是证明你什么都会，而是证明**这个系统是你想清楚、跑起来、能解释、知道边界的**。把不会的诚实说出来，把会的讲清楚，就已经赢过大多数“背稿型”答辩。",
    fill="FFF7E6",
    border=GOLD,
)

# save
doc.save(str(OUT))
print("SAVED", OUT, OUT.stat().st_size)
