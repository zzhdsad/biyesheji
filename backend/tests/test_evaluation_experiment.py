"""TASK-009 中医问答评测体系（Evaluation Baseline）测试。

覆盖：
- 测试集结构与问题分类：question_type 受控词表、dataset_version、needs_review
- 标准答案未确认时 answer_correctness 记为「未评估」（None），不污染均值
- Baseline 实验归档：evaluation_runs（experiment_name / retrieval_strategy /
  dataset_version / config_snapshot / by_question_type）
- 按问题类型分组分析（为 TASK-011 Query Analyzer / Dynamic Router 准备）
- 人工确认标准答案后可参与答案正确度统计
- 旧格式兼容：不含新字段的上传请求仍可用，aggregate() 旧签名不变
- 中医测试集种子文件（data/tcm_eval_seed_v1.json）可完整导入

说明：
- 需要 PostgreSQL（沿用 test_upload.PG_AVAILABLE 开关）
- 每个用例使用独立 engine（与 test_upload 一致），避免连接池跨事件循环绑定
- RAG 链路用桩替换，避免依赖 Milvus / 真实 LLM，保证断言确定性
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.evaluation_service import (
    BASELINE_RETRIEVAL_STRATEGY,
    QUESTION_TYPES,
    CaseResult,
    EvaluationService,
    aggregate,
    breakdown_by_question_type,
)
from src.application.rag_service import RagService
from src.core.config import settings
from src.domain import models
from tests.test_upload import PG_AVAILABLE

# 模块级别名：避免 pytest 把以 Test 开头的模型类当作测试类收集
EvalCase = models.TestCase
EvalResult = models.EvaluationResult
EvalRun = models.EvaluationRun

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

_SUFFIX = uuid.uuid4().hex[:8]
_SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "tcm_eval_seed_v1.json"


# ── 数据库会话（每用例独立 engine）──────────────────────────────────────────


@asynccontextmanager
async def _session():
    """独立 engine 的一次性会话，避免连接池绑定到其他事件循环。"""
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


# ── RAG 桩 ──────────────────────────────────────────────────────────────────


def _stub_answer(question: str) -> str:
    """桩答案：与 question 强相关，便于构造可预期的高分用例。"""
    return f"模拟答案：{question}"


async def _fake_retrieve_and_answer(
    self,
    kb_ids,
    question,
    history=None,
    strategy=None,
    resource_types=None,
    analysis=None,
):
    """桩：忽略阶段十二/十三的检索参数（策略与 KG 实验由专项测试覆盖）。"""
    return _stub_answer(question), [
        {
            "content": f"模拟上下文：{question}",
            "document_id": str(uuid.uuid4()),
            "score": 0.9,
        }
    ]


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    monkeypatch.setattr(RagService, "retrieve_and_answer", _fake_retrieve_and_answer)


# ── 夹具 ────────────────────────────────────────────────────────────────────


async def _purge_eval_data(kb_id: uuid.UUID) -> None:
    """清理评估数据（test_cases / evaluation_results / evaluation_runs）。"""
    async with _session() as db:
        case_ids = (
            await db.scalars(select(EvalCase.id).where(EvalCase.kb_id == kb_id))
        ).all()
        if case_ids:
            await db.execute(
                delete(EvalResult).where(
                    EvalResult.test_case_id.in_(case_ids)
                )
            )
        await db.execute(delete(EvalRun).where(EvalRun.kb_id == kb_id))
        await db.execute(delete(EvalCase).where(EvalCase.kb_id == kb_id))
        await db.commit()


@pytest.fixture
def eval_kb(client):
    """创建临时知识库，用后仅清理该 KB 的评估数据（不触碰既有业务数据）。"""
    resp = client.post(
        "/api/v1/kb", json={"name": f"评估测试KB-{_SUFFIX}", "visibility": "private"}
    )
    assert resp.status_code == 201, resp.text
    kb = uuid.UUID(resp.json()["id"])
    yield kb
    asyncio.run(_purge_eval_data(kb))
    client.delete(f"/api/v1/kb/{kb}")


# ── 工具 ────────────────────────────────────────────────────────────────────


def _case(question: str, **kw) -> dict:
    base = {
        "question": question,
        "question_type": "herb",
        "golden_answer": "",
        "golden_contexts": [],
    }
    base.update(kw)
    return base


# ── 测试集结构与问题分类 ────────────────────────────────────────────────────


async def test_upload_persists_question_type_and_dataset_version(eval_kb):
    """上传测试集：question_type / dataset_version / needs_review 正确落库。"""
    async with _session() as db:
        svc = EvaluationService(db)
        ids = await svc.save_test_set(
            eval_kb,
            [
                _case(f"黄芪功效-{_SUFFIX}", question_type="herb"),
                _case(
                    f"桂枝汤组成-{_SUFFIX}",
                    question_type="prescription",
                    dataset_version="tcm-v1",
                ),
            ],
            dataset_version="v1",
        )
        rows = (
            await db.scalars(select(EvalCase).where(EvalCase.id.in_(ids)))
        ).all()
        by_q = {t.question: t for t in rows}
        assert len(rows) == 2
        assert by_q[f"黄芪功效-{_SUFFIX}"].question_type == "herb"
        assert by_q[f"黄芪功效-{_SUFFIX}"].dataset_version == "v1"
        # 未提供 golden_answer → 自动标记为需人工确认
        assert by_q[f"黄芪功效-{_SUFFIX}"].needs_review is True
        assert by_q[f"桂枝汤组成-{_SUFFIX}"].dataset_version == "tcm-v1"


async def test_upload_rejects_unknown_question_type(eval_kb):
    """未知 question_type → 422（受控词表校验）。"""
    async with _session() as db:
        svc = EvaluationService(db)
        with pytest.raises(Exception) as exc_info:
            await svc.save_test_set(
                eval_kb, [_case(f"非法类型-{_SUFFIX}", question_type="acupuncture")]
            )
        assert "question_type" in str(exc_info.value)


async def test_upload_legacy_payload_keeps_backward_compat(eval_kb):
    """旧格式（仅 question/golden_answer/golden_contexts）仍可用且默认已确认。"""
    async with _session() as db:
        svc = EvaluationService(db)
        ids = await svc.save_test_set(
            eval_kb,
            [
                {
                    "question": f"旧格式用例-{_SUFFIX}",
                    "golden_answer": _stub_answer(f"旧格式用例-{_SUFFIX}"),
                    "golden_contexts": [],
                }
            ],
        )
        tc = await db.get(EvalCase, ids[0])
        assert tc.question_type == "general"
        assert tc.needs_review is False  # 提供了标准答案 → 视为已确认


# ── 指标与人工确认策略 ──────────────────────────────────────────────────────


async def test_run_skips_unconfirmed_golden_answer(eval_kb):
    """标准答案待确认：只算 context_relevancy，answer_correctness 记为未评估。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"未确认-{_SUFFIX}", question_type="theory", needs_review=True)],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(
            eval_kb,
            experiment_name="baseline",
            retrieval_strategy=BASELINE_RETRIEVAL_STRATEGY,
        )

        assert len(results) == 1
        assert results[0].answer_correctness is None
        assert results[0].context_relevancy > 0  # 检索质量仍被度量
        assert run.evaluated_count == 0
        assert run.skipped_count == 1
        assert run.answer_correctness is None
        assert run.passed is False  # 无已评估用例，不得判为通过门禁


async def test_run_computes_answer_correctness_for_confirmed_case(eval_kb):
    """已确认标准答案的用例正常计算 answer_correctness。"""
    async with _session() as db:
        svc = EvaluationService(db)
        q = f"已确认-{_SUFFIX}"
        await svc.save_test_set(
            eval_kb,
            [_case(q, question_type="literature", golden_answer=_stub_answer(q))],
            dataset_version="tcm-v1",
        )
        run, results = await svc.run(eval_kb)
        assert results[0].answer_correctness is not None
        assert results[0].answer_correctness > 0.5
        assert run.evaluated_count == 1
        assert run.skipped_count == 0
        assert run.answer_correctness is not None


async def test_confirm_test_case_enables_answer_correctness(eval_kb):
    """人工确认标准答案后，该用例在后续运行中参与答案正确度统计。"""
    async with _session() as db:
        svc = EvaluationService(db)
        ids = await svc.save_test_set(
            eval_kb,
            [_case(f"待确认-{_SUFFIX}", question_type="herb")],
            dataset_version="tcm-v1",
        )
        case_id = ids[0]
        _run1, r1 = await svc.run(eval_kb)
        assert r1[0].answer_correctness is None

        updated = await svc.confirm_test_case(
            case_id,
            golden_answer=_stub_answer(f"待确认-{_SUFFIX}"),
            source_reference="由中医药专业人员依据知识库原文核对填写",
        )
        assert updated["needs_review"] is False

        _run2, r2 = await svc.run(eval_kb, experiment_name="after-annotation")
        assert r2[0].answer_correctness is not None
        assert r2[0].answer_correctness > 0.5


# ── Baseline 实验归档 ───────────────────────────────────────────────────────


async def test_run_archives_experiment_fields(eval_kb):
    """Baseline 实验归档：实验名/检索策略/测试集版本/配置快照/按类型指标。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [
                _case(f"黄芪-{_SUFFIX}", question_type="herb"),
                _case(f"桂枝汤-{_SUFFIX}", question_type="prescription"),
                _case(
                    f"拒答-{_SUFFIX}",
                    question_type="unanswerable",
                    golden_answer="根据现有资料，我无法回答该问题。",
                    needs_review=False,
                ),
            ],
            dataset_version="tcm-v1",
        )
        run, _ = await svc.run(
            eval_kb,
            experiment_name="baseline-run-1",
            retrieval_strategy="hybrid_rrf_rerank_hyde",
            dataset_version="tcm-v1",
        )

        assert run.experiment_name == "baseline-run-1"
        assert run.retrieval_strategy == "hybrid_rrf_rerank_hyde"
        assert run.dataset_version == "tcm-v1"
        assert run.case_count == 3
        assert run.threshold == settings.EVAL_ACCURACY_THRESHOLD
        snapshot = run.config_snapshot or {}
        assert snapshot["retrieval_strategy"] == "hybrid_rrf_rerank_hyde"
        assert "recall_top_k" in snapshot["retrieval"]
        assert "accuracy_threshold" in snapshot["evaluation"]

        types = {b["question_type"] for b in (run.by_question_type or [])}
        assert types == {"herb", "prescription", "unanswerable"}

        # evaluation_results 冗余记录 run_id / 实验维度，便于按实验直接查询
        rows = (
            await db.scalars(
                select(EvalResult).where(EvalResult.run_id == str(run.id))
            )
        ).all()
        assert len(rows) == 3
        assert {r.question_type for r in rows} == {"herb", "prescription", "unanswerable"}
        assert all(r.run_id == str(run.id) for r in rows)


async def test_runs_can_be_listed_and_compared(eval_kb):
    """多次运行可区分（不同 experiment_name），支持后续消融实验对比。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [_case(f"对比-{_SUFFIX}", question_type="multi_source")],
            dataset_version="tcm-v1",
        )
        run_a, _ = await svc.run(eval_kb, experiment_name="baseline")
        run_b, _ = await svc.run(eval_kb, experiment_name="ablation-no-hyde")

        runs = await svc.list_runs(eval_kb)
        names = {r["experiment_name"] for r in runs}
        assert {"baseline", "ablation-no-hyde"} <= names
        assert {r["run_id"] for r in runs} >= {str(run_a.id), str(run_b.id)}

        detail = await svc.get_run(run_b.id)
        assert detail["experiment_name"] == "ablation-no-hyde"
        assert detail["dataset_version"] == "tcm-v1"


async def test_list_test_cases_and_history(eval_kb):
    """测试集列表与历史结果查询（含问题分类、运行维度）。"""
    async with _session() as db:
        svc = EvaluationService(db)
        await svc.save_test_set(
            eval_kb,
            [
                _case(f"列表-{_SUFFIX}", question_type="theory"),
                _case(f"列表2-{_SUFFIX}", question_type="theory"),
            ],
            dataset_version="tcm-v1",
        )
        cases = await svc.list_test_cases(eval_kb, dataset_version="tcm-v1")
        assert len(cases) == 2
        assert all(c["question_type"] == "theory" for c in cases)
        assert all(c["question_type_label"] == "中医理论" for c in cases)

        run, _ = await svc.run(eval_kb)
        history = await svc.list_history(eval_kb, run_id=str(run.id))
        assert len(history) == 2
        assert all(h["run_id"] == str(run.id) for h in history)
        assert all(h["question_type"] == "theory" for h in history)


async def test_seed_testset_can_be_imported(eval_kb):
    """中医测试集种子文件可通过 EvaluationService 完整导入（≥30 条）。"""
    with _SEED_FILE.open(encoding="utf-8") as f:
        payload = json.load(f)
    cases = payload["cases"]

    async with _session() as db:
        svc = EvaluationService(db)
        ids = await svc.save_test_set(
            eval_kb, cases, dataset_version=payload["dataset_version"]
        )
        assert len(ids) >= 30
        rows = (
            await db.scalars(select(EvalCase).where(EvalCase.id.in_(ids)))
        ).all()
        assert len(rows) >= 30
        assert all(t.dataset_version == "tcm-v1" for t in rows)
        # 未确认的知识类问题不参与答案正确度
        assert sum(1 for t in rows if t.needs_review) >= 30


# ── 纯函数 / 指标 ───────────────────────────────────────────────────────────


def test_aggregate_keeps_backward_compatible_signature():
    """aggregate() 旧签名（cr, ac, passed）保持不变。"""
    results = [
        CaseResult(
            test_case_id="1",
            question="q1",
            golden_answer="a",
            context_relevancy=0.5,
            answer_correctness=0.8,
            question_type="herb",
        ),
        CaseResult(
            test_case_id="2",
            question="q2",
            golden_answer="a",
            context_relevancy=0.7,
            answer_correctness=None,  # 未评估，不参与均值
            question_type="theory",
        ),
    ]
    cr, ac, passed = aggregate(results)
    assert cr == pytest.approx(0.6, abs=1e-4)
    assert ac == pytest.approx(0.8, abs=1e-4)  # 只统计已评估用例
    assert passed is True  # 0.8 ≥ 0.75


def test_breakdown_by_question_type():
    """按问题类型分组聚合：支持「不同类型下的策略效果」分析。"""
    results = [
        CaseResult(
            test_case_id=str(i),
            question=f"q{i}",
            golden_answer="a",
            context_relevancy=0.4 + 0.1 * i,
            answer_correctness=0.6 if i % 2 == 0 else None,
            question_type="herb" if i < 2 else "prescription",
        )
        for i in range(4)
    ]
    buckets = {b["question_type"]: b for b in breakdown_by_question_type(results)}
    assert set(buckets) == {"herb", "prescription"}
    assert buckets["herb"]["case_count"] == 2
    assert buckets["herb"]["evaluated_count"] == 1
    assert buckets["herb"]["skipped_count"] == 1
    assert buckets["herb"]["answer_correctness"] == pytest.approx(0.6, abs=1e-4)


# ── API 层 ──────────────────────────────────────────────────────────────────


def test_api_question_types_endpoint(client):
    """GET /evaluation/question-types 返回受控词表。"""
    resp = client.get("/api/v1/evaluation/question-types")
    assert resp.status_code == 200, resp.text
    values = {i["value"] for i in resp.json()["question_types"]}
    assert set(QUESTION_TYPES) == values


def test_api_run_archives_experiment(client, eval_kb):
    """POST /evaluation/run 返回实验维度字段并写入归档。"""
    upload_resp = client.post(
        "/api/v1/evaluation/upload",
        json={
            "kb_id": str(eval_kb),
            "dataset_version": "tcm-v1",
            "cases": [
                {
                    "question": f"API黄芪-{_SUFFIX}",
                    "question_type": "herb",
                    "golden_answer": "",
                },
                {
                    "question": f"API拒答-{_SUFFIX}",
                    "question_type": "unanswerable",
                    "golden_answer": "根据现有资料，我无法回答该问题。",
                    "needs_review": False,
                },
            ],
        },
    )
    assert upload_resp.status_code == 200, upload_resp.text

    resp = client.post(
        "/api/v1/evaluation/run",
        json={
            "kb_id": str(eval_kb),
            "experiment_name": "baseline",
            "retrieval_strategy": BASELINE_RETRIEVAL_STRATEGY,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"]
    assert body["experiment_name"] == "baseline"
    assert body["retrieval_strategy"] == BASELINE_RETRIEVAL_STRATEGY
    assert body["dataset_version"] == "tcm-v1"
    assert body["evaluated_count"] == 1
    assert body["skipped_count"] == 1
    assert body["case_count"] == 2
    assert {b["question_type"] for b in body["by_question_type"]} == {
        "herb",
        "unanswerable",
    }
    ac_list = [r["answer_correctness"] for r in body["results"]]
    assert None in ac_list  # 待确认用例为未评估
    assert any(v is not None for v in ac_list)

    # 归档列表可查到该运行
    runs = client.get("/api/v1/evaluation/runs", params={"kb_id": str(eval_kb)})
    assert runs.status_code == 200
    assert any(r["run_id"] == body["run_id"] for r in runs.json())

    # 按 run_id 过滤历史结果
    hist = client.get(
        "/api/v1/evaluation/results",
        params={"kb_id": str(eval_kb), "run_id": body["run_id"]},
    )
    assert hist.status_code == 200
    assert len(hist.json()) == 2
    assert all(h["question_type"] in {"herb", "unanswerable"} for h in hist.json())


def test_api_test_cases_listing_and_patch(client, eval_kb):
    """GET /evaluation/test-cases 与 PATCH /evaluation/test-cases/{id}。"""
    upload = client.post(
        "/api/v1/evaluation/upload",
        json={
            "kb_id": str(eval_kb),
            "dataset_version": "tcm-v1",
            "cases": [
                {
                    "question": f"PATCH用例-{_SUFFIX}",
                    "question_type": "prescription",
                    "golden_answer": "",
                    "source_reference": "方剂资源条目",
                }
            ],
        },
    )
    assert upload.status_code == 200, upload.text
    case_id = upload.json()["case_ids"][0]

    listing = client.get(
        "/api/v1/evaluation/test-cases",
        params={"kb_id": str(eval_kb), "dataset_version": "tcm-v1"},
    )
    assert listing.status_code == 200, listing.text
    case = next(c for c in listing.json() if c["id"] == case_id)
    assert case["needs_review"] is True
    assert case["question_type_label"] == "方剂知识"

    patched = client.patch(
        f"/api/v1/evaluation/test-cases/{case_id}",
        json={
            "golden_answer": _stub_answer(f"PATCH用例-{_SUFFIX}"),
            "source_reference": "已由专业人员核对",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["needs_review"] is False
    assert patched.json()["golden_answer"] == _stub_answer(f"PATCH用例-{_SUFFIX}")


def test_api_upload_rejects_unknown_question_type(client, eval_kb):
    """未知 question_type 经 API 上传 → 422。"""
    resp = client.post(
        "/api/v1/evaluation/upload",
        json={
            "kb_id": str(eval_kb),
            "cases": [{"question": f"非法-{_SUFFIX}", "question_type": "unknown"}],
        },
    )
    assert resp.status_code == 422


def test_api_metrics_breakdown_documents_current_metrics(client):
    """GET /evaluation/metrics/breakdown 明确当前两个指标与阈值。"""
    resp = client.get("/api/v1/evaluation/metrics/breakdown")
    assert resp.status_code == 200, resp.text
    keys = {m["key"] for m in resp.json()["metrics"]}
    assert keys == {"context_relevancy", "answer_correctness"}
    assert resp.json()["threshold"] == settings.EVAL_ACCURACY_THRESHOLD
