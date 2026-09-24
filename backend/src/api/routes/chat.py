"""智能问答路由：RAG 问答（同步 / SSE 流式）、会话管理。

安全规范（AGENTS.md §3 / TECH_DESIGN RBAC）：
- 所有端点受 protected_router 统一鉴权（Depends(get_current_user)）
- kb_ids 必须校验：用户可访问 ∩ 请求的 kb_ids == 请求的 kb_ids
- 会话归属校验：Conversation.user_id == request.state.user.id
- 新建会话用当前登录用户（不再硬编码 DEFAULT_ADMIN_EMAIL）
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.evidence import SOURCE_KIND_KG, package_evidence
from src.application.rag_service import RagService
from src.core.config import settings
from src.core.deps import get_accessible_kb_ids
from src.core.exceptions import PermissionDeniedError
from src.domain.models import Conversation, Message, User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[uuid.UUID] = Field(
        default_factory=list, description="检索的知识库列表（权限过滤的依据）"
    )
    conversation_id: uuid.UUID | None = None


class Citation(BaseModel):
    """引用来源（同时是统一 Evidence 的载体）。

    兼容约定（阶段十）：
    - 旧字段 chunk_id / source_index / doc_id / doc_name / page_num /
      title_path / content / score / source_type / era / credibility_level 不变；
    - Stage 4-5 增加 source_kind / evidence_level / resource_type / resource_id /
      resource_name；
    - 阶段十增加 evidence_id / source_id / source_name / source_label /
      evidence_text，用于 Document / Resource 统一表达（均为可选，缺省兼容旧数据）。
    """

    chunk_id: str
    source_index: int  # 来源编号（1-based，对应答案中 [citation: 编号, 页码] 的编号）
    doc_id: str
    doc_name: str
    page_num: int | None = None
    title_path: str | None = None
    content: str
    score: float
    source_type: str | None = None
    era: str | None = None
    credibility_level: int | None = None
    source_kind: str = "document"
    evidence_level: str = "insufficient"
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    # 阶段十：统一 Evidence 字段（向后兼容，缺省由调用方补齐）
    evidence_id: str = ""
    source_id: str | None = None
    source_name: str = ""
    source_label: str = ""
    evidence_text: str = ""


class Evidence(Citation):
    """统一 Evidence 模型（阶段十）。

    字段与 Citation 完全一致：Document 命中与 Resource（herb / prescription /
    theory / literature）命中统一映射到这里，避免为不同资源类型各建一套结构。
    """


class EvidenceSource(BaseModel):
    """同一来源（某味中药 / 某篇文献 / 某个文档）下的证据聚合。"""

    source_id: str | None = None
    source_name: str
    source_kind: str
    source_type: str | None = None
    source_label: str
    evidence_count: int
    max_score: float
    evidence_level: str
    evidences: list[Evidence]


class EvidenceGroup(BaseModel):
    """多来源证据分组：document / resource:{herb,prescription,theory,literature}。"""

    group_key: str
    source_kind: str
    source_type: str | None = None
    source_label: str
    source_count: int
    evidence_count: int
    max_score: float
    evidence_level: str
    sources: list[EvidenceSource]


class EvidenceSummary(BaseModel):
    evidence_count: int
    source_count: int
    group_count: int
    max_score: float
    by_level: dict  # {high: n, medium: n, insufficient: n}


class QueryAnalysisOut(BaseModel):
    """阶段十一：Query 分析结果（结构化，供前端与阶段十二 Dynamic Router 消费）。

    字段与 application.query_analyzer.QueryAnalysis.to_dict() 对齐。
    question_type 复用阶段九 QUESTION_TYPES 受控词表。

    注意：is_unanswerable_candidate 只是"可能无法可靠回答"的候选标记，
    不代表最终拒答（是否拒答仍由既有 Relevance Gate 决定）。
    """

    query: str
    question_type: str
    question_type_label: str
    resource_types: list[str] = []
    is_multi_source: bool = False
    is_unanswerable_candidate: bool = False
    keywords: list[str] = []
    entities: list[dict] = []
    features: dict = {}
    analyzer_version: str
    is_valid: bool = True
    fallback_reason: str | None = None


class RouterDecisionOut(BaseModel):
    """阶段十二：Dynamic Router 决策（解释"为什么用这个检索策略"）。

    字段与 application.dynamic_router.RouterDecision.to_dict() 对齐。
    strategy_name 对应 retrieval_strategies.STRATEGIES 中的策略，
    并可写入 EvaluationRun.retrieval_strategy 做实验对比。
    """

    strategy_name: str
    reason: str
    question_type: str
    resource_types: list[str] = []
    router_version: str
    strategy_description: str = ""
    resource_filter: dict = {}
    retrieval_config: dict = {}
    is_valid: bool = True
    fallback_reason: str | None = None


class GateDecisionOut(BaseModel):
    """阶段十四：Evidence Gate 决策（解释"证据是否足够 / 是否重试过"）。

    字段与 application.evidence_gate.GateDecision.to_dict() 对齐。
    decision ∈ {accept, insufficient, retry}；Gate 关闭时整个字段为 None。
    """

    decision: str
    reason: str = ""
    gate_version: str = "gate-v1"
    evidence_count: int = 0
    accepted_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    weak_count: int = 0
    source_kind_counts: dict = Field(default_factory=dict)
    best_score: float = 0.0
    is_valid: bool = True
    fallback_reason: str | None = None
    # retry 信息：最多一次，retry 后仍不足 → decision=insufficient
    retry_reason: str | None = None
    original_strategy: str | None = None
    retry_strategy: str | None = None
    retried: bool = False
    details: dict = Field(default_factory=dict)


class ReflectionDecisionOut(BaseModel):
    """阶段十五：Self Reflection 决策（解释"答案是否忠实使用了证据"）。

    字段与 application.self_reflection.ReflectionDecision.to_dict() 对齐。
    decision ∈ {accept, revise, retry}：
    - accept：答案与证据一致
    - revise：答案表达超出证据，已基于同一份证据重写（最多一次）
    - retry：证据不足以支撑答案，已换策略重检索一次（最多一次）
    Reflection 关闭时整个字段为 None。
    """

    decision: str
    reason: str = ""
    reflection_version: str = "reflection-v1"
    confidence: float = 0.0
    issues: list[str] = Field(default_factory=list)
    retry_strategy: str | None = None
    retried: bool = False
    is_valid: bool = True
    fallback_reason: str | None = None
    details: dict = Field(default_factory=dict)
    # 阶段十五 §6：Gate retry 与 Reflection retry 分别计数，保证总次数有界
    gate_retry_count: int = 0
    reflection_retry_count: int = 0
    total_retry_count: int = 0
    revised: bool = False
    # 直接引用 Gate 结论（Reflection 不重复计算 Evidence Gate）
    gate_decision: str | None = None
    gate_version: str | None = None
    original_strategy: str | None = None
    retry_reason: str | None = None
    llm_used: bool = False


class ChatAnswerResponse(BaseModel):
    conversation_id: str
    message_id: str
    answer: str
    citations: list[Citation]
    # 阶段十：多来源证据（citations 保持原样，前端旧展示逻辑不受影响）
    evidence: list[Evidence] = []
    evidence_groups: list[EvidenceGroup] = []
    evidence_summary: EvidenceSummary | None = None
    # 阶段十一：Query 分析（新增字段，旧字段与旧解析逻辑不受影响）
    query_analysis: QueryAnalysisOut | None = None
    # 阶段十二：Dynamic Router 决策（新增字段，旧字段不变）
    router_decision: RouterDecisionOut | None = None
    # 阶段十三：KG 关系证据切片（citations/evidence 已包含，此处仅为便于观察/实验）
    kg_evidence: list[Evidence] = []
    # 阶段十四：Evidence Gate 决策（向后兼容：Gate 关闭时为 None，旧字段不变）
    evidence_gate: GateDecisionOut | None = None
    # 阶段十五：Self Reflection 决策（向后兼容：Reflection 关闭时为 None）
    reflection: ReflectionDecisionOut | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    kb_ids: list[uuid.UUID]
    created_at: object


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: dict | None
    created_at: object


# ── 公共安全校验 ─────────────────────────────────────────────────────────────


async def _check_model_configured(db: AsyncSession) -> None:
    """BUSINESS_RULES §10：模型未配置时禁止提问。

    mock 模式允许（开发/测试）；非 mock 模式必须有 base_url 和 model。
    """
    from src.application.model_config_service import get_effective_config_cached
    from src.core.exceptions import AppException

    config = await get_effective_config_cached(db)
    provider = config.get("llm_provider", "mock")
    if provider == "mock":
        return  # mock 模式视为已配置（开发/测试用）
    base_url = config.get("llm_base_url", "")
    model = config.get("llm_model", "")
    if not base_url or not model:
        # BUG-034：412 Precondition Failed 语义为"请求头前置条件不满足"
        # （ETag/If-Match），此处是服务端缺少必要配置 → 503 Service Unavailable。
        raise AppException(
            503,
            "模型尚未配置，请先在管理中心-系统设置中配置模型参数",
        )


async def _validate_kb_access(
    db: AsyncSession, user: User, kb_ids: list[uuid.UUID]
) -> None:
    """校验用户是否可访问请求的全部 kb_ids。

    Raises:
        PermissionDeniedError: 存在无权访问的 kb_id
    """
    accessible = await get_accessible_kb_ids(db, user)
    forbidden = set(kb_ids) - accessible
    if forbidden:
        raise PermissionDeniedError(f"无权访问以下知识库: {forbidden}")


async def _validate_conversation_owner(
    db: AsyncSession, user: User, conversation_id: uuid.UUID
) -> Conversation:
    """校验会话归属并返回会话。

    Raises:
        NotFoundError: 会话不存在
        PermissionDeniedError: 会话不属于当前用户
    """
    from src.core.exceptions import NotFoundError

    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("会话不存在")
    if conv.user_id != user.id:
        raise PermissionDeniedError("无权访问该会话")
    return conv


async def _with_heartbeat(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """为 SSE 业务事件流补心跳与总超时保护（BUG-035）。

    反向代理（nginx proxy_read_timeout 默认 60s）会掐断检索阶段长时间无数据的
    连接——此时后端仍在工作，客户端却只看到连接被断。本函数在等待业务事件的
    间隙发送 SSE 注释帧（``: ping``）：注释帧对事件解析无副作用（SSE 规范以
    冒号开头的行是注释，前端 useSSE 与测试解析器均按 event:/data: 解析），
    但足以让代理与客户端认定连接存活。

    总超时（默认 900s）到期时先发 error 事件再关闭流，避免"卡住不返回"的
    请求无限占用连接。单次请求内的等待用 shield 包裹：超时只取消外层等待，
    不会打断尚未完成的业务协程（直接 await __anext__ 被取消会连带终止生成器）。
    """
    interval = max(settings.SSE_HEARTBEAT_INTERVAL_SECONDS, 1)
    total = settings.SSE_TOTAL_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total if total > 0 else None
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(source.__anext__())
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                err = json.dumps(
                    {"message": f"问答超时（超过 {total} 秒），请重试"}, ensure_ascii=False
                )
                yield f"event: error\ndata: {err}\n\n"
                return
            try:
                event = await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=interval if remaining is None else min(interval, remaining),
                )
            except asyncio.TimeoutError:
                if deadline is not None and loop.time() >= deadline:
                    err = json.dumps(
                        {"message": f"问答超时（超过 {total} 秒），请重试"},
                        ensure_ascii=False,
                    )
                    yield f"event: error\ndata: {err}\n\n"
                    return
                yield ": ping\n\n"
                continue
            except StopAsyncIteration:
                return
            except Exception as exc:  # 生成器逃逸的异常也要转成 error 事件，不留半截流
                logger.exception(f"流式问答异常: {exc}")
                err = json.dumps({"message": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {err}\n\n"
                return
            pending = None
            yield event
    finally:
        if pending is not None:
            pending.cancel()
        try:
            await source.aclose()
        except Exception:  # noqa: BLE001  关闭阶段不影响已发出的响应
            pass


# ── 端点实现 ────────────────────────────────────────────────────────────────

@router.post("/ask", response_model=ChatAnswerResponse)
async def ask(
    payload: ChatAskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChatAnswerResponse:
    """RAG 问答（同步）。

    安全：校验 kb_ids 权限 + 校验 conversation_id 归属 + RagService 使用当前用户。
    """
    from src.core.exceptions import AppException

    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    user: User = request.state.user
    await _check_model_configured(db)
    await _validate_kb_access(db, user, payload.kb_ids)

    if payload.conversation_id is not None:
        await _validate_conversation_owner(db, user, payload.conversation_id)

    service = RagService(db)
    conv, assistant, citations, query_analysis, router_decision = await service.ask(
        user=user,
        kb_ids=payload.kb_ids,
        question=payload.question.strip(),
        conversation_id=payload.conversation_id,
    )
    # 阶段十：多来源证据分组（与 SSE 路径共用同一套分组逻辑）
    groups, summary = package_evidence(citations)
    return ChatAnswerResponse(
        conversation_id=str(conv.id),
        message_id=str(assistant.id),
        answer=assistant.content,
        citations=[Citation(**c) for c in citations],
        evidence=[Evidence(**c) for c in citations],
        evidence_groups=groups,
        evidence_summary=summary,
        # 阶段十一：Query 分析（检索前分析）
        query_analysis=QueryAnalysisOut(**query_analysis.to_dict()),
        # 阶段十二：路由决策（检索前选择策略，检索仍走 Baseline 组件）
        router_decision=RouterDecisionOut(**router_decision.to_dict()),
        # 阶段十三：KG 证据切片（未启用 KG 策略时为空列表）
        kg_evidence=[
            Evidence(**c) for c in citations if c.get("source_kind") == SOURCE_KIND_KG
        ],
        # 阶段十四：Evidence Gate 决策（Gate 关闭时为 None）
        evidence_gate=(
            GateDecisionOut(**service.last_gate_decision.to_dict())
            if service.last_gate_decision is not None
            else None
        ),
        # 阶段十五：Self Reflection 决策（Reflection 关闭时为 None）
        reflection=(
            ReflectionDecisionOut(**service.last_reflection_decision.to_dict())
            if service.last_reflection_decision is not None
            else None
        ),
    )


@router.post("/ask-stream")
async def ask_stream(
    payload: ChatAskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """RAG 问答（SSE 流式）。

    安全：校验 kb_ids 权限 + 校验 conversation_id 归属 + RagService 使用当前用户。
    事件协议见原注释，不变（start → citations → delta×N → done）。
    阶段十一：start 事件 data 增加 query_analysis（事件名与顺序不变）。
    阶段十二：start 事件 data 增加 router_decision（事件名与顺序不变）。
    BUG-035：事件流外层包 _with_heartbeat（注释帧心跳 + 总超时），
    事件名与顺序不变，旧客户端解析不受影响。
    """
    from src.core.exceptions import AppException

    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    user: User = request.state.user
    await _check_model_configured(db)
    await _validate_kb_access(db, user, payload.kb_ids)

    if payload.conversation_id is not None:
        await _validate_conversation_owner(db, user, payload.conversation_id)

    service = RagService(db)

    async def event_stream():
        try:
            async for evt in service.ask_stream(
                user=user,
                kb_ids=payload.kb_ids,
                question=payload.question.strip(),
                conversation_id=payload.conversation_id,
            ):
                data = json.dumps(evt["data"], ensure_ascii=False)
                yield f"event: {evt['event']}\ndata: {data}\n\n"
        except Exception as exc:  # 兜底，避免流中断无提示
            logger.exception(f"流式问答异常: {exc}")
            err = json.dumps({"message": str(exc)}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        _with_heartbeat(event_stream()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 不缓冲，保证实时推送
        },
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    """历史会话列表（按创建时间倒序）。

    安全隔离：只返回当前用户自己创建的会话（Conversation.user_id == current_user.id）。
    """
    user: User = request.state.user
    rows = await db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
    )
    return list(rows)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    """会话消息记录（按时间升序，含 citations JSON）。

    安全隔离：校验会话归属（Conversation.user_id == current_user.id），防止越权读取他人会话消息。
    """
    user: User = request.state.user
    await _validate_conversation_owner(db, user, conversation_id)

    rows = await db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return list(rows)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除历史会话（级联删除消息 + 清理 Redis 缓存）。

    安全隔离：校验会话归属（Conversation.user_id == current_user.id），防止越权删除他人会话。
    Message 通过 ORM relationship cascade="all, delete-orphan" 级联删除，
    无需手动逐条删除消息。
    """
    from src.core.exceptions import NotFoundError
    from src.infrastructure.redis_client import get_conversation_cache

    user: User = request.state.user
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("会话不存在")
    if conv.user_id != user.id:
        raise PermissionDeniedError("无权删除该会话")

    await db.delete(conv)
    await db.commit()

    # 清理 Redis 对话缓存（失败仅告警，不阻断删除主流程）
    try:
        cache = get_conversation_cache()
        await cache.delete(conversation_id)
    except Exception as exc:
        logger.warning(f"清理会话缓存失败（不影响删除）: {exc}")

    logger.info(f"用户 {user.id} 删除会话 {conversation_id}")
    return {"id": str(conversation_id), "deleted": True}


@router.delete("/conversations")
async def delete_all_conversations(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除当前用户的全部历史会话（级联删除消息 + 清理 Redis 缓存）。

    安全隔离：只删除当前用户创建的会话（Conversation.user_id == current_user.id）。
    """
    from sqlalchemy import delete as sa_delete

    from src.infrastructure.redis_client import get_conversation_cache

    user: User = request.state.user

    # 先查出所有会话 ID（供清理 Redis 缓存用）
    rows = await db.scalars(
        select(Conversation.id).where(Conversation.user_id == user.id)
    )
    conv_ids = list(rows)
    if not conv_ids:
        return {"deleted_count": 0}

    # 批量删除会话 → Message 通过 DB 级 ondelete=CASCADE 自动级联删除
    await db.execute(
        sa_delete(Conversation).where(Conversation.id.in_(conv_ids))
    )
    await db.commit()

    # 清理 Redis 对话缓存（BUG-037）：逐条独立容错——单条失败不得中断其余会话的
    # 清理（旧实现的 try 包住整个循环，第一条失败即放弃剩余）；失败清单随响应
    # 返回，调用方可见"哪些缓存没清掉"，而不是以为全部成功。
    failed: list[str] = []
    try:
        cache = get_conversation_cache()
    except Exception as exc:
        logger.warning(f"会话缓存不可用，跳过缓存清理（会话已删除）: {exc}")
        cache = None
        failed = [str(cid) for cid in conv_ids]

    if cache is not None:
        for cid in conv_ids:
            try:
                await cache.delete(cid)
            except Exception as exc:
                failed.append(str(cid))
                logger.warning(f"清理会话缓存失败 cid={cid}: {exc}")

    logger.info(
        f"用户 {user.id} 删除全部会话，共 {len(conv_ids)} 条，"
        f"缓存清理失败 {len(failed)} 条"
    )
    return {"deleted_count": len(conv_ids), "cache_cleanup_failed": failed}
