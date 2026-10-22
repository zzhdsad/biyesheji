"""资源名称实体消歧（Entity Resolution）：为 Query Analyzer 提供「事实依据」。

背景（为什么需要这一层）：
现有 Query Analyzer 只看文本信号（功效 / 主治 / 组成…），不知道数据库里实际
有哪些资源。于是"沉香曲的功效与主治？"会因为"功效""主治"是中药强信号，
被判成 herb；而 PG 里"沉香曲"是**唯一的方剂名**。本模块用真实数据消歧。

职责边界（刻意做薄）：
- 只做「问题里是否出现了某个已存在的资源名称」这一件事
- 只查 Herb / Prescription / Theory / Literature 的 name / aliases
  （**不扫描 document chunk**、不读向量库、不调用 LLM、不做医学判断）
- 不决定是否拒答、不改检索策略参数、不写任何业务数据
- 是否用该事实覆盖 question_type / resource_types，由 query_analyzer
  的 apply_entity_resolution() 决定（本模块不替 Analyzer 做决策）
- 任何异常 → 返回 None，调用方保持原有 Analyzer 行为（非单点故障）
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select

from src.domain.models import Herb, Literature, Prescription, Theory

# 参与消歧的资源表：resource_type → ORM 模型（与 Evidence 资源词表同源）
_RESOURCE_MODELS: dict[str, type] = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

# 名称索引缓存有效期（秒）：资源名称变更频率低，避免每次请求全表扫描
_INDEX_TTL_SECONDS = 300

# 参与匹配的最短名称长度：单字名称在中文里极易误命中（"曲""参"），直接排除
_MIN_NAME_LENGTH = 2

# 去标点后的查询核心（用于"整句就是一个资源名"的精确匹配判定）
_PUNCT_PATTERN = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


@dataclass(frozen=True)
class ResourceNameHit:
    """一条「名称/别名 → 资源」的可匹配事实。"""

    resource_type: str
    resource_id: uuid.UUID
    resource_name: str  # 资源的规范名
    matched_text: str  # 实际参与匹配的名称或别名
    is_alias: bool = False


@dataclass(frozen=True)
class ResourceNameIndex:
    """内存中的资源名称索引（PG 快照，按首字分组以缩小比对范围）。"""

    by_first_char: dict[str, tuple[ResourceNameHit, ...]] = field(default_factory=dict)
    size: int = 0

    def lookup(self, query: str) -> list[ResourceNameHit]:
        """找出所有作为子串出现在 query 中的资源名称/别名。"""
        chars = set(query)
        hits: list[ResourceNameHit] = []
        for ch in chars:
            for hit in self.by_first_char.get(ch, ()):
                if hit.matched_text in query:
                    hits.append(hit)
        return hits


@dataclass(frozen=True)
class EntityResolutionResult:
    """消歧结果：只描述「是否命中了确定的资源名称」这一事实。"""

    matched: bool
    ambiguous: bool = False
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    matched_text: str | None = None
    # exact：去标点后整句就等于该名称；contains：名称作为片段出现在问题中
    match_kind: str | None = None
    alias_hit: bool = False
    candidate_types: tuple[str, ...] = ()
    candidate_count: int = 0
    # 问题中识别到的**极大**资源名 spans（已剔除被更长名称包含的嵌套命中，
    # 如 「沉香」⊂「沉香曲」）。是否据此覆盖类型由 Query Analyzer 决定：
    # Analyzer 会用既有信号词表剔除形如「功效/主治/组成」这类通用词同名的资源。
    entity_texts: tuple[str, ...] = ()
    # 未命中 / 未采用的原因（调试用）
    reason: str | None = None

    def to_feature(self) -> dict:
        """结构化调试信息（写入 QueryAnalysis.features["entity_match"]）。

        只暴露 name / type / id 这类定位信息，不含资源正文或原始行数据。
        """
        return {
            "matched": self.matched,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "matched_text": self.matched_text,
            "match_kind": self.match_kind,
            "alias_hit": self.alias_hit,
            "ambiguous": self.ambiguous,
            "candidate_types": list(self.candidate_types),
            "candidate_count": self.candidate_count,
            "entity_texts": list(self.entity_texts),
            "reason": self.reason,
        }


# ── 名称索引（PG → 内存快照）──────────────────────────────────────────────

_index_cache: ResourceNameIndex | None = None
_index_built_at: float = 0.0


def clear_resource_name_index_cache() -> None:
    """清空索引缓存（测试 / 资源批量导入后手动刷新用）。"""
    global _index_cache, _index_built_at
    _index_cache = None
    _index_built_at = 0.0


def _build_index(rows: dict[str, list[tuple]]) -> ResourceNameIndex:
    """把「(id, name, aliases) 行」建成按首字分组的可匹配索引。"""
    by_first_char: dict[str, list[ResourceNameHit]] = {}
    size = 0
    for rtype, entries in rows.items():
        for resource_id, name, aliases in entries:
            if not name:
                continue
            canonical = str(name).strip()
            texts: list[tuple[str, bool]] = []
            if len(canonical) >= _MIN_NAME_LENGTH:
                texts.append((canonical, False))
            for alias in aliases or []:
                if not alias:
                    continue
                alias_text = str(alias).strip()
                if (
                    len(alias_text) >= _MIN_NAME_LENGTH
                    and alias_text != canonical
                    and (alias_text, True) not in texts
                ):
                    texts.append((alias_text, True))
            for text, is_alias in texts:
                by_first_char.setdefault(text[0], []).append(
                    ResourceNameHit(
                        resource_type=rtype,
                        resource_id=resource_id,
                        resource_name=canonical,
                        matched_text=text,
                        is_alias=is_alias,
                    )
                )
                size += 1
    return ResourceNameIndex(
        by_first_char={k: tuple(v) for k, v in by_first_char.items()}, size=size
    )


async def load_resource_name_index(session) -> ResourceNameIndex:
    """全量加载资源名称 / 别名（只读 id+name+aliases，不触碰正文）。"""
    rows: dict[str, list[tuple]] = {}
    for rtype, model in _RESOURCE_MODELS.items():
        result = await session.execute(select(model.id, model.name, model.aliases))
        rows[rtype] = [(r[0], r[1], r[2]) for r in result.all()]
    index = _build_index(rows)
    logger.info(f"资源名称索引加载完成 名称/别名={index.size}")
    return index


async def get_resource_name_index(
    session, *, ttl: float = _INDEX_TTL_SECONDS
) -> ResourceNameIndex:
    """带 TTL 的索引获取；并发重复加载最多多一次快照读，无锁不跨事件循环。"""
    global _index_cache, _index_built_at
    now = time.monotonic()
    if _index_cache is not None and now - _index_built_at < ttl:
        return _index_cache
    index = await load_resource_name_index(session)
    _index_cache = index
    _index_built_at = now
    return index


# ── 纯函数：给定索引做名称消歧（便于无 DB 单测）────────────────────────────


def _rank_key(hit: ResourceNameHit, core: str) -> tuple[int, int, int, str]:
    """命中排序：精确匹配 > 包含匹配；同级长名优先；同名优先于别名；id 兜底稳定。"""
    kind_rank = 0 if core == hit.matched_text else 1
    return (kind_rank, -len(hit.matched_text), 1 if hit.is_alias else 0, str(hit.resource_id))


def resolve_in_index(index: ResourceNameIndex, query: str) -> EntityResolutionResult:
    """在给定索引上对问题做名称消歧。

    - 精确匹配（整句即资源名）优先于包含匹配；
    - 同级取**最长**名称，避免"沉香"盖过"沉香曲"；
    - 同一命中文本落在多个资源类型 → ambiguous=True，由调用方决定不覆盖。
    """
    raw = (query or "").strip()
    if len(raw) < _MIN_NAME_LENGTH:
        return EntityResolutionResult(matched=False, reason="query_too_short")

    core = _PUNCT_PATTERN.sub("", raw)
    hits = index.lookup(raw)
    if not hits:
        return EntityResolutionResult(matched=False, reason="no_name_match")

    # 极大匹配 span：命中文本没有被更长的命中文本包含。
    # 「沉香」⊂「沉香曲」→ 只算一个实体；「金银花」+「本草纲目」→ 两个并列实体。
    texts = {h.matched_text for h in hits}
    maximal_texts = tuple(
        sorted(
            (t for t in texts if not any(t != o and t in o for o in texts)),
            key=lambda t: (-len(t), t),
        )
    )

    ranked = sorted(hits, key=lambda h: _rank_key(h, core))
    best = ranked[0]
    # 同一命中文本可能同时是某些资源的规范名与另一些资源的别名（重名 / 别名冲突）
    group = [h for h in ranked if h.matched_text == best.matched_text]
    types = tuple(sorted({h.resource_type for h in group}))

    ambiguous = len(types) > 1
    # 同一文本可能既是某资源的规范名、又是另一资源的别名：优先规范名，其次 id 稳定
    chosen = sorted(group, key=lambda h: (1 if h.is_alias else 0, str(h.resource_id)))[0]
    kind = "exact" if core == chosen.matched_text else "contains"
    return EntityResolutionResult(
        matched=True,
        ambiguous=ambiguous,
        resource_type=chosen.resource_type,
        resource_id=str(chosen.resource_id),
        resource_name=chosen.resource_name,
        matched_text=chosen.matched_text,
        match_kind=kind,
        alias_hit=chosen.is_alias,
        candidate_types=types,
        candidate_count=len(ranked),
        entity_texts=maximal_texts,
        reason="ambiguous_multi_type" if ambiguous else None,
    )


# ── 对外入口 ────────────────────────────────────────────────────────────────


async def resolve_resource_name(
    session, query: str, *, ttl: float = _INDEX_TTL_SECONDS
) -> EntityResolutionResult | None:
    """Pipeline 入口：名称命中事实；任何异常返回 None（调用方保持旧行为）。"""
    try:
        index = await get_resource_name_index(session, ttl=ttl)
        return resolve_in_index(index, query)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 消歧绝不能成为检索链路的失败点
        logger.warning(f"资源名称消歧失败（沿用 Analyzer 结果）: {exc}")
        return None
