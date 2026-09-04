# Contributing

Thanks for helping improve astor-memory.

## Before you PR

1. **Search for the R-class.** If your fix or feature violates an existing
   R-class, the PR will be rejected. R-classes are operator-locked
   decisions, not guidelines to debate.

2. **Search for the bug or feature in issues.** Avoid duplicate work.

3. **Check the bus hard_rules table** for cross-cutting rules (R218, R252,
   R354, R365, R380, ...).

## Code style

- Python 3.11+
- Type hints on every public function
- Docstrings on every module, class, and public function (Google-style)
- Tests required for any new feature or bugfix
- The `pre-commit` framework runs ruff + mypy; install with
  `pip install -e ".[dev]"` then `pre-commit install`

## Commit message format

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `revert`.
Scope: the affected module (e.g. `bus`, `nest`, `cli`, `server`,
`tests`).

Subject line under 72 characters. Body wraps at 72. Reference R-class
in footer if applicable.

Example:
```
fix(acl): R354 forget ownership check

The /v1/forget endpoint did not enforce fact ownership; a non-admin
caller could delete any admin's public-tier fact. Verified pre-patch:
yuqi successfully forgot admin's fact 4457.

Refs: R354, R365
```

## Testing

```bash
PYTHONPATH=. ASTOR_DIR=/tmp/astor-test /d/AI/PY-311/Scripts/python.exe -m pytest tests/ -q
```

209 tests, expected pass. If you add a new test, ensure it uses
`fresh_db`-style isolation — don't depend on real `$ASTOR_DIR` data.

## Pre-publish audit

Before pushing to GitHub, run the pre-publish audit:

1. **No PII in source.** `grep -rnE "Yuqi|TheNuts|C:/Users/[a-z]+|D:/AI/[A-Za-z]+"`
   should return 0 hits in `astor_memory/`, `bin/`, `tests/`, `scripts/`.
2. **No hardcoded tokens.** `grep -rnE "(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]"`
   (excluding env-var defaults and SQL `IS NULL` checks).
3. **Architecture consistency.** Doc claims (e.g. "3-store triplet") match
   code reality (e.g. `bus/`, `forge/`, `nest/`).
4. **Tests pass.** `pytest tests/` is green.
5. **R-class audit.** No new R-class violations.

## Code of conduct

Be kind. Be useful. Don't refactor unrelated code in the same PR.

## Questions?

Open an issue with the `question` label.