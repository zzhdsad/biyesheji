"""统计指定 Milvus 集合的向量数（供 reset-database.ps1 调用，输出单个整数）。

集合名必须显式传入：未指定时输出 -1，避免把生产集合的统计误当开发环境结果。
"""

import os
import sys

from pymilvus import MilvusClient

name = sys.argv[1].strip() if len(sys.argv) > 1 else ""
if not name:
    print(-1)
    raise SystemExit(2)

uri = sys.argv[2] if len(sys.argv) > 2 else (os.getenv("MILVUS_URI") or "http://milvus:19530")
try:
    client = MilvusClient(uri=uri)
    if not client.has_collection(name):
        print(-1)
    else:
        print(client.get_collection_stats(name).get("row_count", 0))
except Exception:
    print(-1)
