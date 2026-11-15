"""问题类型受控词表（Query Analyzer / Dynamic Router 共用）。

词表只服务于当前查询分析与路由，不依赖已下线的 evaluation 模块。
不要为问题类型另建一套字符串（AGENTS.md：不重复定义 question_type）。

- herb         中药知识（性味归经、功效主治、用法用量、配伍禁忌等）
- prescription 方剂知识（组成、功用、主治、用法、加减等）
- theory       中医理论（阴阳五行、藏象、气血津液、病因病机、治则等）
- literature   文献/出处（经典著作、成书年代、作者、原文出处等）
- multi_source 多来源知识（答案需综合中药/方剂/理论/文献中多类证据）
- unanswerable 无依据 / 知识库中不存在（期望系统拒答）
- general      其他基础事实（非上述专属类型）
"""

# 全部合法 question_type（顺序即展示顺序）
QUESTION_TYPES: tuple[str, ...] = (
    "herb",
    "prescription",
    "theory",
    "literature",
    "multi_source",
    "unanswerable",
    "general",
)

QUESTION_TYPE_LABELS: dict[str, str] = {
    "herb": "中药知识",
    "prescription": "方剂知识",
    "theory": "中医理论",
    "literature": "文献/出处",
    "multi_source": "多来源知识",
    "unanswerable": "无依据/不存在",
    "general": "其他基础事实",
}

# 兜底类型：Analyzer 异常 / 校验失败 / 无法归类时统一回落到 general
DEFAULT_QUESTION_TYPE = "general"
