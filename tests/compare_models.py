"""compare embedding models for Chinese recall.
Run: python compare_models.py
"""
import subprocess
import time
import numpy as np
from fastembed import TextEmbedding


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def load_and_eval(model_name, queries, docs, label):
    print(f"\n=== {label}: {model_name} ===")
    t0 = time.time()
    try:
        m = TextEmbedding(model_name=model_name)
    except Exception as e:
        print(f"  FAILED to load: {e}")
        return None
    print(f"  loaded in {time.time()-t0:.1f}s")
    q_vecs = list(m.embed(queries))
    d_vecs = list(m.embed(docs))
    results = []
    for i, q in enumerate(queries):
        sims = sorted(
            [(cos(q_vecs[i], d_vecs[j]), docs[j][:60]) for j in range(len(docs))],
            reverse=True,
        )
        top1 = sims[0]
        results.append({"query": q, "top1_sim": top1[0], "top1_text": top1[1]})
        print(f"  q='{q[:30]}' top1_sim={top1[0]:.3f}  {top1[1][:50]}")
    return results


QUERIES = [
    "流天书 协议 客户档案",
    "紫微 命盘 输出",
    "moomoo 4-env PYTHONPATH",
    "ACTIVE_ prefix 备份",
    "ship cadence 批量 ship",
]

DOCS = [
    "用户的 fortune 流天书项目文件在 D:/AI/fortune-model/,有 eval_results/ + scripts/ 子目录,项目专属脚本不进 D:/AI/scripts/",
    "流天书项目包含64gua和tcm内容,古籍/古文是核心功能",
    "用户认为古籍信息量大且很重要,确认古籍引用是流天书的核心能力",
    "紫微斗数推算 v18 LoRA 模型支持 12 宫 + 四化飞星",
    "用户看 v18 紫微命盘 + 八字日运双重验证",
    "moomoo scripts 改用 ml-venv 跑通的验证:4 个文件已改并测过 SDK 连接",
    "4-env Python 架构:hermes venv / system Python / PY-311 / PY-314-ml",
    "ACTIVE_ prefix 用法:用户 backup 目录前缀 ACTIVE_ 标记当前在用",
    "用户工作流偏好:批量 ship(每批 3 个 R-class),bus fact + ship log 标准化结尾",
    "R107 ship cadence 锁定:感觉=wait, 开干=all-in ship, 继续=继续 P-card",
]

if __name__ == "__main__":
    models = [
        ("bge-base-en-v1.5 (current)", "BAAI/bge-base-en-v1.5"),
        ("bge-small-zh-v1.5", "BAAI/bge-small-zh-v1.5"),
        ("jinaai-v2-base-zh", "jinaai/jina-embeddings-v2-base-zh"),
        ("multilingual-e5-large", "intfloat/multilingual-e5-large"),
        ("multilingual-MiniLM-L12", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
    ]
    summary = {}
    for label, mname in models:
        r = load_and_eval(mname, QUERIES, DOCS, label)
        if r:
            summary[label] = r
    print("\n=== SUMMARY (avg top1 cosine per model) ===")
    for label, r in summary.items():
        avg = sum(x["top1_sim"] for x in r) / len(r)
        print(f"  {label}: avg={avg:.3f}")
