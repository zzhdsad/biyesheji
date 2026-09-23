"""阶段十三：知识图谱（KG）路由。

- POST /kg/build：构建 / 重建图谱（仅 admin，写操作）
- GET  /kg/stats：图谱规模统计
- GET  /kg/relations：关系类型受控词表（含中文标签与来源说明）
- GET  /kg/query：KG 检索调试（Query Analyzer → KG Retrieval → 统一 Evidence）

与 chat / evaluation 的兼容约定：
- 不改动 /chat/ask 与 /chat/ask-stream 的既有字段与 SSE 事件；
- KG 证据在进入 /chat/* 响应时是 citations / evidence 的一部分，
  此处 /kg/query 只是把同一批证据单独暴露，便于观察与实验。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.evidence import build_evidence, package_evidence
from src.application.kg_retrieval import KgRetriever
from src.application.kg_service import KgService
from src.application.query_analyzer import QueryAnalyzer
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import AppException, PermissionDeniedError
from src.domain.models import (
    KG_PROVENANCE_INGREDIENT,
    KG_PROVENANCE_SHARED_TAG,
    KG_PROVENANCE_SOURCE,
    KG_RELATION_LABELS,
    KG_RELATIONS,
    User,
)
from src.infrastructure.database import get_db

router = APIRouter(prefix="/kg", tags=["kg"])


class KgBuildRequest(BaseModel):
    rebuild: bool = False


class KgBuildOut(BaseModel):
    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_removed: int = 0
    edges_created: int = 0
    rebuilt: bool = False
    node_count: int = 0
    edge_count: int = 0


class KgStatsOut(BaseModel):
    node_count: int
    edge_count: int
    by_resource_type: dict = {}
    by_relation: dict = {}


class KgQueryOut(BaseModel):
    question: str
    question_type: str
    resource_types: list[str] = []
    entities: list[dict] = []
    kg_hits: int = 0
    evidence: list[dict] = []
    evidence_groups: list[dict] = []
    evidence_summary: dict | None = None


@router.post("/build", response_model=KgBuildOut)
async def build_kg(
    payload: KgBuildRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> KgBuildOut:
    """构建 / 重建知识图谱（仅 admin）。

    rebuild=True 时先清空 kg_edges / kg_nodes 再全量重建；否则增量 upsert，
    重复执行不会产生重复节点或边。
    """
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可构建知识图谱")

    result = await KgService(db).build(rebuild=payload.rebuild)
    return KgBuildOut(**result.to_dict())


@router.get("/stats", response_model=KgStatsOut)
async def kg_stats(db: AsyncSession = Depends(get_db)) -> KgStatsOut:
    """图谱规模统计：节点数 / 边数 / 按资源类型与关系类型分布。"""
    return KgStatsOut(**(await KgService(db).stats()))


@router.get("/relations")
async def kg_relations() -> dict:
    """关系类型受控词表（前端展示与实验配置用）。"""
    return {
        "relations": [
            {"value": rel, "label": KG_RELATION_LABELS.get(rel, rel)}
            for rel in KG_RELATIONS
        ],
        "provenances": [
            KG_PROVENANCE_INGREDIENT,
            KG_PROVENANCE_SOURCE,
            KG_PROVENANCE_SHARED_TAG,
        ],
    }


@router.get("/query", response_model=KgQueryOut)
async def kg_query(
    request: Request,
    question: str = Query(min_length=1),
    kb_id: uuid.UUID = Query(...),
    max_hops: int = Query(default=2, ge=1, le=3),
    top_k: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> KgQueryOut:
    """KG 检索调试：Query Analyzer → KG Retrieval → 统一 Evidence。

    仅返回已挂载到该 KB 的资源所对应的关系证据（与 /chat/ask 同一权限口径）。
    """
    user: User = request.state.user
    accessible = await get_accessible_kb_ids(db, user)
    if kb_id not in accessible:
        raise AppException(403, "无权访问该知识库")

    analysis = QueryAnalyzer().analyze(question)
    hits = await KgRetriever(db).retrieve(
        [kb_id], analysis, max_hops=max_hops, top_k=top_k
    )
    evidence = build_evidence(hits)
    groups, summary = package_evidence(evidence)
    return KgQueryOut(
        question=question,
        question_type=analysis.question_type,
        resource_types=list(analysis.resource_types),
        entities=list(analysis.entities),
        kg_hits=len(hits),
        evidence=evidence,
        evidence_groups=groups,
        evidence_summary=summary,
    )
