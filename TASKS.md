# TASKS.md

> 本文件是当前开发任务清单。
> 开发前先阅读：PRD.md、BUSINESS_RULES.md、TECH_DESIGN.md、AGENTS.md、SYSTEM_MAP.md。
> 本文件以实际代码现状为准，不重复开发已经完成的功能。

---

# 1. 当前项目状态

项目名称：

中医药知识资源管理与智能问答系统

当前系统已经完成：

- 用户登录、JWT 认证
- 用户角色与权限
- 用户管理
- 审计日志
- 管理员仪表盘
- 知识库 CRUD
- 文档上传、解析、切分
- 文档向量化
- Milvus 检索
- BGE-M3 Dense + Sparse 检索
- RRF
- Reranker
- HyDE
- LLM 流式回答
- Citation
- 多轮对话
- Redis + PostgreSQL 历史记录
- 拒答
- 知识库权限过滤
- SSE
- 点赞/点踩
- 基础 Evaluation
- Docker 部署

现有 RAG Baseline 已经完成。

当前最大缺口：

- 中药管理
- 方剂管理
- 中医理论管理
- 经典及现代文献管理
- 分类管理
- 标签管理
- 普通关键词搜索

研究增强功能暂未实现：

- Query Analyzer
- Dynamic Router
- Knowledge Graph / KG Retrieval
- Evidence Gate
- Self Reflection
- 多来源证据分组展示
- 检索策略日志

---

# 2. 总开发原则

## 2.1 增量开发

不要重写现有系统。

优先复用：

- 现有认证
- 现有权限
- 现有用户系统
- 现有知识库
- 现有文档系统
- 现有 RAG
- 现有 API 结构
- 现有前端布局

修改前必须先阅读相关代码。

---

## 2.2 不重复开发 RAG Baseline

以下功能已经存在，不允许重新实现：

- BGE-M3 Dense Retrieval
- BGE-M3 Sparse Retrieval
- Milvus
- RRF
- Reranker
- HyDE
- LLM
- SSE
- Citation
- 多轮对话
- Redis 历史缓存
- PostgreSQL 历史回退
- KB 权限过滤
- 基础拒答

如果新功能需要修改 RAG，应在现有代码基础上增量修改。

禁止为了实现新功能而整体重写 `_retrieve()`。

---

## 2.3 中医药资源管理是当前主线

当前优先完成传统业务系统。

开发顺序：

1. Alembic 数据库迁移
2. 分类
3. 标签
4. 中药
5. 方剂
6. 中医理论
7. 经典及现代文献
8. 普通关键词搜索
9. 与现有知识库/RAG 衔接
10. 中医领域评测
11. RAG 研究增强

---

# 3. 阶段一：数据库迁移体系

## 目标

将数据库结构管理从 `create_all` 逐步迁移到 Alembic。

## 要求

- 添加 Alembic
- 配置开发环境迁移
- 保留现有数据库结构
- 不删除现有数据
- 不破坏现有 13 张表
- 为后续中医资源表提供迁移能力

## 验收

- 可以执行 migration
- 新增表可以通过 migration 创建
- 现有用户、知识库、文档数据不丢失
- 项目启动不依赖删除数据库重建

## 禁止

- 删除现有数据库
- 重建整个数据库
- 修改现有 RAG 数据结构而没有必要

---

# 4. 阶段二：分类与标签

状态：已完成（TASK-002，2026-09-19 完成，交付明细见 §24）

## 目标

建立中医药知识资源的基础分类和标签体系。

## 功能

### 分类

- 分类新增
- 分类修改
- 分类删除
- 分类列表
- 分类层级

### 标签

- 标签新增
- 标签修改
- 标签删除
- 标签列表

## 要求

- 后端 Model
- CRUD API
- 前端管理页面
- 权限控制
- 数据库迁移
- 基础测试

## 验收

管理员可以创建、修改、删除分类和标签。

---

# 5. 阶段三：中药管理

## 目标

建立中药知识资源管理模块。

## 功能

- 中药列表
- 中药新增
- 中药详情
- 中药修改
- 中药删除
- 分类
- 标签
- 关键词搜索
- 分页

## 建议字段

根据实际业务设计，不要为了字段数量而增加无用字段。

至少考虑：

- 名称
- 别名
- 分类
- 性味
- 归经
- 功效
- 来源
- 描述
- 标签
- 创建时间
- 更新时间

具体字段以项目实际需求为准。

## 验收

管理员可以完整维护中药资源，普通用户可以浏览和搜索公开资源。

---

# 6. 阶段四：方剂管理

## 目标

建立方剂知识资源管理模块。

## 功能

- 方剂列表
- 方剂新增
- 方剂详情
- 方剂修改
- 方剂删除
- 分类
- 标签
- 关键词搜索
- 分页

## 建议内容

至少考虑：

- 方剂名称
- 别名
- 组成
- 功效
- 主治
- 用法
- 来源
- 说明
- 标签

## 验收

管理员可以维护方剂资源，普通用户可以浏览和搜索公开资源。

---

# 7. 阶段五：中医理论管理

## 目标

建立中医理论知识资源模块。

## 功能

- 理论知识列表
- 新增
- 修改
- 删除
- 详情
- 分类
- 标签
- 关键词搜索
- 分页

## 内容

用于保存中医基础理论、概念、理论知识等内容。

## 验收

管理员可以维护理论知识，普通用户可以浏览和搜索。

---

# 8. 阶段六：经典及现代文献管理

## 目标

建立文献资源管理模块。

## 功能

- 文献列表
- 文献新增
- 文献详情
- 文献修改
- 文献删除
- 分类
- 标签
- 关键词搜索
- 分页

## 建议字段

至少考虑：

- 标题
- 作者
- 来源
- 年代
- 类型
- 摘要
- 内容
- 标签

具体字段根据实际资料调整。

---

# 9. 阶段七：普通关键词搜索

## 目标

建立传统知识资源查询功能。

## 要求

普通搜索与 RAG 问答分开。

搜索对象：

- 中药
- 方剂
- 中医理论
- 文献

支持：

- 关键词
- 分类
- 标签
- 分页
- 排序

优先使用 PostgreSQL。

不要为了普通搜索引入新的搜索服务。

---

# 10. 阶段八：传统资源与知识库/RAG 衔接

## 目标

让传统知识资源能够成为智能问答的知识来源。

需要明确区分：

传统搜索：

用户
→ PostgreSQL
→ 关键词搜索
→ 资源结果

智能问答：

用户问题
→ RAG
→ 知识库
→ 检索
→ LLM
→ 回答 + Citation

不要把普通搜索和 RAG 混成一个接口。

---

# 11. 阶段九：中医领域评测

## 目标

把当前 HR 测试集替换/扩展为中医领域测试集。

## 要求

测试集至少 30 题。

建议覆盖：

- 基础事实问题
- 中药问题
- 方剂问题
- 理论问题
- 关系问题
- 多轮问题
- 无相关知识问题

增加：

- question_type
- 标准答案
- 相关知识来源

已有 Evaluation API 和前端评估面板优先复用。

---

# 12. 阶段十：多来源证据展示

## 目标

让回答能够清楚展示多个来源。

例如：

回答
├── 来源 1
├── 来源 2
└── 来源 3

要求：

- 引用真实存在
- 来源可追溯
- 显示来源名称
- 保留来源类型/可信度信息
- 不允许虚构引用

先完成展示，不急于实现复杂的自动冲突判断。

---

# 13. 阶段十一：Query Analyzer

## 目标

分析用户问题类型。

示例：

```text
用户问题
    ↓
Query Analyzer
    ↓
事实型 / 关系型 / 综合型 / 多轮型 / 其他

输出应该是结构化结果。

不要让 LLM 直接决定后端执行什么操作。

14. 阶段十二：Dynamic Router
目标

根据 Query Analyzer 结果选择检索策略。

例如：

问题
 ↓
Query Analyzer
 ↓
Dynamic Router
 ↓
关键词检索
向量检索
混合检索
KG检索

要求：

策略可扩展
策略可记录
默认策略保持现有 RAG Baseline
不破坏现有检索流程
15. 阶段十三：Knowledge Graph / KG Retrieval
目标

建立小规模、可追溯的中医知识图谱，并作为一种检索来源。

优先实体：

中药
方剂
功效
性味
归经
理论概念

优先关系：

中药 → 功效
中药 → 归经
方剂 → 组成
方剂 → 功效
理论 → 相关概念

不要一开始建设大型知识图谱系统。

16. 阶段十四：Evidence Gate
目标

判断当前检索证据是否足以支持回答。

基本流程：

检索
 ↓
Evidence Gate
 ↓
证据充分 → 生成
证据不足 → 改写/重新检索
 ↓
再次判断
 ↓
仍不足 → 拒答

现有：

dense score < 0.3 → 拒答

属于已有的相关性门槛。

不要把它直接称为完整 Evidence Gate。

17. 阶段十五：Self Reflection
目标

检查生成回答是否与检索证据一致。

检查：

回答是否使用检索证据
是否出现证据中没有的内容
Citation 是否对应
是否需要重新检索

如果发现问题：

回答
 ↓
Self Reflection
 ↓
通过 → 返回
不通过 → 重新检索/限制回答
18. 阶段十六：前端测试

最后补充：

登录测试
用户管理测试
资源 CRUD 测试
搜索测试
RAG 页面测试
评测页面测试
关键流程 E2E

不要在早期为了测试框架本身阻塞业务开发。

19. 当前“还债任务”

这些功能后端已经存在，不要重新设计：

修复或移除 /register
KB 成员管理 UI
KB 回收站 UI
文档回收站 UI
feedback 列表管理 UI
backfill-source 前端入口
Milvus 存量 source 字段回填

这些属于已有能力的前端补接。

20. 开发优先级

当前优先级：

P0
Alembic
↓
分类 / 标签
↓
中药
↓
方剂
↓
中医理论
↓
文献
↓
普通关键词搜索

P1
传统资源与知识库/RAG衔接
↓
中医评测集
↓
多来源证据展示

P2
Query Analyzer
↓
Dynamic Router
↓
KG Retrieval
↓
Evidence Gate
↓
Self Reflection

P3
前端测试 / E2E
数据库与部署进一步完善
21. 每次开发任务规则

每次只完成一个明确任务。

AI 开始修改前必须：

阅读相关代码
找到现有 Model / Router / Service / Page
说明准备修改哪些文件
说明是否影响现有功能
再开始编码

完成后必须：

说明修改了什么
说明新增了哪些文件
说明 API
说明数据库变化
运行相关测试
报告测试结果
不自动修改无关模块
22. 严禁事项

禁止：

重写整个项目
重写现有 RAG
删除已有功能
删除数据库重建表
随意更换技术栈
引入 LangChain 代替现有 RAG
为简单功能引入微服务
未确认代码结构就直接修改
修改 API 后不检查前端调用
修改数据库模型后不建立 migration
编造不存在的中医知识
编造 Citation
绕过知识库权限
把 AI 回答当成诊断、处方或医疗决策
23. 当前第一任务
TASK-001：建立 Alembic 数据库迁移体系

状态：已完成（2026-09-19），交付明细与验收证据见第 24 节。

目标

在不破坏现有数据库和业务功能的前提下，引入 Alembic，为后续中医资源表开发提供正式迁移能力。

必须完成
检查当前 SQLAlchemy 配置
检查现有 13 张表
配置 Alembic
正确加载 SQLAlchemy metadata
生成与当前数据库结构对应的初始 migration
验证 migration
不删除现有数据
不修改现有 RAG 逻辑
验收标准
Alembic 可以正常运行
数据库结构没有被破坏
现有登录、知识库、文档、RAG 功能不受影响
后续新增中医资源表可以通过 migration 管理
禁止
DROP DATABASE
删除现有表
删除现有文档
重写 RAG
修改前端无关代码
24. 当前开发状态

当前正在进行：

TASK-003：中药管理（尚未开始，等待确认）

已完成：

TASK-002：分类与标签（2026-09-19 完成）

交付内容：
- Model：backend/src/domain/models.py 新增 Category、Tag
  - Category：id/resource_type(herb|prescription|theory|literature)/name/parent_id(自引用,
    ondelete=RESTRICT, nullable)/sort_order/description + 时间戳；层级结构，不引入闭包表
  - 唯一约束：表达式唯一索引 uq_categories_type_parent_name
    (resource_type, coalesce(parent_id, 零 UUID), name)，根节点同名同样被阻止
  - Tag：id/name(全局唯一)/color/description + 时间戳，扁平结构，无 resource_type、无层级
  - 未创建任何资源关联表（herb_tags 等推迟到各资源模块）
- Migration：7bc61cd31f48_add_categories_and_tags（down_revision=70c66bb630c4）
  - 对独立临时空库（先升到 baseline）autogenerate，仅 create_table categories/tags，
    不修改已有 13 张表；临时库 upgrade/downgrade 往返通过后清理
  - 现有库 upgrade head 成功，数据零变动：users=112 / knowledge_bases=1022 /
    documents=734 / chunks=1381；upgrade 后 alembic_version 记账异常停留旧值，
    已用 alembic stamp 7bc61cd31f48 修正（纯 UPDATE，无 DDL），current=head
- API：backend/src/api/routes/taxonomy.py（8 端点，挂在 protected_router）
  - categories：GET（resource_type 过滤 + tree=true 树形）、POST、PUT（仅
    name/sort_order/description，不允许改 resource_type/parent_id）、DELETE
  - tags：GET（keyword 模糊 + limit/offset）、POST、PUT、DELETE
  - 查询需登录；写操作要求 admin；全部写操作接入 AuditService
  - 删除保护：有子分类 409；通过 metadata 反射检查 category_id/tag_id 引用，
    已被资源引用 409（自动覆盖未来资源表）
- 前端：frontend/src/app/taxonomy/page.tsx（Tabs：分类管理 + 标签管理）
  - 分类：资源类型 Segmented 切换、树形展示、新建根/子分类、编辑、删除、TreeSelect 父分类、排序
  - 标签：关键词搜索、表格、新建/编辑/删除、色块/color/description
  - API 封装 frontend/src/services/api.ts（8 个）；类型 frontend/src/types/index.ts
  - AppSider 管理中心新增"分类与标签"（/taxonomy）
- 测试：backend/tests/test_taxonomy.py 15 项全部通过（CRUD/层级树/resource_type/
  跨树/缺失父/同级与根重名/删除保护/Tag CRUD 与唯一性/404/非 admin 403）
- TASK-001 的 test_alembic_smoke.py 同步更新（EXPECTED_TABLES 增加 categories/tags，
  baseline 断言改为遍历 revision 找唯一根迁移），3 项通过
- 全量后端测试：147 passed / 2 skipped（旧 144 passed + 冒烟 3）
- 前端 npm run build 成功，/taxonomy 路由已生成（15.5 kB）

TASK-001：Alembic 数据库迁移体系（2026-09-19 完成）

交付内容：
- backend/alembic.ini + backend/alembic/（env.py 为 asyncpg 异步模式，
  URL 读取 src.core.config.settings.DATABASE_URL，target_metadata=Base.metadata）
- baseline migration：70c66bb630c4（对独立临时空库 autogenerate 生成，纯 create_table，13 张表，
  已人工核对 server_default / timezone / ARRAY / JSONB / nullable / PK / FK / index / unique）
- 现有库仅执行 alembic stamp head（未执行任何 DDL），版本 70c66bb630c4
- stamp 前后数据一致：users=102 / knowledge_bases=983 / documents=705 / chunks=1326
- 临时库 upgrade head → downgrade base → 再 upgrade 往返验证通过，临时库已清理
- 冒烟测试 backend/tests/test_alembic_smoke.py（3 项，含一次性临时库往返）
- 全量测试 130 passed / 2 skipped

已知遗留（不影响使用，记录备查）：
- alembic check 在现有库报告历史漂移：documents.progress_percent、evaluation_results.faithfulness
  为库中存在但 Model 已删除的列；users.deleted_at 库为 timestamptz 而 Model 为无时区 DateTime；
  users.name/department/must_change_password/is_active 存在库端 server_default/nullable 差异。
  按 TASK-001 约定未修改 Model，留待后续任务决定是否出 drift 迁移。

然后按照本文件顺序继续。

每完成一个任务，再更新本文件中的任务状态。