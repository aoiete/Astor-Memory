"""Calibrate the fanout gate on real facts instead of on invented queries.

Every threshold in this file was picked by hand until now, from a
handful of examples. This draws the examples from the corpus itself:
real stored facts become the "this node knows it" queries, and their
titles are the queries. That is the population the gate will actually
see -- a customer's question about something it stored.

For each query it computes the candidate signals:

    cov_count   the best document's matched-term count (today's signal)
    cov_idf     the best document's share of the query's IDF mass
    ratio       today's vocabulary ratio

and reports where each candidate's threshold would misfire.

Run with the project interpreter:
    D:/AI/PY-311/Scripts/python.exe tools/calibrate_fanout_gate.py
"""
from __future__ import annotations

import math
import os
import random
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("ASTOR_DIR", os.environ.get("ASTOR_DIR", ""))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.nest.lex_index import astor_lex, _tokenize  # noqa: E402

RUNTIME = Path(os.environ["ASTOR_DIR"])
BUS = RUNTIME / "users/admin/memory/astor_bus_admin.db"

# Topics this node has nothing about. They are deliberately made of
# words that DO appear in the corpus -- common English and common
# Chinese -- so they measure the thing that actually goes wrong: a
# query built from familiar words about an unfamiliar subject.
UNKNOWN = [
    "quantum entanglement protocol calibration",
    "kubernetes pod sidecar init container",
    "rust async trait object safe lifetime",
    "postgres vacuum analyze replication slot",
    "terraform module registry workspace backend",
    "golang goroutine channel select deadlock",
    "nginx reverse proxy upstream keepalive timeout",
    "matplotlib contour fill colorbar legend",
    "typescript discriminated union exhaustiveness",
    "cuda stream synchronization warp occupancy",
    "redis cluster hash slot failover",
    "graphql resolver dataloader batch",
    "蚂蚁花呗 额度 提现 冻结",
    "量子纠缠 校准 协议",
    "涨停 跌停 复牌 公告",
    "屋顶光伏 补贴 并网 电价",
    "钢琴 调音 踏板 琴弦",
    "麻醉 手术 剂量 术后 护理",
    "有机 蔬菜 农药 残留 检测",
    "考研 数学 极限 洛必达",
    "自动驾驶 激光雷达 点云 融合",
    "普洱 生茶 熟茶 发酵 仓储",
    "信用卡 年费 积分 兑换 里程",
    "少儿 编程  Scratch 课程 设计",
    "房贷 等额本金 等额本息 利率",
    "吉他 指法 和弦 节奏 练习",
    "疫苗 接种 免疫 抗体",
    "足球 阵型 越位 战术 压迫",
    "台风 路径 登陆 风力 预警",
]


def load_known_queries(n: int = 40) -> list[tuple[str, str]]:
    """Real stored facts, used as queries about things this node has."""
    con = sqlite3.connect(str(BUS))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, content FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind IN ('lesson','rule','fact') "
        "AND LENGTH(content) BETWEEN 80 AND 600 "
        "ORDER BY RANDOM() LIMIT ?", (n * 3,)).fetchall()
    con.close()

    out = []
    for r in rows:
        text = (r["content"] or "").replace("\n", " ")
        # the leading clause of a stored fact is the most query-like
        # part of it
        head = text[:120].strip()
        head = head.split("。")[0].split(". ")[0].strip()
        if len(head) < 12:
            continue
        out.append((f"known#{r['id']}", head))
        if len(out) >= n:
            break
    return out


def main() -> int:
    lex = astor_lex(tier="private", user_id="admin")
    N = lex._conn.execute(
        "SELECT COUNT(*) FROM documents WHERE tombstoned = 0").fetchone()[0] or 1

    def df_of(term):
        row = lex._conn.execute(
            "SELECT df FROM terms WHERE term=?", (term,)).fetchone()
        return int(row[0]) if row else 0

    def idf(term):
        df = df_of(term)
        return math.log(max((N - df + 0.5) / (df + 0.5) + 1.0, 1e-9))

    def measure(q):
        uniq = set(_tokenize(q))
        total_idf = sum(idf(t) for t in uniq)
        cov = lex.bm25_coverage(q, limit=5)
        best_cov = 0
        best_idf = 0.0
        for fid, _s, _m in cov:
            ph = ",".join("?" * len(uniq))
            held = {r[0] for r in lex._conn.execute(
                f"SELECT DISTINCT term FROM postings WHERE fact_id=? "
                f"AND term IN ({ph})", [fid, *uniq]).fetchall()}
            best_cov = max(best_cov, len(held & uniq))
            best_idf = max(best_idf, sum(idf(t) for t in (held & uniq)))
        num = den = 0.0
        for t in uniq:
            w = idf(t)
            den += w
            if df_of(t) > 0:
                num += w
        ratio = num / den if den else 0.0
        return best_cov, (best_idf / total_idf if total_idf else 0.0), ratio

    known = load_known_queries(40)
    queries = [(k, q) for k, q in known] + [("unknown", q) for q in UNKNOWN]
    print(f"  {len(known)} 条真实 fact + {len(UNKNOWN)} 条生查询\n")

    stats = []
    for kind, q in queries:
        cov, covidf, ratio = measure(q)
        stats.append((kind, cov, covidf, ratio))

    def report(name, pick, thr):
        known_hit = sum(1 for k, *_ in stats if k != "unknown" and pick(*_) >= thr)
        known_n = sum(1 for k, *_ in stats if k != "unknown")
        unk_hit = sum(1 for k, *_ in stats if k == "unknown" and pick(*_) >= thr)
        unk_n = sum(1 for k, *_ in stats if k == "unknown")
        print(f"  {name:26} 真实 fact 判为本地 {known_hit:3}/{known_n:3} "
              f"({known_hit/known_n:5.1%})   生查询误判本地 {unk_hit:2}/{unk_n:2} "
              f"({unk_hit/unk_n:5.1%})")

    print("  现状 (cov>=2 OR ratio>=30%):")
    cur_known = sum(1 for k, c, ci, r in stats
                    if k != "unknown" and (c >= 2 or r >= 0.30))
    cur_unk = sum(1 for k, c, ci, r in stats
                  if k == "unknown" and (c >= 2 or r >= 0.30))
    kn = sum(1 for k, *_ in stats if k != "unknown")
    un = sum(1 for k, *_ in stats if k == "unknown")
    print(f"  {'':26} 真实 fact 判为本地 {cur_known:3}/{kn:3} ({cur_known/kn:5.1%})"
          f"   生查询误判本地 {cur_unk:2}/{un:2} ({cur_unk/un:5.1%})")

    print("\n  只用 cov_idf:")
    for thr in (0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60):
        report(f"cov_idf>={thr:.0%}", lambda c, ci, r, t=thr: ci if t else 0.0, thr)

    print("\n  cov_idf + cov_count>=2 (两个都要):")
    for thr in (0.25, 0.30, 0.35, 0.40):
        kn2 = sum(1 for k, c, ci, r in stats
                  if k != "unknown" and ci >= thr and c >= 2)
        un2 = sum(1 for k, c, ci, r in stats
                  if k == "unknown" and ci >= thr and c >= 2)
        print(f"  {'':26} 真实 fact 判为本地 {kn2:3}/{kn:3} ({kn2/kn:5.1%})"
              f"   生查询误判本地 {un2:2}/{un:2} ({un2/un:5.1%})")

    print("\n  分布:")
    kcov = sorted(ci for k, c, ci, r in stats if k != "unknown")
    ucov = sorted(ci for k, c, ci, r in stats if k == "unknown")
    print(f"    真实 fact 的 cov_idf: min={kcov[0]:.0%} p10={kcov[len(kcov)//10]:.0%} "
          f"p50={kcov[len(kcov)//2]:.0%} max={kcov[-1]:.0%}")
    print(f"    生查询   的 cov_idf: " +
          " ".join(f"{v:.0%}" for v in ucov))
    return 0


if __name__ == "__main__":
    sys.exit(main())
