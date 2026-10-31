"""清空指定 Milvus 集合的全部向量（供 reset-database.ps1 调用，分批删除避免超限）。

必须显式传入集合名：不再回退到默认的 document_chunks，避免环境配置缺失时误清生产集合。
"""

import os
import sys

from pymilvus import MilvusClient

name = sys.argv[1].strip() if len(sys.argv) > 1 else ""
if not name:
    print("refused: 未显式指定 Milvus 集合，拒绝清理", file=sys.stderr)
    raise SystemExit(2)

uri = sys.argv[2] if len(sys.argv) > 2 else (os.getenv("MILVUS_URI") or "http://milvus:19530")
client = MilvusClient(uri=uri)
if not client.has_collection(name):
    print("no-collection")
else:
    removed = 0
    while True:
        rows = client.query(collection_name=name, filter="", output_fields=["id"], limit=10000)
        if not rows:
            break
        client.delete(collection_name=name, pks=[r["id"] for r in rows])
        removed += len(rows)
    print(removed)
