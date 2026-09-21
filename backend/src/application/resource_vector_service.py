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
import uuid
from dataclasses import dataclass
from decimal import Decimal

from src.core.source_meta import credibility_for, is_valid_era, is_valid_source_type
from src.domain.models import Herb, Literature, Prescription, Theory
from src.infrastructure.embedding import BaseEmbedding, EmbeddingError, get_embedding
from src.infrastructure.milvus_store import VectorRow
from src.utils.chunking import chunk_document

# 长文本切片阈值（字符数，非 token）：
# Theory/Literature 超过此值时复用 chunking.py 的结构感知切片，避免单向量过长。
LONG_TEXT_THRESHOLD = 6000

# 受控资源类型词表（与 KnowledgeBaseResource.resource_type 对齐）。
RESOURCE_TYPES: tuple[str, ...] = ("herb", "prescription", "theory", "literature")


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

    本阶段（Stage 4-2）只负责：
    1. 生成 Canonical Text（按资源类型字段映射）
    2. 切片（Herb/Prescription 单向量；Theory/Literature 按长度复用 chunking）
    3. 调用 BGE-M3 双路向量化（复用 BaseEmbedding 接口）
    4. 组装 VectorRow（携带 resource_type/resource_id/resource_name 等元数据）

    本阶段不写入 Milvus；写入逻辑由 Stage 4-3 实现。

    Embedding 解析顺序：构造注入 → 方法参数注入 → get_embedding(None) 回退 settings。
    测试可注入 BaseEmbedding 实例避免加载真实模型。
    """

    def __init__(self, embedding: BaseEmbedding | None = None) -> None:
        self.embedding = embedding

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
