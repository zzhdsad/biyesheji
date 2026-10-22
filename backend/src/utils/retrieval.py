"""检索相关纯函数：RRF（Reciprocal Rank Fusion）多路召回融合。

TECH_DESIGN：多路召回（稠密 + 稀疏）→ RRF 融合 → Rerank 精排。

RRF 评分：score(d) = Σ_list 1 / (k + rank_list(d))，rank 从 1 开始。
优点：无需分数归一化（稠密余弦与稀疏内点积量纲不同），对召回排名稳健。
"""

from __future__ import annotations


def rrf_fusion(
    rank_lists: list[list[dict]],
    k: int = 60,
    top_n: int | None = None,
) -> list[dict]:
    """对多路召回结果做 RRF 融合。

    Args:
        rank_lists: 各路召回的有序列表（已按相关性降序）。每个 hit 必须含 ``id``。
        k: RRF 平滑常数（默认 60，经验值）。
        top_n: 返回前 N 条；None 表示返回全部。

    Returns:
        融合后按 RRF 分数降序的 hit 列表。每条新增字段：
        - ``score``：RRF 融合分数（替换原路得分）
        - ``rrf_sources``：命中的召回路数（1 表示仅单路召回）
    """
    fused: dict[str, dict] = {}
    for rank_list in rank_lists:
        for rank, hit in enumerate(rank_list, start=1):
            hid = hit["id"]
            if hid not in fused:
                # 首次出现：以该 hit 的字段为基础（含 content/page_num/title_path 等）
                fused[hid] = {**hit, "score": 0.0, "rrf_sources": 0}
            fused[hid]["score"] += 1.0 / (k + rank)
            fused[hid]["rrf_sources"] += 1

    results = sorted(fused.values(), key=lambda h: h["score"], reverse=True)
    return results[:top_n] if top_n is not None else results
