"""问题类型受控词表（阶段九建立，阶段十一复用）。

阶段九（评测体系）已定义 question_type 词表并写入 test_cases /
evaluation_runs / evaluation_results。阶段十一的 Query Analyzer 必须复用同一
词表，而不是另建一套字符串（AGENTS.md §22：不重复定义 question_type）。

历史位置：src/application/evaluation_service.py（词表原在那里定义）。
为避免 evaluation_service → rag_service → query_analyzer → evaluation_service
的循环导入，将词表下沉到本模块，evaluation_service 从此处导入并对外重导出，
旧 import 路径 `from src.application.evaluation_service import QUESTION_TYPES`
保持可用。

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
