# -*- coding: utf-8 -*-
"""生成 RAG 评估测试集 evaluation/dataset/questions.xlsx。
数据全部来自运行中的真实知识库（PostgreSQL + 古籍切片），不调用问答接口、不跑评估、不改业务代码。
"""
import hashlib
import json
import os
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

KB_ID = "e379da9c-7fde-4aba-b118-cffff4d3df76"
KB_NAME = "真实中医知识库"

data = json.load(open("evaluation/_extract.json", encoding="utf-8"))
HERBS = {r[1]: r for r in data["herbs"]}
PRESCS = {r[1]: r for r in data["prescriptions"]}
THEORIES = {r[1]: r for r in data["theories"]}
LITS = {}
for b in data["literature"]:
    if b["candidates"]:
        LITS[b["book"]] = b["candidates"][0]


def vec_id(rtype, rid, idx=0):
    raw = f"resource|{rtype}|{rid}|{KB_ID}|{idx}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean(s):
    return (s or "").strip().rstrip("。；;，, ")


def herb_answer(r, mode):
    _, name, props, ch, ef, src, desc = r
    if mode == "all":
        return f"性味（四气五味）：{props}；归经：{ch}；功效：{ef}"
    if mode == "props_ch":
        return f"四气五味：{props}；归经：{ch}"
    if mode == "effect":
        return f"功效：{ef}"
    if mode == "effect_ch":
        return f"功效：{ef}；归经：{ch}"
    return ""


def presc_answer(r, mode):
    _, name, ef, ind, us, desc, src = r
    if mode == "ind":
        return f"主治：{_clean(ind)}"
    if mode == "comp":
        return desc
    if mode == "ef_ind":
        return (f"功效：{_clean(ef)}；主治：{_clean(ind)}" if ef else f"主治：{_clean(ind)}")
    if mode == "ind_comp":
        return f"主治：{_clean(ind)}；{desc}"
    return ""


rows = []


def add(question, answer, ktype, qtype, rname, rid, cid, behavior,
        evidence, status, reason="", review="否", note=""):
    rows.append({
        "编号": len(rows) + 1,
        "问题": question,
        "标准答案": answer,
        "知识类型": ktype,
        "问题类型": qtype,
        "对应资源名称": rname,
        "资源 ID": rid,
        "相关 Chunk ID": cid,
        "预期行为": behavior,
        "知识库ID": KB_ID,
        "证据摘要": evidence,
        "核实状态": status,
        "拒答原因": reason,
        "人工审核标记": review,
        "备注": note,
    })


# ========================= 中药知识（8） =========================
herb_specs = [
    ("丹参", "知识库中，丹参的性味、归经和功效分别是什么？", "all", "事实查询", "通过"),
    ("乳香", "乳香的性味、归经与功效是什么？", "all", "事实查询", "通过"),
    ("川楝子", "知识库中川楝子的性味、归经与功效分别是什么？", "all", "事实查询", "通过"),
    ("黑胡椒", "黑胡椒的四气五味与归经是什么？", "props_ch", "事实查询", "通过"),
    ("艾片（左旋龙脑）", "艾片（左旋龙脑）在知识库中记载的功效是什么？", "effect", "事实查询", "通过"),
    ("紫花前胡", "紫花前胡的功效与归经是什么？", "effect_ch", "事实查询", "通过"),
]
for name, q, mode, qtype, status in herb_specs:
    r = HERBS[name]
    ans = herb_answer(r, mode)
    add(q, ans, "中药知识", qtype, name, r[0], vec_id("herb", r[0]),
        "正常回答", f"资源向量内容：{ans}", status)

# 对比归纳（2 道，多资源）
r1, r2 = HERBS["丹参"], HERBS["乳香"]
add("丹参和乳香在功效上有什么共同点？",
    f"二者的功效均为：{r1[4]}（即活血化瘀类表述）。",
    "中药知识", "对比归纳", "丹参；乳香", f"{r1[0]}；{r2[0]}",
    f"{vec_id('herb', r1[0])}；{vec_id('herb', r2[0])}",
    "正常回答",
    f"丹参功效：{r1[4]}；乳香功效：{r2[4]}",
    "通过")

r1, r2 = HERBS["五加皮"], HERBS["油松节"]
add("五加皮与油松节在功效上的共同点是什么？",
    f"二者的功效均为：{r1[4]}（即祛风湿类表述）。",
    "中药知识", "对比归纳", "五加皮；油松节", f"{r1[0]}；{r2[0]}",
    f"{vec_id('herb', r1[0])}；{vec_id('herb', r2[0])}",
    "正常回答",
    f"五加皮功效：{r1[4]}；油松节功效：{r2[4]}",
    "通过")

# ========================= 方剂知识（5，实际可用方剂仅 5 个） =========================
presc_specs = [
    ("枳术丸", "枳术丸的主治是什么？", "ind", "事实查询"),
    ("清心丸", "清心丸的主治是什么？", "ind", "事实查询"),
    ("烂积丸", "烂积丸的功效与主治是什么？", "ef_ind", "事实查询"),
    ("无极丸", "无极丸的主治与组成分别是什么？", "ind_comp", "事实查询"),
    ("沉香曲", "沉香曲的功效与主治是什么？", "ef_ind", "事实查询"),
]
for name, q, mode, qtype in presc_specs:
    r = PRESCS[name]
    ans = presc_answer(r, mode)
    add(q, ans, "方剂知识", qtype, name, r[0], vec_id("prescription", r[0]),
        "正常回答", f"资源向量内容：{ans}", "通过")

# ========================= 中医理论（7） =========================
theory_specs = [
    ("奔豚", "中医所说的“奔豚”有哪些临床表现？", "证候"),
    ("温补", "“温补”是一种什么治法？", "治法"),
    ("痰阻精室证", "“痰阻精室证”的临床表现有哪些？", "证候"),
    ("漆疮", "“漆疮”是什么？有哪些典型表现？", "病证"),
    ("阴邪", "中医理论中的“阴邪”指什么？", "病邪"),
    ("提脓祛腐", "“提脓祛腐”是一种什么治疗方法？", "治法"),
    ("肠痹", "“肠痹”的症状表现有哪些？", "证候"),
]
for name, q, sub in theory_specs:
    r = THEORIES[name]
    add(q, r[2], "中医理论", "定义解释", name, r[0], vec_id("theory", r[0]),
        "正常回答", f"资源向量内容（英文释义）：{r[2][:220]}",
        "待复核（原文为英文释义）", review="是",
        note=f"理论子类：{sub}；答案保留知识库原文英文释义")

# ========================= 文献知识（7，仅取已入库可检索的古籍切片） =========================
lit_specs = [
    ("肘后备急方", "4cd1f596-abea-4a6f-84e4-033d6ce89e72",
     "007231d9-3f2c-467e-ac07-df3b2e72c75d",
     "《肘后备急方》中治疗乳痈（妒乳）有哪些外治方法？",
     "《肘后备急方》载多种乳痈/妒乳外治法：芫花末以胶汁调和涂敷；柳根皮熟捣、火温后以帛囊贮熨，冷则更换；大黄、伏龙肝（灶下黄土）、生姜各二分捣筛，醋和涂乳，痛止；黄芩、白蔹、芍药等分为末，浆服；另可捣生地黄敷之、赤小豆亦佳。",
     "又方，芫花末，胶汁和，粘贴，燥复易，化为水……乳肿。桂心，甘草各二分，乌头一分，炮，捣为末，和苦酒，涂纸覆之，脓化为水，则神效。"),
    ("严氏济生方", "b16dc3a0-52cf-46ec-9272-8cc2f8e505a9",
     "03de4da7-c959-4bc9-884c-d27b3df8529b",
     "《严氏济生方》大便门·泄泻论治中，火轮丸的主治、组成与服法是什么？",
     "火轮丸治肠胃虚寒、心腹冷痛、泄泻不止。组成：干姜（炮）、附子（炮，去皮脐）、肉豆蔻（面裹煨）各等分；为细末，米糊为丸如梧桐子大，每服五十丸，空心米饮送下。",
     "<目录>大便门·泄泻论治 <篇名>火轮丸 属性：治肠胃虚寒，心腹冷痛，泄泻不止。干姜（炮） 附子（炮，去皮脐） 肉豆蔻（面裹煨） 上等分，为细末，米糊为丸，如梧桐子大，每服五十丸，空心，米饮送下。"),
    ("普济本事方", "81e1d4ea-eab4-419c-99fe-720ef75158a0",
     "01d899f8-316f-41a2-bebe-9cc581efaebb",
     "《普济本事方》中退阴散主治什么？由哪些药组成、如何服用？",
     "退阴散治阴毒伤寒，手足逆冷，脉沉细，头痛腰重。组成：川乌（炮，去皮脐）、干姜（炮）各等分；为粗末，炒令转色，放冷再捣为细末，每服一钱，水一盏，入盐一捻，煎半盏，去滓温服。阴毒伤寒咳逆者，煎一服细细热呷便止。",
     "<目录>卷第九·伤寒时疫（下） <篇名>退阴散 属性：治阴毒伤寒，手足逆冷，脉沉细，头痛腰重。川乌（炮去皮脐） 干姜（炮，各等分） 上为粗末，炒令转色，放冷再捣为细末，每服一钱，水一盏，盐一捻，煎半盏，去滓温服。"),
    ("塘医话", "54e0256b-cacc-44ca-a3f5-5a0b360e38e4",
     "0adb4528-256b-4b34-9758-06b412e561ca",
     "《塘医话》中如何根据舌苔辨别“脾湿盛”与“脾瘅”？",
     "舌苔不燥、自觉闷极者，属脾湿盛；舌上白苔粘腻、吐出浊浓涎沫、口有甜味者，为脾瘅病，乃湿热气聚与谷气相搏、土有余，当用省头草芳香辛散以逐之。若舌苔如咸，为胃中宿滞挟浊秽郁伏，当急急开泄。",
     "舌苔不燥，自觉闷极者，属脾湿盛也。……再舌上白苔粘腻，吐出浊浓涎沫者，口必甜味也，为脾瘅病，乃湿热气聚与谷气相搏，土有余也。……当用省头草芳香辛散以逐之则退。"),
    ("辅行诀脏腑用药法要", "a6082e52-84df-46b7-b96b-1f1e85cfb13b",
     "1ae3368c-ba19-46a5-b60a-9a8c91ec7af1",
     "《辅行诀脏腑用药法要》中小泻肺汤的主治与组成是什么？",
     "小泻肺汤治咳喘上气、胸中迫满、不可卧者。组成：葶苈子（熬黑，捣如泥）、大黄、芍药各三两；以水三升煮取二升，温分再服，喘定止后服。",
     "小泻肺汤 治咳喘上气，胸中迫满，不可卧者方：葶苈子（熬黑，捣如泥）大黄芍药（各三两） 上三味，以水三升，煮取二升，温分再服，喘定止后服。"),
    ("孙真人海上方", "a1d8255b-a472-460d-af69-19b1a3658d8a",
     "2398af6c-52cf-41e6-abf1-ce144e794d51",
     "《孙真人海上方》中治疗“小便不禁”（来溺）与“骨鲠”分别用什么方药？",
     "来溺（小便不禁）：天台乌药研末，饭后以米饮送服，日两次，每次三钱。骨鲠：金樱根用醋煎，入喉时切莫沾牙，骨出即愈。",
     "<篇名>来溺 内容：小便不禁有何难，寻取天台乌药研，饭后服时宜米饮，日须两次每三钱。<篇名>骨鲠 内容：骨鲠难医苦叫天，金樱根用醋来煎。入喉切莫沾牙上，骨出令人体自便。"),
    ("褚氏遗书", "0c3fdeab-3b3c-4183-bb66-3e7f9e8c39ce",
     "1f9e8647-cbe0-4d98-9728-0ad2b195c7a8",
     "《褚氏遗书》平脉篇如何论述寸、关、尺三部与脏腑的配属？",
     "脉分两手，手分三部，关前一寸为寸、关后为尺，中间为关。男子阳顺自下生上，右手尺为受命根本，尺上关为脾，关上寸为肺，右寸越左尺为肾，左尺上关为肝，关上寸为心；女子阴逆自上生下，左寸为受命根本，左寸下关为脾，关下尺为肺，左尺越右寸为肾，右寸下关为肝，关下尺为心。",
     "<篇名>平脉 属性：脉分两手，手分三部，隔寸尺者，命之曰关，去肘度尺曰尺，关前一寸为寸……男子阳顺，自下生上……女子阴逆自上生下……"),
]
for book, lit_id, chunk_id, q, ans, ev in lit_specs:
    add(q, ans, "文献知识", "原文理解", book, lit_id, chunk_id,
        "正常回答", f"切片原文：{ev}",
        "待复核（答案据原文摘录归纳）", review="是",
        note="文献已入库可检索（chunk 在 Milvus）；答案由原文摘录归纳，建议对照原文复核")

# ========================= 拒答（10） =========================
reject = [
    ("iPhone 15 的价格是多少？", "领域外问题", "关键词“iPhone”在 chunks 中命中 0 条，知识库为中医药领域，无数码产品价格信息。"),
    ("量子计算机的工作原理是什么？", "领域外问题", "关键词“量子计算机”命中 0 条，属现代物理/计算机领域，超出知识库范围。"),
    ("比特币是如何挖矿的？", "领域外问题", "关键词“比特币”命中 0 条，属加密货币领域，知识库无相关内容。"),
    ("2022年世界杯的冠军是哪支球队？", "领域外问题", "关键词“世界杯”命中 0 条，属体育赛事，知识库无相关内容。"),
    ("公务员考试需要考哪些科目？", "领域外问题", "关键词“公务员”命中 0 条，属考试制度，知识库无相关内容。"),
    ("火箭是如何进入太空的？", "领域外问题", "关键词“火箭”命中 0 条，属航天领域，超出知识库范围。"),
    ("抖音的推荐算法是如何工作的？", "领域外问题", "关键词“抖音”命中 0 条，属互联网产品，知识库无相关内容。"),
    ("六味地黄丸治疗2型糖尿病的随机双盲对照试验结论是什么？", "知识库证据不足",
     "关键词“随机双盲”“双盲对照”命中均为 0 条；知识库虽含“糖尿病”相关文本，但无随机双盲对照试验结论。"),
    ("中医药治疗高血压的临床指南推荐意见是什么？", "知识库证据不足",
     "关键词“临床指南”命中 0 条；知识库无临床指南类规范性文件，无法给出指南推荐意见。"),
    ("连花清瘟胶囊治疗新冠的双盲对照试验样本量是多少？", "知识库证据不足",
     "关键词“双盲对照”命中 0 条；“连花清瘟”仅 6 条且无随机对照试验样本量信息。"),
]
for q, ktype, reason in reject:
    add(q, "（知识库无充分证据，应拒答）", ktype, "拒答", "—", "—", "—",
        "拒答", f"校验说明：{reason}",
        "通过（关键词 0/低命中已核验）", reason=reason,
        review=("是" if ktype == "知识库证据不足" else "否"),
        note="")

# ========================= 校验 =========================
questions = [r["问题"].strip() for r in rows]
dupes = sorted({q for q in questions if questions.count(q) > 1})
missing = [r["编号"] for r in rows
           if r["预期行为"] == "正常回答"
           and ("—" in [r["对应资源名称"], r["资源 ID"], r["相关 Chunk ID"]] or
                not r["对应资源名称"] or not r["资源 ID"] or not r["相关 Chunk ID"])]

from collections import Counter
cat = Counter(r["知识类型"] for r in rows)

# ========================= 写 xlsx =========================
os.makedirs("evaluation/dataset", exist_ok=True)
wb = Workbook()
ws = wb.active
ws.title = "测试集"

HEAD = ["编号", "问题", "标准答案", "知识类型", "问题类型", "对应资源名称",
        "资源 ID", "相关 Chunk ID", "预期行为", "知识库ID", "证据摘要",
        "核实状态", "拒答原因", "人工审核标记", "备注"]
ws.append(HEAD)

head_font = Font(name="Microsoft YaHei", bold=True, size=10, color="FFFFFF")
head_fill = PatternFill("solid", fgColor="4472C4")
thin = Side(style="thin", color="D9D9D9")
border = Border(left=thin, right=thin, top=thin, bottom=thin)
body_font = Font(name="Microsoft YaHei", size=9)

for c in range(1, len(HEAD) + 1):
    cell = ws.cell(row=1, column=c)
    cell.font = head_font
    cell.fill = head_fill
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = border

for r in rows:
    ws.append([r[h] for h in HEAD])

for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(HEAD)):
    for cell in row:
        cell.font = body_font
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        cell.border = border

widths = {"编号": 6, "问题": 34, "标准答案": 52, "知识类型": 12, "问题类型": 10,
          "对应资源名称": 14, "资源 ID": 38, "相关 Chunk ID": 40, "预期行为": 10,
          "知识库ID": 38, "证据摘要": 46, "核实状态": 20, "拒答原因": 34,
          "人工审核标记": 10, "备注": 30}
for idx, h in enumerate(HEAD, 1):
    ws.column_dimensions[get_column_letter(idx)].width = widths[h]
ws.freeze_panes = "A2"
ws.auto_filter.ref = f"A1:{get_column_letter(len(HEAD))}{ws.max_row}"
ws.row_dimensions[1].height = 30

# ---------- 说明 sheet ----------
ws2 = wb.create_sheet("说明")
ws2.append(["RAG 评估测试集说明"])
ws2["A1"].font = Font(name="Microsoft YaHei", bold=True, size=14)
info = [
    ("数据集名称", "中医药知识资源管理与智能问答系统 - RAG 评估测试集"),
    ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ("目标知识库", f"{KB_NAME}（{KB_ID}）"),
    ("数据来源", "运行中的 PostgreSQL（knowledge_platform）与古籍文档切片 chunks；结构化答案直接取资源字段原文，文献答案为原文摘录归纳并标记待复核"),
    ("生成方式", "从真实资源表（herbs/prescriptions/theories/literatures）与 chunks 切片抽取；资源类题目的 Chunk ID 按系统规则计算：sha256('resource|类型|资源ID|KB|chunk_index')"),
    ("题目总数", str(len(rows))),
    ("分类统计", "；".join(f"{k} {v} 道" for k, v in cat.items())),
    ("重复题目检查", f"重复数：{len(dupes)}" + (f"（{dupes}）" if dupes else "（无）")),
    ("缺失来源检查", f"缺失资源/Chunk 的正常问答题：{len(missing)} 道" + (f"（编号 {missing}）" if missing else "（无）")),
    ("未执行项", "未调用问答接口、未执行评估、未修改任何业务代码"),
    ("已知限制 1", "结构化资源数据质量不均：草药/方剂的 effects 等字段为英文；理论 content 为英文释义。方剂类实际仅 5 个方剂有可用结构化字段，故只生成 5 道，未重复堆题。"),
    ("已知限制 2", "文献类只选用已真正写入 Milvus、可被检索的古籍切片（7 本）；PG 中大量未入库的 legacy 切片不纳入，避免“有题无证”。"),
    ("已知限制 3", "资源类 Chunk ID 为确定性 sha256（系统写入 Milvus 的向量主键），非 PostgreSQL chunks.id；文献类为真实 chunks.id。"),
    ("人工审核建议", "重点复核“人工审核标记=是”的条目（理论英文释义、文献摘录归纳、证据不足类），确认标准答案语义无误。"),
]
for k, v in info:
    ws2.append([k, v])
for row in ws2.iter_rows(min_row=2, max_row=ws2.max_row, max_col=2):
    row[0].font = Font(name="Microsoft YaHei", bold=True, size=10)
    row[1].font = Font(name="Microsoft YaHei", size=10)
    row[1].alignment = Alignment(wrap_text=True, vertical="top")
ws2.column_dimensions["A"].width = 22
ws2.column_dimensions["B"].width = 110

# ---------- 审核清单 sheet ----------
ws3 = wb.create_sheet("审核清单")
ws3.append(["编号", "知识类型", "对应资源名称", "核实状态", "需人工审核", "备注"])
for r in rows:
    if r["人工审核标记"] == "是" or r["备注"]:
        ws3.append([r["编号"], r["知识类型"], r["对应资源名称"],
                    r["核实状态"], r["人工审核标记"], r["备注"]])
for c in range(1, 7):
    cell = ws3.cell(row=1, column=c)
    cell.font = head_font
    cell.fill = head_fill
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
for row in ws3.iter_rows(min_row=2, max_row=ws3.max_row, max_col=6):
    for cell in row:
        cell.font = body_font
        cell.alignment = Alignment(vertical="top", wrap_text=True)
for idx, w in enumerate([6, 12, 18, 24, 12, 50], 1):
    ws3.column_dimensions[get_column_letter(idx)].width = w
ws3.freeze_panes = "A2"

OUT = "evaluation/dataset/questions.xlsx"
wb.save(OUT)

print("saved", OUT)
print("total", len(rows))
print("categories", dict(cat))
print("duplicates", dupes)
print("missing", missing)
