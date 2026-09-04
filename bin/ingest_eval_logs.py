#!/usr/bin/env python3
"""
ingest_eval_logs.py — Ingest ABM eval logs into astor bus (source tier).

Scans $EVAL_LOG_DIR (default: <repo>/agent-memory-benchmark-ll/) for *.log files matching the v1109
eval pattern, extracts (run_name, total, correct, accuracy, llm, top_k,
rerank, timestamp), and POSTs each as a canonical fact to
http://127.0.0.1:7803/v1/write with tier=source.

After successful ingest, the file is appended with `# astor: ingested fid=N`
so re-runs are idempotent.

Usage:
    python bin/ingest_eval_logs.py [--dry-run]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# R-class: replaced hardcoded operator benchmark path with env var EVAL_LOG_DIR.
# Operators point EVAL_LOG_DIR at their benchmark dir; default <repo>/agent-memory-benchmark-ll.
LOG_DIR = Path(os.environ.get('EVAL_LOG_DIR', '<repo>/agent-memory-benchmark-ll'))
ASTOR_URL = os.environ.get('ASTOR_HTTP_URL', 'http://127.0.0.1:7803')
LOG_PATTERN = re.compile(
    r'(?P<key>Total(?:\s+Queries)?|Correct|Accuracy)\s*[:=]\s*(?P<val>[\d.]+%?)'
)
LLM_PATTERN = re.compile(r'Answer LLM:\s*(\S+)')
# Rich text logs use `[bold]Dataset:[/bold] locomo` — colon may be inside the tag.
DATASET_PATTERN = re.compile(r'(?:Dataset|--- Settings ---|dataset)\s*:?\s*(?:\[[^]]*\])?\s*(\w+)', re.I)
_RUN_TAG = re.compile(r'\[/?[a-z]+\]', re.I)
RUN_PATTERN = re.compile(r'(?:^|[\s\[\]])Run\s*:\s*(?:\[[^]]*\])?\s*([A-Za-z0-9_.\-]+)', re.I)
# Settings block (non-rich): `top_k=20` or `rerank=on`
TOPK_PATTERN = re.compile(r'top[_-]?k\s*[=:]\s*(\d+)', re.I)
RERANK_PATTERN = re.compile(r'rerank\s*[=:]\s*(on|off|1|0|true|false)', re.I)


def parse_log(path: Path) -> dict | None:
    """Extract structured metrics from a single eval log."""
    text = path.read_text(encoding='utf-8', errors='replace')

    metrics = {}
    for m in LOG_PATTERN.finditer(text):
        key = m.group('key').rstrip(':').lower()
        # Normalize 'Total Queries' -> 'total'
        key = key.replace(' queries', '').replace(' ', '_')
        val = m.group('val').rstrip('%')
        try:
            metrics[key] = float(val) if '.' in val else int(val)
        except ValueError:
            metrics[key] = val

    if 'total' not in metrics or 'accuracy' not in metrics:
        return None

    # Strip rich-text tags ([bold], [/bold], etc.) so regex matches cleanly.
    text_clean = _RUN_TAG.sub('', text)

    llm_match = LLM_PATTERN.search(text)
    dataset_match = DATASET_PATTERN.search(text)
    # findall + filter — `Running LoCoMo:` shouldn't match `Run: ...`
    run_candidates = RUN_PATTERN.findall(text_clean)
    # Filter: `Running` would match `Run:ning` but our pattern requires ':'
    # and `Run:` followed by valid identifier. Use last match (most specific).
    run_match_value = run_candidates[-1] if run_candidates else None
    topk_match = TOPK_PATTERN.search(text)
    rerank_match = RERANK_PATTERN.search(text)

    # Derive run_name from filename if not in log body
    run_name = run_match_value if run_match_value else path.stem

    # Derive timestamp from file mtime
    mtime = path.stat().st_mtime

    # Derive tags from filename for searchability
    tags = ['eval_result', f'log:{path.name}']
    fname_parts = path.stem.split('_')
    if len(fname_parts) >= 2 and fname_parts[0].startswith('v'):
        tags.append(f'version:{fname_parts[0]}')

    # Infer top_k from run_name or filename pattern (legacy heuristic).
    # `q100_*` → 100, full v1109 rerun → 1540, otherwise None.
    inferred_top_k = None
    run_name_lower = (run_name or '').lower()
    fname_str = path.stem.lower()
    if 'q100' in run_name_lower or 'q100' in fname_str:
        inferred_top_k = 100
    elif 'v1109_rerun' in fname_str or 'v1109_v2' in fname_str:
        inferred_top_k = 1540

    # If rerank wasn't logged, leave None (don't guess).
    top_k_val = int(topk_match.group(1)) if topk_match else inferred_top_k

    # Infer rerank from filename heuristic.
    # `rerankon` / `synpatch` / etc. suggest ON; `baseline` / `norerank` suggest OFF.
    inferred_rerank = None
    if rerank_match:
        inferred_rerank = rerank_match.group(1).lower()
    else:
        # filename contains 'rerankon' or 'synpatch' (synthetic patch uses rerank)
        if any(k in fname_str for k in ('rerankon', 'synpatch', 'expansion')):
            inferred_rerank = 'on'
        elif 'baseline' in fname_str or 'bridge_off' in fname_str:
            inferred_rerank = 'off'

    return {
        'run_name': run_name,
        'total': int(metrics.get('total', 0)),
        'correct': int(metrics.get('correct', 0)),
        'accuracy_pct': float(metrics.get('accuracy', 0.0)),
        'dataset': dataset_match.group(1) if dataset_match else 'unknown',
        'llm': llm_match.group(1) if llm_match else 'unknown',
        'top_k': top_k_val,
        'rerank': inferred_rerank,
        'log_file': path.name,
        'mtime_unix': int(mtime),
        'tags': tags,
    }


def build_fact_text(m: dict) -> str:
    """One-line canonical fact string for astor extract."""
    rerank = m['rerank'] or 'unknown'
    topk = m['top_k'] or 'unknown'
    return (
        f"Eval result: {m['run_name']} | dataset={m['dataset']} | "
        f"total={m['total']} correct={m['correct']} accuracy={m['accuracy_pct']:.2f}% | "
        f"llm={m['llm']} | top_k={topk} | rerank={rerank}"
    )


def post_fact(text: str, tags: list[str], tier: str = 'source',
              user: str = 'eval_log_ingester', retries: int = 2) -> dict | None:
    body = json.dumps({
        'text': text,
        'tier': tier,
        'mode': 'regex',
        'tags': tags,
        'user': user,
        'user_id': user,
        'metadata': {'source': 'ingest_eval_logs.py', 'ingested_at': int(time.time())},
    }).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                f'{ASTOR_URL}/v1/write',
                data=body,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            time.sleep(1)
    print(f'  ERROR POST: {last_err}', file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Parse and print without POSTing.')
    parser.add_argument('--pattern', default='*v1109*.log',
                        help='Glob pattern relative to LOG_DIR (default: *v1109*.log).')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max logs to ingest (0 = no limit).')
    args = parser.parse_args()

    # Health check first
    try:
        urllib.request.urlopen(f'{ASTOR_URL}/v1/health', timeout=5).read()
        print(f'✓ astor server reachable at {ASTOR_URL}')
    except Exception as e:
        print(f'✗ astor server unreachable at {ASTOR_URL}: {e}', file=sys.stderr)
        sys.exit(1)

    logs = sorted(LOG_DIR.glob(args.pattern))
    if args.limit:
        logs = logs[:args.limit]

    print(f'Scanning {len(logs)} log(s) matching {args.pattern!r} in {LOG_DIR}')

    ingested = 0
    skipped = 0
    failed = 0

    for log_path in logs:
        # Idempotency check: skip if already ingested marker present
        try:
            first_lines = log_path.read_text(encoding='utf-8', errors='replace')[:2000]
        except Exception:
            first_lines = ''
        if '# astor: ingested' in first_lines:
            skipped += 1
            continue

        parsed = parse_log(log_path)
        if not parsed:
            print(f'  SKIP (no metrics): {log_path.name}')
            skipped += 1
            continue

        text = build_fact_text(parsed)
        print(f'\n  → {log_path.name}')
        print(f'    {text}')

        if args.dry_run:
            continue

        result = post_fact(text, parsed['tags'])
        if result and result.get('fact_ids'):
            fid = result['fact_ids'][0] if isinstance(result['fact_ids'], list) else result['fact_ids']
            print(f'    ✓ ingested fid={fid}')
            # Append idempotency marker (best-effort, append-only)
            try:
                with log_path.open('a', encoding='utf-8') as f:
                    f.write(f'\n# astor: ingested fid={fid} at {int(time.time())}\n')
            except Exception as e:
                print(f'    WARN: could not append marker: {e}', file=sys.stderr)
            ingested += 1
        else:
            failed += 1

    print(f'\n=== Summary ===')
    print(f'  Ingested: {ingested}')
    print(f'  Skipped (already ingested or no metrics): {skipped}')
    print(f'  Failed: {failed}')


if __name__ == '__main__':
    main()
