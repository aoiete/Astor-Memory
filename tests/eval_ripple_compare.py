#!/usr/bin/env python
"""eval_ripple_compare.py — A/B test Ship A params on M01-M10 queries.

Compares each M query WITHOUT (baseline) vs WITH (Ship A hint/filter/time)
the new optional /v1/read params. Reports hit rate delta and which
queries improved/regressed.

Output: astor/metrics/eval_ripple_compare_<ts>.json

Usage:
    python tests/eval_ripple_compare.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SERVER = os.environ.get('ASTOR_SERVER', 'http://127.0.0.1:7803')
EVAL_SET = Path(__file__).parent / 'eval_set.jsonl'
METRICS_DIR = Path(__file__).parent.parent / 'astor' / 'metrics'


def call_recall(body: dict, server: str = SERVER) -> tuple[list[dict], float]:
    """POST /v1/read; return (results, latency_ms)."""
    req = urllib.request.Request(
        f'{server}/v1/read',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode('utf-8'))
    lat_ms = (time.time() - t0) * 1000
    return d.get('results', []), lat_ms


def score(results: list[dict], expected_keywords: list[str], min_hits: int = 1) -> tuple[bool, int]:
    """Check whether expected_keywords appear across top-k results."""
    flat = ' '.join(r.get('content', '') for r in results).lower()
    matches = sum(1 for kw in expected_keywords if kw.lower() in flat)
    return matches >= min_hits, matches


def main() -> int:
    # Read eval set
    queries = []
    with open(EVAL_SET, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                q = json.loads(line)
                if q.get('qid', '').startswith('M'):
                    queries.append(q)
    print(f'Comparing {len(queries)} M queries: WITH vs WITHOUT Ship A params\n')

    detail = []
    for q in queries:
        base_body = {
            'query': q['query'],
            'tier': q.get('tier', 'public'),
            'user': q.get('user', 'admin'),
            'top_k': q.get('top_k', 10),
        }
        # WITHOUT (baseline)
        res0, lat0 = call_recall(base_body)
        hit0, n0 = score(res0, q['expected_keywords'], q.get('min_hits', 1))

        # WITH (Ship A params)
        body1 = dict(base_body)
        for k in ('missing_hint', 'entity_filter', 'since_ts', 'until_ts'):
            if k in q:
                body1[k] = q[k]
        res1, lat1 = call_recall(body1)
        hit1, n1 = score(res1, q['expected_keywords'], q.get('min_hits', 1))

        detail.append({
            'qid': q['qid'],
            'query': q['query'],
            'missing_hint': q.get('missing_hint'),
            'entity_filter': q.get('entity_filter'),
            'since_ts': q.get('since_ts'),
            'until_ts': q.get('until_ts'),
            'without_hit': hit0,
            'without_n_matches': n0,
            'with_hit': hit1,
            'with_n_matches': n1,
            'without_lat_ms': round(lat0, 1),
            'with_lat_ms': round(lat1, 1),
            'improved': bool(hit1 and not hit0),
            'regressed': bool(hit0 and not hit1),
        })

    # Print table
    print(f'{"qid":5s} {"without":16s} {"with":16s} {"lat(ms)":12s} {"Δ":5s}')
    print('-' * 60)
    for r in detail:
        w0 = f"{'HIT' if r['without_hit'] else 'miss'}({r['without_n_matches']})"
        w1 = f"{'HIT' if r['with_hit'] else 'miss'}({r['with_n_matches']})"
        lat = f"{r['without_lat_ms']:.0f}/{r['with_lat_ms']:.0f}"
        delta = ''
        if r['improved']:
            delta = '+UP'
        elif r['regressed']:
            delta = 'DOWN'
        print(f'{r["qid"]:5s} {w0:16s} {w1:16s} {lat:12s} {delta:5s}')
    print('-' * 60)

    hit0_count = sum(1 for r in detail if r['without_hit'])
    hit1_count = sum(1 for r in detail if r['with_hit'])
    improved = sum(1 for r in detail if r['improved'])
    regressed = sum(1 for r in detail if r['regressed'])

    avg_lat0 = sum(r['without_lat_ms'] for r in detail) / max(1, len(detail))
    avg_lat1 = sum(r['with_lat_ms'] for r in detail) / max(1, len(detail))

    summary = {
        'n_queries': len(detail),
        'without_hit_rate': hit0_count / max(1, len(detail)),
        'with_hit_rate': hit1_count / max(1, len(detail)),
        'delta_hit_rate': (hit1_count - hit0_count) / max(1, len(detail)),
        'improved': improved,
        'regressed': regressed,
        'avg_lat_without_ms': round(avg_lat0, 1),
        'avg_lat_with_ms': round(avg_lat1, 1),
        'lat_overhead_ms': round(avg_lat1 - avg_lat0, 1),
    }

    print(f'\nSummary (M01-M{len(detail):02d}):')
    print(f'  without hint/filter: {hit0_count}/{len(detail)} = {hit0_count/len(detail):.1%}')
    print(f'  with    hint/filter: {hit1_count}/{len(detail)} = {hit1_count/len(detail):.1%}')
    print(f'  delta:   {summary["delta_hit_rate"]:+.1%}')
    print(f'  improved: {improved}, regressed: {regressed}')
    print(f'  avg latency: without={avg_lat0:.1f}ms, with={avg_lat1:.1f}ms, overhead={avg_lat1 - avg_lat0:+.1f}ms')

    # Save
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    out = {
        'summary': summary,
        'detail': detail,
        'eval_set': str(EVAL_SET),
        'server': SERVER,
        'ts': ts,
    }
    out_path = METRICS_DIR / f'eval_ripple_compare_{ts}.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\nsaved: {out_path}')

    # v1.14.30 Ship S2 (2026-09-15): optional astor_write to record the
    # A/B result as a bus fact. Triggered by ASTOR_WRITE=1 env var
    # (no argparse — keeps script signature free for piping).
    if os.environ.get('ASTOR_WRITE') == '1':
        try:
            text = (
                f'eval_ripple_compare {len(detail)}q: '
                f'without={summary["without_hit_rate"]:.1%} '
                f'with={summary["with_hit_rate"]:.1%} '
                f'delta={summary["delta_hit_rate"]:+.1%} '
                f'imp={summary["improved"]} reg={summary["regressed"]} '
                f'lat_overhead={summary["lat_overhead_ms"]:+.1f}ms'
            )
            body = json.dumps({
                'text': text[:500],
                'user': 'admin',
                'tier': 'private',
                'kind': 'eval_ripple_compare',
                'importance': 0.5,
            }).encode('utf-8')
            req = urllib.request.Request(
                'http://127.0.0.1:7803/v1/write',
                data=body, method='POST',
                headers={
                    'Content-Type': 'application/json',
                    # v1.14.30 Ship S2: X-Actor=admin required because
                    # /v1/write enforces ACL and rejects anonymous 403.
                    'X-Actor': 'admin',
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f'\nastor_write: {r.status} {json.loads(r.read())["fact_ids"]}')
        except Exception as e:
            print(f'\nastor_write failed: {e}', file=sys.stderr)

    # Recommendation
    if summary['delta_hit_rate'] >= 0.10:
        print('\n[RECOMMENDATION] Ship A params lift hit_rate >= 10%. '
              'RippleMem hypothesis CONFIRMED for this corpus.')
        return 0
    elif summary['delta_hit_rate'] >= 0.0 and regressed == 0:
        print('\n[RECOMMENDATION] Ship A params neutral to slight lift. '
              'Keep them as optional caller guidance.')
        return 0
    else:
        print('\n[RECOMMENDATION] Ship A params regress hit_rate. '
              'Investigate before promoting to caller defaults.')
        return 1


if __name__ == '__main__':
    sys.exit(main())