技术设计文档（TECH_DESIGN）

项目：中医药知识资源管理与智能问答系统
更新时间：2026-09-26
作用：说明系统整体技术架构与核心实现方式。
产品需求以 PRD.md 为准，AI 开发规则以 AGENTS.md 为准。

1. 总体架构

系统采用前后端分离架构：

Next.js
   ↓ HTTP / SSE
FastAPI
   ↓
PostgreSQL ─── Redis
   ↓
Milvus
   ↓
Embedding / Reranker / LLM
技术栈
前端：Next.js + React + Ant Design
后端：FastAPI + SQLAlchemy Async
关系数据库：PostgreSQL
缓存与会话：Redis
向量数据库：Milvus
部署：Docker Compose
Embedding：BGE-M3（权重随镜像内置）
Reranker：BGE-Reranker（权重随镜像内置）
大语言模型：LLM（OpenAI 兼容接口）
HyDE：默认开启，复用主 LLM；不提供 mock 实现
不使用 LangChain
2. 系统分层
2.1 传统业务层

传统系统是项目主体，负责：

用户管理
角色与权限
知识资源管理
中药管理
方剂管理
中医理论知识管理
文献管理
分类与标签
普通检索

结构化业务数据主要使用 PostgreSQL 存储。

2.2 AI 能力层

AI 作为传统知识管理系统的增强模块。

基本流程：

用户问题
 ↓
Query Processing
 ↓
Retriever
 ↓
Reranker
 ↓
Evidence Gate
 ↓
LLM
 ↓
Citation

AI 问答优先基于系统已有知识资源生成回答，不将 LLM 作为主要知识来源。

3. 数据存储
PostgreSQL

保存结构化业务数据：

用户
角色
权限
知识库
知识资源
中药
方剂
理论知识
文献
分类
标签
文档
Chunk 元数据
问答记录
操作记录
Redis

主要用于：

会话历史
缓存
临时状态
Milvus

用于 AI 语义检索。

主要保存：

Chunk 向量
Chunk 标识
文档标识
知识库标识
必要来源信息

PostgreSQL 是业务数据的主要来源，Milvus 是 AI 检索索引。

### 3.4 可配置参数的运行时取值

六个系统级参数（回收站保留期、文件大小限制、多轮历史轮数、
检索 Top-K、精排 Top-N、相似度拒答阈值）统一由
`src/core/runtime_config.py` 提供取值入口：

    读预置默认值（core/config.py Settings）
        ↑ 回落
    运行时配置（DB system_configs 单行表，id=1）
        ↓
    进程内快照（TTL 30s 异步刷新 + 写入后即时失效）
        ↓
    业务代码同步取值 get_system_value(name)

设计要点：
- 消费点中存在同步上下文（`resolve_retrieval_config`、
  `displayable_hits`、`_is_relevant`）与拿不到 DB session 的
  `redis_client`，因此采用"异步刷新快照 + 同步 O(1) 取值"，不在业务代码里查库。
- 后台保存配置（`PUT /settings/system`）立即调用 `invalidate_system_config()`；
  另有 HTTP 中间件按 TTL 刷新，覆盖多副本 / 外部直接改库的场景。
- DB 不可用时沿用上一份快照（首次则回落 .env 默认值），不阻断服务。

4. 文档处理流程
上传文档（大小受运行时配置 max_file_size_mb 限制，默认 50MB）
 ↓
文件解析
 ↓
结构感知切分
 ↓
保存 Chunk
 ↓
BGE-M3 向量化（本地模型路径 EMBEDDING_MODEL_PATH）
 ↓
Dense + Sparse
 ↓
写入 Milvus

文档处理需要保证：

文档与 Chunk 可以关联
Chunk 可以追溯到原始文档
来源信息不会在处理过程中丢失

具体支持的文件类型以当前代码实现为准。

5. 普通检索

传统检索不依赖 AI。

关键词
 ↓
PostgreSQL
 ↓
分类 / 标签 / 类型过滤
 ↓
结果列表

普通检索主要用于：

知识资源查询
文献查询
分类查询
标签查询
6. RAG Baseline

当前 RAG Baseline：

用户问题
 ↓
HyDE
 ↓
BGE-M3
 ↓
Dense + Sparse
 ↓
RRF
 ↓
Reranker
 ↓
相似度 Gate
 ↓
LLM
 ↓
Citation

现有 RAG 能力包括：

HyDE 查询增强
Dense Retrieval
Sparse Retrieval
RRF 融合
Reranker 重排序
证据不足限制生成
来源引用
多轮会话
SSE 流式回答

Baseline 应保持稳定，后续研究功能在其基础上扩展。

关键参数默认值（后台可覆盖）：Top-K=10、Top-N=3、相似度拒答阈值=0.35、
多轮历史=5 轮；HyDE 默认开启并复用主 LLM。

7. AI 问答接口

主要接口：

POST /ask
POST /ask-stream

流式接口使用 SSE。

主要事件：

citation
delta
done

多轮对话保留最近若干轮历史（默认 5 轮，可在后台「系统配置」调整后立即生效）。

8. 未来研究架构

Baseline 稳定后，可以逐步增加动态检索和证据增强。

Query
 ↓
Query Analyzer
 ↓
Dynamic Router
 ↓
┌─────────────────┐
│ Hybrid RAG      │
│ KG Retrieval    │
│ Other Retrieval │
└─────────────────┘
 ↓
Fusion
 ↓
Reranker
 ↓
Evidence Gate
 ↓
LLM
 ↓
Citation

其中：

Query Analyzer：分析问题类型
Dynamic Router：选择检索策略
Retriever：执行具体检索
Evidence Gate：判断证据是否足够
LLM：根据证据生成回答
Citation：提供来源信息

研究功能应支持关闭或固定策略，方便进行对比实验。

9. 知识图谱

如果后续需要加入知识图谱，知识图谱作为检索来源之一，不作为整个系统的核心架构。

原则：

数据来源可追溯
优先使用公开或经过审核的数据
控制图谱规模
不伪造大量知识三元组
可以与文本 RAG 并行使用
10. 安全与领域约束

系统定位为中医知识学习与查询辅助工具。

不实现：

根据症状进行疾病诊断
自动生成处方
针对具体患者制定治疗方案

知识来源需要尽可能保留。

来源等级属于内部管理信息，不代表绝对正确。

11. 实验设计

研究实验采用逐步增加能力的方式：

Baseline
 ↓
Dense Retrieval
 ↓
Hybrid Retrieval
 ↓
+ Reranker
 ↓
+ HyDE
 ↓
+ Research Method

实验需要使用真实测试数据，并记录：

测试集
实验配置
实验结果
对比结果

不得在没有实验数据的情况下预先声称某方法一定有效。

12. 开发原则
优先复用现有代码，不推倒重做。
传统业务优先保证稳定和完整。
AI 功能作为传统系统的增强模块。
不为了技术先进而随意增加框架、服务和依赖。
数据库结构修改必须考虑已有数据。
配置类数据统一以 DB 为准、.env 为回落；运行时不得绕过统一取值入口。
禁止在生产链路保留 mock / 假数据实现（测试替身仅限测试使用）。
API 修改需要考虑前端兼容性。
RAG 核心链路修改后必须进行完整问答测试。
新研究功能应尽量模块化，并支持实验开关。