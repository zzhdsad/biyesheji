"""清空 Milvus 集合全部向量（供 reset-database.ps1 调用，分批删除避免超限）。"""

import os

from pymilvus import MilvusClient

uri = os.getenv("MILVUS_URI") or "http://milvus:19530"
name = os.getenv("MILVUS_COLLECTION") or "document_chunks"
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
