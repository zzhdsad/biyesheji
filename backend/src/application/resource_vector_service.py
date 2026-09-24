"""TASK-008 Stage 4-2：Resource → Canonical Text → Chunk → BGE-M3 → VectorRow。

将 Herb/Prescription/Theory/Literature 结构化资源转换为 Milvus VectorRow，
为下一阶段（Stage 4-3）Milvus 写入提供标准化数据。本阶段不写入 Milvus。

设计要点：
- Canonical Text：按资源类型字段映射生成纯文本，空字段跳过（不输出 None）；
  category_id / tag 等内部 ID 不拼入文本。
- Chunk：Herb/Prescription 1 Resource → 1 Vector；Theory/Literature ≤ 6000 字符
  → 1 Vector，超长复用 chunking.py。chunk_index 从 0 开始，page_num 恒为 None。
- Embedding：复用项目现有 BGE-M3 embedding service（BaseEmbedding 接口），
  不创建新模型，不修改现有 Document Vectorization。
- VectorRow id：sha256(resource_type|resource_id|kb_id|chunk_index) 十六进制，
  64 字符恰好满足 Milvus VARCHAR(64)，稳定可重复。
- doc_id：sha256(resource|resource_type|resource_id|kb_id) 十六进制，
  按 KB+Resource 隔离，取消挂载/重新向量化时按 doc_id 幂等清理，不误删其他 KB。
- source_type/era/credibility_level：资源表无受控枚举字段，不自行猜测；
  调用方可经显式参数传入 source_type/era，credibility_level 由 source_meta
  固定映射推导；未传入时为 None。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from src.core.source_meta import credibility_for, is_valid_era, is_valid_source_type
from src.domain.models import Herb, Literature, Prescription, Theory
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.milvus_store import (
    BaseVectorStore,
    VectorRow,
    VectorStoreError,
    get_vector_store,
)
from src.utils.chunking import chunk_document

# 长文本切片阈值（字符数，非 token）：
# Theory/Literature 超过此值时复用 chunking.py 的结构感知切片，避免单向量过长。
LONG_TEXT_THRESHOLD = 6000

# 受控资源类型词表（与 KnowledgeBaseResource.resource_type 对齐）。
RESOURCE_TYPES: tuple[str, ...] = ("herb", "prescription", "theory", "literature")


def _row_from_dict(raw: dict) -> VectorRow:
    """快照 dict → VectorRow（BUG-045 补偿回写用）。"""
    return VectorRow(
        id=str(raw.get("id")),
        doc_id=str(raw.get("doc_id")),
        kb_id=str(raw.get("kb_id")),
        chunk_index=int(raw.get("chunk_index") or 0),
        content=raw.get("content") or "",
        page_num=raw.get("page_num"),
        title_path=raw.get("title_path"),
        dense_vector=[float(x) for x in (raw.get("dense_vector") or [])],
        sparse_vector={
            int(k): float(v) for k, v in (raw.get("sparse_vector") or {}).items()
        },
        source_type=raw.get("source_type"),
        credibility_level=raw.get("credibility_level"),
        resource_type=raw.get("resource_type"),
        resource_id=raw.get("resource_id"),
        resource_name=raw.get("resource_name"),
        era=raw.get("era"),
    )


class ResourceVectorError(Exception):
    """资源向量化异常。"""


# ---------------------------------------------------------------------------
# Canonical Text 生成
# ---------------------------------------------------------------------------


def _join_nonempty(parts: list[str], sep: str = "\n") -> str:
    """拼接非空字段，跳过空字符串与 None。"""
    return sep.join(p for p in parts if p and p.strip())


def _clean(value: str | None) -> str:
    """Strip 并返回字符串；None / 空白串返回空字符串。"""
    return (value or "").strip()


def _format_aliases(aliases: list[str] | None) -> str:
    """格式化别名列表：返回顿号分隔字符串，空列表/全空返回空字符串。"""
    if not aliases:
        return ""
    return "、".join(a.strip() for a in aliases if a and a.strip())


def _format_amount(amount: Decimal | float | int | None) -> str:
    """格式化用量：去除 Decimal 尾随零；None 返回空字符串。"""
    if amount is None:
        return ""
    if isinstance(amount, Decimal):
        # normalize() 会把 Decimal("10.00") → Decimal("1E+1")，
        # format(.., "f") 还原为普通计数法 "10"
        normalized = amount.normalize()
        s = format(normalized, "f")
        return s if s != "0" else "0"
    return str(amount)


def _build_herb_text(herb: Herb) -> str:
    """生成 Herb Canonical Text。

    包含：name、aliases、properties、channels、effects、source、description。
    空字段不输出；category_id / tag 等内部 ID 不拼入文本。
    """
    parts: list[str] = []
    name = _clean(herb.name)
    if name:
        parts.append(f"【药名】{name}")
    aliases = _format_aliases(herb.aliases)
    if aliases:
        parts.append(f"【别名】{aliases}")
    properties = _clean(herb.properties)
    if properties:
        parts.append(f"【性味】{properties}")
    if herb.channels:
        channels = "、".join(_clean(c) for c in herb.channels if _clean(c))
        if channels:
            parts.append(f"【归经】{channels}")
    effects = _clean(herb.effects)
    if effects:
        parts.append(f"【功效】{effects}")
    source = _clean(herb.source)
    if source:
        parts.append(f"【出处】{source}")
    description = _clean(herb.description)
    if description:
        parts.append(f"【描述】{description}")
    return _join_nonempty(parts)


def _build_prescription_text(prescription: Prescription) -> str:
    """生成 Prescription Canonical Text。

    包含：name、aliases、ingredients（展开为 Herb.name + amount/unit/role/processing）、
    efficacy、indications、usage_method、source、description。
    """
    parts: list[str] = []
    name = _clean(prescription.name)
    if name:
        parts.append(f"【方名】{name}")
    aliases = _format_aliases(prescription.aliases)
    if aliases:
        parts.append(f"【别名】{aliases}")
    if prescription.ingredients:
        ing_lines: list[str] = []
        for ing in prescription.ingredients:
            if not ing.herb:
                continue
            herb_name = _clean(ing.herb.name)
            if not herb_name:
                continue
            line = herb_name
            amount_str = _format_amount(ing.amount)
            if amount_str:
                line += f" {amount_str}"
            unit = _clean(ing.unit)
            if unit:
                line += unit
            processing = _clean(ing.processing)
            if processing:
                line += f"（{processing}）"
            role = _clean(ing.role)
            if role:
                line += f" [{role}]"
            ing_lines.append(line)
        if ing_lines:
            parts.append("【组成】\n" + "\n".join(ing_lines))
    efficacy = _clean(prescription.efficacy)
    if efficacy:
        parts.append(f"【功效】{efficacy}")
    indications = _clean(prescription.indications)
    if indications:
        parts.append(f"【主治】{indications}")
    usage_method = _clean(prescription.usage_method)
    if usage_method:
        parts.append(f"【用法】{usage_method}")
    source = _clean(prescription.source)
    if source:
        parts.append(f"【出处】{source}")
    description = _clean(prescription.description)
    if description:
        parts.append(f"【方解】{description}")
    return _join_nonempty(parts)


def _build_theory_text(theory: Theory) -> str:
    """生成 Theory Canonical Text。

    包含：name、aliases、source、content。
    """
    parts: list[str] = []
    name = _clean(theory.name)
    if name:
        parts.append(f"【理论名】{name}")
    aliases = _format_aliases(theory.aliases)
    if aliases:
        parts.append(f"【别名】{aliases}")
    source = _clean(theory.source)
    if source:
        parts.append(f"【出处】{source}")
    content = _clean(theory.content)
    if content:
        parts.append(f"【正文】\n{content}")
    return _join_nonempty(parts)


def _build_literature_text(literature: Literature) -> str:
    """生成 Literature Canonical Text。

    包含：name、aliases、author、dynasty、summary、source、content。
    """
    parts: list[str] = []
    name = _clean(literature.name)
    if name:
        parts.append(f"【文献名】{name}")
    aliases = _format_aliases(literature.aliases)
    if aliases:
        parts.append(f"【别名】{aliases}")
    author = _clean(literature.author)
    if author:
        parts.append(f"【作者】{author}")
    dynasty = _clean(literature.dynasty)
    if dynasty:
        parts.append(f"【成书年代】{dynasty}")
    summary = _clean(literature.summary)
    if summary:
        parts.append(f"【摘要】{summary}")
    source = _clean(literature.source)
    if source:
        parts.append(f"【版本】{source}")
    content = _clean(literature.content)
    if content:
        parts.append(f"【正文】\n{content}")
    return _join_nonempty(parts)


def build_canonical_text(
    resource: Herb | Prescription | Theory | Literature,
) -> str:
    """按资源类型生成 Canonical Text。

    Raises:
        ResourceVectorError: 不支持的资源类型
    """
    if isinstance(resource, Herb):
        return _build_herb_text(resource)
    if isinstance(resource, Prescription):
        return _build_prescription_text(resource)
    if isinstance(resource, Theory):
        return _build_theory_text(resource)
    if isinstance(resource, Literature):
        return _build_literature_text(resource)
    raise ResourceVectorError(f"不支持的资源类型：{type(resource).__name__}")


# ---------------------------------------------------------------------------
# Chunk 策略
# ---------------------------------------------------------------------------


@dataclass
class ResourceChunk:
    """Resource 切片结果（入库前）。

    chunk_index 从 0 开始；Resource 无真实页码，page_num 恒为 None。
    """

    content: str
    title_path: str | None
    chunk_index: int


def chunk_resource(
    resource: Herb | Prescription | Theory | Literature,
    canonical_text: str,
) -> list[ResourceChunk]:
    """按资源类型切片。

    - Herb / Prescription：1 Resource → 1 Vector（整条 canonical text 作为单向量）
    - Theory / Literature：≤ LONG_TEXT_THRESHOLD 字符 → 1 Vector；
      超长 → 复用 chunk_document 结构感知切片
    """
    if not canonical_text or not canonical_text.strip():
        return []
    if isinstance(resource, (Herb, Prescription)):
        return [ResourceChunk(content=canonical_text, title_path=None, chunk_index=0)]
    if isinstance(resource, (Theory, Literature)):
        if len(canonical_text) <= LONG_TEXT_THRESHOLD:
            return [
                ResourceChunk(
                    content=canonical_text, title_path=None, chunk_index=0
                )
            ]
        raw_chunks = chunk_document(canonical_text)
        if not raw_chunks:
            return []
        return [
            ResourceChunk(
                content=rc.content,
                title_path=rc.title_path,
                chunk_index=rc.chunk_index,
            )
            for rc in raw_chunks
        ]
    return []


# ---------------------------------------------------------------------------
# 稳定 ID 生成
# ---------------------------------------------------------------------------


def _stable_hash(*parts: str) -> str:
    """对输入片段生成 sha256 十六进制（64 字符，恰好满足 Milvus VARCHAR(64)）。"""
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_vector_id(
    resource_type: str,
    resource_id: uuid.UUID,
    kb_id: uuid.UUID,
    chunk_index: int,
) -> str:
    """生成 (Resource + KB + chunk_index) 维度的稳定向量 ID。

    64 字符 sha256 hex，稳定可重复，满足 Milvus VARCHAR(64)。
    """
    return _stable_hash(
        "resource", resource_type, str(resource_id), str(kb_id), str(chunk_index)
    )


def make_doc_id(
    resource_type: str,
    resource_id: uuid.UUID,
    kb_id: uuid.UUID,
) -> str:
    """生成 (Resource + KB) 维度的稳定 doc_id。

    用于 delete_by_doc 幂等清理：取消挂载或重新向量化时按 doc_id 删除，
    不会误删其他 KB 中同一 Resource 的向量。64 字符 sha256 hex。
    """
    return _stable_hash(
        "resource", resource_type, str(resource_id), str(kb_id)
    )


# ---------------------------------------------------------------------------
# ResourceVectorService
# ---------------------------------------------------------------------------


class ResourceVectorService:
    """将传统资源（Herb/Prescription/Theory/Literature）转换为 Milvus VectorRow。

    Stage 4-2：生成 Canonical Text → Chunk → BGE-M3 → VectorRow。
    Stage 4-3：扩展 vectorize_and_store / delete_vectors，将 VectorRow 写入
    Milvus（或当前注入的 BaseVectorStore），并在卸载时按 doc_id 幂等清理。

    Embedding 解析顺序：构造注入 → 方法参数注入 → get_embedding(None) 回退 settings。
    测试可注入 BaseEmbedding / BaseVectorStore 实例避免加载真实模型 / 连接真实 Milvus。
    """

    def __init__(
        self,
        embedding: BaseEmbedding | None = None,
        store: BaseVectorStore | None = None,
    ) -> None:
        self.embedding = embedding
        self.store = store or get_vector_store()
        # BUG-047：最近一次 revectorize_all_mounts 的失败清单
        # （[{kb_id, error}]，空列表表示全部成功），供调用方写入审计
        self.last_revectorize_failures: list[dict] = []
        # BUG-045：最近一次 cleanup_resource_mounts 中"向量补回失败"的清单
        self.last_cleanup_failures: list[dict] = []

    def _resolve_embedding(
        self, embedding: BaseEmbedding | None = None
    ) -> BaseEmbedding:
        """解析生效的 embedding 实例。"""
        if self.embedding is not None:
            return self.embedding
        if embedding is not None:
            return embedding
        return get_embedding(None)

    def vectorize(
        self,
        resource: Herb | Prescription | Theory | Literature,
        *,
        kb_id: uuid.UUID,
        resource_type: str | None = None,
        source_type: str | None = None,
        era: str | None = None,
        embedding: BaseEmbedding | None = None,
    ) -> list[VectorRow]:
        """将单个资源向量化为一组 VectorRow（按 chunk 生成）。

        Args:
            resource: Herb/Prescription/Theory/Literature 实例
            kb_id: 目标知识库 ID（同一资源在多个 KB 各自生成独立向量）
            resource_type: 资源类型（herb/prescription/theory/literature）；
                为 None 时按 isinstance 推断。
            source_type: 来源类型（受控枚举，见 source_meta.SOURCE_TYPES）；
                None 时不标注。Resource 模型本身无此字段，由调用方按需提供。
            era: 成书/出版年代（受控枚举，见 source_meta.ERAS）；
                None 时不标注。
            embedding: 向量化实例（测试注入用）；None 时回退构造实例。

        Returns:
            VectorRow 列表（按 chunk_index 升序）；空资源返回空列表。

        Raises:
            ResourceVectorError: 资源类型不支持 / source_type/era 非受控枚举 /
                向量化失败 / embedding 输出数量不一致
        """
        rtype = resource_type or self._infer_resource_type(resource)
        if rtype not in RESOURCE_TYPES:
            raise ResourceVectorError(f"不支持的资源类型：{rtype}")
        self._validate_source_meta(source_type, era)

        canonical = build_canonical_text(resource)
        chunks = chunk_resource(resource, canonical)
        if not chunks:
            return []

        emb = self._resolve_embedding(embedding)
        texts = [c.content for c in chunks]
        try:
            dense, sparse = emb.encode(texts)
        except EmbeddingError as exc:
            raise ResourceVectorError(
                f"资源向量化失败 {rtype}={getattr(resource, 'id', '?')}: {exc}"
            ) from exc
        except Exception as exc:
            raise ResourceVectorError(
                f"资源向量化异常 {rtype}={getattr(resource, 'id', '?')}: {exc}"
            ) from exc

        if len(dense) != len(chunks) or len(sparse) != len(chunks):
            raise ResourceVectorError(
                f"Embedding 输出数量不一致：期望 {len(chunks)}，"
                f"dense={len(dense)} sparse={len(sparse)}"
            )

        credibility = credibility_for(source_type)
        doc_id = make_doc_id(rtype, resource.id, kb_id)
        resource_name = _clean(getattr(resource, "name", ""))

        rows: list[VectorRow] = []
        for chunk, dv, sv in zip(chunks, dense, sparse):
            rows.append(
                VectorRow(
                    id=make_vector_id(rtype, resource.id, kb_id, chunk.chunk_index),
                    doc_id=doc_id,
                    kb_id=str(kb_id),
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    page_num=None,  # Resource 无真实页码，不伪造
                    title_path=chunk.title_path,
                    dense_vector=dv,
                    sparse_vector=sv,
                    source_type=source_type,
                    credibility_level=credibility,
                    # 资源元数据（Stage 4-3 写入 Milvus 动态字段时使用）
                    resource_type=rtype,
                    resource_id=str(resource.id),
                    resource_name=resource_name,
                    era=era,
                )
            )
        return rows

    @staticmethod
    def _infer_resource_type(
        resource: Herb | Prescription | Theory | Literature,
    ) -> str:
        if isinstance(resource, Herb):
            return "herb"
        if isinstance(resource, Prescription):
            return "prescription"
        if isinstance(resource, Theory):
            return "theory"
        if isinstance(resource, Literature):
            return "literature"
        raise ResourceVectorError(
            f"无法推断资源类型：{type(resource).__name__}"
        )

    @staticmethod
    def _validate_source_meta(
        source_type: str | None, era: str | None
    ) -> None:
        if not is_valid_source_type(source_type):
            raise ResourceVectorError(
                f"source_type 非受控枚举：{source_type}（合法值见 source_meta.SOURCE_TYPES）"
            )
        if not is_valid_era(era):
            raise ResourceVectorError(
                f"era 非受控枚举：{era}（合法值见 source_meta.ERAS）"
            )

    # ------------------------------------------------------------------
    # Stage 4-3：Milvus 写入 + 卸载清理
    # ------------------------------------------------------------------

    def vectorize_and_store(
        self,
        resource: Herb | Prescription | Theory | Literature,
        *,
        kb_id: uuid.UUID,
        resource_type: str | None = None,
        source_type: str | None = None,
        era: str | None = None,
        embedding: BaseEmbedding | None = None,
    ) -> list[VectorRow]:
        """向量化资源并写入向量库（幂等：先按 doc_id 清旧再插入）。

        挂载端点在 KBR INSERT 后、COMMIT 前调用：
        - 失败时由调用方 rollback DB 事务，KBR 与向量保持一致（均不存在）。
        - 成功时由调用方 commit，KBR 与向量同时生效。

        Returns:
            写入的 VectorRow 列表（空资源返回空列表，不写向量也不报错）。

        Raises:
            ResourceVectorError: 向量化失败
            VectorStoreError: 向量库写入失败
        """
        rows = self.vectorize(
            resource,
            kb_id=kb_id,
            resource_type=resource_type,
            source_type=source_type,
            era=era,
            embedding=embedding,
        )
        if not rows:
            return []
        # 幂等：同一 (Resource + KB) 重新挂载/重新向量化时整体替换
        # （BUG-046：先写新再清旧，避免"删了插失败"导致该资源向量全灭）
        self.store.ensure_collection()
        self.store.replace_doc(rows[0].doc_id, rows)
        return rows

    def delete_vectors(
        self,
        *,
        kb_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        """删除指定 (Resource + KB) 对应的全部向量（幂等）。

        卸载端点在 KBR DELETE 前/后调用均可：doc_id 稳定且按 (Resource+KB)
        维度生成，不会误删其他 KB 中同一 Resource 的向量。
        """
        doc_id = make_doc_id(resource_type, resource_id, kb_id)
        self.store.ensure_collection()
        self.store.delete_by_doc(doc_id)

    # ------------------------------------------------------------------
    # Stage 4-6：Resource 生命周期
    # ------------------------------------------------------------------

    async def revectorize_all_mounts(
        self,
        db,
        resource,
        resource_type: str,
    ) -> int:
        """Resource 更新后，对所有已挂载它的 KB 重新向量化并写入。

        遍历 knowledge_base_resources 表中该资源的全部挂载记录，
        对每个 KB 调用 vectorize_and_store（整体替换，见 BUG-046）。

        BUG-047：失败不再只是 warning —— 失败清单写入
        ``last_revectorize_failures``（[{kb_id, error}]）并以 error 级记录，
        调用方据此把失败写进审计详情，避免"PG 已更新、向量静默过期"。

        Returns:
            重新向量化成功的 KB 数量
        """
        import asyncio

        from sqlalchemy import select

        from src.domain.models import KnowledgeBaseResource

        kbrs = (
            await db.scalars(
                select(KnowledgeBaseResource).where(
                    KnowledgeBaseResource.resource_type == resource_type,
                    KnowledgeBaseResource.resource_id == resource.id,
                )
            )
        ).all()
        logger = logging.getLogger(__name__)
        failures: list[dict] = []
        count = 0
        for kbr in kbrs:
            try:
                await asyncio.to_thread(
                    self.vectorize_and_store,
                    resource,
                    kb_id=kbr.knowledge_base_id,
                    resource_type=resource_type,
                )
                count += 1
            except Exception as exc:
                failures.append(
                    {"kb_id": str(kbr.knowledge_base_id), "error": str(exc)}
                )
                logger.error(
                    f"重新向量化失败 {resource_type}={resource.id} "
                    f"kb={kbr.knowledge_base_id}: {exc}",
                    exc_info=True,
                )
        self.last_revectorize_failures = failures
        return count

    async def cleanup_resource_mounts(
        self,
        db,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> int:
        """Resource 删除时，清理所有 KBR 关联及对应 Milvus vectors。

        遍历所有挂载该资源的 KB，逐个删除 KBR 记录 + Milvus 向量。

        BUG-045（顺序即安全）：**先删 KBR（同一事务、未提交），再删向量**。
        向量删除失败 → 抛异常 → 调用方事务回滚，KBR 恢复，
        不会出现"资源仍挂载但向量永久消失"；反之（先删向量）一旦挂载删除
        回滚就会留下永久检索不到的挂载。

        Returns:
            清理的挂载数量
        """
        import asyncio

        from sqlalchemy import select

        from src.domain.models import KnowledgeBaseResource

        kbrs = (
            await db.scalars(
                select(KnowledgeBaseResource).where(
                    KnowledgeBaseResource.resource_type == resource_type,
                    KnowledgeBaseResource.resource_id == resource_id,
                )
            )
        ).all()
        self.store.ensure_collection()
        # (doc_id, 快照)：已删向量的挂载，失败时用于补偿回写
        done: list[tuple[str, list[dict]]] = []
        try:
            for kbr in kbrs:
                doc_id = make_doc_id(
                    resource_type, resource_id, kbr.knowledge_base_id
                )
                # 先删挂载（未提交，失败可回滚），再删向量
                await db.delete(kbr)
                await db.flush()
                snapshot = await asyncio.to_thread(self.store.query_rows, doc_id)
                await asyncio.to_thread(self.store.delete_by_doc, doc_id)
                done.append((doc_id, snapshot))
        except Exception as exc:
            # 失败安全：挂载删除随事务回滚；已删向量尽力补回，
            # 避免出现"资源仍挂载但永久检索不到"（BUG-045）
            failed = self._restore_vectors(done)
            self.last_cleanup_failures = [
                {"doc_id": doc_id, "error": str(exc)}
                for doc_id, _ in done
                if doc_id in failed
            ]
            logging.getLogger(__name__).error(
                f"资源挂载清理失败 {resource_type}={resource_id}: {exc}；"
                f"已恢复向量 {len(done) - len(failed)}/{len(done)} 个 KB"
            )
            raise
        self.last_cleanup_failures = []
        return len(kbrs)

    def _restore_vectors(self, done: list[tuple[str, list[dict]]]) -> set[str]:
        """把已删除的向量补回（用删除前快照）；返回补回失败的 doc_id 集合。"""
        failed: set[str] = set()
        for doc_id, snapshot in done:
            if not snapshot:
                continue
            try:
                rows = [_row_from_dict(r) for r in snapshot]
                self.store.insert(rows)
            except Exception as exc:  # noqa: BLE001  补偿失败只记录，不掩盖原始异常
                failed.add(doc_id)
                logging.getLogger(__name__).error(
                    f"向量补回失败 doc_id={doc_id}: {exc}", exc_info=True
                )
        return failed

    async def cleanup_kb_resource_vectors(self, db, kb_id) -> int:
        """KB purge 时，删除该 KB 下所有 Resource 向量。

        不删除 KBR 记录（KB CASCADE 会清理）。仅清理 Milvus 向量。
        """
        import asyncio

        from sqlalchemy import select

        from src.domain.models import KnowledgeBaseResource

        kbrs = (
            await db.scalars(
                select(KnowledgeBaseResource).where(
                    KnowledgeBaseResource.knowledge_base_id == kb_id,
                )
            )
        ).all()
        self.store.ensure_collection()
        for kbr in kbrs:
            doc_id = make_doc_id(
                kbr.resource_type, kbr.resource_id, kb_id
            )
            try:
                await asyncio.to_thread(self.store.delete_by_doc, doc_id)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"KB purge 向量清理失败 kb={kb_id} "
                    f"{kbr.resource_type}={kbr.resource_id}",
                    exc_info=True,
                )
        return len(kbrs)
