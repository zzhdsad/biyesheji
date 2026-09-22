"""向量库抽象：Milvus（TECH_DESIGN document_chunks 集合）/ 内存实现可切换（依赖倒置）。

Collection: document_chunks
- 标量：id(PK), doc_id, kb_id, chunk_index, content, page_num, title_path
- dense_vector (FLOAT_VECTOR, 1024d) → HNSW/COSINE
- sparse_vector (SPARSE_FLOAT_VECTOR) → SPARSE_INVERTED_INDEX/IP
- 动态标量（enable_dynamic_field=True 的集合）：source_type(VARCHAR)、
  credibility_level(INT64)，来源可信度标注随向量写入、检索带回；
  老集合（未开启动态字段）自动降级：不写/不取这两个字段，由 PG enrichment 兜底
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from loguru import logger

from src.core.config import settings


class VectorStoreError(Exception):
    """向量库层异常。"""


@dataclass
class VectorRow:
    """Milvus 插入行（与 chunks 表字段对齐 + 双路向量）。

    Task-008 Stage 4-2 扩展：resource_type/resource_id/resource_name/era 用于
    传统资源（Herb/Prescription/Theory/Literature）向量行携带资源元数据；
    均为可选字段，默认 None，Document 向量化路径不填这些字段，向后兼容。
    Stage 4-3 将扩展 Milvus insert() 把这些字段作为动态字段写入；本阶段不写入。
    """

    id: str
    doc_id: str
    kb_id: str
    chunk_index: int
    content: str
    page_num: int | None
    title_path: str | None
    dense_vector: list[float]
    sparse_vector: dict[int, float] = field(default_factory=dict)
    # 来源可信度（动态字段；None/0 表示该文档未标注，写入时降级为缺省值）
    source_type: str | None = None
    credibility_level: int | None = None
    # 资源元数据（Stage 4-2 扩展；Document 向量行不填，保持 None）
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    era: str | None = None


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

    @abstractmethod
    def search(
        self, query_vector: list[float], kb_ids: list[str], top_k: int
    ) -> list[dict]:
        """稠密向量检索（强制 kb_id 过滤，TECH_DESIGN 权限隔离）。

        Returns:
            [{id, doc_id, kb_id, chunk_index, content, page_num, title_path, score}]，
            score 为相似度（越大越相关），按 score 降序。
        """

    @abstractmethod
    def search_sparse(
        self, query_sparse: dict, kb_ids: list[str], top_k: int
    ) -> list[dict]:
        """稀疏向量检索（BM25 关键词召回，强制 kb_id 过滤）。

        query_sparse 为 BGE-M3 lexical weights（{token_id: weight}）。
        返回结构同 search（score 为稀疏内点积，越大越相关）。
        """


class MilvusStore(BaseVectorStore):
    """Milvus 2.4 实现（MilvusClient API）。连接惰性建立，失败抛 VectorStoreError。"""

    COLLECTION = settings.MILVUS_COLLECTION

    def __init__(self, uri: str | None = None) -> None:
        self._uri = uri or settings.MILVUS_URI
        self._client = None
        # 动态字段能力缓存：None=未探测 / True=已开启 / False=老集合不支持
        self._dynamic_enabled: bool | None = None

    def _supports_dynamic_fields(self) -> bool:
        """探测集合是否开启动态字段（老集合无法事后加列，需降级）。结果缓存。"""
        if self._dynamic_enabled is not None:
            return self._dynamic_enabled
        client = self._get_client()
        try:
            if not client.has_collection(self.COLLECTION):
                # 集合不存在：ensure_collection 会以动态字段方式创建
                self._dynamic_enabled = True
            else:
                desc = client.describe_collection(self.COLLECTION)
                self._dynamic_enabled = bool(desc.get("enable_dynamic_field", False))
        except Exception as exc:
            logger.warning(f"Milvus 动态字段能力探测失败，按不支持降级：{exc}")
            self._dynamic_enabled = False
        return self._dynamic_enabled

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
            self._supports_dynamic_fields()
            return
        try:
            from pymilvus import DataType

            # 动态字段开启：来源可信度标量（source_type/credibility_level）
            # 直接随实体写入，无需显式建列，避免后续加字段时重建集合
            schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
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
            self._dynamic_enabled = True
            logger.info(
                f"Milvus 集合已创建：{self.COLLECTION}（dense HNSW + sparse 倒排 + 动态字段）"
            )
        except Exception as exc:
            raise VectorStoreError(f"创建 Milvus 集合失败：{exc}") from exc

    def insert(self, rows: list[VectorRow]) -> int:
        if not rows:
            return 0
        client = self._get_client()
        # 老集合（未开启动态字段）无法写入来源标量：降级跳过，来源信息由 PG 兜底
        dynamic = self._supports_dynamic_fields()
        data = []
        for r in rows:
            item = {
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
            if dynamic:
                # 动态字段缺省值与 page_num 约定一致："" / 0 表示未标注
                item["source_type"] = r.source_type or ""
                item["credibility_level"] = r.credibility_level or 0
                # TASK-008 Stage 4-3：Resource 元数据作为动态字段写入
                # Document 向量行 resource_* 均为 None → 写入空串，与 source_type 约定一致
                item["resource_type"] = r.resource_type or ""
                item["resource_id"] = r.resource_id or ""
                item["resource_name"] = r.resource_name or ""
                item["era"] = r.era or ""
            data.append(item)
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
        output_fields = [
            "id",
            "doc_id",
            "kb_id",
            "chunk_index",
            "content",
            "page_num",
            "title_path",
            "dense_vector",
            "sparse_vector",
        ]
        if self._supports_dynamic_fields():
            output_fields += [
                "source_type",
                "credibility_level",
                "resource_type",
                "resource_id",
                "resource_name",
                "era",
            ]
        rows = client.query(
            collection_name=self.COLLECTION,
            filter=f'doc_id == "{doc_id}"',
            output_fields=output_fields,
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
                "source_type": r.get("source_type") or None,
                "credibility_level": r.get("credibility_level") or None,
                "resource_type": r.get("resource_type") or None,
                "resource_id": r.get("resource_id") or None,
                "resource_name": r.get("resource_name") or None,
                "era": r.get("era") or None,
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

    def search(
        self, query_vector: list[float], kb_ids: list[str], top_k: int
    ) -> list[dict]:
        client = self._get_client()
        if not client.has_collection(self.COLLECTION) or not kb_ids:
            return []
        # 强制 kb_id 过滤（TECH_DESIGN：所有检索加 kb_id 过滤，禁止越权访问）
        kb_filter = "kb_id in [" + ", ".join(f'"{k}"' for k in kb_ids) + "]"
        output_fields = [
            "id",
            "doc_id",
            "kb_id",
            "chunk_index",
            "content",
            "page_num",
            "title_path",
        ]
        if self._supports_dynamic_fields():
            output_fields += [
                "source_type",
                "credibility_level",
                "resource_type",
                "resource_id",
                "resource_name",
                "era",
            ]
        try:
            results = client.search(
                collection_name=self.COLLECTION,
                data=[query_vector],
                anns_field="dense_vector",
                limit=top_k,
                filter=kb_filter,
                output_fields=output_fields,
            )[0]
        except Exception as exc:
            raise VectorStoreError(f"Milvus 检索失败：{exc}") from exc
        return [self._parse_hit(hit) for hit in results]

    def search_sparse(
        self, query_sparse: dict, kb_ids: list[str], top_k: int
    ) -> list[dict]:
        """稀疏向量检索（BM25 关键词召回，SPARSE_INVERTED_INDEX + IP）。"""
        client = self._get_client()
        if not client.has_collection(self.COLLECTION) or not kb_ids or not query_sparse:
            return []
        kb_filter = "kb_id in [" + ", ".join(f'"{k}"' for k in kb_ids) + "]"
        output_fields = [
            "id",
            "doc_id",
            "kb_id",
            "chunk_index",
            "content",
            "page_num",
            "title_path",
        ]
        if self._supports_dynamic_fields():
            output_fields += [
                "source_type",
                "credibility_level",
                "resource_type",
                "resource_id",
                "resource_name",
                "era",
            ]
        try:
            results = client.search(
                collection_name=self.COLLECTION,
                data=[query_sparse],
                anns_field="sparse_vector",
                limit=top_k,
                filter=kb_filter,
                output_fields=output_fields,
            )[0]
        except Exception as exc:
            raise VectorStoreError(f"Milvus 稀疏检索失败：{exc}") from exc
        return [self._parse_hit(hit) for hit in results]

    @staticmethod
    def _parse_hit(hit: dict) -> dict:
        """标准化检索命中：page_num=0/source 缺省值还原为 None。"""
        entity = hit.get("entity", {})
        page_num = entity.get("page_num")
        source_type = entity.get("source_type")
        credibility_level = entity.get("credibility_level")
        return {
            "id": entity.get("id"),
            "doc_id": entity.get("doc_id"),
            "kb_id": entity.get("kb_id"),
            "chunk_index": entity.get("chunk_index"),
            "content": entity.get("content", ""),
            # page_num=0 表示暂无页码（见 insert 注释），对调用方还原为 None
            "page_num": page_num if page_num else None,
            "title_path": entity.get("title_path") or None,
            # 动态字段缺省值 "" / 0 同样还原为 None（未标注）
            "source_type": source_type or None,
            "credibility_level": credibility_level or None,
            # TASK-008 Stage 4-3：Resource 元数据带回（空串还原为 None）
            "resource_type": entity.get("resource_type") or None,
            "resource_id": entity.get("resource_id") or None,
            "resource_name": entity.get("resource_name") or None,
            "era": entity.get("era") or None,
            "score": float(hit.get("distance", 0.0)),
        }


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

    def search(  # noqa: D102
        self, query_vector: list[float], kb_ids: list[str], top_k: int
    ) -> list[dict]:
        kb_set = set(kb_ids)

        def _cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

        scored = [
            (_cosine(query_vector, v.dense_vector), v)
            for v in self._rows.values()
            if v.kb_id in kb_set
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "id": v.id,
                "doc_id": v.doc_id,
                "kb_id": v.kb_id,
                "chunk_index": v.chunk_index,
                "content": v.content,
                "page_num": v.page_num,
                "title_path": v.title_path,
                "source_type": v.source_type,
                "credibility_level": v.credibility_level,
                "resource_type": v.resource_type,
                "resource_id": v.resource_id,
                "resource_name": v.resource_name,
                "era": v.era,
                "score": round(score, 6),
            }
            for score, v in scored[:top_k]
        ]

    def search_sparse(  # noqa: D102
        self, query_sparse: dict, kb_ids: list[str], top_k: int
    ) -> list[dict]:
        kb_set = set(kb_ids)
        if not query_sparse:
            return []

        def _ip(q: dict, d: dict) -> float:
            # 稀疏内点积：仅对 query 中出现的 token 求和（匹配 Milvus IP 度量）
            return sum(w * d.get(t, 0.0) for t, w in q.items())

        scored = [
            (_ip(query_sparse, v.sparse_vector), v)
            for v in self._rows.values()
            if v.kb_id in kb_set and v.sparse_vector
        ]
        scored = [(s, v) for s, v in scored if s > 0]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "id": v.id,
                "doc_id": v.doc_id,
                "kb_id": v.kb_id,
                "chunk_index": v.chunk_index,
                "content": v.content,
                "page_num": v.page_num,
                "title_path": v.title_path,
                "source_type": v.source_type,
                "credibility_level": v.credibility_level,
                "resource_type": v.resource_type,
                "resource_id": v.resource_id,
                "resource_name": v.resource_name,
                "era": v.era,
                "score": round(score, 6),
            }
            for score, v in scored[:top_k]
        ]

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
                "source_type": r.source_type,
                "credibility_level": r.credibility_level,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "resource_name": r.resource_name,
                "era": r.era,
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
