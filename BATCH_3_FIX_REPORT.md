# BATCH_3_FIX_REPORT.md

> 第二轮 Batch 3（评测可信度）修复报告
> 依据：`BUG_AUDIT_REPORT.md`　生成时间：2026-09-23
> 范围：**仅 Batch 3 的 6 个 Bug**（BUG-011、019、022、023、024、025），未触碰 Batch 4 及以后内容。

---

## 1. 总览

| 项 | 结果 |
|---|---|
| 本批计划修复 | 6 个（BUG-011、019、022、023、024、025） |
| 已修复 | **6 个**（全部） |
| 未修复 / 跳过 | 0 |
| 新增回归测试 | **18 条**（`backend/tests/test_batch3_evaluation_trust.py`，全部通过） |
| 修改文件 | 4 个（后端源码 3 + 新增迁移 1）+ 新增 1 个测试文件 |
| pytest（单文件） | **18 passed** |
| pytest（全量） | **635 passed, 2 skipped**（Batch 2 后基线 617 → **+18，无回归**） |
| Alembic | `current = heads = c9d1e4f70a2b`（单链无多 head；**本批新增 1 个迁移**并已执行） |
| 前端改动 | 无（本批为后端批次；新增字段在前端的展示归 Batch 5，见 §6） |

### 本批先确认的三项决策（用户已拍板）

| 决策点 | 选择 |
|---|---|
| BUG-019 `allow_retry` 差异 | **只做可追溯标注，不改行为**：写入 `config_snapshot`，两侧运行时行为完全不变 |
| BUG-011/023 新增字段落点 | **新增可空列 + Alembic 迁移**（`failed_count` / `idempotency_key`，后者带唯一索引） |
| BUG-011 全失败时的响应 | **保持 HTTP 200**，新增 `failed_count` / `infra_failed` 字段；失败用例不计入任何均值 |

---

## 2. 逐 Bug 修复明细

### BUG-011（P2，论文实验致命）失败用例被记成 0 分并计入均值

- **根因**：`_evaluate_case` 的 `except Exception` 只写 `base.error`，`context_relevancy` 保持默认值 **0.0**；`summarize()` 又把全部用例（含失败）放进均值，`skipped = len(results) - evaluated` 把失败算成"跳过"；全部失败时接口照样返回 **200 + 一份看起来正常的 0 分报告**。基础设施故障被包装成"检索质量为 0"，直接污染实验结论。
- **修复**：
  - 新增 `RunSummary` 数据类 + `summarize_full()`：**失败用例（`error` 非空）不进 CR / AC 均值**，单独计入 `failed_count`；三类计数互斥且覆盖全部用例（`evaluated` / `skipped` / `failed`）。
  - 新增 `infra_failed(failed_count, case_count)`：全部用例失败 ⇒ 报告不可信。
  - `summarize()` **保留五元组旧签名**（兼容 TASK-008 及之前的调用方），内部委托给 `summarize_full`，均值语义随之修正。
  - `evaluation_runs.failed_count` 落库；`_run_out` 与 `POST /run` 响应新增 `failed_count`、`infra_failed`；`by_question_type` 每组新增 `failed_count`。
  - 有失败用例时以 **error 级**日志记录（`failed=n/total` 并提示检查基础设施），不再只是单条 warn。
- **回归测试**：`test_summarize_full_excludes_failed_cases_from_mean`、`test_all_failed_run_marks_infra_failed`、`test_summarize_keeps_legacy_five_tuple`、`test_run_reports_infra_failed_instead_of_zero_score`、`test_partial_failure_still_reports_successful_mean`。

### BUG-019（P2，需决策）`allow_retry` 两侧不一致

- **根因**：评测走 `retrieve_and_answer`（非流式，`allow_retry=True`），线上 SSE 走 `ask_stream`（`allow_retry=False`，因为 citations 在生成前已下发，换策略会导致证据与已下发卡片不一致）。差异本身是**设计取舍**，但此前没有任何地方记录，实验结论容易被误读到线上。
- **修复（按决策：只标注不改行为）**：
  - 新增模块常量 `EVAL_ALLOW_RETRY = True` / `ONLINE_STREAM_ALLOW_RETRY = False`，并注释解释流式侧禁止 retry 的原因。
  - 每次运行的 `config_snapshot["self_reflection"]` 写入 `allow_retry` 与 `allow_retry_online_stream`，使"评测结论能否迁移到线上"可追溯。
  - **两条路径的运行时行为一字未改**。
- **回归测试**：`test_run_records_allow_retry_difference`。

### BUG-022（P2）Gate / Reflection 开关污染共享实例

- **根因**：`run()` 就地改写 `self.rag.reflector.enabled` / `self.rag.gate.enabled`，没有 `try/finally`；RagService 一旦复用（共享实例），上一次对照实验的开关会残留到后续请求/实验。
- **修复**：
  - `run()` 重构为"对外入口（开关治理 + 幂等）"，实际执行下沉到 `_execute()`。
  - 入口记录原始开关值 → 覆盖 → 在 `finally` 中**无条件还原**，异常路径也不残留；原来依赖 `except AttributeError` 判断"注入桩不提供该能力"的写法改为显式 `getattr(...)/hasattr(...)` 判断，语义等价。
- **回归测试**：`test_gate_and_reflection_switches_restored_after_run`、`test_switches_restored_even_when_run_fails`。

### BUG-023（P2）无幂等/并发保护 + 硬编码 limit 无 offset

- **根因**：同一实验可无限重复提交，产生重复 `run + results`；`/runs` 硬编码 `limit(100)`、`/results` `limit(200)` 且无 offset，无法翻页也无上限保护。
- **修复**：
  - `evaluation_runs.idempotency_key`（可空、**唯一索引**）+ 迁移 `c9d1e4f70a2b`。
  - `service.run(..., idempotency_key=...)`：键命中既有 run 则直接返回该 run 与其结果（不重复跑昂贵评测）；同键跨 KB 复用 ⇒ **409**。
  - **并发**：先 `flush` 带幂等键的 run，捕获 `IntegrityError` ⇒ 回滚并返回抢先落库的那条（数据库唯一约束兜住并发双写）。
  - `POST /run` 支持 body 字段 `idempotency_key` 或标准请求头 `Idempotency-Key`（body 优先）。
  - `list_runs` / `list_history` 新增 `limit` / `offset`（默认值 100 / 200，**与旧行为一致**）；路由层 `Query(ge=1, le=500)` 校验非法参数为 **422**，服务层 `_page_clamp` 做兜底夹取。
- **回归测试**：`test_idempotency_key_prevents_duplicate_runs`、`test_idempotency_key_is_bound_to_single_kb`、`test_run_endpoint_accepts_idempotency_key_in_body_and_header`、`test_list_endpoints_support_pagination`、`test_pagination_params_are_validated`、`test_page_clamp_fallback`。

### BUG-024（P2）超长字段 500 而非 422

- **根因**：`experiment_name`(128) / `dataset_version`(64) / `retrieval_strategy`(64) / `question_type`(32) 在请求模型上没有长度约束，超长直接打到 PG，触发 `StringDataRightTruncation` → 500。
- **修复**：`EvaluationRunRequest` / `EvaluationUploadRequest` / `TestCaseItem` / `TestCaseUpdateRequest` 按**列宽同名常量**补 `Field(max_length=...)`，超长由 FastAPI 返回 **422**。
- **回归测试**：`test_run_rejects_overlong_fields`、`test_upload_rejects_overlong_fields`。

### BUG-025（P2）动态路由失败被归档成 `dynamic_router`

- **根因**：`plan_retrieval` 抛错时 `base.retrieval_strategy` 仍为 None，落库时 `res.retrieval_strategy or run.retrieval_strategy` 回落成 run 级 `dynamic_router` ⇒ "问题类型 × 策略 × 指标"对比把基础设施故障算成了该策略的效果。
- **修复**：新增常量 `ROUTER_FAILED_STRATEGY = "router_failed"`；动态路由运行且未选出策略的用例归档为 `router_failed`，**不再回落**；`config_snapshot["router"]["failed_count"]` 单独计数；固定策略运行维持原行为（回归保护）。
- **回归测试**：`test_dynamic_router_failure_not_archived_as_dynamic_router`、`test_fixed_strategy_run_still_archives_run_level_strategy`。

---

## 3. 修改文件清单

| 文件 | 改动要点 |
|---|---|
| `backend/src/domain/models.py` | `EvaluationRun` 新增 `failed_count`（Integer, default 0）、`idempotency_key`（String(128), 可空 + 唯一索引） |
| `backend/alembic/versions/c9d1e4f70a2b_*.py`（新增） | 迁移：加两列 + `failed_count` 回填 0 + `ix_evaluation_runs_idempotency_key` 唯一索引；`downgrade` 完整可逆 |
| `backend/src/application/evaluation_service.py` | `run()` 拆分为入口（BUG-022 try/finally + BUG-023 幂等）与 `_execute()`；`RunSummary` / `summarize_full` / `infra_failed` / `_page_clamp`；`_find_run_by_idempotency_key` / `_load_results`；`failed_count` 落库与 error 日志；BUG-019 快照标注；BUG-025 策略归档；`list_runs` / `list_history` 分页 |
| `backend/src/api/routes/evaluation.py` | 各请求模型补 `max_length`（BUG-024）；`EvaluationReport` / `EvaluationRunOut` / `QuestionTypeMetric` 新增失败维度字段；`/run` 幂等键（body + 请求头）；`/runs`、`/results` 分页参数与 422 校验 |
| `backend/tests/test_batch3_evaluation_trust.py`（新增） | 18 条 Batch 3 回归测试 |

---

## 4. 向后兼容性说明

| 兼容对象 | 说明 |
|---|---|
| `aggregate()` / `summarize()` 旧签名 | 签名不变；仅修正"失败不计入均值"的语义（这正是本批要修的缺陷） |
| `list_runs` / `list_history` | 新增参数均在末尾、默认值沿用旧硬编码（100 / 200），旧调用行为不变 |
| `run()` 位置参数 | `idempotency_key` 为**末尾新增可选参数**，旧调用不受影响 |
| API 响应字段 | **只增不改**：`failed_count`、`infra_failed`、`by_question_type[].failed_count`（新字段均有默认值 0 / False） |
| 幂等键 | 不传即**完全走旧路径**（NULL 不参与唯一约束） |
| 历史数据 | 迁移把既有 run 的 `failed_count` 回填为 0，`infra_failed` 因此对历史运行恒为 False |
| 数据库 | 新增两列均为可空/带默认值；`downgrade` 可完整回滚 |

---

## 5. 需要知晓的行为变更（对使用方）

| 场景 | 变更前 | 变更后 |
|---|---|---|
| 检索/生成抛异常（Milvus 宕机等） | 计 0 分并拉低均值，报告仍 200 且无提示 | 失败用例不进均值，`failed_count` 单独计数，全部失败时 `infra_failed=True` 并 error 日志 |
| 同一实验重复/并发提交 | 每次都跑，产生重复 run + results | 带同一 `idempotency_key` 只跑一次；并发由唯一索引兜底返回既有 run |
| 幂等键跨知识库复用 | 无此机制 | **409**，要求换 key |
| `experiment_name` 等字段超长 | **500**（PG 截断异常） | **422**（入参校验） |
| `/runs`、`/results` 分页 | 只能拿前 100 / 200 条 | 支持 `limit`（≤500）/ `offset`，非法值 422 |
| 动态路由选策略失败 | 归档成 `dynamic_router`（污染策略对比） | 归档为 `router_failed`，并在快照里单独计数 |

---

## 6. 遗留与待办

| 项 | 归属 | 说明 |
|---|---|---|
| 前端展示 `failed_count` / `infra_failed` | Batch 5 | 后端已产出字段，UI 尚未提示"评测基础设施异常"；Batch 3 未改前端，`EvaluationReport` / `EvaluationRunItem` 类型需在 Batch 5 补齐后，再由展示层做提示 |
| `allow_retry` 是否真正统一 | 决策 U-2（已按你的选择标注） | 若未来要在评测侧禁止 retry 做对照实验，只需改 `EVAL_ALLOW_RETRY` 并在 `retrieve_and_answer` 增加入参，无需再动评测编排 |
| 幂等键的清理策略 | 未实现（非 Bug） | 幂等键目前长期留存；如需过期清理，建议后续单独评估（暂不在"不新增功能"范围内做） |
| 环境验证 | 本机 | 迁移已在本地 PostgreSQL 执行成功（`current = c9d1e4f70a2b`）；Milvus 不可用，评测链路依赖桩测试验证，端到端实跑待环境恢复后补充 |
