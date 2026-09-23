"""阶段十三：KG Retrieval（知识图谱检索）。

输入：Query Analysis（复用阶段十一已提取的 entities / resource_types）+ kb_ids
输出：与向量检索同构的 hit 列表，经 ``application.evidence`` 转成**同一套 Evidence**，
进入 Evidence / Evidence Groups / Citation（不创建第二套 Evidence 结构）。

约束（需求 §7）：
- 不做关键词分析：实体来自 QueryAnalyzer，本模块只做「实体 → 节点 → 关系」的图遍历；
- 不删除/替代 Vector Retrieval：KG 命中是**补充证据**（关系证据 / 多跳证据）；
- 权限隔离：只返回已挂载到本次 kb_ids 的资源节点对应的关系；
- 分数含义：KG 匹配分（实体命中分 × 跳数衰减），不是向量余弦相似度，
  写入 dense_score 仅为让既有 Relevance Gate 对"只有 KG 命中"的场景也能正常判定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.kg_service import KgService
from src.application.query_analyzer import QueryAnalysis
from src.domain.models import (
    KG_RELATION_LABELS,
    KnowledgeBaseResource,
    KgEdge,
    KgNode,
)

# KG 命中的来源类别（与 document / resource 并列，走同一套 Evidence）
KG_SOURCE_KIND = "kg"

# 跳数衰减：1 跳（实体直连）完整保留，之后逐跳衰减
KG_HOP_DECAY = 0.85
# 允许的最大跳数（多跳关系证据）
KG_MAX_HOPS_LIMIT = 3

_ENTITY_MIN_LEN = 2  # 过短文本不做匹配（避免"的""和"之类噪声）


@dataclass(frozen=True)
class KgFact:
    """一条图谱关系事实（边 + 跳数 + 匹配分）。"""

    edge_id: uuid.UUID
    relation: str
    provenance: str
    description: str
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    # 与查询实体直接相关的那一端（用于把证据归到具体资源）
    anchor_node_id: uuid.UUID
    hop: int
    score: float


class KgRetriever:
    """知识图谱检索：QueryAnalysis → 关系事实 → Evidence 兼容命中。"""

    def __init__(self, db: AsyncSession, service: KgService | None = None) -> None:
        self.db = db
        self.service = service or KgService(db)

    async def facts(
        self,
        kb_ids: list[uuid.UUID],
        analysis: QueryAnalysis,
        max_hops: int = 1,
    ) -> tuple[list[KgFact], dict[uuid.UUID, KgNode]]:
        """图遍历产出关系事实。

        Returns:
            (facts, nodes) — facts 已按 (分数降序, 边 id) 稳定排序；nodes 为
            涉及节点的 id → KgNode 映射（供构造 Evidence 使用）。
        """
        if not kb_ids or analysis is None:
            return [], {}

        entity_texts = [
            (e.get("text") or "").strip()
            for e in (analysis.entities or [])
            if isinstance(e, dict) and len((e.get("text") or "").strip()) >= _ENTITY_MIN_LEN
        ]
        if not entity_texts:
            return [], {}

        matches = await self.service.match_nodes(
            entity_texts,
            resource_types=list(analysis.resource_types or []) or None,
            limit=10,
        )
        if not matches:
            return [], {}

        allowed = await self._mounted_resources(kb_ids)
        seeds = {
            node.id: score
            for node, score in matches
            if (node.resource_type, node.resource_id) in allowed
        }
        if not seeds:
            return [], {}

        hops = max(1, min(int(max_hops or 1), KG_MAX_HOPS_LIMIT))
        facts: dict[uuid.UUID, KgFact] = {}
        visited: set[uuid.UUID] = set(seeds)
        frontier: dict[uuid.UUID, float] = dict(seeds)

        for hop in range(1, hops + 1):
            if not frontier:
                break
            edges: list[KgEdge] = await self.service.edges_of(list(frontier))
            next_frontier: dict[uuid.UUID, float] = {}
            for edge in edges:
                if edge.id in facts:
                    continue
                src_in = edge.source_node_id in frontier
                tgt_in = edge.target_node_id in frontier
                if not (src_in or tgt_in):
                    continue
                base = max(
                    frontier.get(edge.source_node_id, 0.0),
                    frontier.get(edge.target_node_id, 0.0),
                )
                score = round(base * (KG_HOP_DECAY ** (hop - 1)), 4)
                anchor = (
                    edge.source_node_id
                    if src_in
                    else edge.target_node_id
                )
                facts[edge.id] = KgFact(
                    edge_id=edge.id,
                    relation=edge.relation_type,
                    provenance=edge.provenance or "",
                    description=edge.description or "",
                    source_node_id=edge.source_node_id,
                    target_node_id=edge.target_node_id,
                    anchor_node_id=anchor,
                    hop=hop,
                    score=score,
                )
                other = edge.target_node_id if src_in else edge.source_node_id
                if other not in visited:
                    visited.add(other)
                    next_frontier[other] = round(score * KG_HOP_DECAY, 4)
            frontier = next_frontier

        node_ids = set()
        for f in facts.values():
            node_ids.update({f.source_node_id, f.target_node_id})
        nodes = await self.service.nodes_by_ids(list(node_ids))
        ordered = sorted(facts.values(), key=lambda f: (-f.score, str(f.edge_id)))
        return ordered, nodes

    async def retrieve(
        self,
        kb_ids: list[uuid.UUID],
        analysis: QueryAnalysis,
        max_hops: int = 1,
        top_k: int = 5,
    ) -> list[dict]:
        """KG 检索 → Evidence 兼容命中列表（可直接并入向量命中）。"""
        ordered, nodes = await self.facts(kb_ids, analysis, max_hops)
        if not ordered:
            return []

        hits: list[dict] = []
        for fact in ordered[: max(0, int(top_k or 0))]:
            anchor = nodes.get(fact.anchor_node_id)
            source = nodes.get(fact.source_node_id)
            target = nodes.get(fact.target_node_id)
            if source is None or target is None:
                continue
            relation_label = KG_RELATION_LABELS.get(fact.relation, fact.relation)
            title_path = f"{source.name} → {relation_label} → {target.name}"
            content = f"{source.name} {relation_label} {target.name}"
            if fact.description:
                content = f"{content}（{fact.description}）"
            node = anchor or source
            hits.append(
                {
                    # id / doc_id：KG 证据的稳定标识（与向量 chunk id 同命名空间）
                    "id": f"kg:{fact.edge_id}",
                    "doc_id": f"kg:{fact.edge_id}",
                    "kb_id": None,
                    "content": content,
                    "page_num": None,
                    "title_path": title_path,
                    "score": fact.score,
                    # 供既有 Relevance Gate 使用（KG 匹配分，非向量相似度）
                    "dense_score": fact.score,
                    "source_kind": KG_SOURCE_KIND,
                    "resource_type": node.resource_type,
                    "resource_id": str(node.resource_id),
                    "resource_name": node.name,
                    "doc_name": node.name,
                    "source_type": None,
                    "era": None,
                    "credibility_level": None,
                    # KG 专属（可选字段，旧 Evidence 结构不受影响）
                    "kg_relation": fact.relation,
                    "kg_hop": fact.hop,
                    "kg_provenance": fact.provenance,
                }
            )
        if hits:
            logger.info(
                f"KG 检索命中 {len(hits)} 条（max_hops={max_hops}, top_k={top_k}）"
            )
        return hits

    async def _mounted_resources(
        self, kb_ids: list[uuid.UUID]
    ) -> set[tuple[str, uuid.UUID]]:
        """本次可访问 KB 已挂载的资源集合（权限隔离，与向量检索口径一致）。"""
        rows = (
            await self.db.execute(
                select(
                    KnowledgeBaseResource.resource_type,
                    KnowledgeBaseResource.resource_id,
                ).where(KnowledgeBaseResource.knowledge_base_id.in_(list(kb_ids)))
            )
        ).all()
        return {(rt, rid) for rt, rid in rows}
