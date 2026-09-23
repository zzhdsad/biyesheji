"""阶段十三：知识图谱构建服务（KG Builder）。

定位（需求 §3）：KG 是「现有资源 → 图谱节点/关系」的派生物，**不是第二套中医知识库**：
- 节点只保存定位与实体匹配所需的最小信息（node_type / resource_type / resource_id /
  name / aliases），不复制资源正文；
- 边只保存能由现有业务数据**可靠推导**的关系，并记 provenance（哪张业务表/哪条规则）。

已实现关系（需求 §4，按可靠性取舍）：
- ``contains``   方剂 → 中药：来自 ``prescription_ingredients``（业务事实，100% 可靠）
- ``records``    文献 → 资源：资源 ``source`` 字段命中文献名/别名（著录事实）
- ``related_to`` 跨类型资源共享同一人工标签（弱相关，仅作补充；单标签上限裁剪，
                 超泛化标签直接跳过，避免噪声边爆炸）

构建语义（需求 §6）：
- 可重复执行：节点按 (resource_type, resource_id) 幂等 upsert，边按
  (source, target, relation_type) 去重；
- 支持重建：``rebuild=True`` 先清边再清节点后全量重建；
- 增量构建会清理已删除资源的孤立节点（边随 FK 级联删除）；
- 全程只写 kg_nodes / kg_edges，不影响 Resource RAG（向量、挂载、切片均不动）。
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.resource_vector_service import RESOURCE_TYPES
from src.domain.models import (
    KG_NODE_TYPE_RESOURCE,
    KG_PROVENANCE_INGREDIENT,
    KG_PROVENANCE_SHARED_TAG,
    KG_PROVENANCE_SOURCE,
    KG_RELATION_CONTAINS,
    KG_RELATION_RELATED_TO,
    KG_RELATION_RECORDS,
    Herb,
    KgEdge,
    KgNode,
    Literature,
    Prescription,
    PrescriptionIngredient,
    Tag,
    Theory,
    herb_tags,
    literature_tags,
    prescription_tags,
    theory_tags,
)

# 资源类型 → ORM 模型（复用既有资源表，不新建知识表）
_RESOURCE_MODELS: dict[str, type] = {
    "herb": Herb,
    "prescription": Prescription,
    "theory": Theory,
    "literature": Literature,
}

# 资源类型 → 标签关联表（related_to 的数据来源）
_TAG_TABLES: tuple[tuple[str, object, str], ...] = (
    ("herb", herb_tags, "herb_id"),
    ("prescription", prescription_tags, "prescription_id"),
    ("theory", theory_tags, "theory_id"),
    ("literature", literature_tags, "literature_id"),
)

# 文献名/别名参与 records 匹配的最小长度（过短易误命中）
_MIN_RECORD_NAME_LEN = 2

# shared_tag 关系裁剪：单标签资源数上限（超出视为过泛化，跳过）
_MAX_RESOURCES_PER_TAG = 30
# 单标签最多生成的边数（防止组合爆炸）
_MAX_EDGES_PER_TAG = 20


class KgBuildResult:
    """一次构建的结果（供 API 与测试断言）。"""

    __slots__ = (
        "nodes_created", "nodes_updated", "nodes_removed",
        "edges_created", "rebuilt", "node_count", "edge_count",
    )

    def __init__(
        self,
        nodes_created: int = 0,
        nodes_updated: int = 0,
        nodes_removed: int = 0,
        edges_created: int = 0,
        rebuilt: bool = False,
        node_count: int = 0,
        edge_count: int = 0,
    ) -> None:
        self.nodes_created = nodes_created
        self.nodes_updated = nodes_updated
        self.nodes_removed = nodes_removed
        self.edges_created = edges_created
        self.rebuilt = rebuilt
        self.node_count = node_count
        self.edge_count = edge_count

    def to_dict(self) -> dict:
        return {
            "nodes_created": self.nodes_created,
            "nodes_updated": self.nodes_updated,
            "nodes_removed": self.nodes_removed,
            "edges_created": self.edges_created,
            "rebuilt": self.rebuilt,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
        }


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _norm_aliases(aliases: list[str] | None) -> list[str]:
    return [a.strip() for a in (aliases or []) if a and a.strip()]


def _ingredient_description(amount, unit: str, role: str) -> str:
    """组成边的可读说明（用量 / 角色），全空返回空串。"""
    parts: list[str] = []
    if amount is not None:
        # Decimal → 去掉尾随零，避免 "9.00 克"
        text = format(amount, "f").rstrip("0").rstrip(".")
        if text:
            parts.append(f"{text}{_clean(unit)}")
    elif _clean(unit):
        parts.append(_clean(unit))
    if _clean(role):
        parts.append(_clean(role))
    return "；".join(parts)[:255]


class KgService:
    """知识图谱构建与查询（PostgreSQL 关系表实现，不引入图数据库）。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 构建 ─────────────────────────────────────────────────────────────────

    async def build(self, rebuild: bool = False) -> KgBuildResult:
        """构建 / 重建知识图谱（幂等，可重复执行）。

        Args:
            rebuild: True = 先清空既有节点与边再全量重建

        Returns:
            KgBuildResult：本次新增/更新/删除计数与重建后的总数
        """
        if rebuild:
            # 先边后节点（FK CASCADE 也可，但显式删除更可控）
            await self.db.execute(delete(KgEdge))
            await self.db.execute(delete(KgNode))
            await self.db.commit()

        created, updated, removed = await self._sync_nodes()
        await self.db.flush()
        index = await self._node_index()
        edges_created = await self._sync_edges(index)
        await self.db.commit()

        stats = await self.stats()
        result = KgBuildResult(
            nodes_created=created,
            nodes_updated=updated,
            nodes_removed=removed,
            edges_created=edges_created,
            rebuilt=rebuild,
            node_count=stats["node_count"],
            edge_count=stats["edge_count"],
        )
        logger.info(
            f"知识图谱构建完成: {result.to_dict()}"
        )
        return result

    async def _sync_nodes(self) -> tuple[int, int, int]:
        """资源 → 节点（upsert + 清理已删除资源的孤立节点）。"""
        created = updated = removed = 0
        for rtype in RESOURCE_TYPES:
            model = _RESOURCE_MODELS[rtype]
            rows = (
                await self.db.execute(select(model.id, model.name, model.aliases))
            ).all()
            existing = {
                n.resource_id: n
                for n in (
                    await self.db.scalars(
                        select(KgNode).where(KgNode.resource_type == rtype)
                    )
                ).all()
            }
            alive: set[uuid.UUID] = set()
            for row in rows:
                alive.add(row.id)
                aliases = _norm_aliases(row.aliases)
                node = existing.get(row.id)
                if node is None:
                    self.db.add(
                        KgNode(
                            node_type=KG_NODE_TYPE_RESOURCE,
                            resource_type=rtype,
                            resource_id=row.id,
                            name=row.name,
                            aliases=aliases,
                        )
                    )
                    created += 1
                elif node.name != row.name or _norm_aliases(node.aliases) != aliases:
                    node.name = row.name
                    node.aliases = aliases
                    updated += 1
            stale = [n for rid, n in existing.items() if rid not in alive]
            for node in stale:
                await self.db.delete(node)
            removed += len(stale)
        return created, updated, removed

    async def _node_index(self) -> dict[tuple[str, uuid.UUID], KgNode]:
        """(resource_type, resource_id) → KgNode（构建期一次性加载）。"""
        nodes = (await self.db.scalars(select(KgNode))).all()
        return {(n.resource_type, n.resource_id): n for n in nodes}

    async def _sync_edges(self, index: dict[tuple[str, uuid.UUID], KgNode]) -> int:
        """按可靠业务关系生成边（去重、不产生重复边）。"""
        existing = {
            (e.source_node_id, e.target_node_id, e.relation_type)
            for e in (await self.db.scalars(select(KgEdge))).all()
        }
        pending: list[tuple[uuid.UUID, uuid.UUID, str, str, str]] = []
        seen: set[tuple[uuid.UUID, uuid.UUID, str]] = set()

        def add(src: KgNode | None, tgt: KgNode | None, relation: str,
                provenance: str, description: str = "") -> None:
            if src is None or tgt is None or src.id == tgt.id:
                return
            key = (src.id, tgt.id, relation)
            if key in existing or key in seen:
                return
            seen.add(key)
            pending.append((src.id, tgt.id, relation, provenance, description[:255]))

        # 1) contains：方剂 → 中药（prescription_ingredients 业务事实）
        ingredients = (
            await self.db.execute(
                select(
                    PrescriptionIngredient.prescription_id,
                    PrescriptionIngredient.herb_id,
                    PrescriptionIngredient.amount,
                    PrescriptionIngredient.unit,
                    PrescriptionIngredient.role,
                )
            )
        ).all()
        for row in ingredients:
            src = index.get(("prescription", row.prescription_id))
            tgt = index.get(("herb", row.herb_id))
            add(
                src,
                tgt,
                KG_RELATION_CONTAINS,
                KG_PROVENANCE_INGREDIENT,
                _ingredient_description(row.amount, row.unit, row.role),
            )

        # 2) records：文献 → 资源（资源 source 命中文献名/别名）
        literature_nodes = [n for (rt, _rid), n in index.items() if rt == "literature"]
        if literature_nodes:
            resources: list[tuple[str, uuid.UUID, str]] = []
            for rtype in ("herb", "prescription", "theory"):
                model = _RESOURCE_MODELS[rtype]
                rows = (await self.db.execute(select(model.id, model.source))).all()
                resources.extend((rtype, r.id, _clean(r.source)) for r in rows)
            for lit in literature_nodes:
                names = [n for n in [lit.name, *_norm_aliases(lit.aliases)]
                         if len(n) >= _MIN_RECORD_NAME_LEN]
                if not names:
                    continue
                for rtype, rid, source in resources:
                    if not source:
                        continue
                    hit_name = next((n for n in names if n in source), None)
                    if hit_name is None:
                        continue
                    add(
                        lit,
                        index.get((rtype, rid)),
                        KG_RELATION_RECORDS,
                        KG_PROVENANCE_SOURCE,
                        f"出处：{source}",
                    )

        # 3) related_to：跨类型资源共享人工标签（弱相关，裁剪防噪声）
        await self._collect_shared_tag_edges(index, add)

        for src_id, tgt_id, relation, provenance, description in pending:
            self.db.add(
                KgEdge(
                    source_node_id=src_id,
                    target_node_id=tgt_id,
                    relation_type=relation,
                    provenance=provenance,
                    description=description,
                )
            )
        if pending:
            await self.db.flush()
        return len(pending)

    async def _collect_shared_tag_edges(self, index, add) -> None:
        """共享标签 → related_to（仅跨类型、单标签限量）。

        标签由人工维护，共享同一标签视为"弱相关"而非医学结论；
        单标签命中资源过多（过泛化）时直接跳过，避免噪声边爆炸。
        """
        by_tag: dict[uuid.UUID, list[tuple[str, uuid.UUID]]] = defaultdict(list)
        for rtype, table, col in _TAG_TABLES:
            rows = (await self.db.execute(select(table.c[col], table.c.tag_id))).all()
            for rid, tag_id in rows:
                by_tag[tag_id].append((rtype, rid))
        if not by_tag:
            return

        tag_names = {
            t.id: t.name
            for t in (await self.db.scalars(select(Tag))).all()
        }
        for tag_id, members in by_tag.items():
            # 去重保序，且按 (类型, id) 排序保证构建确定性
            members = sorted(set(members))
            if len(members) < 2 or len(members) > _MAX_RESOURCES_PER_TAG:
                continue
            label = tag_names.get(tag_id, "")
            description = f"共同标签：{label}" if label else "共同标签"
            emitted = 0
            for i, (rt_a, rid_a) in enumerate(members):
                for rt_b, rid_b in members[i + 1:]:
                    if rt_a == rt_b:
                        continue  # 仅跨类型（同类型关系无额外信息量）
                    src = index.get((rt_a, rid_a))
                    tgt = index.get((rt_b, rid_b))
                    add(src, tgt, KG_RELATION_RELATED_TO,
                        KG_PROVENANCE_SHARED_TAG, description)
                    emitted += 1
                    if emitted >= _MAX_EDGES_PER_TAG:
                        return

    # ── 查询 ─────────────────────────────────────────────────────────────────

    async def stats(self) -> dict:
        """图谱规模统计（节点数 / 边数 / 按类型与关系分布）。"""
        node_count = int(
            await self.db.scalar(select(func.count()).select_from(KgNode)) or 0
        )
        edge_count = int(
            await self.db.scalar(select(func.count()).select_from(KgEdge)) or 0
        )
        by_resource_type = {
            rtype: int(count or 0)
            for rtype, count in (
                await self.db.execute(
                    select(KgNode.resource_type, func.count(KgNode.id)).group_by(
                        KgNode.resource_type
                    )
                )
            ).all()
        }
        by_relation = {
            rel: int(count or 0)
            for rel, count in (
                await self.db.execute(
                    select(KgEdge.relation_type, func.count(KgEdge.id)).group_by(
                        KgEdge.relation_type
                    )
                )
            ).all()
        }
        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "by_resource_type": by_resource_type,
            "by_relation": by_relation,
        }

    async def match_nodes(
        self,
        texts: list[str],
        resource_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[tuple[KgNode, float]]:
        """按实体文本匹配节点：名称/别名精确命中优先，其次子串包含。

        Args:
            texts: Query Analyzer 提取的实体文本（不做二次分词/关键词分析）
            resource_types: 限定资源类型；None = 不限
            limit: 返回条数上限

        Returns:
            [(node, score)]，score：别名精确 0.95 / 名称精确 1.0 / 名称子串 0.7
        """
        names = [t.strip() for t in texts if t and len(t.strip()) >= 2]
        if not names:
            return []
        stmt = select(KgNode)
        if resource_types:
            stmt = stmt.where(KgNode.resource_type.in_(list(resource_types)))
        nodes = (await self.db.scalars(stmt)).all()

        matched: dict[uuid.UUID, tuple[KgNode, float]] = {}
        for node in nodes:
            aliases = _norm_aliases(node.aliases)
            best = 0.0
            for name in names:
                if node.name == name:
                    best = max(best, 1.0)
                elif name in aliases:
                    best = max(best, 0.95)
                elif len(name) >= _MIN_RECORD_NAME_LEN and (
                    name in node.name or node.name in name
                ):
                    best = max(best, 0.7)
            if best > 0:
                prev = matched.get(node.id)
                if prev is None or best > prev[1]:
                    matched[node.id] = (node, best)
        result = sorted(matched.values(), key=lambda t: (-t[1], str(t[0].id)))
        return result[:limit]

    async def edges_of(
        self, node_ids: list[uuid.UUID], relation: str | None = None
    ) -> list[KgEdge]:
        """取与给定节点相邻（任一方向）的边；relation 为空表示不限关系。"""
        if not node_ids:
            return []
        stmt = select(KgEdge).where(
            (KgEdge.source_node_id.in_(node_ids))
            | (KgEdge.target_node_id.in_(node_ids))
        )
        if relation:
            stmt = stmt.where(KgEdge.relation_type == relation)
        return list((await self.db.scalars(stmt)).all())

    async def nodes_by_ids(self, node_ids: list[uuid.UUID]) -> dict[uuid.UUID, KgNode]:
        if not node_ids:
            return {}
        nodes = (
            await self.db.scalars(select(KgNode).where(KgNode.id.in_(node_ids)))
        ).all()
        return {n.id: n for n in nodes}
