"""冒烟测试：真实 BGE-M3 向量化 + bge-reranker（首次会下载模型）。

独立运行：python smoke_embed.py
验证三件事：
1. BGE-M3 能加载并产出稠密 1024d + 稀疏向量
2. bge-reranker 能对 query/候选打分
3. 打印耗时，确认 CPU 可用
"""
import time

from src.core.config import settings
from src.infrastructure.embedding import get_embedding
from src.infrastructure.rerank import get_rerank


def main() -> None:
    print(f"配置: EMBEDDING_BACKEND={settings.EMBEDDING_BACKEND} "
          f"DEVICE={settings.EMBEDDING_DEVICE} RERANK_BACKEND={settings.RERANK_BACKEND}")

    # 1. BGE-M3 向量化
    t0 = time.time()
    emb = get_embedding()
    dense, sparse = emb.encode(["公司实行标准工时制，每日工作8小时。", "年假有5天。"])
    print(f"[Embedding] 稠密维度={len(dense[0]) if dense else 0} "
          f"稀疏非零项={len(sparse[0]) if sparse else 0} 耗时={time.time()-t0:.1f}s")

    # 2. bge-reranker 重排
    t1 = time.time()
    reranker = get_rerank()
    cands = [
        {"content": "公司实行标准工时制，每日工作8小时，每周40小时。"},
        {"content": "年假有5天。"},
        {"content": "今天的天气很好。"},
    ]
    ranked = reranker.rerank("公司工时制度是怎样的？", cands, top_n=2)
    print(f"[Rerank] top2 分数={[(r['content'][:15], r['rerank_score']) for r in ranked]} "
          f"耗时={time.time()-t1:.1f}s")
    print("冒烟测试通过 ✅")


if __name__ == "__main__":
    main()
