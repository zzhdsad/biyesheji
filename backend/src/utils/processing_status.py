"""统一处理状态（第五阶段）：把三条已有链路的真实状态字段翻译成同一套语义。

不新增任务表、不改任何业务状态列，也不给解析器加新流程：这里只做只读派生。
三条链路保留各自的库字段（documents.parse_status / index_status、
structured_parses.status、structured_groups.status），但对外说同一句话：

- pending          待处理
- processing       处理中
- awaiting_confirm 待用户确认（只有关联文件组有这一步）
- success          已完成
- failed           处理失败

阶段（stages）只由已经落库的真实信号推出：切片数、向量数、字段数、记录数、
关系是否已确认、校验报告是否存在、关联记录条数。因此进度可以核对，
不存在估算出来的百分比；documents.progress_percent 那列历史遗留值本模块不使用。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

# ── 统一状态词汇 ─────────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_AWAITING_CONFIRM = "awaiting_confirm"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

# 三条链路都不提供「取消」：后台任务一旦派发就跑完或失败，
# 因此这里没有 cancelled 状态，界面上也不出现取消按钮。

# ── 各链路的真实阶段序列（前端直接按顺序渲染）───────────────────────────────
STAGE_LABELS_DOCUMENT = ["上传", "内容解析", "文本切片", "向量化", "完成"]
STAGE_LABELS_STRUCTURED = ["上传", "字段识别", "数据解析", "知识记录生成", "完成"]
STAGE_LABELS_GROUP = [
    "文件组上传",
    "字段识别",
    "配置关联字段",
    "用户确认",
    "数据校验",
    "关联记录生成",
    "完成",
]

PIPELINE_DOCUMENT = "document"
PIPELINE_STRUCTURED = "structured"
PIPELINE_GROUP = "group"

STAGE_DONE = "done"
STAGE_ACTIVE = "active"
STAGE_PENDING = "pending"
STAGE_FAILED = "failed"

# 与 structured_parse_service._STALE_PARSING_MINUTES 同口径：崩溃残留允许重来
_STALE_PARSING_MINUTES = 30


def _stage_dict(labels: Sequence[str], completed: Sequence[bool], current: int | None = None,
                failed_at: int | None = None) -> list[dict[str, Any]]:
    """按「已完成 / 正在进行 / 失败在哪一步」生成阶段列表。"""
    stages: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        if failed_at is not None and index == failed_at:
            state = STAGE_FAILED
        elif index < len(completed) and completed[index]:
            state = STAGE_DONE
        elif current == index:
            state = STAGE_ACTIVE
        else:
            state = STAGE_PENDING
        stages.append({"index": index, "label": label, "state": state})
    return stages


def _first_open_index(stages: Sequence[Mapping[str, Any]]) -> int:
    for stage in stages:
        if stage["state"] != STAGE_DONE:
            return int(stage["index"])
    return len(stages) - 1


def _result(
    *,
    pipeline: str,
    labels: Sequence[str],
    completed: Sequence[bool],
    current: int | None,
    failed_at: int | None,
    status: str,
    reason: str = "",
    blocked_reason: str = "",
    results: Mapping[str, Any] | None = None,
    retry_actions: Sequence[str] = (),
    next_action: str = "none",
) -> dict[str, Any]:
    stages = _stage_dict(labels, completed, current, failed_at)
    stage_index = _first_open_index(stages)
    return {
        "pipeline": pipeline,
        "status": status,
        "stage_index": stage_index,
        "stage_label": labels[stage_index],
        "stage_total": len(labels),
        "stages": stages,
        "reason": reason,
        "blocked_reason": blocked_reason,
        "retryable": bool(retry_actions),
        "retry_actions": list(retry_actions),
        "next_action": next_action,
        "results": dict(results or {}),
    }


def is_stale_processing(stamp: Any, minutes: int = _STALE_PARSING_MINUTES) -> bool:
    """时间戳是否已经旧到可以判定为「任务残留」而非「确实在跑」。

    后台任务被进程杀掉会把状态永久停在处理中，重试守卫据此放行，
    口径与第三阶段结构化解析服务的 _STALE_PARSING_MINUTES 一致。
    """
    if stamp is None:
        return True
    if getattr(stamp, "tzinfo", None) is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < datetime.now(timezone.utc) - timedelta(minutes=minutes)


_is_stale = is_stale_processing


# ── 普通文档：上传 → 内容解析 → 文本切片 → 向量化 → 完成 ─────────────────────
def document_processing(doc: Any) -> dict[str, Any]:
    labels = STAGE_LABELS_DOCUMENT
    deleted = getattr(doc, "deleted_at", None) is not None
    missing_file = not getattr(doc, "storage_path", "")
    blocked = ""
    if deleted:
        blocked = "文件已在回收站，停止处理；如需恢复请先从回收站还原"
    elif missing_file:
        blocked = "原始文件不存在，无法重新处理"

    parse_status = str(getattr(doc, "parse_status", "pending") or "pending")
    index_status = str(getattr(doc, "index_status", "not_indexed") or "not_indexed")
    chunk_count = int(getattr(doc, "chunk_count", 0) or 0)
    vector_count = int(getattr(doc, "vector_count", 0) or 0)

    completed = [True, False, False, False, False]
    current: int | None = 1
    failed_at: int | None = None
    status = STATUS_PENDING
    reason = ""
    retry: list[str] = []
    next_action = "reparse"

    if parse_status == "parsing":
        status = STATUS_PROCESSING
        # 长时间停在 parsing 说明后台进程已经没了：按残留放行，允许重试
        if _is_stale(getattr(doc, "updated_at", None)):
            reason = "解析任务超过 30 分钟未推进，视为中断，可重新发起"
            retry = ["reparse"]
    elif parse_status == "failed":
        status = STATUS_FAILED
        failed_at = 1
        reason = str(getattr(doc, "error_message", "") or "解析失败")
        retry = ["reparse"]
        next_action = "reparse"
    elif parse_status == "success":
        completed = [True, True, True, False, False]
        current = 3
        if index_status == "failed":
            status = STATUS_FAILED
            failed_at = 3
            reason = str(getattr(doc, "index_error", "") or getattr(doc, "error_message", "") or "向量化失败")
            retry = ["revectorize"]
            next_action = "revectorize"
        elif index_status == "stale":
            status = STATUS_PENDING
            reason = "内容已变更，现有向量与最新文件不一致，需重新向量化"
            retry = ["revectorize"]
            next_action = "revectorize"
        elif index_status == "indexed" and vector_count:
            completed = [True, True, True, True, True]
            current = None
            status = STATUS_SUCCESS
            next_action = "none"
            retry = ["revectorize", "reparse"]
        else:
            status = STATUS_PENDING
            reason = "切片已就绪，等待向量化"
            retry = ["revectorize", "reparse"]
            next_action = "revectorize"
    elif parse_status == "completed":
        completed = [True, True, True, True, True]
        current = None
        status = STATUS_SUCCESS
        next_action = "none"
        if index_status == "stale" or (vector_count and chunk_count and vector_count != chunk_count):
            # 切片数与向量数不一致 = 索引与内容脱节，如实回退到「向量化」这一步
            completed = [True, True, True, False, False]
            current = 3
            status = STATUS_PENDING
            reason = (
                "内容已变更，需重新向量化"
                if index_status == "stale"
                else f"切片 {chunk_count} 条与向量 {vector_count} 条不一致，需重新向量化"
            )
            retry = ["revectorize"]
            next_action = "revectorize"
    elif parse_status == "skipped":
        # 结构化空间的文件：普通文档流水线本就不参与，交给 structured 视图
        completed = [True, False, False, False, False]
        current = None
        status = STATUS_PENDING
        reason = "结构化数据文件不参与解析与向量化"
        next_action = "none"
    else:  # pending
        status = STATUS_PENDING
        reason = "尚未解析"
        retry = ["reparse"]

    if blocked:
        status = STATUS_FAILED if status == STATUS_PENDING else status
        current = None
        retry = []
        next_action = "none"

    return _result(
        pipeline=PIPELINE_DOCUMENT,
        labels=labels,
        completed=completed,
        current=current,
        failed_at=failed_at,
        status=status,
        reason=reason,
        blocked_reason=blocked,
        results={
            "parse_status": parse_status,
            "index_status": index_status,
            "chunk_count": chunk_count,
            "vector_count": vector_count,
            "vector_model": getattr(doc, "vector_model", None),
            "indexed_at": getattr(doc, "indexed_at", None),
            "file_size": int(getattr(doc, "file_size", 0) or 0),
            "file_type": getattr(doc, "file_type", ""),
            "deleted": deleted,
        },
        retry_actions=retry,
        next_action=next_action,
    )


# ── 非关联结构化：上传 → 字段识别 → 数据解析 → 知识记录生成 → 完成 ───────────
def structured_processing(doc: Any, parse: Any, *, format_supported: bool = True) -> dict[str, Any]:
    labels = STAGE_LABELS_STRUCTURED
    deleted = getattr(doc, "deleted_at", None) is not None
    missing_file = not getattr(doc, "storage_path", "")
    blocked = "文件已在回收站，停止处理；如需恢复请先从回收站还原" if deleted else (
        "" if not missing_file else "原始文件不存在，无法重新处理"
    )

    status_value = str(getattr(parse, "status", "") or "") if parse is not None else ""
    field_count = int(getattr(parse, "field_count", 0) or 0) if parse is not None else 0
    record_count = int(getattr(parse, "record_count", 0) or 0) if parse is not None else 0
    anomaly_count = int(getattr(parse, "anomaly_count", 0) or 0) if parse is not None else 0
    skipped_count = int(getattr(parse, "skipped_count", 0) or 0) if parse is not None else 0
    # 第六阶段：向量化状态（None = 该行还没有向量状态，按阶段五行为处理）
    vector_status = getattr(parse, "vector_status", None) if parse is not None else None
    vector_count = int(getattr(parse, "vector_count", 0) or 0) if parse is not None else 0
    vector_error = str(getattr(parse, "vector_error", "") or "") if parse is not None else ""

    completed = [True, False, False, False, False]
    current: int | None = 1
    failed_at: int | None = None
    status = STATUS_PENDING
    reason = ""
    retry: list[str] = []
    next_action = "structured_reparse"

    if not format_supported:
        completed = [True, False, False, False, False]
        current = None
        status = STATUS_PENDING
        reason = "该格式不参与结构化解析（仅作为文件保存）"
        next_action = "none"
    elif parse is None:
        reason = "尚未解析"
        retry = ["structured_reparse"]
    elif status_value == "pending":
        reason = "已排队，等待后台识别字段"
        retry = ["structured_reparse"]
    elif status_value == "parsing":
        status = STATUS_PROCESSING
        if _is_stale(getattr(parse, "updated_at", None) or getattr(parse, "created_at", None)):
            reason = "解析任务超过 30 分钟未推进，视为中断，可重新发起"
            retry = ["structured_reparse"]
    elif status_value == "failed":
        status = STATUS_FAILED
        failed_at = 1
        reason = str(getattr(parse, "error_message", "") or "解析失败")
        retry = ["structured_reparse"]
        # 失败保留旧结果：已经存在的记录不算本次成功，但如实告知还剩多少
        if record_count:
            completed = [True, True, True, True, False]
            reason += f"（上次成功生成的 {record_count} 条记录仍保留）"
    elif status_value == "success":
        completed = [True, True, True, record_count > 0, record_count > 0]
        current = None if record_count else 3
        status = STATUS_SUCCESS if record_count else STATUS_PENDING
        next_action = "none" if record_count else "structured_reparse"
        retry = ["structured_reparse"]
        reason = (
            ""
            if record_count
            else "解析完成但没有产生记录（空文件或全部行被跳过）"
        )
        if record_count and getattr(parse, "truncated", False):
            reason = "记录数达到上限，后面未解析"
        # 第六阶段：记录已生成后再看向量化结果——不把「待向量化」谎报成「已完成」
        if record_count and vector_status == "indexing":
            completed[4] = False
            current = 4
            status = STATUS_PROCESSING
            reason = "知识记录已生成，正在写入向量索引"
            retry = ["revectorize", "structured_reparse"]
            next_action = "revectorize"
        elif record_count and vector_status == "failed":
            completed[4] = False
            current = 4
            failed_at = 4
            status = STATUS_FAILED
            reason = f"向量化失败：{vector_error or '未知原因'}"
            retry = ["revectorize", "structured_reparse"]
            next_action = "revectorize"
        elif record_count and vector_status == "pending" and vector_count <= 0:
            completed[4] = False
            current = 4
            status = STATUS_PENDING
            reason = "知识记录已生成，等待向量化"
            retry = ["revectorize", "structured_reparse"]
            next_action = "revectorize"
        elif record_count and vector_status == "completed" and vector_count <= 0:
            # 解析成功但所有记录的 knowledge_text 都为空：如实说明没有可向量化内容，
            # 不反复提示「重新向量化」（重跑结果不会改变）。
            reason = reason or "已生成记录，但没有可向量化的知识文本"
            next_action = "none"
    else:
        reason = f"未知解析状态：{status_value}"
        status = STATUS_FAILED
        failed_at = 1

    if blocked:
        retry = []
        next_action = "none"

    return _result(
        pipeline=PIPELINE_STRUCTURED,
        labels=labels,
        completed=completed,
        current=current,
        failed_at=failed_at,
        status=status,
        reason=reason,
        blocked_reason=blocked,
        results={
            "parse_status": status_value or "none",
            "detected_format": (getattr(parse, "detected_format", "") if parse is not None else ""),
            "field_count": field_count,
            "record_count": record_count,
            "anomaly_count": anomaly_count,
            "skipped_count": skipped_count,
            "truncated": bool(getattr(parse, "truncated", False)) if parse is not None else False,
            "parsed_at": getattr(parse, "parsed_at", None) if parse is not None else None,
            "file_size": int(getattr(doc, "file_size", 0) or 0),
            "deleted": deleted,
            # 第六阶段：向量索引状态（数量为真实写入条数，不估算）
            "vector_status": vector_status,
            "vector_count": vector_count,
            "vector_model": getattr(parse, "vector_model", None) if parse is not None else None,
            "vector_error": vector_error,
            "vectorized_at": (
                getattr(parse, "vectorized_at", None) if parse is not None else None
            ),
        },
        retry_actions=retry,
        next_action=next_action,
    )


# ── 关联文件组：上传 → 字段识别 → 配置关联字段 → 用户确认 → 校验 → 生成 → 完成 ──
def group_processing(group: Any, files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    labels = STAGE_LABELS_GROUP
    relations = [dict(r) for r in (getattr(group, "relations", None) or [])]
    validation = dict(getattr(group, "validation", None) or {})
    status_value = str(getattr(group, "status", "") or "")
    record_count = int(getattr(group, "record_count", 0) or 0)
    error_message = str(getattr(group, "error_message", "") or "")
    # 第六阶段：关联记录的向量索引状态（None = 尚未接入，按阶段五行为处理）
    vector_status = getattr(group, "vector_status", None)
    vector_count = int(getattr(group, "vector_count", 0) or 0)
    vector_error = str(getattr(group, "vector_error", "") or "")

    usable = [f for f in files if not f.get("removed")]
    succeeded = [f for f in usable if f.get("parse_status") == "success"]
    failed_members = [f for f in usable if f.get("parse_status") == "failed"]
    # 只有 parsing 算「正在处理」；pending 与另两条链路同口径，表示已派发但还没开始
    running_members = [f for f in usable if f.get("parse_status") == "parsing"]
    removed = [f for f in files if f.get("removed")]

    unconfirmed = [r for r in relations if not r.get("confirmed")]
    has_validation = bool(validation.get("issues") is not None) and bool(validation)
    structural_errors = int(validation.get("structural_error_count", 0) or 0)

    completed = [True] + [False] * (len(labels) - 1)
    current: int | None = 1
    failed_at: int | None = None
    status = STATUS_PENDING
    reason = ""
    next_action = "reparse_members"

    if len(usable) < 2:
        current = None
        failed_at = 0
        status = STATUS_FAILED
        reason = "组内可用文件不足 2 个，关联关系无法成立，请补充文件或重新建组"
        next_action = "add_files"
    elif failed_members:
        current = 1
        failed_at = 1
        status = STATUS_FAILED
        reason = "、".join(
            f"「{f.get('alias') or f.get('current_file_name') or f.get('file_name')}」解析失败"
            for f in failed_members
        )
        next_action = "reparse_members"
    elif len(succeeded) < len(usable):
        current = 1
        status = STATUS_PROCESSING if running_members else STATUS_PENDING
        reason = "组内还有文件未完成字段识别，配置关联关系需要全部解析成功"
        next_action = "reparse_members"
    else:
        completed[1] = True
        if not relations:
            current = 2
            reason = "还没有关联关系：请在「关系配置」中手动选择两侧文件和关联字段"
            next_action = "manual_relations"
        elif unconfirmed:
            completed[2] = True
            current = 3
            status = STATUS_AWAITING_CONFIRM
            reason = f"还有 {len(unconfirmed)} 条关系未经你确认，未确认的关系不参与生成"
            next_action = "confirm_relations"
        elif not has_validation:
            completed[2] = True
            completed[3] = True
            current = 4
            status = STATUS_AWAITING_CONFIRM
            reason = "关系已全部确认，请运行数据校验"
            next_action = "validate"
        elif structural_errors:
            completed[2] = True
            current = 4
            failed_at = 4
            status = STATUS_FAILED
            reason = f"校验发现 {structural_errors} 条不成立的关系（文件或字段已不存在），需先修正"
            next_action = "validate"
        else:
            completed[2] = True
            completed[3] = True
            completed[4] = True
            if record_count and status_value == "confirmed":
                completed[5] = True
                completed[6] = True
                current = None
                status = STATUS_SUCCESS
                reason = f"已生成 {record_count} 条关联知识记录"
                next_action = "regenerate"
                # 关联记录已生成，再看向量化结果（未确认的组不会走到这里）
                if vector_status == "indexing":
                    completed[6] = False
                    current = 6
                    status = STATUS_PROCESSING
                    reason = f"已生成 {record_count} 条关联知识记录，正在写入向量索引"
                    next_action = "revectorize"
                elif vector_status == "failed":
                    completed[6] = False
                    current = 6
                    failed_at = 6
                    status = STATUS_FAILED
                    reason = f"关联记录向量化失败：{vector_error or '未知原因'}"
                    next_action = "revectorize"
                elif vector_status == "pending" and vector_count <= 0:
                    completed[6] = False
                    current = 6
                    status = STATUS_PENDING
                    reason = f"已生成 {record_count} 条关联知识记录，等待向量化"
                    next_action = "revectorize"
                elif vector_status == "completed" and vector_count <= 0:
                    reason = reason + "（没有可向量化的知识文本）"
                    next_action = "regenerate"
            elif status_value == "stale":
                current = 4
                status = STATUS_AWAITING_CONFIRM
                reason = error_message or "组内文件已变化，校验与生成结果已作废，需重新校验并确认"
                next_action = "validate"
            else:
                current = 5
                status = STATUS_AWAITING_CONFIRM
                warnings = int(validation.get("warning_count", 0) or 0)
                reason = (
                    f"校验通过（{warnings} 条提示），等待你确认生成关联知识记录"
                    if warnings
                    else "校验通过，等待你确认生成关联知识记录"
                )
                if validation.get("force_required"):
                    reason += "；存在外键无法匹配的记录，确认时需明确知情"
                next_action = "generate"

    if error_message and status in (STATUS_PENDING, STATUS_AWAITING_CONFIRM) and not reason:
        reason = error_message

    if next_action == "revectorize":
        retry_actions = ["revectorize", "validate", "generate"]
    elif next_action in ("validate", "generate", "regenerate"):
        retry_actions = ["validate", "generate"]
    else:
        retry_actions = []
    return _result(
        pipeline=PIPELINE_GROUP,
        labels=labels,
        completed=completed,
        current=current,
        failed_at=failed_at,
        status=status,
        reason=reason,
        blocked_reason="、".join(
            f"「{f.get('file_name')}」已被删除或移入回收站" for f in removed
        ),
        results={
            "group_status": status_value,
            "file_count": len(files),
            "usable_file_count": len(usable),
            "relation_count": len(relations),
            "unconfirmed_count": len(unconfirmed),
            "record_count": record_count,
            "error_count": int(validation.get("error_count", 0) or 0),
            "warning_count": int(validation.get("warning_count", 0) or 0),
            "force_required": bool(validation.get("force_required")),
            "can_confirm": bool(validation.get("can_confirm")),
            "validated_at": getattr(group, "validated_at", None),
            "confirmed_at": getattr(group, "confirmed_at", None),
            # 第六阶段：向量索引状态（数量为真实写入条数，不估算）
            "vector_status": vector_status,
            "vector_count": vector_count,
            "vector_model": getattr(group, "vector_model", None),
            "vector_error": vector_error,
            "vectorized_at": getattr(group, "vectorized_at", None),
        },
        retry_actions=retry_actions,
        next_action=next_action,
    )
