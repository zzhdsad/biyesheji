"""阶段十：统一 Evidence 模型与多来源证据组织。

背景（AGENTS.md §4 / §8 / PRD）：知识库里同时存在 Document（上传文献）与
Resource（中药 / 方剂 / 理论 / 文献条目）两类来源，一次问答的命中往往是
"多来源混合"。阶段十只做证据组织与展示，不改动检索链路：

Query → HyDE → BGE-M3 Dense+Sparse → RRF → Reranker → Gate → 【Evidence 组织】
→ LLM → Citation / Evidence 展示

本模块职责（三件事）：
1. 统一 Evidence：Document 命中与 Resource 命中映射为同一结构的 Evidence。
   Evidence = Citation 的超集（旧字段全部保留），不为四种 Resource 各建一套结构。
2. 证据分级：复用既有 high / medium / insufficient 规则（0.7 / 0.3），
   低于 RELEVANCE_THRESHOLD 的命中不进入 Evidence（与 Citation 过滤一致）。
3. 多来源分组：先按 source_kind（document / resource）+ 细分类型分组，
   组内再按具体来源（某味中药、某篇文献）聚合，供前端分层展示。

兼容性：
- 旧 Citation 字段（chunk_id / source_index / doc_id / doc_name / page_num /
  title_path / content / score / source_type / era / credibility_level）原样保留；
- Stage 4-5 的 source_kind / evidence_level / resource_type / resource_id /
  resource_name 原样保留；
- 阶段十新增 evidence_id / source_id / source_name / source_label / evidence_text，
  仅作为 Document / Resource 的统一访问入口，不表达新概念，不替代旧字段。
"""

from __future__ import annotations

from src.core.config import settings

# ── 来源类别（已有概念，Stage 4-4 引入，继续复用）────────────────────────────
SOURCE_KIND_DOCUMENT = "document"
SOURCE_KIND_RESOURCE = "resource"
# 阶段十三：知识图谱关系证据（与 document / resource 并列，走同一套 Evidence）
SOURCE_KIND_KG = "kg"

SOURCE_KIND_LABELS = {
    SOURCE_KIND_DOCUMENT: "文档",
    SOURCE_KIND_RESOURCE: "资源",
    SOURCE_KIND_KG: "知识图谱",
}

# Resource 细分类型 → 中文标签（与 Stage 4-4 Prompt 标注同源）
RESOURCE_TYPE_LABELS = {
    "herb": "中药",
    "prescription": "方剂",
    "theory": "理论",
    "literature": "文献",
}

# ── 证据等级（已有设计，不重新设计 confidence）──────────────────────────────
EVIDENCE_LEVEL_HIGH = "high"
EVIDENCE_LEVEL_MEDIUM = "medium"
EVIDENCE_LEVEL_INSUFFICIENT = "insufficient"

# score >= 0.7 → high；score >= 0.3 → medium；score < 0.3 → insufficient
EVIDENCE_HIGH_THRESHOLD = 0.7
EVIDENCE_MEDIUM_THRESHOLD = 0.3  # 与 RELEVANCE_THRESHOLD 对齐


def evidence_level(score: float) -> str:
    """按分数推导证据等级：high / medium / insufficient。

    - score >= 0.7 → high（强相关）
    - 0.3 <= score < 0.7 → medium（弱相关）
    - score < 0.3 → insufficient（不足；通常已被相关性门槛剔除）
    """
    if score >= EVIDENCE_HIGH_THRESHOLD:
        return EVIDENCE_LEVEL_HIGH
    if score >= EVIDENCE_MEDIUM_THRESHOLD:
        return EVIDENCE_LEVEL_MEDIUM
    return EVIDENCE_LEVEL_INSUFFICIENT


def hit_score(hit: dict) -> float:
    """取命中分数：rerank_score 优先，回退 score（与既有 Citation 逻辑一致）。"""
    return float(hit.get("rerank_score", hit.get("score", 0.0)) or 0.0)


def is_resource_hit(hit: dict) -> bool:
    """判断命中是否来自 Resource（Stage 4-4 判定逻辑，保持一致）。"""
    return hit.get("source_kind") == SOURCE_KIND_RESOURCE or bool(hit.get("resource_type"))


def hit_to_evidence(hit: dict, source_index: int) -> dict:
    """检索命中 → 统一 Evidence（Citation 兼容超集）。

    Args:
        hit: 检索命中（已注入 doc_name / source_kind 等元数据）
        source_index: 来源编号（1-based，对应答案中 [citation: 编号, 页码]）

    Returns:
        Evidence dict：旧 Citation 字段 + 统一 Evidence 字段。
    """
    score = hit_score(hit)
    is_kg = hit.get("source_kind") == SOURCE_KIND_KG
    is_resource = is_resource_hit(hit) or is_kg
    source_kind = (
        SOURCE_KIND_KG
        if is_kg
        else (SOURCE_KIND_RESOURCE if is_resource else SOURCE_KIND_DOCUMENT)
    )
    content = hit.get("content", "") or ""
    chunk_id = hit.get("id")

    if is_resource:
        # Resource：doc_id 为 SHA256（非真实文档），来源身份用 resource_id/name
        source_id = hit.get("resource_id") or hit.get("doc_id")
        source_name = (
            hit.get("resource_name")
            or hit.get("doc_name")
            or hit.get("resource_type")
            or SOURCE_KIND_LABELS[SOURCE_KIND_RESOURCE]
        )
        resource_type = hit.get("resource_type")
        base_label = RESOURCE_TYPE_LABELS.get(
            resource_type, resource_type or SOURCE_KIND_LABELS[SOURCE_KIND_RESOURCE]
        )
        # KG 命中标注为"图谱·中药"等，便于与同类型 Resource 证据区分
        source_label = (
            f"图谱·{base_label}" if is_kg else base_label
        )
    else:
        source_id = hit.get("doc_id")
        source_name = hit.get("doc_name") or "未知文档"
        resource_type = None
        source_label = SOURCE_KIND_LABELS[SOURCE_KIND_DOCUMENT]

    return {
        # ── 兼容性字段（Stage 4-5 及之前，禁止删除）──────────────────────
        "chunk_id": chunk_id,
        "source_index": source_index,
        "doc_id": hit.get("doc_id"),
        "doc_name": hit.get("doc_name") or ("资源" if is_resource else "未知文档"),
        "page_num": hit.get("page_num"),
        "title_path": hit.get("title_path"),
        "content": content,
        "score": score,
        "source_type": hit.get("source_type"),
        "era": hit.get("era"),
        "credibility_level": hit.get("credibility_level"),
        "source_kind": source_kind,
        "resource_type": resource_type,
        "resource_id": hit.get("resource_id") if is_resource else None,
        "resource_name": hit.get("resource_name") if is_resource else None,
        # ── 阶段十：统一 Evidence 访问入口 ──────────────────────────────
        "evidence_id": str(chunk_id) if chunk_id else f"{source_kind}:{source_id}:{source_index}",
        "source_id": str(source_id) if source_id is not None else None,
        "source_name": source_name,
        "source_label": source_label,
        "evidence_text": content,
        "evidence_level": evidence_level(score),
        # ── 阶段十四：KG 证据的关系属性（可选）───────────────────────────
        # Evidence Gate 需要按「关系类型 + 跳数 + 来源」判断 KG 证据可信度，
        # 不能只依赖 score（KG score 是实体匹配分，不是向量相似度）。
        # 仅 KG 证据携带；Citation / Evidence 的 Pydantic 输出字段保持不变，
        # 因此 API 响应结构与旧客户端解析不受影响。
        **(
            {
                "kg_relation": hit.get("kg_relation"),
                "kg_hop": hit.get("kg_hop"),
                "kg_provenance": hit.get("kg_provenance"),
            }
            if is_kg
            else {}
        ),
    }


def displayable_hits(hits: list[dict]) -> list[dict]:
    """可展示命中的子集（BUSINESS_RULES §6：相关度 ≥ RELEVANCE_THRESHOLD）。

    BUG-016：Prompt 编号 / Evidence 编号 / Citation 编号必须共用同一份
    「可展示命中」，否则模型会引用一个最终被阈值过滤掉的编号，UI 出现
    无对应卡片的 [citation:N]。阈值本身不变。

    注意：**缺少分数字段的命中视为相关度未知，不做静默丢弃**（按 0 处理会
    把这类命中整体过滤掉，与既有行为不兼容）；只有明确低于阈值的才过滤。
    """
    threshold = settings.RELEVANCE_THRESHOLD
    displayable: list[dict] = []
    for h in hits:
        raw = h.get("rerank_score", h.get("score", None))
        if raw is None:
            displayable.append(h)
            continue
        if float(raw or 0.0) >= threshold:
            displayable.append(h)
    return displayable


def build_evidence(hits: list[dict]) -> list[dict]:
    """检索命中 → 统一 Evidence 列表（过滤 + 分级）。

    过滤规则与既有 Citation 一致（BUSINESS_RULES §6）：
    低于 RELEVANCE_THRESHOLD 的命中不作为 Evidence 展示。

    BUG-016：编号按**过滤后**的顺序连续编号，与 Prompt 中展示给模型的
    编号一致（过去用未过滤位序，导致 Evidence/Reflection 与答案编号错位）。
    """
    evidence: list[dict] = []
    for hit in displayable_hits(hits):
        evidence.append(hit_to_evidence(hit, len(evidence) + 1))
    return evidence


def _group_key_and_label(evidence: dict) -> tuple[str, str, str | None]:
    """Evidence → (group_key, group_label, 细分类型)。

    - Resource：按 resource_type（herb / prescription / theory / literature）分组
    - KG：按 resource_type 归入 kg:<type> 组（与同名 Resource 组分开，便于实验对比）
    - Document：统一归入 document 组，组内按具体文档聚合
    """
    if evidence.get("source_kind") == SOURCE_KIND_KG:
        subtype = evidence.get("resource_type") or SOURCE_KIND_KG
        return (
            f"{SOURCE_KIND_KG}:{subtype}",
            f"{SOURCE_KIND_LABELS[SOURCE_KIND_KG]}·{RESOURCE_TYPE_LABELS.get(subtype, subtype)}",
            subtype,
        )
    if evidence.get("source_kind") == SOURCE_KIND_RESOURCE:
        subtype = evidence.get("resource_type") or SOURCE_KIND_RESOURCE
        return (
            f"{SOURCE_KIND_RESOURCE}:{subtype}",
            RESOURCE_TYPE_LABELS.get(subtype, subtype),
            subtype,
        )
    return SOURCE_KIND_DOCUMENT, SOURCE_KIND_LABELS[SOURCE_KIND_DOCUMENT], None


def group_evidence(evidence: list[dict]) -> list[dict]:
    """多来源证据分组：先按来源类别（+ 细分类型），组内再按具体来源聚合。

    结构：
        [
          {
            "group_key": "resource:herb", "source_kind": "resource",
            "source_type": "herb", "source_label": "中药",
            "source_count": 2, "evidence_count": 3,
            "max_score": 0.86, "evidence_level": "high",
            "sources": [
              {"source_id": ..., "source_name": "金银花", "evidence_level": "high",
               "max_score": 0.86, "evidence_count": 2, "evidences": [...]},
              ...
            ]
          },
          ...
        ]

    排序：组间按组内最高分降序（同分保持首次出现顺序）；组内来源同样按最高分降序。
    """
    groups: dict[str, dict] = {}
    order: list[str] = []

    for ev in evidence:
        group_key, group_label, subtype = _group_key_and_label(ev)
        group = groups.get(group_key)
        if group is None:
            group = {
                "group_key": group_key,
                "source_kind": ev.get("source_kind") or SOURCE_KIND_DOCUMENT,
                "source_type": subtype,
                "source_label": group_label,
                "sources": [],
                "_by_source": {},
            }
            groups[group_key] = group
            order.append(group_key)

        # 同一来源（同一味中药 / 同一个文档）的多个 chunk 聚合到一条来源下
        sid = ev.get("source_id") or ev.get("source_name") or group_key
        source = group["_by_source"].get(sid)
        if source is None:
            source = {
                "source_id": ev.get("source_id"),
                "source_name": ev.get("source_name") or "未知来源",
                "source_kind": ev.get("source_kind") or SOURCE_KIND_DOCUMENT,
                "source_type": subtype,
                "source_label": group_label,
                "evidences": [],
            }
            group["_by_source"][sid] = source
            group["sources"].append(source)
        source["evidences"].append(ev)

    result: list[dict] = []
    for group_key in order:
        group = groups[group_key]
        del group["_by_source"]
        for source in group["sources"]:
            source["evidences"].sort(key=lambda e: e.get("score", 0.0), reverse=True)
            source["evidence_count"] = len(source["evidences"])
            source["max_score"] = max((e.get("score", 0.0) for e in source["evidences"]), default=0.0)
            source["evidence_level"] = evidence_level(source["max_score"])
        group["sources"].sort(key=lambda s: s["max_score"], reverse=True)
        group["source_count"] = len(group["sources"])
        group["evidence_count"] = sum(s["evidence_count"] for s in group["sources"])
        group["max_score"] = max((s["max_score"] for s in group["sources"]), default=0.0)
        group["evidence_level"] = evidence_level(group["max_score"])
        result.append(group)

    # 组间按最高分降序（sorted 稳定：同分保持首次出现顺序）
    result.sort(key=lambda g: g["max_score"], reverse=True)
    return result


def summarize_evidence(
    evidence: list[dict], groups: list[dict] | None = None
) -> dict:
    """证据汇总：证据数 / 来源数 / 分组数 / 各等级数量 / 最高分。"""
    groups = groups if groups is not None else group_evidence(evidence)
    by_level = {
        EVIDENCE_LEVEL_HIGH: 0,
        EVIDENCE_LEVEL_MEDIUM: 0,
        EVIDENCE_LEVEL_INSUFFICIENT: 0,
    }
    for ev in evidence:
        level = ev.get("evidence_level") or evidence_level(ev.get("score", 0.0))
        by_level[level] = by_level.get(level, 0) + 1
    return {
        "evidence_count": len(evidence),
        "source_count": sum(g.get("source_count", 0) for g in groups),
        "group_count": len(groups),
        "max_score": max((ev.get("score", 0.0) for ev in evidence), default=0.0),
        "by_level": by_level,
    }


def package_evidence(evidence: list[dict]) -> tuple[list[dict], dict]:
    """把 Evidence 列表打包为 (groups, summary)，供 /ask 与 /ask-stream 复用。"""
    groups = group_evidence(evidence)
    return groups, summarize_evidence(evidence, groups)
