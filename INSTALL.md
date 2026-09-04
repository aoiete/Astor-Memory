# Install

```bash
pip install astor-memory
am init
am admin lock --user-id=admin
```

That's it. The CLI commands are created by `pip install` (see
`pyproject.toml` `[project.scripts]`).

## From source (development)

```bash
git clone https://github.com/<owner>/Astor-Memory
cd Astor-Memory
pip install -e ".[dev]"
pytest tests/
```

## Prerequisites

- Python 3.11 or later
- sqlite3 (stdlib, included)
- For LLM features: an OpenAI-compatible API key
  (`OPENAI_API_KEY` or `OPENROUTER_API_KEY` or `MINIMAX_API_KEY`)

## See also

- [QUICKSTART.md](QUICKSTART.md) — 5-minute end-to-end demo
- [README.md](README.md) — overview + concepts
- [docs/architecture.md](docs/architecture.md) — 3-store × 3-tier design deep dive