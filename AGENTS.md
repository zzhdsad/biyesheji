AGENTS.md

项目：中医药知识资源管理与智能问答系统
更新时间：2026-09-18
作用：约束 Cursor / Codex 等 AI Agent 的开发行为。

1. 文档优先级

开发前阅读：

PRD.md：系统做什么
TECH_DESIGN.md：系统怎么实现
AGENTS.md：AI 如何修改代码

三者职责：

PRD → 产品需求
TECH_DESIGN → 技术设计
AGENTS → 开发规则

如果文档与实际代码不一致，先检查代码现状，不要自行假设。

2. 核心原则
先传统系统，后 AI

系统主体必须首先是完整的中医药知识资源管理系统：

用户 / 权限
知识资源
中药
方剂
理论知识
文献
分类
标签
普通检索

AI 是增强模块，不得把项目重新改造成纯 AI Chat 系统。

3. 修改代码前

执行：

阅读相关文档
↓
检查实际代码
↓
确定影响范围
↓
最小修改
↓
测试

禁止：

没有检查代码就重构
大面积重写已有模块
为新功能删除已有功能
随意增加框架、服务或依赖

优先复用现有实现。

4. 保护现有 RAG

除非用户明确要求，不得删除或破坏现有 RAG Baseline：

HyDE
BGE-M3 Dense Retrieval
Sparse Retrieval
RRF
Reranker
相似度 Gate
Citation
SSE 问答

新研究功能应在 Baseline 上扩展，而不是直接替换。

5. 传统业务开发

传统 CRUD 优先使用：

Next.js
↓
FastAPI
↓
SQLAlchemy
↓
PostgreSQL

普通关键词检索优先使用 PostgreSQL。

不要为了普通检索引入 LLM、Milvus 或复杂 Agent。

6. AI 功能开发

AI 问答优先使用系统知识库中的证据。

基本流程：

Query
↓
Retrieve
↓
Rerank
↓
Evidence
↓
LLM
↓
Citation

证据不足时应限制生成，不允许无依据编造。

7. 研究功能

未来可能增加：

Query Analyzer
Dynamic Router
KG Retrieval
Evidence Gate
Self Reflection
多来源证据展示

要求：

尽量模块化
可以独立开关
不破坏 Baseline
可以固定策略进行对比实验
不为了“创新”堆叠技术

没有实验数据时，不得声称某方法效果更好。

8. 数据与来源

知识资源应尽可能保留来源信息。

保持：

Source
↓
Document
↓
Chunk

之间的可追溯关系。

禁止：

伪造知识来源
伪造实验数据
伪造评测结果

修改数据库结构时必须考虑已有数据和迁移。

9. API 与前端兼容

修改后端 API 时检查：

请求参数
返回结构
错误处理
前端调用

SSE 事件保持：

citation
delta
done

除非明确要求，不随意修改已有 API。

10. 医疗安全边界

系统定位为中医知识学习与查询辅助工具。

禁止实现：

疾病诊断
自动处方
针对具体患者制定治疗方案

保持知识管理和知识查询定位。

11. 测试要求

每次修改后：

代码检查
↓
相关测试
↓
必要时完整测试

后端修改优先运行 pytest。

RAG 修改后进行实际问答链路测试。

前端修改后检查 lint / build。

12. Git 安全

禁止未经明确要求执行：

git reset --hard
git clean -fd

不得覆盖或删除用户已有修改。

较大功能完成后建议提交 Git。

13. 修改规模

优先采用：

小改动
↓
验证
↓
继续

不要一次修改整个项目。

涉及多个模块时，先明确修改范围，再逐步实施。

14. 最终原则
先理解
↓
再修改
↓
再测试
↓
再记录

系统首先必须是一个完整、稳定的中医药知识资源管理系统，然后再逐步增加 AI 能力和研究功能。

