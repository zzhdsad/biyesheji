"""临时探针（用完即删）：同一批文本上对 Embedding 外层批次做 A/B/A 对照。

不连 DB/Milvus，只测 BGE-M3 encode 本身，消除服务层与数据库噪声。
"""

from __future__ import annotations

import random
import time

from src.infrastructure.embedding import get_embedding

N = 160
MIX = ((0.60, 40), (0.30, 80), (0.10, 150))


def make_texts() -> list[str]:
    texts: list[str] = []
    for i in range(N):
        pos = i / N
        acc = 0.0
        target = MIX[-1][1]
        for ratio, length in MIX:
            acc += ratio
            if pos < acc:
                target = length
                break
        base = f"名称：测试药材{i}；性味：甘，微温；功效：补气健脾；归经：脾、肺经；"
        texts.append((base * 40)[:target])
    random.Random(7).shuffle(texts)
    return texts


def run(emb, texts: list[str], chunk: int) -> float:
    started = time.time()
    for start in range(0, len(texts), chunk):
        emb.encode(texts[start : start + chunk])
    return time.time() - started


def main() -> None:
    emb = get_embedding()
    texts = make_texts()
    emb.encode(["名称：预热；药性：甘平。"])
    for chunk in (64, 256, 64):
        elapsed = run(emb, texts, chunk)
        print(f"chunk={chunk} elapsed={elapsed:.1f}s per_row={elapsed / N:.3f}s")


if __name__ == "__main__":
    main()
