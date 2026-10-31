import collections
import os

from pymilvus import MilvusClient

c = MilvusClient(uri=os.environ.get("MILVUS_URI") or "http://milvus:19530")

try:
    total = c.query(collection_name="document_chunks", filter="chunk_index >= 0", output_fields=["count(*)"])
    print("TOTAL", total)
except Exception as e:
    print("total err", e)

rows = c.query(
    collection_name="document_chunks",
    filter="chunk_index >= 0",
    output_fields=["id", "doc_id", "kb_id", "resource_type", "source_type"],
    limit=16384,
)
print("SCANNED", len(rows))
print("by_resource_type", collections.Counter((r.get("resource_type") or "(none)") for r in rows))
print("by_source_type", collections.Counter((r.get("source_type") or "(none)") for r in rows))
print("by_kb", collections.Counter((r.get("kb_id") or "(none)")[:12] for r in rows).most_common(10))

doc_vecs = [r for r in rows if not r.get("resource_type")]
docs = collections.Counter(r.get("doc_id") for r in doc_vecs)
print("document_vectors", len(doc_vecs), "distinct_docs", len(docs))
print("top_docs", docs.most_common(10))
