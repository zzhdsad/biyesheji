"""上传测试集并运行真实评估，打印可视化报告。

用法：python eval_run.py
"""
import json
import time

import httpx

KB_ID = "659c299d-5bb9-4600-9f21-4d372a999171"
BASE = "http://localhost:8000/api/v1"


def main() -> None:
    with open("sample_testset.json", encoding="utf-8") as f:
        cases = json.load(f)

    # 1. 上传测试集
    print(f"上传测试集：{len(cases)} 条 ...", flush=True)
    r = httpx.post(f"{BASE}/evaluation/upload",
                   json={"kb_id": KB_ID, "cases": cases}, timeout=30)
    r.raise_for_status()
    up = r.json()
    print(f"  已上传 {up.get('uploaded')} 条，case_ids={up.get('case_ids')}", flush=True)

    # 2. 运行评估（批量 RAG 问答 + RAGAS 指标，可能较慢）
    print("运行评估（真实 BGE-M3 检索 + bge-reranker + DeepSeek 生成）...", flush=True)
    t0 = time.time()
    r = httpx.post(f"{BASE}/evaluation/run",
                   json={"kb_id": KB_ID}, timeout=600)
    r.raise_for_status()
    rep = r.json()
    print(f"  评估耗时 {time.time()-t0:.0f}s\n", flush=True)

    # 3. 打印报告
    cr = rep["context_relevancy"]
    ac = rep["answer_correctness"]
    passed = "通过 ✅" if rep["passed"] else "未通过 ❌"
    print("=" * 64, flush=True)
    print(f"  评估报告  |  质量门禁阈值 {rep['threshold']:.0%}  |  {passed}", flush=True)
    print("=" * 64, flush=True)
    print(f"  用例数: {rep['case_count']}", flush=True)
    print(f"  Context Relevancy (上下文相关度): {cr:.2%}", flush=True)
    print(f"  Answer Correctness (答案正确度):  {ac:.2%}", flush=True)
    print(f"  门禁: answer_correctness ≥ {rep['threshold']:.0%} → {passed}\n", flush=True)

    print("-" * 64, flush=True)
    print("逐条明细：", flush=True)
    print("-" * 64, flush=True)
    for i, res in enumerate(rep["results"], 1):
        q = res["question"][:30]
        a = (res.get("answer") or "")[:40].replace("\n", " ")
        g = (res.get("golden_answer") or "")[:30].replace("\n", " ")
        rc = res["context_relevancy"]
        aci = res["answer_correctness"]
        print(f"[{i}] Q: {q}", flush=True)
        print(f"    生成: {a}", flush=True)
        print(f"    标准: {g}", flush=True)
        print(f"    CR={rc:.2%}  AC={aci:.2%}\n", flush=True)


if __name__ == "__main__":
    main()
