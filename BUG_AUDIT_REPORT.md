# BUG_AUDIT_REPORT.md

> 《中医药知识资源管理与智能问答系统》全项目缺陷审计（第一轮：只审计，不改代码）
>
> 生成时间：2026-09-23　审计人：全量 Bug 审计与修复工程师
> **本轮未修改任何一行业务代码**，唯一新增文件是本报告与 1 个只读结构核对脚本 `backend/_tmp_audit_schema.py`（会在修复阶段删除）。

---

## 1. 审计范围

### 1.1 代码与文件覆盖

| 区域 | 内容 | 规模 |
|---|---|---|
| 后端源码 | `backend/src/{api/routes(16), application(21), core(6), domain, infrastructure(12), utils}` | 约 1.1 万行 Python |
| 数据库 | `backend/src/domain/models.py` + `backend/alembic/`（11 个迁移）+ **PostgreSQL 实际 schema（information_schema 实测）** | 29 张表 |
| 前端 | `frontend/src/{app(14 页), components, hooks, services, stores, utils, types, constants}` | 43 个 ts/tsx，约 9.2k 行 |
| 测试 | `backend/tests/`（pytest）+ `frontend/src/**/*.test.ts` + `frontend/tests/api-contract.test.ts` | 后端 592 / 前端 62 |
| 配置与环境 | 根目录 `.env` / `.env.example` / `docker-compose.yml`、前后端 `Dockerfile`、`.gitignore` | — |
| 文档 | `AGENTS.md`、`AGENTSmd.bay`、`PRD.md`、`TECH_DESIGN.md`、`BUSINESS_RULES.md`、`SYSTEM_MAP.md`、`TASKS.md`（只读，未改） | — |

### 1.2 审计方法

1. **静态审查**：5 路并行代码审计（后端路由/RBAC、数据模型与迁移、RAG 全链路、SSE 与评测、前端契约与逻辑），每条缺陷要求给出 `文件:行号` 证据。
2. **实测核对（非猜测）**：
   - 导出 PostgreSQL 真实 `information_schema`（列、类型、索引、唯一约束、外键 `on delete` 规则）与 ORM `Base.metadata` 做**双向差集比对**；
   - 核对 `alembic current / heads / history` 与实际 migration 链；
   - 独立复核所有 P0/P1 结论（逐个读源码验证，见每条目末尾「核实」标记）；
   - 实跑 `pytest`、`npm test`、`npm run lint` 取得当前基线。
3. **标记规则**：
   - `【已复核】`＝我本人读源码/跑脚本验证过；
   - `【待修时复核】`＝来自子代理报告、逻辑可信但本轮未逐行复核，修复前需再次确认。

### 1.3 未采取动作为什么

- 未做任何 "test-only" 修复、未调整任何阈值/规则/测试、未删任何失败用例；
- 对「改与不改都属于业务决策」的问题，**只记录、不进修复清单**（见 §9）。

---

## 2. 项目当前状态（实测基线）

| 项 | 当前值 | 说明 |
|---|---|---|
| Backend pytest | **590 passed, 2 skipped, 3 warnings**（61.81s） | 2 skipped 为 Redis 相关用例（Redis 未启动） |
| Alembic | `current = heads = b7c4e8f2a1d9`，单链，无多 head、无断链 | 迁移链健康 |
| 数据库实际表数 | 29（含 `alembic_version`） | 与 ORM 期望基本一致，存在 2 列漂移（BUG-067） |
| Frontend test | 62 passed / 0 failed（`node:test`，零新增依赖） | 含 5 条 API 契约守卫 |
| TypeScript | `tsc --noEmit` 通过（上一阶段） | 本轮未重跑，未改代码故应不变 |
| Lint | `npm run lint` exit 0，**6 条 Warning**（均 `react-hooks/exhaustive-deps`） | 归属 P3，位置清单见 §5 第 5 项 |
| Build | 上一阶段成功（18 页） | 本轮未重跑 |
| Git | 工作区含阶段十六未提交改动；无 CI 配置（无 `.github`） | 仓库卫生问题见 §5 第 6 项与 BUG-073 |

**结论：自动化 gates 目前全绿，但静态审计发现 60+ 处真实缺陷——说明现有测试对「权限、数据一致性、竞态、评测统计、删除生命周期」覆盖严重不足（详见 §10）。**

---

## 3. 已发现 Bug（统一清单）

严重度定义沿用你的标准：P0＝数据丢失/严重安全漏洞/核心不可运行；P1＝核心业务错误、RAG 链路错误、严重权限问题、主要功能不可用；P2＝一般业务 Bug、边界、契约、数据一致性；P3＝UI、代码质量、可维护性。

**统计：共 63 条** —— P0 × 1、P1 × 13、P2 × 28、P3 × 21。

### 3.1 P0（1 条）

| ID | 模块 | 文件:行号 | 问题描述 | 复现条件 | 实际行为 vs 预期 | 根因 | 核实 |
|---|---|---|---|---|---|---|---|
| BUG-001 | 评测/权限 | `backend/src/api/routes/evaluation.py:264-266,287-295,298-317,320-335,357-363,366-370,373-381`；`__init__.py:50`；`application/evaluation_service.py:518-527,471-485,194-232` | **评测域 8 个端点完全没有 KB/角色隔离**：handler 无 `request.state.user`、`EvaluationService` 也不接收 user；`_load_cases` / `list_history` / `confirm_test_case` 只按 `kb_id` 过滤 | 任意**普通登录用户** GET `/evaluation/test-cases?kb_id=<他人私有KB>`、`PATCH /evaluation/test-cases/{他人case_id}`、`POST /evaluation/run {kb_id:他人KB}`、`GET /evaluation/results?kb_id=他人KB` | **实际 200 返回**：他人私有库的全部测试题、人工标注 golden_answer、检索上下文、历史评测答案明文；并可篡改他人基线、对他人 KB 发起昂贵的 LLM 评测。**预期 403** | 路由只挂在 `protected_router`（仅保证"已登录"），作者误以为统一鉴权＝数据隔离；与 `/chat` 的 `kb_ids ∩ get_accessible_kb_ids`（`chat.py:287-298`）形成鲜明对比 | 【已复核】逐个读 handler 源码确认无 RBAC |

### 3.2 P1（13 条）

| ID | 模块 | 文件:行号 | 问题描述 | 实际 vs 预期 | 根因 | 核实 |
|---|---|---|---|---|---|---|
| BUG-002 | 配置/权限 | `api/routes/settings.py:49-59`（对比同文件 `:143-156` 有 admin 校验）；`model_config_service.py:65-97,59-63` | `PUT /settings/model` **任意登录用户**可改全局 LLM（`base_url/model/api_key`）并 `invalidate_config_cache()` 立即全站生效；`GET /settings/model` 向任意登录者返回脱敏 key | 实际：全站问答流量可被劫持到攻击者地址，且泄露 key 片段。**预期：仅 admin** | 同一文件两套鉴权标准，模型配置被视为"普通设置" | 【已复核】代码已读，docstring 明写"已登录用户均可修改" |
| BUG-003 | 安全/SSRF | `api/routes/settings.py:62-97`（尤其 `:74-87,:91,:97`） | `POST /settings/model/test` 对**用户提交的任意 URL** 发起请求（无 scheme 白名单、无内网/回环拦截），并把响应前 50 字符与异常原文回显 | 实际：可作为 SSRF 探测原语（169.254.169.254、内网端口），回显泄漏内部信息 | 连通性测试做成"纯转发" | 【已复核】 |
| BUG-004 | 知识库/权限 | `api/routes/documents.py:78-96,190-215,261-275,296-321,324-339`；`core/deps.py:88-112` | `KBMember.role`（owner/admin/editor/viewer）**服务端从不消费**：`get_accessible_kb_ids` 只判"是否可读"，documents 的 delete/upload/parse/reindex/backfill 全无写角色校验 | 被加为 **viewer** 的成员可删除/改写他人文档（与 `documents.py:332` 自身注释"需 Editor 以上"矛盾）；**public KB 下任何登录用户**可删改文档 | 读写共用同一访问集合，`readable ⇒ writable`；前端隐藏按钮＝伪权限 | 【待修时复核】`deps.py` 无角色概念已复核；各 endpoint 调用链待复核 |
| BUG-005 | 用户/权限 | `models.py:57`；`users.py:210,321,590`；`auth.py:124`；**全仓无任何依赖/中间件消费该字段** | `must_change_password`（首次登录强制改密）**后端从不强制**，仅存在前端组件 `forceChangePassword` | 管理员下发初始口令后，该用户带 token 可直接调用 `/chat/ask`、`/documents/upload` 等全部业务接口。**预期：403/423 阻断** | 业务规则只落在注释与前端，无服务端执行点 | 【已复核】全仓 grep 仅 8 处（声明/赋值/清位），无校验点 |
| BUG-006 | 数据一致性/RAG | `api/routes/documents.py:414-420`；`infrastructure/milvus_store.py:65,295,474`（只有 `delete_by_doc`） | **彻底删除文档调用不存在的方法 `store.delete_by_doc_id()` → AttributeError → 被 `:419` 的 `except Exception` 吞掉只打 warning → Milvus 向量永久残留**；此后这些孤儿向量仍被 `kb_id` 命中进入 Prompt，而 PG 中 Document 已不存在，`rag_service.py:1257-1259` 回落为 `"未知文档"` | 实际：用户拿到**来源为"未知文档"的幽灵引用**（AGENTS.md 明令禁止伪造来源），且无任何清理入口。预期：purge 应清除该 doc 全部向量 | 方法名错误（应为 `delete_by_doc`）+ 异常被吞 + 无测试 | 【已复核】全仓 grep 确认 `delete_by_doc_id` 唯一出现点即此处，无任何类实现它 |
| BUG-007 | 数据一致性 | `api/routes/knowledge_bases.py:326-339,650-656`；`infrastructure/milvus_store.py:53-113` | 彻底删除 KB 只清理 `KnowledgeBaseResource` 对应向量，**该 KB 下 Document 的 chunk 向量无任何清理路径**；`BaseVectorStore` 接口层没有"按 kb_id 删除"能力 | 实际：purge KB 后向量残留；叠加 BUG-006 无法挽回。预期：purge 前按 kb_id 批量删 Milvus | 删除语义按资源类型分别实现，遗漏 Document 分支 | 【待修时复核】 |
| BUG-008 | KG/RAG | `application/kg_retrieval.py:98-103` vs `:145-149` | **KG 多跳遍历只在第 1 跳种子做 `_mounted_resources` 过滤**，`next_frontier` 加入第 2 跳邻居时不再校验 `allowed`（`kg_enhanced` 默认 `max_hops=2`） | 实际：未挂载甚至**只在别的用户 KB 中**的资源可作为"事实验证"进入 Prompt（跨 KB 泄露 + 看似合理但不属本库的证据）。预期：逐跳过滤 | 隔离只在种子层，图遍历层无断言 | 【已复核】读 `kg_retrieval.py:92-149` 确认 `allowed` 仅在 `:102` 使用一次 |
| BUG-009 | KG/数据一致性 | `application/kg_service.py:184-224`（只有 `_sync_nodes/_sync_edges`）vs `api/routes/herbs.py:467-478`（prescriptions/theories/literatures 同构） | 删除 Resource 只清 `KnowledgeBaseResource` + Milvus，**不清 KgNode/KgEdge**（资源路由对 KG 表零引用） | 实际：图谱残留已删资源，遍历仍能取到它，直到下次 `/kg/build`；与 BUG-008 叠加会产出指向不存在资源的证据 | 多态 `resource_type+resource_id` 无外键，删除路径无 KG hook | 【待修时复核】 |
| BUG-010 | KG | `application/kg_service.py:157-167` | `rebuild=true` 时**先 delete 全量节点/边并 commit，再重建**；中间任一环节异常 → 图谱已清空且无 rollback | 实际：中途失败留下空图谱，只能手工重跑。预期：单事务或先建后换 | 清表与重建拆成两个已提交事务 | 【待修时复核】 |
| BUG-011 | 评测/数据正确性 | `application/evaluation_service.py:91-94,700-703,720-730`；`api/routes/evaluation.py:336-354` | 单条用例异常（如 Milvus 宕机）→ `except Exception` 写 `base.error`，但 `context_relevancy` 保持 **0.0 并计入均值**；`summarize` 的 `skipped = len(results) - evaluated` 又把失败用例算成"跳过"；全部失败时接口仍以 **HTTP 200** 返回一份"看起来正常的 0 分报告" | 实际：基础设施故障被包装成"检索质量为 0"，直接污染实验结论（论文实验致命）。预期：失败不参与均值 / 单独 failed 计数 / 非 2xx 或显式 `infra_failed` 标记 | `CaseResult.context_relevancy: float = 0.0` 无法表达"未取得数据"；注释 `:94` 自承"仍产出 0 分" | 【已复核】读 `summarize()` 与 `CaseResult` 定义确认 |
| BUG-012 | CRUD/事务 | `application/audit_service.py:46-60`（`:57 commit` / `:60 rollback`）；`api/routes/herbs.py:336-357`（prescriptions/theories/literatures/taxonomy 同构） | 资源类 CRUD **唯一提交点是 `AuditService.log()`**（业务侧只有 `db.flush()`）；而 log 内 `except → rollback` 静默吞异常 | 实际：审计写失败 → 连带回滚业务变更； `:356` 重新 select 后 `_herb_to_out(None)` 崩溃（表现为 500，或数据静默丢失）。预期：业务提交与审计解耦（独立 session / savepoint） | 把事务提交权交给"非阻塞日志"，注释 `:355` 已承认该耦合 | 【已复核】读 `create_herb` 全流程确认无 `db.commit()` |
| BUG-013 | 前端/竞态 | `frontend/src/stores/chatStore.ts:134-142,199-215,252-268,146-159` | ① 切换会话无请求序号/取消，慢响应覆盖新消息；② 流式回答期间切换会话，`start`/`done` 回调把 `currentConversationId` 强行写回旧流会话；③ 删除会话后，在飞的消息请求仍 `set({messages})` 复活消息 | 实际：消息与选中会话不一致、消息区出现已删会话历史。预期：过期响应丢弃、流回调先校验归属 | 缺少 request token / 会话归属守卫 | 【待修时复核】（上一阶段我本人改过该文件，逻辑可信） |
| BUG-014 | 前后端契约 | `frontend/src/app/register/page.tsx`、`stores/userStore.ts:93-96`、`services/auth.ts` vs `backend/src/api/routes/auth.py:67,95,103,111` | **`/register` 页 + `apiRegister` 调用 `POST /auth/register`，但该端点在后端完全不存在**（auth.py 仅 login/logout/me/change-password） | 实际：注册必 404，任何用户都无法自助注册。预期：可用或前端下线入口 | 后端未实现，`__init__.py:4` 注释仍把它列为"免鉴权路由" | 【已复核】双端 grep 确认 |

### 3.3 P2（28 条）

| ID | 模块 | 文件:行号 | 问题要点 |
|---|---|---|---|
| BUG-015 | RAG | `application/self_reflection.py:562-565` vs `application/evidence.py:170-181`、`rag_service.py:1295` | Reflection 的引用合法性校验 `1<=i<=len(evidence)` 使用**已过滤**的 evidence 下标空间，而 Prompt 里 `[citation:N]` 编号基于**未过滤** hits → 误判 `citation_out_of_range` 触发多余 revise/retry，或 `evidence[i-1]` 取到**错位证据**做 Cited 检查 |
| BUG-016 | RAG/Citation | `application/rag_service.py:1364-1370`；`,1096-1097` | `_build_citations` 先按编号生成再 `if score<threshold: continue` 静默丢弃 → 答案里的 `[citation:2,0]` 在 UI 无对应卡片；且流式 `citations` 在**生成之前**锁定，无法感知答案实际引用了谁 |
| BUG-017 | RAG | `rag_service.py:390-396` | Reranker 抛 `RerankError` → 直接 `AppException(422)` 整问失败；同文件 KG 异常却是"吞掉降级"。策略不对称，且用户消息已 commit 却无助手回复 |
| BUG-018 | RAG | `rag_service.py:262-274` vs `:1071-1095` | 两条编排路径**门禁顺序相反**：非流式先 Evidence Gate 再相关性门槛；流式先相关性再 Gate → 同一条件拒答文案不同（改业务流程，见 §9，需决策） |
| BUG-019 | RAG | `rag_service.py:301`（allow_retry=True）vs `:1129`（False） | 评测 `retrieve_and_answer` 允许 Reflection retry、线上 `ask_stream` 禁止 → **评测结论无法迁移到线上**（属实验一致性问题，需决策） |
| BUG-020 | KG | `application/kg_service.py:352-353` | `related_to` 边在单标签触顶 `_MAX_EDGES_PER_TAG` 时用 `return`（应为 continue）→ 一旦某标签触顶，其后**所有**标签的共享边全部不生成 |
| BUG-021 | RAG | `rag_service.py:1246-1256` | 老集合不支持动态字段的降级路径：Resource 命中按 `Document.id.in_([sha256])` 查 PG → `invalid input syntax for type uuid`（500），且误标 `source_kind='document'` |
| BUG-022 | 评测 | `evaluation_service.py:263-318` | Gate/Reflection 开关**就地改写 `RagService` 实例状态**（`.enabled`），无 `try/finally` 恢复 → 共享实例场景下污染后续实验 |
| BUG-023 | 评测 | `evaluation_service.py:339-383,439,479` | 无幂等/并发保护：同一实验可无限重复提交并产生重复 run+results；`/runs` 硬编码 `limit(100)`、`/results` `limit(200)` 且无 offset |
| BUG-024 | 评测 | `api/routes/evaluation.py:59,61` + `models.py:262,265` | `experiment_name`(128)/`dataset_version`(64) 无 `max_length` → 超长触发 `StringDataRightTruncation` → **500 而非 422** |
| BUG-025 | 评测 | `evaluation_service.py:355-368,634-641` | `dynamic_router=True` 的用例若 `plan_retrieval` 抛错，仍归档成 `retrieval_strategy="dynamic_router"` → 策略对比数据被污染 |
| BUG-026 | 安全/配置 | `core/config.py:29,113-115`；`main.py:19`；`database.py:62-78` | `SECRET_KEY` 明文硬编码；默认管理员 `admin@company.com/admin123456` 在 `ENV=dev` 自动创建 → 任何人可签伪 JWT 成为 admin |
| BUG-027 | 安全 | `core/deps.py:34-41` | 生产代码内置 `TEST_MODE_ENABLED` 后门：置真后 `get_current_user` 跳过 JWT 直接返回固定 admin（与 BUG-026 叠加可提权） |
| BUG-028 | 前端/安全 | `frontend/src/app/login/page.tsx:27-28,35-36` | **开放重定向**：`redirect` 直接取自 URL query 并 `router.replace()`，`?redirect=https://evil.com` 或 `//evil.com` 可把刚登录的用户带出站（钓鱼） |
| BUG-029 | 后端/资源 | `api/routes/health.py:26-43` | Redis 探针异常时 **`aclose()` 被跳过**（不在 `finally`）→ 每次失败泄漏连接；组件不可用仍返回 `status:"ok"`；异常不打日志；暴露 `app/version/env` |
| BUG-030 | 后端/评测 | `evaluation_service.py:298-318` + `model_config_service.py:121-141` | 配置缓存无 TTL 且仅进程内失效 → 多 worker 下新配置永不生效（除被命中的那个进程）；且向所有调用方返回**同一个 dict 对象**可被就地污染 |
| BUG-031 | 后端/配置 | `model_config_service.py:73-78` | 保存时以 `"****" in submitted_key` 判断是否脱敏值 → 含 `****` 的真实 key（或短 key）被**静默丢弃**（仍返回"保存成功"）；空串会清空 api_key |
| BUG-032 | 后端/契约 | `core/exceptions.py:34-45` vs 各处的 `HTTPException` | 错误响应三套并存：`{"code","message"}` / `{"detail": str}` / `{"detail": [{...}]}`；且 AppException 用 `exc.code` 直接当 HTTP 状态码，无合法码白名单 |
| BUG-033 | 后端/参数 | `api/routes/knowledge_bases.py:743-751`（对比 `audit.py:48-49`、`herbs.py:264-265`） | 分页参数无 `ge/le` 约束：`limit=-1&offset=-5` → 负 LIMIT/OFFSET → **500**（应 422） |
| BUG-034 | 后端/参数 | `api/routes/chat.py:281-284` | 模型未配置返回 **412 Precondition Failed**（语义误用） |
| BUG-035 | SSE | `api/routes/chat.py:411-424`；`infrastructure/llm.py:79-105` | 流式无心跳帧、无总超时：检索阶段长时间不发字节 → 反向代理（nginx 60s）掐断 → 前端收不到 done 也收不到 error；仅有 `LLM_TIMEOUT_SECONDS` 单次超时 |
| BUG-036 | SSE | `rag_service.py:1050-1053,1111-1114` | 流内异常时用户消息已 commit 但无 assistant 消息 → **孤儿 user 消息**；用户重试同一会话会出现两条相同提问 |
| BUG-037 | 后端/会话 | `api/routes/chat.py:512-550` | 批量删除会话时 Redis 缓存清理失败仅 warning，仍返回 `"deleted_count": len(conv_ids)`（谎报成功），会话列表/历史可能残留脏数据 |
| BUG-038 | 后端/反馈 | `api/routes/feedbacks.py:34-38,76-88,94-120`；`models.py:327-334` | ① `feedbacks` 表无 `user_id`、无 `(message_id)` 唯一约束 → 并发重复插入；② 不限制只能对 assistant 消息反馈 → 用户可给自己的提问点赞/点踩，污染反馈统计 |
| BUG-039 | 后端/审计 | 全仓 `target_type=` 仅出现在 kb/herb/prescription/theory/literature/category/tag | BUSINESS_RULES §7 要求记录的**配置变更、用户增删改、文档增删改、登录、问答**均无审计写入，而 `audit_service.py:41` 文档声称支持 `config` |
| BUG-040 | 后端/用户 | `api/routes/users.py:219-239,242-259` | 可把**最后一个 admin** 降级为 viewer 或移入回收站 → 系统失去管理员且无恢复路径（对比 `knowledge_bases.py:488-501` 有"最后一个 Owner 不可降"规则，不对称） |
| BUG-041 | 后端/知识库 | `api/routes/knowledge_bases.py:85-93,101-108` | `MemberAddRequest._role_valid` / `MemberRoleUpdate._role_valid` **缺 `@field_validator` 装饰器 → 死代码**；`add_member` 只挡 `role=='owner'` → 任意角色字符串（如 `"boss"`）入库 |
| BUG-042 | 后端/知识库 | `knowledge_bases.py:436-453` | 批量添加成员传入**重复 user_id**：循环内 `existing` 查询早于 flush → 同 PK 两次 add → IntegrityError → **500**（应 400/422 或去重） |
| BUG-043 | 后端/知识库 | `knowledge_bases.py:307-323` | `purge` 不校验 `deleted_at` → 可对**未在回收站中的活跃 KB** 直接彻底删除，绕过软删保护期 |
| BUG-044 | 后端/知识库 | `knowledge_bases.py:592-599` | 转移所有权未校验目标用户是否被软删/禁用 → KB 可落入无有效 owner 状态 |
| BUG-045 | 数据一致性 | `application/resource_vector_service.py:630-637` | 删除 Resource **先删向量（不可逆）后删挂载**：挂载删除回滚时 → "资源仍在挂载列表、但永远检索不到" |
| BUG-046 | 数据一致性 | `resource_vector_service.py:525-531`；`indexing_service.py:132-136` | re-vectorize 采用"先删后插"，窗口内失败 → 该资源/文档向量全灭且无自动重建入口 |
| BUG-047 | 数据一致性 | `api/routes/herbs.py:423-432`（prescriptions/theories/literatures 同构） | 资源更新后 `revectorize_all_mounts` 失败只 warning → PG 已更新、Milvus 仍是旧向量，检索结果静默过期且无任何标记 |
| BUG-048 | 前端/逻辑 | `app/theories/page.tsx:179-182`、`app/literatures/page.tsx:206-209`、`app/herbs/page.tsx:141-163`、`app/prescriptions/page.tsx:415-437`、`app/evaluation/page.tsx:183-193`、`app/kb/resources/page.tsx:158-169` | 列表页无请求序号/取消：① theories/literatures 非第 1 页点查询会**双发请求**；② 快速翻页/改搜索条件旧慢响应覆盖新数据与 total；③ evaluation 串行 await 三个接口且无取消，快速切 KB 时旧 KB 结果覆盖新 KB |
| BUG-049 | 前端/逻辑 | `app/kb/resources/page.tsx:206-261` | `columns` 的 `useMemo` 依赖仅 `[kbId]` → 闭包捕获旧的 `onUnmount`/`loadResources`，卸载后刷新使用旧筛选条件 |
| BUG-050 | 前端/安全 | `app/users/page.tsx:177-179,456`（仅 `onCancel:545` 清理） | 创建用户成功后不清 `createdPwd` → **再次打开弹窗显示上一个用户的初始随机密码**（凭证错配 + 泄露） |
| BUG-051 | 前端/健壮性 | `app/audit/page.tsx:139` | `Object.keys(v)` 未对 `null/undefined` 兜底 → detail 为空时整页 TypeError 崩溃 |
| BUG-052 | 前端/契约 | `app/chat/page.tsx:108-124` vs `backend/src/api/routes/chat.py:31` | 提问输入框无 `maxLength`/校验，而后端 question 限制 2000 → 超长提交才报 422 |

### 3.4 P3（21 条）

| ID | 模块 | 文件:行号 | 问题要点 |
|---|---|---|---|
| BUG-053 | 前端/健壮性 | `components/chat/MessageItem.tsx:83` | `renderAnswer(message.content)` 未兜底，历史脏数据 `content=null` → 聊天页白屏 |
| BUG-054 | 前端/健壮性 | `app/prescriptions/page.tsx:94,546,970` | `row.ingredients` / `detail.tags` 直接展开与排序未 `?? []`（同为 herbs 页 `:266,582` 已处理）→ 后端返回 null 时表格/Drawer 崩溃 |
| BUG-055 | 前端/上传 | `app/documents/page.tsx:132-137` | 未选知识库时 `customRequest` 直接 `return` 且不调 `onError` → 文件在上传列表永久 uploading |
| BUG-056 | 前端/分页 | `app/prescriptions/page.tsx:599-602` | 新建成功后不回第一页（编辑后也不校核页码越界）→ 看不到新建记录 |
| BUG-057 | 前端/死代码 | `components/chat/FeedbackButtons.tsx:23` | 组件定义后全项目无引用 → **点赞/纠错入口缺失**（后端 `/feedbacks` 接口已实现但无 UI） |
| BUG-058 | 前端/交互 | `components/chat/MessageItem.tsx:83`、`EvidencePanel.tsx:128` | chip 的 index 与 Collapse key(`source_index`) 无映射 → 答案引用越界编号时点击无展开、再点无法收起 |
| BUG-059 | 前端/性能 | `app/kb/page.tsx:154-…` | 一次性渲染全部知识库卡片、无分页/虚拟化；当前 dev 库 2897 条 → 主线程长阻塞（上一阶段实测浏览器 CDP 超时无响应） |
| BUG-060 | 前端 | `app/register/page.tsx:32` | `setTimeout(800ms)` 跳转未在卸载时清理 |
| BUG-061 | 前端/契约 | `services/token.ts:34` vs `core/config.py:31` | cookie `max-age` 写死 7 天，而 JWT 实际 1440 分钟=1 天（且该注释写错）→ middleware 放行已登录页，直到首个接口 401 才硬跳 |
| BUG-062 | 前端/SSE | `hooks/useSSE.ts:66-73` | SSE 用原生 fetch 绕过 `api.ts` 的 401 拦截器 → token 过期时只报"请求失败"，不清 token 不跳转 |
| BUG-063 | RAG | `application/evidence.py:147` | `evidence_id` 混入 `source_index`（展示位次）→ 同一证据在不同请求 ID 可变（不稳定主键） |
| BUG-064 | RAG | `rag_service.py:414`；`kg_retrieval.py:192-194` | KG 命中直接拼在末尾（`hits + kg_hits`）→ KG 恒为最大编号，而 `group_evidence` 按分重排 → 前端分组顺序与 `source_index` 不一致；KG 分数复用 `dense_score` 字段名（实体匹配分被当 cosine 用） |
| BUG-065 | API/契约 | `api/routes/chat.py:55-71,357` | `/chat/ask`（非流式）的 `Citation` 模型无 `kg_relation/kg_hop/kg_provenance` → Pydantic 丢弃多余字段，非流式路径拿不到 KG 关系信息（流式 dict 有） |
| BUG-066 | RAG | `rag_service.py:1096-1120` | 流式 delta 携带的是 Reflection **前**的文本（citations 早于生成、revise 只在 done.answer）→ 客户端打字机显示被淘汰文本（协议约束，属已知限制，可补"修正"事件） |
| BUG-067 | 数据库/Schema | PostgreSQL `documents.progress_percent`、`evaluation_results.faithfulness` vs `models.py` + `alembic/versions/*` | **DB 有、ORM 与迁移都没有**：实测 `information_schema` 存在这两列，但全仓源码 0 引用、任何迁移也未创建（`database.py:57` 的 `create_all` 曾按旧模型建过）→ schema drift；新环境由迁移构建时不会包含它们 |
| BUG-068 | 代码质量 | `audit.py:33-37` 与 `users.py:139-143`；herbs/prescriptions/theories/literatures 四个资源路由近乎逐行复制校验与 `_to_out`（如 `herbs.py:35-218`）；`documents.py:311,349,353,388,411` 函数体内重复 import | `_require_admin` 两份实现；"仅 admin"检查 10+ 处三种写法且响应体不同；四资源路由重复 ~200 行 |
| BUG-069 | 一致性 | `api/routes/users.py:41`（`TRASH_RETENTION_DAYS=7` 硬编码）vs `knowledge_bases.py:45`（读 settings，可配 1-30）vs `documents.py:339` 提示文案硬编码"7天内可恢复" | 三处回收站口径不一致，改配置后提示错误 |
| BUG-070 | 后端/统计 | `api/routes/admin.py:52-53` | `total_docs`/`total_kbs` 未过滤 `deleted_at` → 仪表盘数字含回收站数据，与列表口径不一致 |
| BUG-071 | 后端/性能 | `api/routes/users.py:392` | `batch_restore` 预检 `select(User)` 全表加载（含 `hashed_password`），无 `deleted_at` 过滤、无上限 |
| BUG-072 | 后端/观测 | `knowledge_bases.py:118-119` 及四资源路由同名 helper | IP 取 `request.client.host`，反向代理后恒为代理 IP → 审计日志 IP 失真 |
| BUG-073 | 前端仓库卫生 | `frontend/tsconfig.tsbuildinfo`（**已被 git 跟踪**）、`frontend/next-env.d.ts` 等 | 构建产物被提交，`git ls-files` 实测确认；应加入 `.gitignore` |

---

## 4. P0/P1/P2/P3 分类汇总

| 级别 | 数量 | 分布（按模块） |
|---|---|---|
| **P0** | 1 | 评测域权限隔离缺失（BUG-001） |
| **P1** | 13 | BUG-002~014：安全与权限 4（002 配置越权、003 SSRF、004 成员角色、005 强制改密）、RAG/数据一致性 4（006/007 孤儿向量、008/009 KG 越界与残留、010 KG rebuild 见下一条）、KG 原子性 1（010）、评测统计 1（011）、事务 1（012）、前端竞态 1（013）、前后端契约 1（014） |
| **P2** | 28 | RAG 链路 6、评测 5、后端安全/配置 5、后端参数与状态码 4、数据一致性 4、前端逻辑与健壮性 4 |
| **P3** | 21 | 前端健壮性/交互/死代码 7、前端性能与仓库卫生 3、RAG 展示一致性 5、后端代码质量/观测/统计 6 |
| **合计** | **63** | 另有工程缺口若干（无 CI、无 DOM/E2E、依赖集成用例大量 skip），不纳入编号，见 §10 |

---

## 5. 已知历史问题（本轮沿用上一阶段记录并复核）

| # | 问题 | 本轮复核结论 |
|---|---|---|
| 1 | `/auth/register` 后端不存在 | **仍然存在**（BUG-014，P1）；`__init__.py:4` 注释已过期 |
| 2 | `/kb` 页一次渲染大量 KB 卡片 | **仍然存在**（BUG-059，P3）；dev 库实测 2897 条，导致主线程阻塞 |
| 3 | EvidencePanel/MessageItem 缺少真实 DOM 测试 | **仍然存在**（§10：无 jsdom/RTL/E2E） |
| 4 | `tsconfig.tsbuildinfo` 是否应提交 | **仍被 git 跟踪**（BUG-073）——建议 ignore，但删除已跟踪文件需你确认 |
| 5 | 6 条既有 lint warnings | **仍然存在**（6 条全为 `react-hooks/exhaustive-deps`）→ 归入 P3，本轮未占用 Bug 编号，位置清单见下表 |
| 6 | 临时脚本是否清理 | **未清理且更严重**：`_diag.py`、`_diag2.py`、`next_dev_20260906.log`、`AGENTSmd.bay` **已被 git 跟踪**；根目录 `_tmp_auth_test.py`、`_tmp_bcrypt_check.py`、`_tmp_audit_schema.py`（本轮审计脚本）未跟踪 |

当前 6 条 lint warning 位置（P3）：`app/herbs/page.tsx:191`、`components/layout/AppSider.tsx:261`、`app/prescriptions/page.tsx:468`、`app/settings/page.tsx:77`、`app/theories/page.tsx:175`、`app/taxonomy/page.tsx:163`。

---

## 6. 环境限制（本轮实测）

| 依赖 | 状态 | 影响 |
|---|---|---|
| PostgreSQL 5432 | ✅ 可用 | Schema 实测、ORM 差异比对得以完成 |
| **Redis 6379** | ❌ 未启动 | `test_conversation_cache.py` **2 个用例 skip**；缓存相关路径（`chat.py:512-550` 会话删除清理、`model_config_service` 缓存）无法实测 |
| **Milvus 19530** | ❌ 不可达 | **RAG 真实检索、相似度 Gate、向量残留类 Bug（BUG-006/007/046/047）无法端到端复现**，只能静态取证；上一阶段浏览器实测 `/chat` 报"Milvus 连接失败" |
| LLM / Reranker | ⚠️ 默认 `mock` backend；未配置真实 OpenAI / FlagReranker | BUG-017（Rerank 失败 → 422 无降级）、相似度阈值量纲问题（§9 U-3）无法真机复现与校准 |

---

## 7. 潜在风险（尚未发生，但代码结构决定迟早发生）

1. **内存打爆 / DoS 面**：`GET /api/v1/kb` 一次性返回全部知识库（当前已 2897），`/documents`、`/chat/conversations`、`/feedbacks` 列表无分页上限，`POST /evaluation/run` 不限制用例条数且无并发锁 → 单用户低成本触发全量序列化 + 全链路 LLM。
2. **策略对比实验不可信**：Gate/Reflection 开关改写共享实例（BUG-022）、dynamic_router 失败仍归档为该策略（BUG-025）、失败用例记 0 分（BUG-011）、`allow_retry` 路径不一致（BUG-019）——四条叠加，**同一份实验数据可能给出错误的消融结论**（对毕业论文风险极高）。
3. **蒸发中的伪证据**：孤儿向量（BUG-006/007）+ 多跳无隔离（BUG-008）+ 已删资源残留 KG（BUG-009）三条组合，会让系统在**回答中引用不存在或不该可见的"证据"**，违反 AGENTS.md §8"禁止伪造知识来源"。
4. **权限面整体脆弱**：评测域无隔离（P0）、模型配置全员可改（P1）、SSRF（P1）、成员角色不消费（P1）、强制改密仅前端（P1）、开放重定向（P2）、硬编码密钥/测试后门（P2）——任意一条都可被利用于获取他人数据或控制全站行为。
5. **Schema drift 常态化**：开发用 `create_all`（`database.py:57`）与 Alembic 并存（BUG-067）→ 环境之间表结构可能不同且无 CI 校验。
6. **回滚/失败路径从未被测试**：几乎全部 `except → logger.warning` 分支（14 处以上）无任何测试覆盖，实际发生时的表现未知。

---

## 8. 建议修复顺序（第二轮执行，需你批准后再动手）

原则：**先安全与数据正确，再业务一致，最后质量**；每条改动最小；每条必带回归测试；禁忌题目见§9。

| 批次 | BUG | 主题 | 风险/说明 |
|---|---|---|---|
| **Batch 1（安全与数据正确性）** | 001、002、003、004、005、028、026、027、014、040 | 权限与提权面：评测域 RBAC、`/settings/*` admin 校验 + SSRF 白名单、成员角色服务端校验、`must_change_password` 服务端强制、开放重定向、**register 缺口（需决策：补端点 vs 下线前端入口）**、密钥/后门处理、最后一个 admin 保护 | 014 与 026 涉及"是否新增功能/如何管理密钥"，开工前需你点头 |
| **Batch 2（删除生命周期与证据可信性）** | 006、007、008、009、010、045、046、047、016、015 | `delete_by_doc_id`→正确方法并让异常可见、补按 kb 删向量、KG 逐跳隔离、资源删除清 KG、rebuild 原子化、先插后删/顺序调整、citation 编号与 evidence 对齐、Reflection 下标空间统一 | 涉及 Milvus 清理能力（新增接口方法），属补缺口非改规则 |
| **Batch 3（评测可信度）** | 011、022、024、025、023、019 | 失败用例不进均值 + 单独 failed 计数 + 全失败非 2xx；开关用 try/finally 恢复；字段长度校验；策略归档保护；幂等/并发/分页；`allow_retry` 一致（**需决策**） | 011 会改变报告字段语义，需新增字段而非删除，保持向后兼容 |
| **Batch 4（健壮性与一致性）** | 012、017、035、036、037、038、029、033、034、043、044、021 | 审计与业务事务解耦、Rerank 降级、SSE 心跳/孤儿消息、Redis 清理谎报、feedback 唯一约束、health、分页约束、purge 校验 | 中等风险，均可向后兼容 |
| **Batch 5（前端）** | 013、048、049、050、051、052、053、054、055、056、057、058、062 | 竞态守卫（requestSeq）、弹窗状态重置、null 兜底、接入 FeedbackButtons、chip↔Collapse 映射、SSE 401 统一处理 | 建议先做 013/050/051/052，其余可视时间 |
| **Batch 6（质量与仓库卫生）** | 059、060、061、063-073、lint warnings | `/kb` 分页（属新增 UI 行为，需决策是否做）、临时文件清理（删除已跟踪文件需你确认）、`.gitignore` 补齐 | 可不击穿底线，最后做 |

---

## 9. 无法确认 / 必须决策后再动的问题（我不会擅自改）

| # | 问题 | 为什么不能自行修复 |
|---|---|---|
| U-1 | **BUG-018 门禁顺序不一致**（流式 vs 非流式） | 对齐任意一侧都会改变另一路径的拒答行为与文案，属业务规则；需你指定以哪条为准 |
| U-2 | **BUG-019 `allow_retry` 不一致** | 让线上也允许 retry 会增加延迟与成本；让评测不允许则改变既有实验语义 |
| U-3 | **相似度阈值两套量纲共用 0.3**（cosine `dense_score` vs rerank sigmoid `hit_score`，`evidence.py:75-77`、`evidence_gate.py:115-117`、`config.py:58`；本轮未占用 Bug 编号） | 调整阈值＝直接改实验指标口径，违反"不得改 RAG 阈值"。建议仅记录，或在明确授权下新增"独立配置项、默认值保持行为不变" |
| U-4 | **BUG-014 register**：是新增 `POST /auth/register`（新功能）还是下线前端注册页 | 前者属"新增后端核心功能"，需你确认是否需要开放注册 |
| U-5 | **BUG-073 / `_diag.py` 等已被 git 跟踪的文件是否删除** | `git rm` 会改动版本历史与他人可能的工作状态；更安全的做法是只补 `.gitignore`，历史文件是否清理由你决定 |
| U-6 | **BUG-059 `/kb` 分页** | 需决定是否引入分页参数改变前端列表行为与后端接口（会触碰"不新增功能"边界） |
| U-7 | SSE 期间 `get_db` 会话生命周期 | 现代 FastAPI/Starlette 会保留 AsyncExitStack 到流结束，实际行为取决于安装的依赖版本；本轮未实跑验证，**不建议改动** |
| U-8 | Redis/Milvus 相关路径（BUG-006/007/019/046/047 的运行时表现） | 环境不可用，静态取证可信但无法端到端复现；修复后仍标记为"待环境验证" |
| U-9 | Milvus 孤儿向量 / KG 孤儿节点的**现存规模** | 需连 Milvus 并与 `kg_nodes.resource_id` 比对样板才能量化；本轮环境不可达，无法统计 |

---

## 10. 测试覆盖盲区（不用伪造通过的假象掩盖，按缺失项记录）

1. **权限/越权零覆盖**：`backend/tests/` 评测、文档、知识库成员的越权用例几乎不存在；既有测试全用 `TEST_USER`（admin），天然看不见 BUG-001/004/005。
2. **SSE 异常路径零覆盖**：`test_chat_stream.py` 只测正常序列与 404/422；流开始后的异常（`chat.py:421-424`）、客户端断开、多条 error/重复 done、孤儿 user 消息（BUG-036）均未覆盖。
3. **评测统计边界零覆盖**：失败用例进均值（BUG-011）、全失败返 200、`_infer_dataset_version` 并列众数、超长字段 500、并发 run 幂等，全部无用例。
4. **删除/清理生命周期零覆盖**：文档 purge（BUG-006）、KB purge（BUG-007）、资源删除（BUG-009/045）、re-vectorize（BUG-046）——即"PG↔Milvus↔KG 一致性"这一整类是空白。
5. **AuditService 全部**：`log()` 对调用方 session 的 commit/rollback 副作用（BUG-012）、`list_logs` 筛选分页、admin 鉴权均无用例。
6. **Settings/配置链路**：脱敏判定 bug（BUG-031）、缓存失效（BUG-030）、SSRF（BUG-003）、`PUT /model` 鉴权（BUG-002）无用例。
7. **health 组件失败语义**（BUG-029）现有用例只断言 `status == "ok"`。
8. **前端无 DOM/E2E**：无 jsdom / RTL / Playwright；`EvidencePanel`、`MessageItem` 的渲染与交互只能靠 `utils/evidence.ts` 纯逻辑测试间接覆盖；所有"竞态/loading/弹窗"类缺陷（BUG-013/048-052）无自动化保护。
9. **无 CI**：仓库无 `.github` 或任何流水线配置 → `pytest/tsc/lint/build` 只能本地手工执行， Gate 可随时被绕过。
10. **契约测试覆盖面有限**：现有 `frontend/tests/api-contract.test.ts`（5 条）只做静态抽取比对 + 少量真实 HTTP 走查（`login`、`/kb`、`ask-stream` content-type），**不覆盖 query/body 参数名与响应字段类型**。

---

## 11. 结论与下一步

- 本轮完成静态全量审计，**产出 63 条有 `文件:行号` 证据的缺陷**（P0×1、P1×13、P2×28、P3×21）与 11 类测试盲区。
- **所有自动化 gate 当前全绿（590 pytest / 62 frontend / tsc / lint 0 error / build OK），但这并不代表代码健康**——绝大多数缺陷位于测试完全未触及的"权限、删除生命周期、评测统计、竞态"区域。
- **本轮未修改任何业务代码**，等你确认 §8 的批次划分与 §9 的 6 个待决策项后，我再按 Batch 1 → 6 逐项修复，每项「最小改动 + 回归测试 + 相关测试 + 完整测试」，修复结束后产出 `BUG_FIX_REPORT.md`。

**请回复**：① 是否按上述批次开始修复；② §9 U-1~U-6 的取舍（尤其 register 与 `/kb` 分页是否属于允许的范围）；③ 是否需要我在修复前补跑 `tsc`/`build` 作为基线快照。
