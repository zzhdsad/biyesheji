"""向量库抽象：Milvus（TECH_DESIGN document_chunks 集合）/ 内存实现可切换（依赖倒置）。

Collection: document_chunks
- 标量：id(PK), doc_id, kb_id, chunk_index, content, page_num, title_path
- dense_vector (FLOAT_VECTOR, 1024d) → HNSW/COSINE
- sparse_vector (SPARSE_FLOAT_VECTOR) → SPARSE_INVERTED_INDEX/IP
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from loguru import logger

from src.core.config import settings


class VectorStoreError(Exception):
    """向量库层异常。"""


@dataclass
class VectorRow:
    """Milvus 插入行（与 chunks 表字段对齐 + 双路向量）。"""

    id: str
    doc_id: str
    kb_id: str
    chunk_index: int
    content: str
    page_num: int | None
    title_path: str | None
    dense_vector: list[float]
    sparse_vector: dict[int, float] = field(default_factory=dict)


class BaseVectorStore(ABC):
    """向量库接口：集合管理、幂等写入、按文档删除。"""

    @abstractmethod
    def ensure_collection(self) -> None:
        """确保集合与索引存在（幂等）。"""

    @abstractmethod
    def insert(self, rows: list[VectorRow]) -> int:
        """批量插入向量，返回成功条数。"""

    @abstractmethod
    def delete_by_doc(self, doc_id: str) -> None:
        """删除指定文档的全部向量（重新向量化前调用，幂等）。"""

    @abstractmethod
    def count_by_doc(self, doc_id: str) -> int:
        """统计指定文档的向量条数（验证入库结果）。"""


class MilvusStore(BaseVectorStore):
    """Milvus 2.4 实现（MilvusClient API）。连接惰性建立，失败抛 VectorStoreError。"""

    COLLECTION = settings.MILVUS_COLLECTION

    def __init__(self, uri: str | None = None) -> None:
        self._uri = uri or settings.MILVUS_URI
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise VectorStoreError("未安装 pymilvus：pip install pymilvus") from exc
            try:
                self._client = MilvusClient(uri=self._uri)
            except Exception as exc:
                raise VectorStoreError(f"Milvus 连接失败（{self._uri}）：{exc}") from exc
        return self._client

    def ensure_collection(self) -> None:
        client = self._get_client()
        if client.has_collection(self.COLLECTION):
            return
        try:
            from pymilvus import DataType

            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
            schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("content", DataType.VARCHAR, max_length=32768)
            # 注：不用 nullable 字段（pymilvus 2.4 插入 None 兼容性差），
            # page_num=0 表示暂无页码，title_path="" 表示无标题路径
            schema.add_field("page_num", DataType.INT64)
            schema.add_field("title_path", DataType.VARCHAR, max_length=1024)
            schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=settings.MILVUS_DIM)
            schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="dense_vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            index_params.add_index(
                field_name="sparse_vector",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )
            client.create_collection(
                collection_name=self.COLLECTION, schema=schema, index_params=index_params
            )
            logger.info(f"Milvus 集合已创建：{self.COLLECTION}（dense HNSW + sparse 倒排）")
        except Exception as exc:
            raise VectorStoreError(f"创建 Milvus 集合失败：{exc}") from exc

    def insert(self, rows: list[VectorRow]) -> int:
        if not rows:
            return 0
        client = self._get_client()
        data = [
            {
                "id": r.id,
                "doc_id": r.doc_id,
                "kb_id": r.kb_id,
                "chunk_index": r.chunk_index,
                "content": r.content,
                "page_num": r.page_num or 0,
                "title_path": r.title_path or "",
                "dense_vector": r.dense_vector,
                "sparse_vector": r.sparse_vector,
            }
            for r in rows
        ]
        try:
            # 分批插入，避免超大 payload
            total = 0
            batch = settings.VECTORIZE_BATCH_SIZE
            for i in range(0, len(data), batch):
                res = client.insert(collection_name=self.COLLECTION, data=data[i : i + batch])
                total += res.get("insert_count", 0)
            # 立即落盘，保证后续查询可见（growing segment 有可见性延迟）
            client.flush(self.COLLECTION)
            return total
        except Exception as exc:
            raise VectorStoreError(f"Milvus 插入失败：{exc}") from exc

    def query_rows(self, doc_id: str) -> list[dict]:
        """查询指定文档的全部向量行（标准化为 dict，供校验与调试）。"""
        client = self._get_client()
        if not client.has_collection(self.COLLECTION):
            return []
        rows = client.query(
            collection_name=self.COLLECTION,
            filter=f'doc_id == "{doc_id}"',
            output_fields=[
                "id",
                "doc_id",
                "kb_id",
                "chunk_index",
                "content",
                "page_num",
                "title_path",
                "dense_vector",
                "sparse_vector",
            ],
            limit=16384,
        )
        return [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "kb_id": r["kb_id"],
                "chunk_index": r["chunk_index"],
                "content": r["content"],
                "page_num": r["page_num"],
                "title_path": r["title_path"],
                "dense_vector": [float(x) for x in r["dense_vector"]],
                "sparse_vector": {int(k): float(v) for k, v in r["sparse_vector"].items()},
            }
            for r in rows
        ]

    def delete_by_doc(self, doc_id: str) -> None:
        client = self._get_client()
        if not client.has_collection(self.COLLECTION):
            return
        try:
            client.delete(collection_name=self.COLLECTION, filter=f'doc_id == "{doc_id}"')
        except Exception as exc:
            raise VectorStoreError(f"Milvus 删除失败 doc_id={doc_id}：{exc}") from exc

    def count_by_doc(self, doc_id: str) -> int:
        client = self._get_client()
        if not client.has_collection(self.COLLECTION):
            return 0
        try:
            rows = client.query(
                collection_name=self.COLLECTION,
                filter=f'doc_id == "{doc_id}"',
                output_fields=["count(*)"],
            )
            return int(rows[0]["count(*)"]) if rows else 0
        except Exception as exc:
            raise VectorStoreError(f"Milvus 查询失败 doc_id={doc_id}：{exc}") from exc


class InMemoryVectorStore(BaseVectorStore):
    """内存实现：开发/单元测试用（无 Milvus 服务时验证全流程）。"""

    def __init__(self) -> None:
        self._rows: dict[str, VectorRow] = {}

    def ensure_collection(self) -> None:  # noqa: D102
        pass

    def insert(self, rows: list[VectorRow]) -> int:  # noqa: D102
        for r in rows:
            self._rows[r.id] = r
        return len(rows)

    def delete_by_doc(self, doc_id: str) -> None:  # noqa: D102
        self._rows = {k: v for k, v in self._rows.items() if v.doc_id != doc_id}

    def count_by_doc(self, doc_id: str) -> int:  # noqa: D102
        return sum(1 for v in self._rows.values() if v.doc_id == doc_id)

    def query_rows(self, doc_id: str) -> list[dict]:  # noqa: D102
        return [
            {
                "id": r.id,
                "doc_id": r.doc_id,
                "kb_id": r.kb_id,
                "chunk_index": r.chunk_index,
                "content": r.content,
                "page_num": r.page_num,
                "title_path": r.title_path,
                "dense_vector": list(r.dense_vector),
                "sparse_vector": dict(r.sparse_vector),
            }
            for r in self._rows.values()
            if r.doc_id == doc_id
        ]

    def all_rows(self) -> list[VectorRow]:
        return list(self._rows.values())


_store: BaseVectorStore | None = None


def get_vector_store() -> BaseVectorStore:
    """向量库工厂（进程级单例）。"""
    global _store
    if _store is None:
        _store = MilvusStore()
    return _store


def set_vector_store(store: BaseVectorStore) -> None:
    """替换全局向量库实现（测试注入用）。"""
    global _store
    _store = store
