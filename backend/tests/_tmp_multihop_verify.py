"""P0-2 / P0-3 针对性验证（临时脚本）：纯函数层，不写库、不解析、不向量化。

覆盖任务要求的五项场景 + 循环/菱形去重 + 不可达路径提示 + 单跳兼容。
复用 _tmp_caps_verify.py 的 GroupFileSchema / relation_id / check 机制。
"""

from __future__ import annotations

import sys
import uuid

from src.core.config import settings
from src.utils.structured_relation import (
    GroupFileSchema,
    LinkBuilder,
    build_linked_records,
    relation_id,
)

FAIL: list[str] = []
_NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        FAIL.append(name)


def schema(doc_id: str, file_name: str, fields, rows, alias="", order_no=0):
    records = [
        {
            "record_id": str(uuid.uuid5(_NS, f"{doc_id}:{i}")),
            "row_no": i + 2,
            "sheet_name": None,
            "json_path": None,
            "payload": p,
        }
        for i, p in enumerate(rows)
    ]
    return GroupFileSchema(
        doc_id=doc_id,
        file_name=file_name,
        alias=alias,
        order_no=order_no,
        fields=[{"name": n, "type": t, "null_count": 0, "count": len(rows)} for n, t in fields],
        records=records,
        total_rows=len(rows),
    )


def rel(from_doc, from_field, to_doc, to_field, kind="many_to_one", confirmed=True):
    return {
        "id": relation_id(from_doc, from_field, to_doc, to_field),
        "from_doc_id": from_doc,
        "from_field": from_field,
        "to_doc_id": to_doc,
        "to_field": to_field,
        "kind": kind,
        "confirmed": confirmed,
    }


A, B, C, D, E = (x * 32 for x in "abcde")


def rec_ids(items):
    return [str(x.get("record_id")) for x in items]


# ── 场景 1：一对多超过 20 条，溯源全部保留，文本标注省略 ──────────────────────
def test_one_to_many_over_20():
    a = schema(A, "关联表.tsv", [("药材编号", "integer"), ("剂量", "integer")],
               [{"药材编号": 1, "剂量": 5}], alias="关联")
    b = schema(B, "药材表.tsv", [("编号", "integer"), ("名称", "string")],
               [{"编号": 1, "名称": f"药材{i}"} for i in range(30)], alias="药材", order_no=1)
    r = rel(A, "药材编号", B, "编号", kind="one_to_many")
    builder = LinkBuilder([a, b], [r], max_hops=2)
    row = builder.build(a.records[0])
    check("1 parts 保留全部 30 条匹配 + 锚点", len(row["parts"]) == 31, f"parts={len(row['parts'])}")
    check("1 sources 保留全部 30 条 + 锚点", len(row["sources"]) == 31, f"sources={len(row['sources'])}")
    b_ids = set(rec_ids(b.records))
    src_ids = {str(s["record_id"]) for s in row["sources"] if s["doc_id"] == B}
    check("1 全部匹配记录来源齐全", src_ids == b_ids, f"missing={len(b_ids - src_ids)}")
    txt = row["knowledge_text"]
    check("1 文本标注展开数量", "文本展开 20 条" in txt, txt[-80:])
    check("1 文本标注省略数量", "省略 10 条" in txt)
    check("1 文本提示完整列表见详情", "完整列表见详情" in txt)
    check("1 提示含匹配总数与详情指引",
          any("匹配到 30 条" in x and "完整列表见详情" in x for x in row["anomalies"]))
    check("1 文本只展开前 20 个标签", "药材19" in txt and "药材20" not in txt)
    check("1 溯源未截断（第 30 条仍在 parts）",
          any(p["values"].get("名称") == "药材29" for p in row["parts"]))


# ── 场景 2：A→B→C 两跳，C 的信息进入最终知识记录 ─────────────────────────────
def test_two_hop():
    a = schema(A, "甲.tsv", [("b_key", "integer"), ("说明", "string")],
               [{"b_key": 1, "说明": "起点"}], alias="甲")
    b = schema(B, "乙.tsv", [("b_id", "integer"), ("c_key", "integer"), ("名称", "string")],
               [{"b_id": 1, "c_key": 7, "名称": "中间B"}], alias="乙", order_no=1)
    c = schema(C, "丙.tsv", [("c_id", "integer"), ("名称", "string")],
               [{"c_id": 7, "名称": "目标C"}], alias="丙", order_no=2)
    r1 = rel(A, "b_key", B, "b_id")
    r2 = rel(B, "c_key", C, "c_id")
    row = LinkBuilder([a, b, c], [r1, r2], max_hops=2).build(a.records[0])
    txt = row["knowledge_text"]
    check("2 文本含锚点上下文", "起点" in txt, txt)
    check("2 文本含中间跳 B", "中间B" in txt)
    check("2 文本含末跳 C", "目标C" in txt)
    check("2 parts 含 A/B/C 三跳", len(row["parts"]) == 3, f"parts={len(row['parts'])}")
    c_part = next(p for p in row["parts"] if p["doc_id"] == C)
    check("2 C 溯源带 hop=2", c_part.get("hop") == 2)
    check("2 C 溯源带 via_relation_id", c_part.get("via_relation_id") == r2["id"])
    c_src = next(s for s in row["sources"] if s["doc_id"] == C)
    check("2 C 来源角色与关联字段", c_src["role"] == "linked" and c_src["join_field"] == "c_id"
          and c_src.get("hop") == 2)
    check("2 relation_ids 含两跳关系", r1["id"] in row["relation_ids"] and r2["id"] in row["relation_ids"])
    check("2 文本体现经由关系", "经「乙」" in txt, txt)
    # max_hops=1 时不展开 C
    row1 = LinkBuilder([a, b, c], [r1, r2], max_hops=1).build(a.records[0])
    check("2 max_hops=1 不展开 C", all(p["doc_id"] != C for p in row1["parts"])
          and "目标C" not in row1["knowledge_text"])


# ── 场景 3：A→B→A 循环，不无限展开、不重复生成 ───────────────────────────────
def test_cycle():
    a = schema(A, "甲.tsv", [("a_id", "integer"), ("b_key", "integer"), ("名称", "string")],
               [{"a_id": 1, "b_key": 1, "名称": "甲"}], alias="甲")
    b = schema(B, "乙.tsv", [("b_id", "integer"), ("a_key", "integer"), ("名称", "string")],
               [{"b_id": 1, "a_key": 1, "名称": "乙"}], alias="乙", order_no=1)
    r1 = rel(A, "b_key", B, "b_id")
    r2 = rel(B, "a_key", A, "a_id")
    for hops in (2, 3, 5):
        builder = LinkBuilder([a, b], [r1, r2], anchor_doc_id=A, max_hops=hops)
        row = builder.build(a.records[0])  # 若无限递归此处不会返回
        a_count = sum(1 for p in row["parts"] if p["doc_id"] == A)
        check(f"3 hops={hops} 循环终止且锚点不重复", len(row["parts"]) == 2 and a_count == 1,
              f"parts={len(row['parts'])} a_count={a_count}")
        check(f"3 hops={hops} 明确提示循环", any("循环" in x for x in row["anomalies"]))
    check("3 循环只生成一条关联记录", builder.stats()["record_count"] == 1)


# ── 场景 4：未确认关系不进入正式关联知识 ─────────────────────────────────────
def test_unconfirmed_excluded():
    a = schema(A, "甲.tsv", [("b_key", "integer")], [{"b_key": 1}], alias="甲")
    b = schema(B, "乙.tsv", [("b_id", "integer"), ("c_key", "integer"), ("名称", "string")],
               [{"b_id": 1, "c_key": 7, "名称": "乙"}], alias="乙", order_no=1)
    c = schema(C, "丙.tsv", [("c_id", "integer"), ("名称", "string")],
               [{"c_id": 7, "名称": "丙"}], alias="丙", order_no=2)
    r1 = rel(A, "b_key", B, "b_id", confirmed=True)
    r2 = rel(B, "c_key", C, "c_id", confirmed=False)
    row = LinkBuilder([a, b, c], [r1, r2], max_hops=2).build(a.records[0])
    check("4 未确认关系不展开 C", all(p["doc_id"] != C for p in row["parts"])
          and "丙" not in row["knowledge_text"])
    check("4 relation_ids 只含已确认关系", row["relation_ids"] == [r1["id"]])
    rows, stats = build_linked_records([a, b], [rel(A, "b_key", B, "b_id", confirmed=False)])
    check("4 全未确认则跳过生成", rows == [] and stats.get("skipped") == "没有已确认的关系",
          str(stats))


# ── 场景 5：原有单跳关联仍然正常（兼容） ─────────────────────────────────────
def test_single_hop_compat():
    a = schema(A, "甲.tsv", [("b_key", "integer"), ("说明", "string")],
               [{"b_key": 1, "说明": "x"}], alias="甲")
    b = schema(B, "乙.tsv", [("b_id", "integer"), ("名称", "string")],
               [{"b_id": 1, "名称": f"药{i}"} for i in range(3)], alias="乙", order_no=1)
    r = rel(A, "b_key", B, "b_id", kind="one_to_many")
    row = LinkBuilder([a, b], [r], max_hops=1).build(a.records[0])
    check("5 单跳 parts=锚点+3", len(row["parts"]) == 4)
    check("5 单跳文本格式不变", row["knowledge_text"] == "说明：x；乙（匹配 3 条）：药0、药1、药2。",
          row["knowledge_text"])
    check("5 单跳无省略标注", "省略" not in row["knowledge_text"])
    check("5 单跳 parts 不带 hop 键", all("hop" not in p for p in row["parts"]))
    check("5 单跳 sources 不带 hop 键", all("hop" not in s for s in row["sources"]))
    row2 = LinkBuilder([a, b], [r], max_hops=2).build(a.records[0])
    check("5 无更深关系时 hops=1 与 2 结果一致", row == row2)


# ── 附加：菱形路径去重 / 不可达提示 / 配置默认 / 分批独立 ─────────────────────
def test_extras():
    # 菱形：A→B→D 与 A→C→D，D 只保留一次
    a = schema(A, "甲.tsv", [("b_key", "integer"), ("c_key", "integer")],
               [{"b_key": 1, "c_key": 1}], alias="甲")
    b = schema(B, "乙.tsv", [("b_id", "integer"), ("d_key", "integer"), ("名称", "string")],
               [{"b_id": 1, "d_key": 9, "名称": "乙"}], alias="乙", order_no=1)
    c = schema(C, "丙.tsv", [("c_id", "integer"), ("d_key", "integer"), ("名称", "string")],
               [{"c_id": 1, "d_key": 9, "名称": "丙"}], alias="丙", order_no=2)
    d = schema(D, "丁.tsv", [("d_id", "integer"), ("名称", "string")],
               [{"d_id": 9, "名称": "丁"}], alias="丁", order_no=3)
    rels = [rel(A, "b_key", B, "b_id"), rel(A, "c_key", C, "c_id"),
            rel(B, "d_key", D, "d_id"), rel(C, "d_key", D, "d_id")]
    row = LinkBuilder([a, b, c, d], rels, anchor_doc_id=A, max_hops=2).build(a.records[0])
    d_count = sum(1 for p in row["parts"] if p["doc_id"] == D)
    check("X 菱形路径 D 只保留一次", len(row["parts"]) == 4 and d_count == 1,
          f"parts={len(row['parts'])} d={d_count}")
    check("X 菱形文本含丁", "丁" in row["knowledge_text"])

    # 不可达的已确认关系 → 统计明确提示
    e = schema(E, "戊.tsv", [("e_id", "integer")], [{"e_id": 1}], alias="戊", order_no=4)
    far = rel(D, "d_id", E, "e_id")  # D 深度=2=max_hops，D→E 超跳数 → 不可达提示
    builder = LinkBuilder([a, b, c, d, e], rels + [far], anchor_doc_id=A, max_hops=2)
    stats = builder.stats()
    check("X 不可达/超跳数关系有提示", any(u["id"] == far["id"] for u in stats.get("unused_relations", [])),
          str(stats.get("unused_relations")))

    # 配置默认与钳制
    check("X 配置默认跳数为 2", settings.STRUCTURED_RELATION_MAX_HOPS == 2)
    check("X 默认 max_hops=2", LinkBuilder([a, b], [rels[0]]).max_hops == 2)
    check("X max_hops=0 钳制为 1", LinkBuilder([a, b], [rels[0]], max_hops=0).max_hops == 1)

    # 分批独立：两条锚点各自展开，计数累加，溯源互不污染
    a2 = schema(A, "甲.tsv", [("b_key", "integer"), ("c_key", "integer")],
                [{"b_key": 1, "c_key": 1}, {"b_key": 1, "c_key": 1}], alias="甲")
    builder = LinkBuilder([a2, b, c, d], rels, anchor_doc_id=A, max_hops=2)
    batch = builder.build_many(a2.records)
    check("X 分批生成两条独立记录", len(batch) == 2 and builder.stats()["record_count"] == 2
          and builder.stats()["read_count"] == 2)
    check("X 两条记录溯源各自完整", all(len(r["parts"]) == 4 for r in batch))
    whole, _ = build_linked_records([a2, b, c, d], rels, anchor_doc_id=A, max_hops=2)
    check("X 分批 == 整份内存结果", [r["knowledge_text"] for r in batch] == [r["knowledge_text"] for r in whole])


def main() -> int:
    test_one_to_many_over_20()
    test_two_hop()
    test_cycle()
    test_unconfirmed_excluded()
    test_single_hop_compat()
    test_extras()
    print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + str(len(FAIL))}")
    for name in FAIL:
        print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())