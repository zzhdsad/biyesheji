# SYSTEM_MAP.md

> 项目：中医药知识资源管理与智能问答系统
> 用途：项目全局地图，仅供本人快速理解项目。

---

# 1. 一句话理解

传统系统负责：

> 中医知识资源的管理、分类、搜索和展示。

AI系统负责：

> 基于知识库检索证据，并利用LLM生成带引用的答案。

研究部分负责：

> 根据不同问题动态选择合适的检索策略。

---

# 2. 系统总图

                    用户
                     │
          ┌──────────┴──────────┐
          ↓                     ↓
      传统业务系统              AI问答
          │                     │
   用户/权限/资源               用户问题
   中药/方剂/理论               ↓
   文献/分类/标签           Query Analyzer
   普通关键词搜索               ↓
          │                Dynamic Router
      PostgreSQL                 ↓
                              检索策略
                       ┌────────┼────────┐
                       ↓        ↓        ↓
                    Text RAG    KG    KG + RAG
                       └────────┼────────┘
                                ↓
                            Reranker
                                ↓
                         Evidence Gate
                                ↓
                               LLM
                                ↓
                       Answer + Citation
                                ↓
                              用户


# 3. 技术栈

前端：
Next.js + React + Ant Design

后端：
FastAPI + SQLAlchemy Async

数据：
PostgreSQL + Redis + Milvus

AI：
BGE-M3 + BGE-Reranker + LLM

部署：
Docker Compose


# 4. 三个核心数据组件

PostgreSQL
→ 业务数据、用户、资源、文档、Chunk等

Redis
→ 缓存、对话历史、临时状态

Milvus
→ 向量检索


# 5. 文档入库

文件
 ↓
解析
 ↓
Chunk
 ↓
Embedding
 ↓
Milvus

同时保存：

Source → Document → Chunk

保证答案可以追溯来源。


# 6. 当前RAG Baseline

问题
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
Evidence Gate
 ↓
LLM
 ↓
Citation


# 7. 研究方向

核心问题：

> 不同类型的中医知识问题是否需要不同的检索策略？

研究思路：

Query Analyzer
 ↓
Dynamic Router
 ↓
选择：
Text RAG / KG / KG + RAG
 ↓
Evidence Enhancement
 ↓
LLM
 ↓
Self Reflection


# 8. 记住模块的作用

Dense
→ 语义检索

Sparse
→ 关键词检索

RRF
→ 融合检索排名

Reranker
→ 精排

HyDE
→ 改善查询表达

Evidence Gate
→ 判断证据是否足够

KG
→ 提供实体关系信息

LLM
→ 根据证据生成答案

Citation
→ 追溯来源

Dynamic Router
→ 根据问题选择检索策略


# 9. 开发顺序

传统系统
 ↓
普通搜索
 ↓
稳定现有RAG
 ↓
测试集
 ↓
Query Analyzer
 ↓
Dynamic Router
 ↓
KG（按实验需要）
 ↓
Evidence Enhancement
 ↓
Self Reflection
 ↓
消融实验


# 10. 最重要的一句话

不要把项目理解成：

> “一个AI聊天机器人”。

应该理解成：

> “一个中医药知识资源管理系统 + 基于知识库的智能问答 + 动态检索策略研究。”