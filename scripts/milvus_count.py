"""统计 Milvus 集合向量数（供 reset-database.ps1 调用，输出单个整数）。"""

import os

from pymilvus import MilvusClient

uri = os.getenv("MILVUS_URI") or "http://milvus:19530"
name = os.getenv("MILVUS_COLLECTION") or "document_chunks"
try:
    client = MilvusClient(uri=uri)
    if not client.has_collection(name):
        print(-1)
    else:
        print(client.get_collection_stats(name).get("row_count", 0))
except Exception:
    print(-1)
