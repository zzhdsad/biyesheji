# Batch 6 Fix Report

本批为质量收尾批次，处理 BUG-059～BUG-073、lint warnings 与仓库卫生。
原则：最小修改、不改既有业务规则、不改 API/SSE 契约、不删历史数据。

- 基线提交：`8fa27d6 提交 Batch 1-5 修复基线`（本批改动全部叠加在其之上）
- 本批提交：`fix: complete batch 6 bug hardening`

## 1. 批次范围

| 分类 | 编号 |
|---|---|
| 前端渲染 / 交互 | BUG-059、BUG-060、BUG-061 |
| 证据与引用 | BUG-063、BUG-064、BUG-065 |
| SSE 协议 | BUG-066 |
| 数据库迁移 | BUG-067 |
| 后端一致性与健壮性 | BUG-068、BUG-069、BUG-070、BUG-071、BUG-072 |
| 仓库卫生 | BUG-073、`.gitignore` |
| 代码检查 | lint warnings（`react-hooks/exhaustive-deps` 5 处） |

明确不在本批范围：RAG 检索阈值 / Router / Evidence Gate / Self Reflection 的业务规则、
TASKS.md、部署文件（Dockerfile）、历史 migration 重写。

## 2. 修复结果

### BUG-059（/kb 一次渲染全部知识库）

- 新增纯函数模块 `frontend/src/utils/pagination.ts`
  （`KB_WINDOW_SIZE = 24`、`clampVisibleCount`、`nextVisibleCount`）
- `frontend/src/app/kb/page.tsx` 改为窗口渲染：`kbs.slice(0, renderedCount)`
- 首屏渲染 24 条，点击“加载更多”按 `KB_WINDOW_SIZE` 步长扩窗（收敛到总数即止）
- `clampVisibleCount` 保证“数据变少”时窗口不越界，避免残留空白
- **未修改 `GET /kb` API**，未影响其余 5 个消费方
- 新增 `frontend/src/utils/pagination.test.ts`

### BUG-060（注册页与后端能力不一致）

- 关闭，无需改动：注册页 `frontend/src/app/register/page.tsx` 已在 Batch 1-5 基线提交中删除

### BUG-061（前端 Cookie 有效期与后端 JWT 不一致）

- `frontend/src/services/token.ts` 新增导出常量 `TOKEN_MAX_AGE_SECONDS = 86400`
- Cookie 写入时 `maxAge` 使用该常量，生命周期调整为 24 小时
- **JWT TTL 未修改**（后端仍为 1440 分钟 = 24 小时），对齐方式是缩短前端 Cookie 而非放宽后端
- `backend/src/core/config.py` 中错误的“7 天”注释同步为 24 小时
- 新增 `frontend/src/services/token.test.ts`

### BUG-063（evidence_id 兜底分支不稳定）

- `backend/src/application/evidence.py` 新增 `stable_evidence_id()`
- 无 `chunk_id` 时改为「来源 + 内容摘要」生成 ID
- 去除原先以 `source_index` 作为 fallback 的写法（顺序变化会导致同一个证据 ID 漂移）
- 保证同一证据稳定、不同证据可区分

### BUG-064（KG 证据编号与分层展示顺序不一致 / dense_score 量纲混用）

- **仅增加已知限制注释与锁定测试，不修改任何排序逻辑**
- `rag_service.py`：说明 KG 命中拼在向量命中末尾，而 `group_evidence()` 按组内最高分重排，
  因此编号顺序与分层展示顺序可能不一致；刻意不在此处重排（会改变既有 Prompt 编号与引用对应关系）
- `kg_retrieval.py`：说明复用 `dense_score` 是为了让 Relevance Gate 能计入 KG 命中，
  但 `fact.score`（实体匹配分 × 跳数衰减）与 BGE-M3 COSINE 相似度不同量纲，Gate 判断偏乐观；
  修正需重定 Gate 口径并跑评测，不在本批范围

### BUG-065（非流式响应丢失 KG 关系字段）

- `backend/src/api/routes/chat.py` 的 `Citation` 增加可选字段 `kg_relation` / `kg_hop` / `kg_provenance`
- `frontend/src/types/index.ts` 同步这三个可选字段
- 非流式 `/chat/ask` 不再静默丢弃 KG 关系信息，流式与非流式语义一致
- 纯增量字段，非 KG 证据为 `None`，旧客户端解析不受影响

### BUG-066（SSE 流式与最终答案不一致）

- **事件名集合不变**，未新增 `corrected` 事件，未取消流式输出
- `rag_service.py` 增加说明：`delta` 携带的是 Self Reflection **之前**的文本
  （citations 须先于生成下发，Reflection 只能在完整答案后判定，二者有天然先后顺序）
- 明确约定 `done.answer` 为最终权威答案，客户端应以其替换正文，且与落库答案一致
- 由 `backend/tests/test_batch6_quality.py` 锁定“事件名集合不变”这一契约

### BUG-067（迁移链缺失漂移列）

- 新增 Alembic migration `c185a0406aa3`，父版本 `f3a1c7d9b2e4`（追加在原 head 之后，未重写历史）
- `upgrade()` 用 `inspect` 判断列存在才 `add_column`，对已有库幂等
- 补齐 `documents.progress_percent`（INTEGER NOT NULL DEFAULT 0）
  与 `evaluation_results.faithfulness`（DOUBLE PRECISION NOT NULL DEFAULT 0.0）
- `downgrade()` 保持空操作，**原因**：开发库两列分别有 2492 / 40 行真实数据，
  `DROP COLUMN` 会造成不可逆数据损失；测试中以源码守卫禁止后人为“对称性”补上 `drop_column`
- 验证：开发库 `upgrade → downgrade → upgrade` 往返通过，列与数据完好
- 验证：全新数据库（`test_alembic_smoke` 冒烟库）从头 upgrade 后两列被真正创建，类型一致

### BUG-068（`_require_admin` 四处重复实现）

- `backend/src/core/deps.py` 新增 `require_admin(request, message)`（非 admin 抛 `PermissionDeniedError`）
- `audit.py` / `users.py` / `settings.py` / `admin.py` 四处重复实现改为委托调用
- 各调用点保留原有业务文案（如“仅管理员可查看审计日志”），**403 响应体保持不变**
- 返回值语义保持：`users.py` 仍返回当前 `User`（用于“操作者 = 当前管理员”场景）

### BUG-069（回收站保留天数三个口径）

- 三个模块统一使用 `settings.TRASH_RETENTION_DAYS`（1-30 天可配）
- 去除 `users.py` 中写死的 `TRASH_RETENTION_DAYS = 7`
- 去除 `knowledge_bases.py` 在 import 期把配置固化成模块常量的写法（改为函数内读取，运行期变更才生效）
- 提示文案与配置动态同步（`BatchDeleteResult.message` 改用 `default_factory` 求值）
- `documents.py` 删除提示同步跟随配置

### BUG-070（仪表盘统计包含回收站数据）

- `admin.py` 的 `total_docs` / `total_kbs` 增加 `deleted_at IS NULL` 过滤
- 统计口径与列表页（统一过滤软删除）一致

### BUG-071（batch_restore 预检全表加载）

- `users.py` 的 `batch_restore_users` 改为按选中 `User.id.in_(payload.user_ids)` 精确查询
- 冲突预检改为只查可能与待恢复用户冲突的**活跃**用户（`deleted_at IS NULL` + email/username 匹配）
- 不再把整张 `users` 表（含 `hashed_password`）加载进内存，错误文案与 400 返回结构保持等价

### BUG-072（审计 IP 在反向代理后失真）

- `backend/src/core/deps.py` 新增 `get_client_ip(request)`
- `backend/src/core/config.py` 新增 `settings.TRUSTED_PROXY_IPS`（逗号分隔的可信代理 IP）
- **默认为空，行为与改动前完全一致**（返回直连 peer IP）；仅当直连来源命中白名单时才取
  `X-Forwarded-For` 最左跳，避免客户端伪造 XFF 污染审计日志
- `herbs.py` / `prescriptions.py` / `theories.py` / `taxonomy.py` / `literatures.py` /
  `knowledge_bases.py` 六处重复实现改为统一委托
- **Dockerfile 未修改**：uvicorn `--proxy-headers` / `--forwarded-allow-ips` 留到部署阶段决定

### BUG-073（构建产物与临时文件入库）

- `.gitignore` 增加：`*.tsbuildinfo`、`*.log`、`/_diag*.py`、`/_tmp*.py`、
  `backend/_tmp*.py`、`backend/tests/_debug_*.py`
- 对 14 个已跟踪的产物 / 日志 / 临时脚本执行 `git rm --cached`：
  - `frontend/tsconfig.tsbuildinfo`
  - `backend/uvicorn_*.log`（6 个）
  - `next_dev_*.log`（3 个）
  - `_diag.py`、`_diag2.py`、`_tmp_auth_test.py`、`_tmp_bcrypt_check.py`
- **仅移出 Git 索引，本地文件全部保留**（不物理删除、不影响运行）
- **未修改 Git 历史**，未执行 history rewrite
- `AGENTSmd.bay` 按要求**未处理**（仍在版本控制中）
- `backend/_tmp_audit_schema.py` 未跟踪，由新增忽略规则覆盖，本地保留

### Lint

- 修复 5 处 `react-hooks/exhaustive-deps` warning：`herbs` / `prescriptions` / `theories` /
  `users` / `settings` 页面
- 做法：先用 `useCallback` 固定加载函数引用（依赖为稳定的 `message` / `form`），
  再写入 effect 依赖数组；依赖稳定 → effect 仍只在挂载时执行一次，**hook 行为不变**
- 最终 `next lint`：**0 warning / 0 error**

## 3. 验证结果

| 项目 | 结果 |
|---|---|
| Backend pytest | **676 passed, 2 skipped**（基线 652 + 新增 24） |
| Frontend test | **87 passed**（基线 77 + 新增 10） |
| `tsc --noEmit` | **0 error** |
| `next lint` | **0 warning**（本批修复前为 5 warning） |
| `npm run build` | **success** |
| Alembic heads | 唯一 head `c185a0406aa3` |
| migration upgrade / downgrade / upgrade | **passed**（开发库往返，数据与列完好） |
| 全新数据库迁移 | **passed**（冒烟库从头 upgrade 后两列存在且类型一致） |

补充：全量 pytest 中曾出现一次偶发 `PytestUnraisableExceptionWarning`
（asyncpg 连接回收时的 `Connection._cancel` 协程未等待），**非失败项**；
单独重跑 `tests/test_alembic_smoke.py` 与 `tests/test_batch6_quality.py` 均未复现。

## 4. 本批未处理遗留项

以下均为**未修复**，如实记录，供后续排期：

1. `backend/src/api/routes/` 下 `herbs.py` / `prescriptions.py` / `theories.py` /
   `literatures.py` / `taxonomy.py` 仍存在内联的 `user.role != "admin"` 判断
   （BUG-068 按约定只收敛 audit / users / settings / admin 四处，资源路由的业务复制未动）
2. 前端 `/users` 页面回收站提示文案仍写死“7 天内可恢复”（BUG-069 仅统一后端口径，
   前端未接入系统配置的保留天数）
3. `AGENTSmd.bay` 按要求保留在版本控制中，未处理
4. BUG-072 的部署侧（uvicorn `--proxy-headers` / `--forwarded-allow-ips`）未实施，
   容器化部署需自行配置 `TRUSTED_PROXY_IPS` 才能取得真实客户端 IP
5. `config.py` 中的 `DEV_DEFAULT_SECRET_KEY` 开发默认值仍存在（Batch 1 引入，
   已有 `ENV=prod` 强制覆盖校验），本次未改动

## 5. 提交范围

以最终 `git status` 为准：

- 修改文件：27
- 新增文件：6（`c185a0406aa3` 迁移、`test_batch6_quality.py`、`token.test.ts`、
  `pagination.ts`、`pagination.test.ts`、本报告 `BATCH_6_FIX_REPORT.md`）
- `git rm --cached`：14（构建产物 / 日志 / 临时脚本，本地文件保留）
- 合计：47

未纳入本次提交：`frontend/tsconfig.tsbuildinfo`（构建产物，已 `git rm --cached`）、
`backend/_tmp_audit_schema.py`（临时诊断脚本，已被忽略规则覆盖）、
`AGENTSmd.bay`（按要求保留未处理）。
