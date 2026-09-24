"""Batch 3（评测可信度）回归测试。

覆盖 BUG-011 / BUG-019 / BUG-022 / BUG-023 / BUG-024 / BUG-025：

- BUG-011：失败用例不计入 CR / AC 均值，单独 failed_count；
          全部失败 ⇒ infra_failed=True（不再伪装成"检索质量 0 分"的报告）
- BUG-019：config_snapshot 标注评测侧 allow_retry 与线上流式侧的差异（行为不变）
- BUG-022：Gate / Reflection 开关在 try/finally 中还原，任何异常路径都不残留
- BUG-023：幂等键去重（重复提交 / 并发冲突）+ 列表分页参数与合法性校验
- BUG-024：experiment_name / dataset_version / question_type 超长返回 422 而非 500
- BUG-025：动态路由选策略失败不归档成 dynamic_router

说明（与既有评测测试一致）：
- 需要 PostgreSQL（沿用 test_upload.PG_AVAILABLE 开关）
- 每个用例使用独立 engine，避免连接池跨事件循环绑定
- RAG 链路用桩替换，避免依赖 Milvus / 真实 LLM，保证断言确定性
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.dynamic_router import DYNAMIC_ROUTER_STRATEGY
from src.application.evaluation_service import (
    MAX_PAGE_SIZE,
    ONLINE_STREAM_ALLOW_RETRY,
    ROUTER_FAILED_STRATEGY,
    CaseResult,
    EvaluationService,
    _page_clamp,
    infra_failed,
    summarize,
    summarize_full,
)
from src.application.rag_service import RagService
from src.core.config import settings
from src.core.exceptions import AppException
from src.domain import models
from tests.test_upload import PG_AVAILABLE

# 模块级别名：避免 pytest 把以 Test 开头的模型类当作测试类收集
EvalCase = models.TestCase
EvalResult = models.EvaluationResult
EvalRun = models.EvaluationRun

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]


# ── 数据库会话与夹具 ────────────────────────────────────────────────────────


@asynccontextmanager
async def _session():
    """独立 engine 的一次性会话，避免连接池绑定到其他事件循环。"""
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


async def _purge_eval_data(kb_id: uuid.UUID) -> None:
    """清理评估数据（test_cases / evaluation_results / evaluation_runs）。"""
    async with _session() as db:
        case_ids = (
            await db.scalars(select(EvalCase.id).where(EvalCase.kb_id == kb_id))
        ).all()
        if case_ids:
            await db.execute(
                delete(EvalResult).where(EvalResult.test_case_id.in_(case_ids))
            )
        await db.execute(delete(EvalRun).where(EvalRun.kb_id == kb_id))
        await db.execute(delete(EvalCase).where(EvalCase.kb_id == kb_id))
        await db.commit()


@pytest.fixture
def kb(client):
    """临时知识库，用后仅清理该 KB 的评估数据。"""
    resp = client.post(
        "/api/v1/kb", json={"name": f"Batch3评测KB-{_SUFFIX}", "visibility": "private"}
    )
    assert resp.status_code == 201, resp.text
    kbid = uuid.UUID(resp.json()["id"])
    yield kbid
    asyncio.run(_purge_eval_data(kbid))
    client.delete(f"/api/v1/kb/{kbid}")


# ── RAG 桩 ──────────────────────────────────────────────────────────────────


async def _fake_retrieve_and_answer(
    self,
    kb_ids,
    question,
    history=None,
    strategy=None,
    resource_types=None,
    analysis=None,
    router_decision=None,
):
    return f"模拟答案：{question}", [
        {
            "content": f"模拟上下文：{question}",
            "document_id": str(uuid.uuid4()),
            "score": 0.9,
        }
    ]


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    monkeypatch.setattr(RagService, "retrieve_and_answer", _fake_retrieve_and_answer)


async def _seed_cases(db, kb: uuid.UUID, n: int = 2) -> None:
    """上传 n 条已确认标准答案的用例（可参与 answer_correctness）。"""
    await EvaluationService(db).save_test_set(
        kb,
        [
            {
                "question": f"黄芪功效-{_SUFFIX}-{i}",
                "question_type": "herb",
                "golden_answer": "补气固表，托毒排脓",
                "golden_contexts": [],
                "needs_review": False,
            }
            for i in range(n)
        ],
        dataset_version="tcm-v1",
    )


# ── BUG-011：失败用例不进均值 ───────────────────────────────────────────────


def _res(cr: float, ac: float | None = None, error: str | None = None, idx: int = 0):
    return CaseResult(
        test_case_id=str(idx),
        question=f"q{idx}",
        golden_answer="a",
        context_relevancy=cr,
        answer_correctness=ac,
        error=error,
        question_type="herb",
    )


def test_summarize_full_excludes_failed_cases_from_mean():
    """BUG-011：失败用例（error 非空）不参与任何均值，只计入 failed_count。"""
    s = summarize_full(
        [_res(0.8, ac=0.9), _res(0.6, ac=None, idx=1), _res(0.0, error="boom", idx=2)]
    )
    # CR 均值只含成功用例：(0.8 + 0.6) / 2，而不是 (0.8+0.6+0.0)/3
    assert s.context_relevancy == pytest.approx(0.7, abs=1e-4)
    assert s.answer_correctness == pytest.approx(0.9, abs=1e-4)
    assert s.evaluated_count == 1
    assert s.skipped_count == 1
    assert s.failed_count == 1
    assert s.total_count == 3
    assert s.infra_failed is False
    assert s.passed is True


def test_all_failed_run_marks_infra_failed():
    """BUG-011：全部用例失败 ⇒ infra_failed=True，指标不可信且不误判门禁。"""
    s = summarize_full([_res(0.0, error="boom"), _res(0.0, error="boom", idx=1)])
    assert s.failed_count == 2
    assert s.evaluated_count == 0
    assert s.skipped_count == 0
    assert s.passed is False
    assert s.answer_correctness is None
    assert s.infra_failed is True

    assert infra_failed(2, 2) is True
    assert infra_failed(1, 2) is False
    assert infra_failed(0, 2) is False
    assert infra_failed(0, 0) is False


def test_summarize_keeps_legacy_five_tuple():
    """旧签名的五元组聚合仍然可用（TASK-008 及之前的调用方不受影响）。"""
    cr, ac, evaluated, skipped, passed = summarize(
        [_res(0.5, ac=0.8), _res(0.7, idx=1)]
    )
    assert (cr, evaluated, skipped, passed) == (pytest.approx(0.6, abs=1e-4), 1, 1, True)
    assert ac == pytest.approx(0.8, abs=1e-4)


async def test_run_reports_infra_failed_instead_of_zero_score(kb, monkeypatch, client):
    """BUG-011（端到端）：基础设施故障不再产出"看起来正常的 0 分报告"。"""

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("Milvus 连接失败")

    monkeypatch.setattr(RagService, "retrieve_and_answer", _boom)
    async with _session() as db:
        await _seed_cases(db, kb, n=2)

    resp = client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": str(kb), "experiment_name": "batch3-infra-fail"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["case_count"] == 2
    assert body["failed_count"] == 2
    assert body["infra_failed"] is True
    assert body["answer_correctness"] is None
    assert body["passed"] is False
    assert all(r["error"] for r in body["results"])

    # 失败计数同样体现在运行归档里，历史对比时能看出这一轮不可信
    detail = client.get(f"/api/v1/evaluation/runs/{body['run_id']}").json()
    assert detail["failed_count"] == 2
    assert detail["infra_failed"] is True


async def test_partial_failure_still_reports_successful_mean(kb, monkeypatch, client):
    """BUG-011：部分失败时，均值只基于成功用例（不因故障拉低整体评分）。"""
    calls = {"n": 0}

    async def _fail_half(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("检索服务不可用")
        return await _fake_retrieve_and_answer(self, *args, **kwargs)

    monkeypatch.setattr(RagService, "retrieve_and_answer", _fail_half)
    async with _session() as db:
        await _seed_cases(db, kb, n=2)

    resp = client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": str(kb), "experiment_name": "batch3-partial-fail"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["failed_count"] == 1
    assert body["infra_failed"] is False
    assert body["skipped_count"] == 0
    failed = [r for r in body["results"] if r["error"]]
    ok = [r for r in body["results"] if not r["error"]]
    assert len(failed) == 1 and len(ok) == 1
    # 成功用例的相关度不应被失败用例的 0 分拉低
    assert ok[0]["context_relevancy"] > 0
    assert body["context_relevancy"] == pytest.approx(
        ok[0]["context_relevancy"], abs=1e-4
    )


# ── BUG-019：allow_retry 差异标注 ───────────────────────────────────────────


async def test_run_records_allow_retry_difference(kb):
    """BUG-019：快照记录评测侧与线上流式侧的 allow_retry 差异（两侧行为不变）。"""
    async with _session() as db:
        await _seed_cases(db, kb, n=1)
        run, _ = await EvaluationService(db).run(
            kb, experiment_name="batch3-allow-retry", use_self_reflection=True
        )
    snapshot = run.config_snapshot or {}
    assert snapshot["self_reflection"]["allow_retry"] is True
    assert snapshot["self_reflection"]["allow_retry_online_stream"] is False
    assert ONLINE_STREAM_ALLOW_RETRY is False  # 线上仍禁止（citations 已先下发）


# ── BUG-022：开关必须还原 ───────────────────────────────────────────────────


async def test_gate_and_reflection_switches_restored_after_run(kb):
    """BUG-022：开关在正常路径结束后被还原为运行前的值。"""
    async with _session() as db:
        svc = EvaluationService(db)
        rag = svc.rag
        original_reflection = rag.reflector.enabled
        original_gate = rag.gate.enabled
        await _seed_cases(db, kb, n=1)

        await svc.run(
            kb,
            experiment_name="batch3-switch-off",
            use_evidence_gate=False,
            use_self_reflection=False,
        )
        assert rag.reflector.enabled == original_reflection
        assert rag.gate.enabled == original_gate

        await svc.run(
            kb,
            experiment_name="batch3-switch-on",
            use_evidence_gate=True,
            use_self_reflection=True,
        )
        assert rag.reflector.enabled == original_reflection
        assert rag.gate.enabled == original_gate


async def test_switches_restored_even_when_run_fails(kb, monkeypatch):
    """BUG-022：运行中抛异常也不能把开关残留在共享的 RagService 实例上。"""
    async with _session() as db:
        svc = EvaluationService(db)
        rag = svc.rag
        original_reflection = rag.reflector.enabled
        original_gate = rag.gate.enabled
        await _seed_cases(db, kb, n=1)

        async def _boom(*args, **kwargs):
            raise AppException(500, "模拟运行期失败")

        monkeypatch.setattr(svc, "_load_cases", _boom)
        with pytest.raises(AppException):
            await svc.run(kb, use_evidence_gate=False, use_self_reflection=False)

        assert rag.reflector.enabled == original_reflection
        assert rag.gate.enabled == original_gate


# ── BUG-023：幂等与分页 ─────────────────────────────────────────────────────


async def test_idempotency_key_prevents_duplicate_runs(kb):
    """BUG-023：同一幂等键重复提交不产生第二条 run / results。"""
    key = f"batch3-idem-{_SUFFIX}"
    async with _session() as db:
        svc = EvaluationService(db)
        await _seed_cases(db, kb, n=2)
        run_a, results_a = await svc.run(
            kb, experiment_name="batch3-idem", idempotency_key=key
        )
        run_b, results_b = await svc.run(
            kb, experiment_name="batch3-idem", idempotency_key=key
        )

    assert run_a.id == run_b.id
    assert len(results_a) == len(results_b) == 2

    async with _session() as db:
        rows = (
            await db.scalars(
                select(EvalRun).where(EvalRun.idempotency_key == key)
            )
        ).all()
        assert len(rows) == 1


async def test_idempotency_key_is_bound_to_single_kb(kb, client):
    """BUG-023：同一幂等键只能绑定一个知识库，跨库复用须显式报错。"""
    key = f"batch3-idem-cross-{_SUFFIX}"
    resp = client.post(
        "/api/v1/kb",
        json={"name": f"Batch3跨库KB-{_SUFFIX}", "visibility": "private"},
    )
    assert resp.status_code == 201, resp.text
    kb2 = uuid.UUID(resp.json()["id"])
    try:
        async with _session() as db:
            svc = EvaluationService(db)
            await _seed_cases(db, kb, n=1)
            await svc.run(kb, experiment_name="batch3-idem-a", idempotency_key=key)

        async with _session() as db:
            svc = EvaluationService(db)
            await _seed_cases(db, kb2, n=1)
            with pytest.raises(AppException) as exc:
                await svc.run(kb2, experiment_name="batch3-idem-b", idempotency_key=key)
        assert exc.value.code == 409
    finally:
        await _purge_eval_data(kb2)
        client.delete(f"/api/v1/kb/{kb2}")


def test_run_endpoint_accepts_idempotency_key_in_body_and_header(kb, client):
    """BUG-023：POST /run 支持 body 字段与 Idempotency-Key 请求头。"""
    key = f"batch3-idem-http-{_SUFFIX}"
    asyncio.run(_seed_cases_http(kb))
    first = client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": str(kb), "idempotency_key": key},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": str(kb)},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200, second.text
    assert first.json()["run_id"] == second.json()["run_id"]

    # BUG-024：请求头路径也要做长度校验，超长是 422 而不是 PG 截断 500
    too_long = client.post(
        "/api/v1/evaluation/run",
        json={"kb_id": str(kb)},
        headers={"Idempotency-Key": "k" * 200},
    )
    assert too_long.status_code == 422, too_long.text


async def _seed_cases_http(kb: uuid.UUID) -> None:
    async with _session() as db:
        await _seed_cases(db, kb, n=1)


async def test_list_endpoints_support_pagination(kb, client):
    """BUG-023：/runs 与 /results 支持 limit / offset（默认值与旧行为一致）。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await _seed_cases(db, kb, n=1)
        await svc.run(kb, experiment_name="batch3-page-a")
        await svc.run(kb, experiment_name="batch3-page-b")

    page1 = client.get(f"/api/v1/evaluation/runs?kb_id={kb}&limit=1&offset=0")
    page2 = client.get(f"/api/v1/evaluation/runs?kb_id={kb}&limit=1&offset=1")
    assert page1.status_code == 200 and page2.status_code == 200
    first_ids = {r["run_id"] for r in page1.json()}
    second_ids = {r["run_id"] for r in page2.json()}
    assert len(first_ids) == 1 and len(second_ids) == 1
    assert first_ids != second_ids

    results = client.get(f"/api/v1/evaluation/results?kb_id={kb}&limit=1")
    assert results.status_code == 200
    assert len(results.json()) == 1


def test_pagination_params_are_validated(kb, client):
    """BUG-023：非法分页参数 422（而不是被静默吞掉）。"""
    for query in ("limit=0", "limit=-1", f"limit={MAX_PAGE_SIZE + 1}", "offset=-1"):
        resp = client.get(f"/api/v1/evaluation/runs?kb_id={kb}&{query}")
        assert resp.status_code == 422, f"{query} 应被拒绝：{resp.text}"
        resp = client.get(f"/api/v1/evaluation/results?kb_id={kb}&{query}")
        assert resp.status_code == 422, f"{query} 应被拒绝：{resp.text}"


def test_page_clamp_fallback():
    """BUG-023：服务层兜底夹取（防御未来绕过路由层的内部调用）。"""
    assert _page_clamp(0, -5, 100) == (100, 0)
    assert _page_clamp(99999, 0, 100) == (MAX_PAGE_SIZE, 0)
    assert _page_clamp("3", "2", 100) == (3, 2)


# ── BUG-024：字段长度校验 ───────────────────────────────────────────────────


def test_run_rejects_overlong_fields(kb, client):
    """BUG-024：超长字段返回 422，而不是 StringDataRightTruncation 500。"""
    asyncio.run(_seed_cases_http(kb))
    base = {"kb_id": str(kb)}
    for field, value in (
        ("experiment_name", "x" * 200),
        ("dataset_version", "v" * 100),
        ("retrieval_strategy", "s" * 100),
    ):
        resp = client.post(
            "/api/v1/evaluation/run", json={**base, field: value}
        )
        assert resp.status_code == 422, f"{field} 超长应被拒绝：{resp.status_code}"


def test_upload_rejects_overlong_fields(kb, client):
    """BUG-024：上传测试集时 dataset_version / question_type 超长同样 422。"""
    payload = {
        "kb_id": str(kb),
        "dataset_version": "v" * 100,
        "cases": [
            {
                "question": f"超长字段-{_SUFFIX}",
                "question_type": "t" * 100,
                "golden_answer": "",
            }
        ],
    }
    resp = client.post("/api/v1/evaluation/upload", json=payload)
    assert resp.status_code == 422, resp.text


# ── BUG-025：动态路由失败的策略归档 ─────────────────────────────────────────


async def test_dynamic_router_failure_not_archived_as_dynamic_router(
    kb, monkeypatch
):
    """BUG-025：选策略即失败的用例归档为 router_failed，不冒充 dynamic_router。"""

    def _boom(self, question):
        raise RuntimeError("Query Analyzer 不可用")

    monkeypatch.setattr(RagService, "plan_retrieval", _boom)
    async with _session() as db:
        svc = EvaluationService(db)
        await _seed_cases(db, kb, n=2)
        run, results = await svc.run(
            kb, experiment_name="batch3-router-fail", use_dynamic_router=True
        )

    assert run.retrieval_strategy == DYNAMIC_ROUTER_STRATEGY
    assert all(r.error for r in results)
    assert (run.config_snapshot or {})["router"]["failed_count"] == 2

    async with _session() as db:
        rows = (
            await db.scalars(select(EvalResult).where(EvalResult.run_id == str(run.id)))
        ).all()
    assert len(rows) == 2
    assert {r.retrieval_strategy for r in rows} == {ROUTER_FAILED_STRATEGY}


async def test_fixed_strategy_run_still_archives_run_level_strategy(kb):
    """回归保护（BUG-025 修复不能误伤）：固定策略运行仍归档 run 级策略。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await _seed_cases(db, kb, n=1)
        run, results = await svc.run(
            kb,
            experiment_name="batch3-fixed-strategy",
            retrieval_strategy="hybrid_rrf_rerank_hyde",
        )
    assert all(r.retrieval_strategy == run.retrieval_strategy for r in results)
