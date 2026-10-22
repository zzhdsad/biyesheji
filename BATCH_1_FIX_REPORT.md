# BATCH_1_FIX_REPORT.md

> 第二轮 Batch 1（安全与数据正确性）修复报告
> 依据：`BUG_AUDIT_REPORT.md`　生成时间：2026-09-23
> 范围：**仅 Batch 1 的 10 个 Bug**（BUG-001/002/003/004/005/014/026/027/028/040），未触碰 Batch 2 及以后内容。

---

## 1. 总览

| 项 | 结果 |
|---|---|
| 本批计划修复 | 10 个（BUG-001、002、003、004、005、014、026、027、028、040） |
| 已修复 | **10 个**（全部） |
| 未修复 / 跳过 | 0 |
| 新增后端回归测试 | **13 条**（`backend/tests/test_batch1_security.py`，全部通过） |
| 新增前端回归测试 | **2 条**（契约守卫，全部通过） |
| 修改文件 | 18 个（后端 9、前端 9，含删除 `/register` 页）+ 新增 2 个文件（`utils/net.py`、`tests/test_batch1_security.py`） |
| pytest | **603 passed, 2 skipped**（修复前基线 590 passed, 2 skipped → **+13，无回归**） |
| frontend test | **64 passed / 0 failed**（基线 62 → **+2**） |
| tsc --noEmit | 通过（exit 0） |
| lint | 通过（exit 0；6 条既有 `react-hooks/exhaustive-deps` warning **数量与位置不变**，归 Batch 6） |
| build | 成功（17 页，`/register` 已消失） |
| Alembic | `current = heads = b7c4e8f2a1d9`，单链无多 head（本批未动迁移） |

---

## 2. 逐 Bug 修复明细

### BUG-001（P0）评测域完全没有知识库权限隔离

- **根因**：`evaluation.py` 全部 handler 未取 `request.state.user`，`EvaluationService` 也不接收用户信息；`list_test_cases / list_history / confirm_test_case` 只按 `kb_id` 过滤 → 任意登录用户可读改他人私有库测试集与 golden_answer。
- **修复**：
  - `backend/src/core/deps.py` 新增 `get_kb_role / require_kb_read / require_kb_write` 角色校验原语（owner/admin/editor 可写，viewer 只读，public 库对登录用户按 viewer 处理，脏角色值降级为 viewer）。
  - `backend/src/api/routes/evaluation.py`：
    - `POST /upload`、`POST /run`、`PATCH /test-cases/{id}`（按用例所属 KB）→ **写权限校验**；
    - `GET /test-cases`、`GET /runs/{run_id}`、`GET /results`（kb_id 或 run_id 维度）→ **读权限校验**；
    - `GET /runs`、`GET /results` 未传 kb_id 时 → 普通用户按 `get_accessible_kb_ids` 过滤（admin 不变）。
  - `evaluation_service.py`：`list_runs / list_history` 新增 `accessible_kb_ids` 可选参数（**默认 None＝不过滤，旧调用行为完全不变**）。
  - 非法 `run_id` 由静默空结果改为 **422**（`_parse_run_id`）。
- **回归测试**：`test_evaluation_test_cases_require_kb_access`、`test_evaluation_list_endpoints_scoped_to_accessible_kbs`、`test_evaluation_run_detail_requires_kb_access`。

### BUG-002（P1）`PUT /settings/model` 任意登录用户可改全局 LLM 配置

- **根因**：同一文件里 `/system` 有 admin 校验、`/model` 却没有（docstring 明写“已登录用户均可修改”）。
- **修复**：`settings.py` 新增 `_require_admin()`，`PUT /model` 与 `POST /model/test` 均强制 admin；docstring 更正。
- **回归测试**：`test_model_config_update_requires_admin`（普通用户 403 / admin 200）。

### BUG-003（P1）`/settings/model/test` SSRF（任意 URL + 回显原文）

- **根因**：直接把用户提交的 `llm_base_url` 用于服务端请求，异常/响应原文回显。
- **修复**：
  - 新增 `backend/src/utils/net.py`：`is_safe_public_url / assert_safe_public_url`（仅 http/https；解析后 IP 不得落入回环/私有/链路本地 169.254/保留段；DNS 解析失败 fail-closed；支持 monkeypatch 以便离线测试）。
  - `settings.py`：`/model/test` 先 admin 校验，再 `assert_safe_public_url()`，非法地址 **400**；失败信息只回显异常类型，原文写入日志。
- **回归测试**：`test_model_test_blocks_internal_urls`（169.254.169.254 / 127.0.0.1 / localhost / 10.x / file:// 全部 400）、`test_safe_public_url_unit`（含“域名解析到内网也拒绝”）。

### BUG-004（P1）KB 成员角色服务端不校验（viewer / public 库可写）

- **根因**：`deps.get_accessible_kb_ids` 只有“可读”语义，documents 的写操作直接复用它 → `readable ⇒ writable`。
- **修复**：`documents.py` 新增 `_require_kb_write_access()` 与 `_writable_kb_ids()`，在 `upload / backfill-source / parse / reindex / delete` 上执行写角色校验；`backfill-source` 对非 admin 改为按“可写 KB 集合”过滤。
  - 保持既有校验顺序（参数/文件 400 → KB 存在性 404 → 权限 403）：KB 不存在时不在此抛错，交由各 Service 的既有 404 分支处理（已实测 `test_upload_*`、`test_source_fields` 三个既有用例仍按 400 返回）。
- **回归测试**：`test_viewer_cannot_write_documents`（viewer 可读 200；上传/解析/删除 403）、`test_public_kb_allows_read_but_denies_write`（public 库非成员可读、删除 403）。

### BUG-005（P1）`must_change_password` 后端从不强制（仅前端组件）

- **根因**：字段只被写入/清除，全仓无任何消费点。
- **修复**：`deps.py` 新增 `require_password_changed` 依赖，挂在 `protected_router` 上（`__init__.py`）。`/auth/me`、`/auth/logout`、`/auth/change-password` 不在 protected 组内，仍可用，保证用户有改密出口。未改密访问业务接口返回 **403 + “请先修改初始密码，再使用系统功能”**。
- **回归测试**：`test_must_change_password_blocks_business_api`（未改密 403 → /me 200 → 改密后 200）。
- **已知行为变更**：`tests/test_auth.py::test_protected_endpoint_with_token_200` 原先用“新建未改密用户”直连业务接口并断言 200，与本次强制改密规则直接冲突。**未删除该用例**，改为先完成改密再访问（注释说明原因），并新增上述 403 用例佐证规则生效。

### BUG-014（P1）`/register` 调用后端不存在的 `POST /auth/register`

- **修复（按决策 U-4：不新增开放注册，下线入口）**：
  - 删除 `frontend/src/app/register/page.tsx` 及空目录；
  - `services/auth.ts` 移除 `apiRegister` / `RegisterPayload`；
  - `stores/userStore.ts` 移除 `register` action 与相关 import；
  - `services/api.ts`、`middleware.ts`（PUBLIC_PAGES）、`AuthInit.tsx` 注释同步；
  - 后端 `api/routes/__init__.py` 修正“/auth/register 免鉴权”的过期注释。
- **回归测试**：前端契约守卫新增「注册入口已下线」用例（前端无 `/auth/register` 调用与 `apiRegister`、后端无该路由、`app/register` 目录不存在）；`KNOWN_MISSING` 清空。

### BUG-026（P2）生产环境固定 `SECRET_KEY`

- **修复**：`config.py` 抽出 `DEV_DEFAULT_SECRET_KEY`，新增 `model_validator`：ENV=prod 且密钥仍为默认值 → **启动即抛错**；dev/test 行为不变。
- **回归测试**：`test_prod_rejects_default_secret_key`。

### BUG-027（P2）生产代码内置 `TEST_MODE_ENABLED` 后门

- **修复**：`deps.py` 新增 `test_mode_active()`，仅 `ENV ∈ {dev, test, testing}` 时允许测试模式生效；`get_current_user` 改用它 → 生产环境即使开关被置真也不会跳过 JWT。
- **回归测试**：`test_test_mode_backdoor_disabled_in_prod`。

### BUG-028（P2）登录页开放重定向

- **修复**：`app/login/page.tsx` 新增 `safeRedirect()`：跳转地址必须是站内绝对路径，拦截 `//evil.com`、`/\evil.com` 等非站内形式，非法值回退 `/chat`；已登录自动跳转与登录成功后跳转两处都走该函数。
- **回归测试**：前端契约守卫新增「登录页 redirect 仅允许站内路径」用例。

### BUG-040（P2）最后一个管理员可被降级/删除/禁用

- **修复**：`users.py` 新增 `_count_active_admins()` 与 `_ensure_admin_kept()`，在 `PUT /{user_id}`（admin→非 admin）、`DELETE /{user_id}`、`POST /{user_id}/disable`、`POST /batch-delete`（预检阶段）生效；不通过时 **400 + 明确提示**。
- **回归测试**：`test_last_admin_cannot_be_demoted_or_deleted`（仅剩 1 个 admin → 400；非 admin 不受限）、`test_admin_can_still_manage_other_admins`（多 admin 时仍可降级/删除，防止过度收紧）。

---

## 3. 修改文件清单

| 文件 | 改动要点 |
|---|---|
| `backend/src/core/deps.py` | 新增 `test_mode_active()`、KB 角色常量、`get_kb_role / require_kb_read / require_kb_write`、`require_password_changed`；测试模式加环境门禁 |
| `backend/src/core/config.py` | `DEV_DEFAULT_SECRET_KEY` + 生产环境校验器 |
| `backend/src/utils/net.py`（新增） | URL SSRF 安全校验 |
| `backend/src/api/routes/__init__.py` | protected_router 增加 `require_password_changed`；修正 register 注释 |
| `backend/src/api/routes/evaluation.py` | 8 个端点权限校验 + 非法 run_id 422 |
| `backend/src/application/evaluation_service.py` | `list_runs / list_history` 增加可选 `accessible_kb_ids`（默认 None，向后兼容） |
| `backend/src/api/routes/settings.py` | `/model`、`/model/test` admin 校验 + SSRF 校验 + 日志化异常 |
| `backend/src/api/routes/documents.py` | `_require_kb_write_access` / `_writable_kb_ids` 并接入 5 个写端点 |
| `backend/src/api/routes/users.py` | 最后一个管理员保护（4 处） |
| `backend/tests/test_batch1_security.py`（新增） | 13 条 Batch 1 回归测试 |
| `backend/tests/test_auth.py` | 1 条用例按新规则调整（先改密再访问），未删除 |
| `frontend/src/app/register/page.tsx` | **删除**（含空目录） |
| `frontend/src/services/auth.ts` | 移除 `apiRegister` / `RegisterPayload` |
| `frontend/src/stores/userStore.ts` | 移除 `register` action |
| `frontend/src/app/login/page.tsx` | `safeRedirect()` 防开放重定向 |
| `frontend/src/middleware.ts` | PUBLIC_PAGES 去掉 `/register` |
| `frontend/src/services/api.ts`、`components/AuthInit.tsx` | 注释同步 |
| `frontend/tests/api-contract.test.ts` | 清空 KNOWN_MISSING；新增注册下线 + 重定向防护 2 条守卫 |

---

## 4. 向后兼容性说明

1. `evaluation_service.list_runs / list_history` 新参数默认 `None` → **所有旧调用与历史数据行为不变**。
2. 权限校验**只收紧不放宽**：admin 全通、owner/editor 写权限不变；受影响的只有“viewer / 非成员越权 / 未改密”三类此前本就不该放行的调用。
3. 未修改任何 RAG 阈值、策略、实验指标定义；未改 SSE 事件结构；未改数据库 schema（**无新增迁移**）。
4. 前端仅删除死代码与新增校验函数，未改任何请求/响应字段。

---

## 5. 需要知晓的行为变更（对使用方）

| 场景 | 变更前 | 变更后 |
|---|---|---|
| 管理员新建/重置密码的用户直接调业务 API | 可用 | **403**（须先 `POST /auth/change-password`；UI 已有强制改密弹窗，渲染于 `AppLayout`） |
| 任意登录用户读写他人私有库评测数据 | 可用 | **403** |
| viewer 成员 / public 库非成员删改文档 | 可用 | **403**（读取仍可用） |
| 普通用户 `PUT /settings/model`、`POST /settings/model/test` | 可用 | **403（仅 admin）** |
| 连通性测试填内网/元数据地址 | 会真实发起请求并回显 | **400 且拒绝请求** |
| 访问 `/register` | 页面存在但必然 404 | **入口已下线** |
| 删除/降级/禁用最后一个管理员 | 允许 | **400** |

---

## 6. 尚未验证的环境依赖（标记：代码修复完成，待真实环境验证）

| Bug | 待验证项 | 原因 |
|---|---|---|
| BUG-003 | 对真实公网 LLM 地址的连通性测试仍可成功（未因防护被误拦） | 环境无真实 LLM/外网；已用 monkeypatch DNS 做单元级正向验证，真实 DNS 场景待验 |
| BUG-001/004 | 与 Milvus 联调下（评测 run、文档上传→向量化）权限链路 | Milvus 19530 不可达；本次用例走 InMemoryVectorStore，权限逻辑与向量库无关 |
| BUG-005 | Redis 可用时会话缓存与强制改密的交互 | Redis 6379 未启动（2 个既有用例仍 skip） |
| BUG-026/027 | `ENV=prod` 启动失败行为、生产 JWT 签发 | 需真实生产部署验证；已用 `Settings(ENV="prod", ...)` 单测覆盖 |

---

## 7. 本批未处理（按约定留给后续 Batch / 待确认）

- **U-5 临时与调试文件清理**（`_diag.py`、`_diag2.py`、`next_dev_20260906.log`、`AGENTSmd.bay`、`_tmp_*.py`、`frontend/tsconfig.tsbuildinfo`）：属 Batch 6「仓库卫生」，本批未动（`frontend/tsconfig.tsbuildinfo` 因 build 被更新，仍是被跟踪状态，待 Batch 6 与你确认后再 `git rm`）。
- 6 条既有 lint warning：Batch 6 处理，本批数量未增加。
- BUG-041（成员角色 `_role_valid` 缺装饰器）、BUG-042（批量成员重复 500）、BUG-043/044 等知识库侧 P2：不在 Batch 1 清单，未扩大范围。
- 审计中记录但被你裁定为业务决策的项（BUG-018 门禁顺序、BUG-019 allow_retry、相似度阈值量纲）：本批**未改**，等待 Batch 3 按 U-2 处理 BUG-019。

---

## 8. 结论

Batch 1 的 10 个安全与数据正确性缺陷**全部修复并各有回归测试**；全部质量门（pytest / 前端 test / tsc / lint / build / Alembic）通过且较基线只增不减；未引入新功能、未改业务规则与实验指标、未产生新的 Alembic head。

**Batch 1 完成，按你的要求停止，不进入 Batch 2。** 待你确认后再执行 Batch 2（删除生命周期与证据可信性）。
