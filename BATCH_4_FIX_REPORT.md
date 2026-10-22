# BATCH_4_FIX_REPORT.md

> 第二轮 Batch 4（健壮性与一致性）修复报告
> 依据：`BUG_AUDIT_REPORT.md`　生成时间：2026-09-24
> 范围：**仅 Batch 4 的 12 个 Bug**（BUG-012、017、021、029、033、034、035、036、037、038、043、044），未触碰 Batch 5 及以后内容。

---

## 1. 总览

| 项 | 结果 |
|---|---|
| 本批计划修复 | 12 个（BUG-012、017、021、029、033、034、035、036、037、038、043、044） |
| 已修复 | **12 个**（全部） |
| 未修复 / 跳过 | 0 |
| 新增回归测试 | **17 条**（新增 `test_batch4_robustness.py` 14 条；`test_health.py` 重写为 5 条，其中 1 条为契约更新） |
| 修改文件 | 9 个（后端源码 8 + 新增迁移 1）+ 新增 1 个测试文件 + 重写 1 个测试文件 |
| pytest（新增文件） | **14 passed** |
| pytest（重写文件） | **5 passed** |
| pytest（全量） | **652 passed, 2 skipped**（Batch 3 后基线 635 → **+17，无回归**） |
| Alembic | `current = heads = f3a1c7d9b2e4`（单链无多 head；**本批新增 1 个迁移**并已在本地 PostgreSQL 执行） |
| 前端改动 | 无（本批为后端批次；响应只增字段/收紧校验，前端无需同步改动，见 §4） |

### 本批先确认的四项决策（用户已拍板）

| 决策点 | 选择 |
|---|---|
| BUG-012 审计与业务解耦方式 | **同会话 SAVEPOINT**：审计写入包在 `begin_nested()`，审计失败只回滚该 savepoint、业务照常提交；DB 健康时业务与审计原子落盘 |
| BUG-034 模型未配置的状态码 | **503 Service Unavailable**（不再误用 412） |
| BUG-035 SSE 心跳 / 总超时 | **新增 settings 配置项**：`SSE_HEARTBEAT_INTERVAL_SECONDS=15`、`SSE_TOTAL_TIMEOUT_SECONDS=900`，默认值即旧行为边界，运维可按代理超时调整 |
| BUG-038 反馈唯一性 | **新增 `user_id` 列 + `(user_id, message_id)` 联合唯一约束**（`user_id` 可空，历史行不受影响） |

---

## 2. 逐 Bug 修复明细

### BUG-012（P2）审计失败连带回滚业务（`audit_service.py`）

- **根因**：资源类 CRUD 只做 `flush`，唯一提交点是 `AuditService.log()`；而 `log()` 把"审计 INSERT + 业务 COMMIT"放进同一个 `try`，审计写入失败即 `rollback()` —— 已 flush 的业务变更被一并回滚，调用方却仍按成功继续（`herbs.py:356` 重新查询拿到 `None` → 500，或数据静默丢失）。
- **修复**：`log()` 拆成两步：
  1. 审计写入包在 `begin_nested()`（SAVEPOINT）内并显式 `flush`；失败只回滚该 savepoint，以 **error 级**日志记录（`operation`/`target`/原因，便于人工补录）；
  2. 随后照常 `await self.db.commit()` 提交业务，**提交失败向上抛出**（不再被 `except` 吞掉导致静默丢数据）。
  DB 健康时审计与业务仍在同一事务内原子落盘（控制组测试已验证审计行正常写入）。
- **回归测试**：`test_audit_failure_does_not_roll_back_business`、`test_audit_and_business_commit_atomically_when_healthy`。

### BUG-017（P2）Reranker 异常使整次问答 422

- **根因**：`_retrieve` 捕获 `RerankError` 后直接 `raise AppException(422)`。Reranker 是**增强环节**，与 KG 检索同一性质，却被当成致命依赖。
- **修复**：改为与 KG 一致的降级语义：`logger.warning` + 退回 RRF 融合顺序 `fused[:rerank_top_k]`，召回质量下降但仍可作答。
- **回归测试**：`test_rerank_failure_degrades_to_rrf_order`。

### BUG-021（P2）老集合 Resource 命中触发 PG `invalid input syntax for type uuid`

- **根因**：`_enrich_hits_with_doc_name` 用"`resource_type` 是否为空"区分 Document/Resource 行。老集合（未开启动态字段）取不到 `resource_type`，Resource 行（doc_id 为 SHA256）被判成 Document 行 → `Document.id.in_([sha256])` 查 UUID 列 → PG 报错 500，并被误标 `source_kind='document'`。
- **修复**：判定依据改为 **doc_id 是否为 UUID**（Document 行 = 文档 UUID；Resource 行 = SHA256；KG 行 = `kg:<edge_id>`）。两种集合下结论一致：老集合 Resource 行走资源分支（`doc_name` 兜底、`source_kind='resource'`），不再触碰 `documents` 表；KG 行仍保留自带 `source_kind='kg'`。
- **回归测试**：`test_old_collection_resource_hits_are_not_queried_as_documents`、`test_kg_hits_keep_kg_source_kind`。

### BUG-029（P3）健康检查谎报 + Redis 连接泄漏 + 暴露环境信息

- **根因**：`status` 恒为 `"ok"`（组件全挂也健康）；Redis 客户端 `aclose()` 写在 `try` 内，`ping` 失败即跳过 → 连接泄漏；组件异常静默吞掉无日志；免鉴权端点返回 `version` / `env`。
- **修复**：
  - `status`：全部组件 ok → `"ok"`，任一不可用 → `"degraded"`（HTTP 仍 200，本端点表达"进程存活"，组件级故障由 `status`/`components` 承载）；
  - Redis 客户端在 `finally` 中关闭（关闭失败仅告警）；
  - 组件不可用记 warning 并带原因；
  - 移除 `version` / `env`。
- **回归测试**：`test_health_returns_200_with_components`（契约更新）、`test_health_does_not_expose_env_details`、`test_health_reports_degraded_when_redis_unavailable`、`test_health_closes_redis_client_even_when_ping_fails`。

### BUG-033（P3）资源列表分页参数无边界

- **根因**：`GET /kb/{kb_id}/resources` 的 `limit: int = 20` / `offset: int = 0` 裸参数，`limit=0` 返回空、`limit=100000` 全表拉取。
- **修复**：`Query(default=20, ge=1, le=100)` / `Query(default=0, ge=0)`，与 herbs / prescriptions 等列表端点口径一致。
- **回归测试**：无单独用例（与既有列表端点同构，参数非法由 FastAPI 返回 422）；全量测试无回归。

### BUG-034（P3）模型未配置返回 412（语义误用）

- **根因**：`_check_model_configured` 用 `AppException(412)`。412 的语义是请求头前置条件（ETag / If-Match）不满足，此处是服务端缺配置，且前端按网络错误处理。
- **修复**：改为 `AppException(503)`，文案不变。
- **回归测试**：`test_ask_returns_503_when_model_not_configured`。

### BUG-035（P2）SSE 无心跳、无总超时

- **根因**：`/chat/ask-stream` 在检索阶段长时间不发送任何字节，反向代理（`proxy_read_timeout` 默认 60s）掐断连接 → 客户端既收不到 `done` 也收不到 `error`；只有单次 `LLM_TIMEOUT_SECONDS`，无整体上限。
- **修复**：新增 `_with_heartbeat()` 包住业务事件流：
  - 等待业务事件的间隙发送 **SSE 注释帧** `: ping`（注释帧对事件解析无副作用：前端 `useSSE` 与测试解析器只认 `event:` / `data:`，事件名与顺序完全不变）；
  - 用 `asyncio.shield` 包裹 `__anext__`：超时只取消外层等待，不会打断/终止尚未完成的业务生成器；
  - 总超时到期先发 `error` 事件再关闭流；
  - `finally` 中取消未完成任务并 `aclose()` 生成器（客户端断开也不泄漏）；
  - 两个阈值走配置项，默认值 15s / 900s。
- **回归测试**：`test_heartbeat_frames_keep_stream_alive`、`test_total_timeout_emits_error_event`。

### BUG-036（P2）失败路径留下"有提问无回答"的孤儿用户消息

- **根因**：流式/非流式都先持久化用户消息（保证与助手消息 `created_at` 顺序确定），检索 / Gate / 生成任一环节失败时，助手消息不会落库 → 会话里留下无回答的提问，用户重试后同一问题出现两次，且历史上下文被重复问题污染。
- **修复**：新增 `_discard_user_message()`（补偿删除，失败仅 error 日志并复位会话，绝不掩盖原始异常），在三条未产出助手消息的失败路径上调用：流式 `_retrieve`/Gate 异常、流式 `LLMError`、非流式 `retrieve_and_answer` 异常。文档串同步更新（不再声称"生成失败时用户意图仍留存"）。
- **回归测试**：`test_stream_failure_removes_orphan_user_message`。

### BUG-037（P3）批量删除会话时 Redis 清理失败被谎报

- **根因**：`try` 包住整个循环，第一条缓存清理失败即放弃剩余会话的清理；失败只 `warning`，响应仍是 `{"deleted_count": n}` —— 调用方无从得知缓存残留。
- **修复**：逐条独立容错（单条失败不中断其余）；缓存客户端构造失败时全部记为失败；响应新增 `cache_cleanup_failed: list[str]`（`deleted_count` 字段名与语义不变，前端无需改动）。
- **回归测试**：`test_delete_all_conversations_reports_cache_cleanup_failures`。

### BUG-038（P2）反馈无归属、无唯一约束、可对 user 消息反馈

- **根因**：`feedbacks` 表只有 `message_id`，反馈归属靠 `message → conversation → user_id` 反推；无唯一约束，"先查后写"在并发下会双插；对 `user` 消息也能反馈（无业务含义且污染统计）。
- **修复**：
  - 迁移 `f3a1c7d9b2e4`：`user_id`（可空 UUID，FK `users.id` ON DELETE CASCADE + 索引）+ `uq_feedbacks_user_message (user_id, message_id)` 唯一约束（PostgreSQL 中 NULL 互不相等，历史行不冲突，无需清理重复数据）；`downgrade` 完整可逆；
  - 模型：字段 + `UniqueConstraint`（含注释说明历史兼容取舍）；
  - 路由：仅 `assistant` 消息可反馈（否则 **400**）；写入 `user_id`；唯一性按 `(user_id, message_id)` 判定；并发命中唯一约束时 `IntegrityError` → 回滚后退化为覆盖更新（而不是把 500 抛给先到的请求）；`GET /feedbacks/{message_id}` 按 `(user_id, message_id)` 过滤。
- **回归测试**：`test_feedback_rejects_non_assistant_message`、`test_feedback_carries_owner_and_overwrites_per_user`；既有 `test_feedbacks.py` 9 条**全部保持通过**（未改断言）。

### BUG-043（P2）purge 可绕过回收站直接硬删

- **根因**：`DELETE /kb/{kb_id}/purge` 只校验 KB 存在与 admin 权限，不校验 `deleted_at` → 可绕过软删保护期直接不可逆删除。
- **修复**：`deleted_at is None` 时返回 **400**"只能彻底删除回收站中的知识库，请先执行删除移入回收站"。
- **回归测试**：`test_purge_requires_soft_deleted_kb`（含"软删后 purge 成功"的控制组断言）。

### BUG-044（P2）所有权可转移给已删除/停用账号

- **根因**：只校验 `KBMember` 行存在；成员行不随用户软删/停用级联清理，可把所有权转给不可用账号 → 知识库变孤儿。
- **修复**：转移前校验目标用户存在、`deleted_at is None`、`is_active=True`，否则 **400**。
- **回归测试**：`test_transfer_ownership_rejects_disabled_user`（含"恢复启用后转移成功"的控制组断言）。

---

## 3. 修改文件清单

| 文件 | 改动要点 |
|---|---|
| `backend/src/application/audit_service.py` | `log()` 改为 SAVEPOINT 写入 + 业务提交分离（BUG-012），审计失败 error 级日志、业务提交失败上抛 |
| `backend/src/application/rag_service.py` | Rerank 降级（BUG-017）；`_enrich_hits_with_doc_name` 按 doc_id 是否 UUID 分类（BUG-021）；新增 `_discard_user_message` 并在 `ask` / `ask_stream` 失败路径补偿删除（BUG-036） |
| `backend/src/api/routes/chat.py` | 新增 `_with_heartbeat`（BUG-035）；模型未配置 412→503（BUG-034）；`DELETE /conversations` 缓存清理逐条容错 + `cache_cleanup_failed`（BUG-037） |
| `backend/src/api/routes/health.py` | 重写：如实 `ok`/`degraded`、Redis `finally` 关闭、组件失败日志、移除 `version`/`env`（BUG-029） |
| `backend/src/api/routes/knowledge_bases.py` | 资源列表分页约束（BUG-033）；purge 需先软删（BUG-043）；转移所有权校验目标账号状态（BUG-044） |
| `backend/src/api/routes/feedbacks.py` | 仅助手消息可反馈、写入 `user_id`、按 `(user_id, message_id)` 覆盖、并发 `IntegrityError` 退化更新（BUG-038） |
| `backend/src/domain/models.py` | `Feedback.user_id` + `uq_feedbacks_user_message`（BUG-038） |
| `backend/src/core/config.py` | `SSE_HEARTBEAT_INTERVAL_SECONDS` / `SSE_TOTAL_TIMEOUT_SECONDS`（BUG-035） |
| `backend/alembic/versions/f3a1c7d9b2e4_*.py`（新增） | `feedbacks.user_id` + 外键/索引 + 联合唯一约束，可逆（BUG-038） |
| `backend/tests/test_batch4_robustness.py`（新增） | 14 条 Batch 4 回归测试 |
| `backend/tests/test_health.py`（重写） | 5 条：状态一致性、不泄露环境信息、Redis 不可用降级、ping 失败仍关闭连接、文档可访问 |

---

## 4. 向后兼容性说明

| 兼容对象 | 说明 |
|---|---|
| SSE 事件协议 | **事件名与顺序完全不变**：心跳是注释帧（`: ping`），不产生 `event:`/`data:` 行；前端 `useSSE` 与测试解析器天然忽略 |
| `DELETE /chat/conversations` | 只**新增** `cache_cleanup_failed`，`deleted_count` 字段名与语义不变 |
| `/health` | 移除 `version` / `env`（信息泄露面，前端 `HealthResponse` 中二者本就是可选且未使用）；`components` 与 HTTP 200 不变 |
| 反馈接口 | 新增约束只收紧非法用法（对 user 消息反馈 → 400）；同一用户覆盖更新语义不变；历史行 `user_id` 为 NULL，不参与唯一约束 |
| KB purge | 仅拦截"活跃 KB 直接硬删"（本就是绕过保护期的用法）；先软删再 purge 的既有流程完全不变 |
| 分页参数 | 默认值沿用旧的 20 / 0，仅对越界值返回 422 |
| 数据库 | 新增列可空 + 唯一约束（NULL 不冲突）；`downgrade` 可完整回滚 |
| 前端 | 无需改动（本批未改前端） |

---

## 5. 需要知晓的行为变更（对使用方）

| 场景 | 变更前 | 变更后 |
|---|---|---|
| 审计写入失败 | 连带回滚已 flush 的业务（数据静默丢失 / 500） | 业务照常提交，审计缺失以 error 日志记录（需人工补录） |
| 业务提交失败 | 被 `except` 吞掉，接口仍返回成功 | 向上抛出，返回错误响应 |
| Reranker 服务不可用 | 整次问答 422 | 降级为 RRF 融合顺序，正常作答 |
| 老集合资源命中 | 500（PG `invalid input syntax for type uuid`）+ 误标 document | 正常返回，`source_kind='resource'`，`doc_name` 走兜底 |
| `/health`（Redis 宕机） | `status: "ok"` | `status: "degraded"`，`components.redis: "unavailable"`，并记录 warning |
| 模型未配置 | 412 | 503 |
| 检索 / 生成失败 | 会话留下无回答的提问 | 用户消息被补偿删除（重试不会重复） |
| 批量删除会话时 Redis 抖动 | 剩余会话缓存不再清理，响应仍报"全部成功" | 逐条清理，失败 id 在 `cache_cleanup_failed` 中如实返回 |
| 对 user 消息提交反馈 | 允许（写入无意义反馈） | 400 |
| 活跃 KB 直接 purge | 允许（绕过保护期） | 400，需先移入回收站 |
| 所有权转移给停用/已删用户 | 允许（KB 变孤儿） | 400 |
| SSE 长等待 | 可能被反向代理掐断且无任何提示 | 每 15s 心跳保活；超过 900s 先发 `error` 再关闭 |

---

## 6. 遗留与待办

| 项 | 归属 | 说明 |
|---|---|---|
| `taxonomy.py:376-377` 分页同样无约束 | 未在 BUG-033 编号范围内 | 与本批 BUG-033 属同一类问题（`limit: int = 100` / `offset: int = 0` 裸参数），本批按"最小改动"未顺手改，建议 Batch 6 统一处理 |
| 审计缺失的补录手段 | 未实现（非 Bug） | BUG-012 保证审计失败不影响业务，但缺失的审计行目前只能靠 error 日志人工补录；如需自动重试/补偿表，属新增功能，需另行确认 |
| BUG-036 的空会话残留 | 已知取舍 | 失败发生在"本次新建会话"时，用户消息被删除但空会话保留（前端已拿到 `conversation_id`）；清理空会话会改变会话列表行为，暂不做 |
| BUG-021 老集合 `doc_name` 信息缺失 | 环境限制 | 老集合未开启动态字段时取不到 `resource_name`，`doc_name` 兜底为"资源"——属**信息缺失**而非错误；彻底解决需重建集合迁移向量（不在本批范围） |
| 前端 `HealthResponse` 类型 | Batch 5 | `version` / `env` 字段在类型中仍为可选（后端已不再下发），可在 Batch 5 顺手清理 |
| 环境验证 | 本机 | 迁移已在本地 PostgreSQL 执行成功（`current = f3a1c7d9b2e4`）；本机 **Redis 不可用**（故 `/health` 现为 `degraded`，这正是修复后的正确表现）、**Milvus 不可用**（检索相关用例使用内存向量库/桩），端到端实跑待环境恢复后补充 |
