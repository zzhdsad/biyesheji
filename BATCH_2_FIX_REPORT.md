# BATCH_2_FIX_REPORT.md

> 第二轮 Batch 2（删除生命周期与证据可信性）修复报告
> 依据：`BUG_AUDIT_REPORT.md`　前置：`BATCH_1_FIX_REPORT.md`（已确认通过）
> 生成时间：2026-09-23
> 范围：**仅 Batch 2 的 10 个 Bug**（BUG-006/007/008/009/010/015/016/045/046/047），未触碰 Batch 3 及以后内容。

---

## 1. 总览

| 项 | 结果 |
|---|---|
| 本批计划修复 | 10 个（006、007、008、009、010、015、016、045、046、047） |
| 已修复 | **10 个**（全部） |
| 未修复 / 跳过 | 0 |
| 新增回归测试 | **14 条**（`backend/tests/test_batch2_lifecycle.py`，全部通过） |
| 修改文件 | 12 个后端源文件（本批改动）+ 新增 1 个测试文件 |
| pytest | **617 passed, 2 skipped**（Batch 1 后基线 603 → **+14，无回归**） |
| frontend test | **64 passed / 0 failed**（与基线一致，未下降） |
| tsc --noEmit | 通过（exit 0） |
| lint | 通过（exit 0；既有 6 条 `react-hooks/exhaustive-deps` warning **数量与位置不变**，归 Batch 6） |
| build | 成功 |
| Alembic | `heads = b7c4e8f2a1d9`（单链无多 head；本批未动迁移） |

反证校验（确认新测试非空转）：临时关闭 BUG-008 的逐跳隔离后，`test_kg_multihop_respects_mount_isolation` 立即失败并报出 2 条跨 KB 泄露事实；恢复后通过。其余用例断言的状态（向量条数、KG 节点数、图谱计数、编号序列）在旧代码下均不成立。

---

## 2. 逐 Bug 修复明细

### BUG-006（P1）文档 purge 调用不存在的方法，向量永久残留

- **根因**：`documents.py:463` 调用 `store.delete_by_doc_id()`（全仓无实现）→ AttributeError → 被 `except Exception: logger.warning` 吞掉 → Milvus 向量残留，PG 记录已删；此后这些孤儿向量仍按 `kb_id` 被检索命中，`rag_service` 回落为 `"未知文档"`。
- **修复**：改用既有 `store.delete_by_doc()`；清理失败**不再静默** —— 记 error 日志并返回 **422「文档向量清理失败，无法彻底删除」**，PG 删除不执行（避免不可逆操作留下孤儿向量）。文件清理仍为 best-effort。
- **回归测试**：`test_document_purge_deletes_vectors`（purge 后向量清零）、`test_document_purge_fails_loud_when_vector_cleanup_fails`（清理失败 → 422 且 PG 记录仍在）。

### BUG-007（P1）KB purge 不清理 Document 向量

- **根因**：只清理 `KnowledgeBaseResource` 对应向量；`BaseVectorStore` 接口层没有"按 kb_id 删除"能力；失败同样被吞。
- **修复**：
  - `BaseVectorStore` 新增 `delete_by_kb(kb_id)`（Milvus / InMemory 双实现）；
  - `purge_kb` 在 Resource 清理之后**再按 kb_id 兜底清理该 KB 全部向量**（Document chunk 与任何遗留行）；
  - 两步清理失败均改为 **422**，KB 不被删除。
- **回归测试**：`test_kb_purge_deletes_document_vectors`、`test_kb_purge_fails_loud_when_vector_cleanup_fails`。

### BUG-008（P1）KG 多跳遍历只在种子层隔离

- **根因**：`allowed` 只用于过滤第 1 跳种子，`next_frontier` 与事实生成不再校验挂载 → 未挂载、甚至只挂载在他人 KB 的资源可进入 Prompt（默认 `max_hops=2`）。
- **修复**：`kg_retrieval.facts()` 增加节点资源身份缓存（`res_of`），**逐跳**判定：
  - 每跳先补齐本跳涉及节点的 `(resource_type, resource_id)`；
  - 边的**两端**都必须落在已挂载集合内，否则整条边不产出事实；
  - 未挂载节点不再进入 `next_frontier`，遍历不会扩散到库外资源。
- **回归测试**：`test_kg_multihop_respects_mount_isolation`（只挂载方剂、两味中药未挂载；图谱确有 contains 出边；逐跳隔离下 `facts == []`，且不得引用未挂载资源）。

### BUG-009（P1）删除 Resource 不清 KgNode/KgEdge

- **根因**：资源路由对 KG 表零引用，只清 KBR + Milvus；图谱残留已删资源直到下次 `/kg/build`。
- **修复**：`KgService.delete_resource_nodes(resource_type, resource_id)`（边随 FK CASCADE）；在 `herbs / prescriptions / theories / literatures` 四个删除路由中，于向量清理之后调用（KG 为派生数据，失败仅 error 级留痕，不阻断删除）。
- **回归测试**：`test_delete_herb_cleans_kg_nodes`（删除后 `kg_nodes` 无残留）。

### BUG-010（P1）rebuild 先提交清空再重建

- **根因**：`build(rebuild=True)` 先 `delete + commit`，再同步节点/边；中途异常 → 已清空的空图谱无法回滚。
- **修复**：清表与重建合并为**同一事务**（删除中间 `commit`），`try/except` 中 `rollback` + error 日志后重新抛出；整体成功才提交。
- **回归测试**：`test_kg_rebuild_is_atomic`（注入 `_sync_edges` 异常 → 抛错且节点/边计数与重建前完全一致）。

### BUG-045（P2）卸载顺序不可逆 + 无补偿

- **根因**：先删向量（不可逆）再删挂载；多挂载部分失败或后续回滚时 → "资源仍挂载但永久检索不到"。
- **修复**：`cleanup_resource_mounts` 改为
  1. 先删 KBR（事务内，可回滚）+ `flush`；
  2. 删除前**快照**该 doc 的向量行，再删向量；
  3. 任一步失败 → 用快照**补偿回写**已删向量（`_restore_vectors`），挂载随事务回滚，随后抛出（保持既有 422 契约）；补偿失败记入 `last_cleanup_failures` 并 error 留痕。
- **回归测试**：`test_vector_cleanup_failure_restores_vectors`（第 1 个 KB 删成功、第 2 个失败 → 挂载全部回滚保留 + 已删向量条数完全恢复；恢复后删除仍可成功）。

### BUG-046（P2）重新向量化"先删后插"

- **根因**：`vectorize_and_store` 与 `indexing_service._write_milvus` 均先 `delete_by_doc` 再 `insert`，窗口内失败 → 该资源/文档向量全灭且无重建入口。
- **修复**：`BaseVectorStore` 新增 `ids_by_doc` / `delete_by_ids` 与统一的 **`replace_doc()`**：
  1. 记录旧 id；2. **先写入新向量**；3. 只删除「旧 id ∉ 新 id」的多余分片；4. 校验条数，异常（如同主键未覆盖产生重复行）时回退为"清旧写新"。
  写入失败时旧向量仍在，不留空窗。文档（`indexing_service`）与资源（`resource_vector_service`）两条路径统一改用它。
- **回归测试**：`test_replace_doc_inserts_before_deleting_stale`、`test_replace_doc_keeps_old_vectors_when_insert_fails`。

### BUG-047（P2）更新后重新向量化失败只 warning

- **根因**：`revectorize_all_mounts` 内部 `except → warning` 后返回成功数量，调用方无从得知 → PG 已更新、Milvus 仍是旧向量，检索结果静默过期。
- **修复**：失败清单 `[{kb_id, error}]` 写入 `svc.last_revectorize_failures` 并改为 **error 级日志**；四个资源更新路由在审计详情里写入 `_vector_revectorize_failed`（审计可查），**更新本身仍保持 best-effort 不被阻塞**（既有"向量化失败不阻塞更新"契约不变）。
- **回归测试**：`test_revectorize_failure_is_recorded`（注入向量写入失败 → 更新仍 200，审计详情含失败标记）。

### BUG-015（P2）Reflection 引用下标空间错位

- **根因**：`valid = [i for i in indices if 1 <= i <= len(evidence)]`、`cited = [evidence[i-1] ...]` 用的是**过滤后**的位序，而答案/ Prompt 的 `[citation:N]` 基于**未过滤** hits → 误判 `citation_out_of_range`（多余 revise/retry）或取到错位证据做 Cited 判定；`build_revision_messages` / `build_consistency_messages` 同样按过滤后位序重新编号。
- **修复**：新增 `evidence_number(ev, position)`（取 `source_index`，缺失才回退位置）；三处（校验、revise 提示、一致性提示）统一按 `source_index` 建映射/编号。
- **回归测试**：`test_reflection_uses_source_index_not_position`（编号 2/5 的证据：引用 5 合法、引用 3 越界）、`test_reflection_prompt_numbering_matches_source_index`（提示里是 `[2] [5]`，不得出现 `[1]`）。

### BUG-016（P2）Citation 编号与 Evidence 索引不统一

- **根因**：Prompt 按**全部 hits** 编号，`_build_citations` 生成后又按阈值静默丢弃 → 答案里的 `[citation:2]` 在 UI 无对应卡片；流式 `citations` 在生成前锁定，两条路径语义也不一致。
- **修复**：新增 `evidence.displayable_hits()`（阈值不变），**Prompt / Evidence / Citation 三处共用同一份"可展示命中"并按过滤后顺序连续编号**：
  - `_build_user_prompt` 只对可展示命中编号 → 模型不可能引用没有卡片的编号；
  - `build_evidence` 改为过滤后连续编号（`source_index` 与 Prompt 一致）；
  - `_build_citations` 在同一份列表上取号，不再二次过滤（保证"编号 → 卡片"一一对应）。
  - 兼容细节：**缺少分数字段的命中视为相关度未知，不做静默丢弃**（按 0 处理会整体过滤掉这类命中，与既有行为/用例不兼容）。
- **回归测试**：`test_prompt_evidence_citation_share_numbering`（高分 + 低分命中：Evidence 编号为 `[1]`；Prompt 有 `[1]` 无 `[2]`；引用 `[citation:1]` 恰好 1 张卡片）。

---

## 3. 修改文件清单

| 文件 | 改动要点 |
|---|---|
| `backend/src/infrastructure/milvus_store.py` | 新增 `ids_by_doc` / `delete_by_ids` / `delete_by_kb`（接口 + Milvus + InMemory 三处）；新增统一 `replace_doc()`（先插后删 + 条数校验回退） |
| `backend/src/application/indexing_service.py` | `_write_milvus` 改用 `replace_doc` |
| `backend/src/application/resource_vector_service.py` | `vectorize_and_store` 改用 `replace_doc`；`cleanup_resource_mounts` 顺序调整 + 快照补偿（`_restore_vectors`、`_row_from_dict`、`last_cleanup_failures`）；`revectorize_all_mounts` 失败留痕（`last_revectorize_failures` + error 日志） |
| `backend/src/api/routes/documents.py` | purge 用正确方法 + 失败 422 |
| `backend/src/api/routes/knowledge_bases.py` | purge 增加 `delete_by_kb` 兜底 + 失败 422；引入 `logger` / `AppException` |
| `backend/src/application/kg_retrieval.py` | 逐跳挂载隔离（节点资源身份缓存 + 两端校验） |
| `backend/src/application/kg_service.py` | `build(rebuild=True)` 单事务 + 回滚；新增 `delete_resource_nodes` |
| `backend/src/api/routes/{herbs,prescriptions,theories,literatures}.py` | 删除时同步清理 KG 节点；更新时把重新向量化失败写入审计详情 |
| `backend/src/application/evidence.py` | 新增 `displayable_hits()`；`build_evidence` 按过滤后顺序连续编号 |
| `backend/src/application/rag_service.py` | `_build_user_prompt` / `_build_citations` 共用 `displayable_hits` 编号 |
| `backend/src/application/self_reflection.py` | 新增 `evidence_number()`；校验与两类 LLM 提示统一按 `source_index` 编号 |
| `backend/tests/test_batch2_lifecycle.py`（新增） | 14 条 Batch 2 回归测试 |

---

## 4. 向后兼容性说明

1. **未改任何阈值数值**（`RELEVANCE_THRESHOLD` 等保持原值，符合 U-3）。
2. **未改 API 响应结构**：Evidence / Citation 字段集合不变；审计详情新增的 `_vector_revectorize_failed` 为附加键。
3. **未改数据库 schema，无新增迁移**；`ids_by_doc` / `delete_by_ids` / `delete_by_kb` 是向量库接口新增方法（补缺口，不改既有方法语义）。
4. **既有契约保留**：资源更新仍 best-effort（向量失败不阻塞）；资源删除时向量清理失败仍 422 且资源保留（既有用例 `test_delete_blocked_when_cleanup_fails` 未受影响）。
5. 唯一对外可感知的行为变化：进入 Prompt 的资料块只包含"可展示命中"（≥ 阈值），编号因此连续 —— 这是修复 BUG-016 的必要条件，见 §5。

---

## 5. 需要知晓的行为变更（对使用方）

| 场景 | 变更前 | 变更后 |
|---|---|---|
| 文档 purge 时向量库不可用 | 静默成功、向量残留 | **422**，文档保留，待恢复后重试 |
| KB purge 时向量库不可用 | 同上 | **422**，KB 仍在回收站 |
| 资源删除时向量清理部分失败 | 挂载回滚但向量已丢 | 挂载回滚 + 已删向量**补偿回写** |
| 低于阈值的检索命中 | 仍在 Prompt 中编号（可被引用但无卡片） | **不进入 Prompt 编号**，不可能被引用 |
| Reflection / revise 提示中的资料编号 | 过滤后位序 | 与答案 `[citation:N]` **同一编号空间** |
| 资源更新后重新向量化失败 | 仅 warning | error 日志 + 审计详情 `_vector_revectorize_failed`（更新仍成功） |
| KG 多跳检索 | 可走到未挂载/他库资源 | 逐跳校验，只产出两端均已挂载的事实 |

---

## 6. 尚未验证的环境依赖（代码修复完成，待真实环境验证）

| Bug | 待验证项 | 原因 |
|---|---|---|
| BUG-006 / 007 | 真实 Milvus 下 `delete_by_doc` / `delete_by_kb` 的实际删除效果与性能 | Milvus 19530 不可达；用例走 `InMemoryVectorStore`（conftest 注入），仅验证调用与结果 |
| BUG-046 | Milvus 对**同主键 insert** 的语义（覆盖 vs 重复）；`replace_doc` 的条数校验与"清旧写新"回退分支 | 同上；已用 InMemory 实现验证两条路径（正常 + 写入失败），Milvus 语义待真机确认 |
| BUG-045 | 补偿回写在真实 Milvus 上的可用性（向量读取再写回） | 同上 |
| BUG-008 / 009 / 010 | 大数据量图谱下的遍历开销与重建耗时 | 需真实数据规模 |
| BUG-015 / 016 | 真实 LLM 下答案引用编号与卡片的一致性（编号变更前后答案文本可能有差异） | LLM 为 mock；已用确定性单测覆盖编号映射 |

---

## 7. 本批未处理（按约定留给后续 Batch / 待确认）

- BUG-011（评测失败被记 0 分）、BUG-019（allow_retry 路径一致）等评测可信度问题：归 Batch 3。
- BUG-012（审计失败回滚业务事务）：归 Batch 4，本批未动 `AuditService`。
- BUG-047 的**界面层**"向量过期"提示（前端标记）未做：本批只保证后端留痕（error 日志 + 审计），避免改动响应契约。
- 孤儿向量的**历史存量清理**脚本未做：需要 Milvus 可用时执行，建议待环境恢复后按 `delete_by_kb` / `delete_by_doc` 批量核对（可列入 Batch 6 运维项）。
- 6 条既有 lint warning：Batch 6 处理，本批数量未增加。

---

## 8. 结论

Batch 2 的 10 个生命周期与证据可信性缺陷**全部修复并各有回归测试**；全部质量门（pytest / 前端 test / tsc / lint / build / Alembic）通过且较 Batch 1 后基线只增不减；未引入新业务功能、未改阈值与实验指标定义、未改数据库结构、未产生新的 Alembic head。

**Batch 2 完成，按你的要求停止，不进入 Batch 3。** 待你确认后再执行 Batch 3（评测可信度）。
