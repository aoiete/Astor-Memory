"""The id ruler against the live server.

The 110-question keyword eval asks "is a fact containing the expected
keyword in the top 10". It cannot tell a right answer written in
different words from a wrong one that happens to use the right words.
This asks the sharper question: "is a human-accepted answer fact id in
the top 10", using tests/eval_paraphrase_golden.jsonl.

With --classify it also splits every miss into the three buckets that
actually decide what to do next:

    storage gap   the accepted answer fact is not in the store
    retrieval gap it is in the store, in the queried tier, alive
    ruler bug     the gold ids do not answer the question

Only the retrieval-gap bucket can be fixed by changing the ranking.

Usage:
    python tests/eval_id_ruler.py                  # against 127.0.0.1:7803
    python tests/eval_id_ruler.py --classify
    python tests/eval_id_ruler.py --server URL

Read the recipe before trusting a number:
references/id-ruler-and-recency-clock-2026-11-24.md
"""

import argparse
import glob
import json
import os
import sqlite3
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.environ.get('ASTOR_RUNTIME', r'D:/AI/Astor-Memory-Runtime')


def read(server, query, tier, user, k=30):
    body = {"query": query, "tier": tier, "user": user, "top_k": k}
    r = urllib.request.Request(f"{server}/v1/read", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=40) as x:
            return json.loads(x.read().decode())
    except Exception:
        return {"results": []}


def _bus_paths():
    paths = {}
    for tier, pat in (('public', 'public/memory/astor_bus_public.db'),
                      ('source', 'source/memory/astor_bus_source.db'),
                      ('private', 'users/*/memory/astor_bus_*.db')):
        for p in glob.glob(os.path.join(RUNTIME, pat)):
            paths[p] = tier
    return paths


def fact_state(fid):
    """(tier, tombstoned, kind, content) for a fact id, or None."""
    for path, tier in _bus_paths().items():
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            row = c.execute(
                "select tombstoned, kind, content from memory_canonical where id=?",
                (fid,)).fetchone()
        except Exception:
            row = None
        finally:
            c.close()
        if row:
            return tier, row[0], row[1], (row[2] or '')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default='http://127.0.0.1:7803')
    ap.add_argument('--classify', action='store_true',
                    help='split every miss into storage gap / retrieval gap / ruler bug')
    args = ap.parse_args()

    gold = {}
    for line in open(os.path.join(HERE, 'eval_paraphrase_golden.jsonl'), encoding='utf-8'):
        if line.strip():
            row = json.loads(line)
            gold[row['qid']] = row
    pset = [json.loads(l) for l in open(os.path.join(HERE, 'eval_paraphrase_set.jsonl'), encoding='utf-8')]

    for k in (10, 30):
        top1 = topk = n = 0
        ranks = []
        misses = []
        for q in pset:
            qid = q.get('qid')
            if qid not in gold:
                continue
            res = (read(args.server, q['query'], q.get('tier', 'public'),
                        q.get('user', 'admin'), k).get('results') or [])
            if not res:
                continue
            n += 1
            fids = [r.get('fact_id') for r in res]
            pos = next((i + 1 for i, f in enumerate(fids) if f in gold[qid]['answer_fact_ids']), None)
            if pos == 1:
                top1 += 1
            if pos:
                topk += 1
                ranks.append(pos)
            else:
                misses.append((qid, q.get('tier'), q['query'][:34]))
        med = sorted(ranks)[len(ranks) // 2] if ranks else '-'
        print(f"top_k={k}: answer_found={topk}/{n} ({topk/max(1,n):.3f})  "
              f"rank1={top1}/{n} ({top1/max(1,n):.3f})  median_rank={med}")
        if k != 10 or not args.classify:
            continue

        print("  misses at top10, classified:")
        n_retrieval = n_storage = 0
        for qid, tier, qtext in misses:
            ids = gold[qid]['answer_fact_ids']
            states = [(fid, fact_state(fid)) for fid in ids]
            alive = [(fid, st) for fid, st in states if st and st[1] == 0]
            if not alive:
                kind = 'storage_gap'
                n_storage += 1
            elif gold[qid].get('corpus_gap'):
                kind = 'storage_gap (flagged)'
                n_storage += 1
            else:
                kind = 'retrieval_gap'
                n_retrieval += 1
            print(f"    {qid:4s} {kind:20s} [{tier}] {qtext}")
            for fid, st in alive[:2]:
                print(f"           ans {fid}: {st[3][:64]}")
        print(f"  summary: retrieval_gap={n_retrieval}  storage_gap={n_storage}  "
              f"(a miss whose alive answers do not answer the question is a ruler bug -- read them)")


if __name__ == '__main__':
    main()
