"""汇总压力测试 JSON 结果为可读表格（在容器内执行）。"""

import json
import sys

rows = []
for name in ("small", "medium", "full"):
    try:
        with open(f"/tmp/stress_{name}.json", encoding="utf-8") as fh:
            rows.append(json.load(fh))
    except FileNotFoundError:
        continue

if not rows:
    print("没有找到压力测试结果")
    sys.exit(1)

header = [
    "规模", "总行数", "解析s", "发现s", "导入s", "重导s", "总耗时s",
    "峰值内存MB", "实体", "原始记录", "关系", "KG节点", "KG边",
    "失败", "重导新增实体", "重导后实体", "关联表",
]
print(" | ".join(header))
print("-" * 140)
for r in rows:
    print(
        " | ".join([
            str(r.get("scale")),
            str(r.get("total_rows")),
            str(r.get("parse_seconds")),
            str(r.get("discover_seconds")),
            str(r.get("import_seconds")),
            str(r.get("reimport_seconds")),
            str(r.get("total_seconds")),
            str(r.get("peak_mem_mb")),
            str(r.get("db_entities")),
            str(r.get("db_records")),
            str(r.get("db_relations")),
            str(r.get("db_kg_nodes")),
            str(r.get("db_kg_edges")),
            str(r.get("stats", {}).get("failed_rows")),
            str(r.get("reimport_entities_added")),
            str(r.get("db_entities_after_reimport")),
            str(len(r.get("junctions", []))),
        ])
    )

print("\n=== 各规模详情 ===")
for r in rows:
    print(f"\n[{r.get('scale')}] 注入坏行 {r.get('injected_bad_rows')} 条")
    print("  主键:", json.dumps(r.get("primary_keys"), ensure_ascii=False))
    print("  外键候选数:", r.get("foreign_keys"))
    print("  关联表:", json.dumps(r.get("junctions"), ensure_ascii=False))
    print("  解析错误:", r.get("parse_errors"))
    print("  行级错误 %s 条（坏行已跳过，其余照常入库）:" % r.get("row_error_count"))
    for item in (r.get("row_errors") or [])[:5]:
        print("    ", json.dumps(item, ensure_ascii=False))
    print("  失败明细:", json.dumps(r.get("stats", {}).get("errors"), ensure_ascii=False))
    print("  索引校验: %ss, 文档 %s, clean=%s" % (
        r.get("index_diff_seconds"), r.get("index_diff_documents"), r.get("index_clean")))
    print("  内存增长: %s MB" % r.get("mem_growth_mb"))
