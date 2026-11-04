"""llm_judge.py — LLM-assisted abstraction from a private note to a shareable one.

Why this exists
---------------
The rule classifier answers one question: *does this text contain
private data*. It cannot answer the other one: *is there a generalisable
method buried inside a private story*.

That second question is semantic, and a rule for it does not survive
contact with real notes. A Chinese personal-name detector built for it
missed all four names it was written for and fired on ten method notes
already in the shared tier -- "CI 连红 3 次的真凶", where 连红 is a term
and not a name. The distiller had tried the same trick earlier: its
proper-noun replacement is commented out in the source for the same
reason.

So the LLM is used exactly where the rules fail, and only there.

The shape of the thing
----------------------
    rules    decide the tier on their own, first, always
    LLM      proposes a shareable abstraction, but only when asked
    rules    judge the proposal again before it may be published

The LLM never decides the tier. It proposes; the rules dispose. Every
failure mode -- no provider configured, timeout, malformed output, an
abstraction that still carries a private signal, an abstraction that
invents content the source did not contain -- leaves the note private.
The proposal is checked against the same detector set that guards the
write path, and it has to pass every one of them.

The path is opt-in and never automatic. Private text leaving the host is
a decision a human makes, not one the system makes on its own.

Usage
-----
    from astor_memory.nest.llm_judge import abstract_to_shareable
    r = abstract_to_shareable(text)
    if r["shareable"]:
        publish(r["text"])          # the abstraction, never the original
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

# Provider candidates, in order. The first one that answers wins.
#
# Measured on this deployment (2026-11-04): OpenRouter answers
# gpt-4o-mini, but refuses every google/gemini-* model on this key with
# "violation of provider Terms Of Service". A pinned ASTOR_LLM_MODEL is
# therefore tried first and a plain model second -- otherwise one
# provider-side policy change silently disables the whole path.
#
# The same is true of nest/llm_rerank.py, whose pinned default
# google/gemini-3-flash-preview is refused by the same key.
_API_KEY = (
    os.environ.get("OPENAI_API_KEY", "")
    or os.environ.get("OPENROUTER_API_KEY", "")
)
_BASE_URL = os.environ.get(
    "ASTOR_LLM_BASE_URL", "https://openrouter.ai/api/v1")
_MODEL = os.environ.get("ASTOR_LLM_MODEL", "gpt-4o-mini")


# The providers a caller may ask for by name. A caller cannot point
# astor at an arbitrary endpoint: it may only name one of these, and
# astor supplies the key from its own pool. Letting a request choose
# the destination would turn astor into a relay that ships private
# text wherever the caller says.
KNOWN_PROVIDERS = ("minimax", "openai", "openrouter", "custom")

def normalise_provider(name: str, model: str = "") -> tuple[str, str]:
    """(provider, model) for a caller-supplied preference.

    Only the names astor knows are accepted, and only the model name
    travels -- the endpoint and the key always come from astor's own
    side. A caller must not be able to point astor at an arbitrary
    destination: that would turn astor into a relay that ships private
    text wherever the request says.
    """
    name = (name or "").strip().lower()
    if name not in KNOWN_PROVIDERS:
        return "", ""
    return name, (model or "").strip()


def _agents_path():
    """Where the agent -> provider table lives.

    It is in astor's own runtime directory, not in any agent's config
    tree: astor must not reach into another program's installation to
    find out what model it runs on. The operator fills it in, or `am
    agent-set` does.
    """
    import json
    import os as _os
    from pathlib import Path as _Path
    try:
        from ..config import get_default_astor_dir
        base = get_default_astor_dir()
    except Exception:
        base = _Path(_os.environ.get("ASTOR_DIR") or (_Path.home() / ".astor"))
    return _Path(base) / "agents.json"


def load_agent_table() -> dict:
    """{agent_name: {"provider": ..., "model": ...}} -- empty on any error."""
    import json
    try:
        raw = _agents_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        prov = str(cfg.get("provider") or "").strip().lower()
        if prov not in KNOWN_PROVIDERS:
            continue
        out[str(name).strip().lower()] = {
            "provider": prov,
            "model": str(cfg.get("model") or "").strip(),
        }
    return out


def save_agent_table(table: dict) -> bool:
    import json
    try:
        path = _agents_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


def resolve_agent_provider(agent: str) -> tuple[str, str]:
    """(provider, model) for a named agent. ("", "") when unknown."""
    name = (agent or "").strip().lower()
    if not name:
        return "", ""
    cfg = load_agent_table().get(name)
    if not cfg:
        return "", ""
    return cfg.get("provider", ""), cfg.get("model", "")


def _candidates(prefer: tuple[str, str] | None = None
                ) -> list[tuple[str, str, str, str]]:
    """(name, base_url, key, model) tries, in order.

    Three ways in, in this order:

      1. the operator's own slot -- ASTOR_LLM_BASE_URL / ASTOR_LLM_API_KEY
         / ASTOR_LLM_MODEL. This is the "fill it in yourself" path: any
         OpenAI-compatible endpoint works.
      2. the built-in providers, each with its own key env var and
         default model.
      3. for every provider, a second try without the pinned model, so
         that a provider-side policy change (OpenRouter refusing every
         google/gemini-* model on one key, measured 2026-11-04) does not
         silently disable the whole path.
    """
    pinned = os.environ.get("ASTOR_LLM_MODEL", "").strip()
    default_model = (
        os.environ.get("ASTOR_LLM_JUDGE_MODEL", "").strip() or "gpt-4o-mini")
    out: list[tuple[str, str, str, str]] = []

    def add(name: str, base: str, key: str, model: str) -> None:
        if not key or not base:
            return
        out.append((name, base, key, model))
        if pinned and model == pinned and pinned != default_model:
            out.append((name, base, key, default_model))

    # 1. the operator's own endpoint
    add("custom",
        os.environ.get("ASTOR_LLM_BASE_URL", "").strip(),
        os.environ.get("ASTOR_LLM_API_KEY", "").strip(),
        pinned or default_model)

    # 2. built-ins
    add("minimax", "https://api.minimax.io/v1",
        os.environ.get("MINIMAX_API_KEY", "").strip(),
        pinned or os.environ.get("ASTOR_LLM_MINIMAX_MODEL", "").strip()
        or "MiniMax-M3.1-Flash-Preview")
    add("openai",
        os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
        os.environ.get("OPENAI_API_KEY", "").strip(),
        pinned or default_model)
    add("openrouter", "https://openrouter.ai/api/v1",
        os.environ.get("OPENROUTER_API_KEY", "").strip(),
        pinned or default_model)

    # A caller that named its own provider goes first: the agent that
    # connected is the one that knows which model it runs on.
    if prefer:
        want, want_model = prefer
        for i, c in enumerate(out):
            if c[0] == want and (not want_model or c[3] == want_model):
                out.insert(0, out.pop(i))
                break
    return out


_SYSTEM = """You extract the generalisable method from a note.

Return JSON only, with exactly these keys:
  "shareable": true or false
  "method":    the method, pattern, process or model, written so that
               anybody could reuse it
  "reason":    one short sentence, in English, saying what you did

Rules for "method":
1. Keep only what generalises. Drop the personal specifics entirely:
   no names, no place names, no dates, no amounts, no company names, no
   account or credential details, no relations to the writer.
2. Write it in the third person. Never use "I", "we", "my", "our".
   "First set a stop loss, then size the position" -- not "I set a stop
   loss, then sized my position".
3. Preserve the order and the causality of any procedure. "First X, then
   Y, finally Z" must stay "First X, then Y, finally Z".
4. Invent nothing. Every word must come from the note.
5. If the note contains no generalisable method -- only private facts,
   only status, only opinions with nothing reusable -- return
   "shareable": false and an empty "method".

A note that describes a method with private decoration around it is
still shareable: extract the method and drop the decoration."""


def provider_available() -> tuple[bool, str]:
    """Is there a provider to talk to? (available, model)"""
    cands = _candidates()
    if not cands:
        return False, ""
    return True, cands[0][3]


def _call(text: str, timeout: int,
          prefer: tuple[str, str] | None = None
          ) -> tuple[dict | None, str]:
    """Try each candidate in order. Returns (parsed, provider)."""
    if not text or len(text.strip()) < 4:
        return None, ""
    for name, base, key, model in _candidates(prefer):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text[:6000]},
            ],
            "max_tokens": 800,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{base}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            content = raw["choices"][0]["message"].get("content", "") or ""
        except Exception:
            continue
        # The model may wrap the JSON in a fence, and MiniMax-M3 emits a
        # <think> block before the answer.
        content = content.strip()
        if "</think>" in content:
            content = content.split("</think>", 1)[1].strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        try:
            parsed = json.loads(content)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed, f"{name}/{model}"
    return None, ""



def _grounded(source: str, candidate: str, min_overlap: float) -> bool:
    """Is the proposal made of the source's own words?

    Same shape as forge.llm_extract._grounding_check: CJK characters
    and Latin words of the proposal must appear in the source. This is
    what stops the model from inventing a method that was never there.
    """
    if not source or not candidate:
        return False
    src_chars = set(re.findall(r'[\u4e00-\u9fff]', source))
    src_words = {w.lower() for w in re.findall(r'[A-Za-z]{2,}', source)}
    if not src_chars and not src_words:
        return False
    cand_chars = set(re.findall(r'[\u4e00-\u9fff]', candidate))
    cand_words = {w.lower() for w in re.findall(r'[A-Za-z]{2,}', candidate)}
    total = len(cand_chars) + len(cand_words)
    if total == 0:
        return False
    hit = (sum(1 for c in cand_chars if c in src_chars)
           + sum(1 for w in cand_words if w in src_words))
    return (hit / total) >= min_overlap


def abstract_to_shareable(
    text: str,
    *,
    timeout: int = 60,
    min_overlap: float = 0.5,
    min_chars: int = 8,
    prefer: tuple[str, str] | None = None,
) -> dict:
    """Propose a shareable abstraction of `text`.

    Returns a dict:
        shareable  bool -- the proposal passed every check
        text       str  -- the abstraction, only when shareable
        reason     str  -- why it was refused, or what it did
        provider   str  -- the model that answered
        signals    dict -- the detector hits, when refused

    Fails closed: any error, timeout or malformed answer returns
    shareable=False with the text untouched.
    """
    out = {
        "shareable": False,
        "text": "",
        "reason": "",
        "provider": "",
        "signals": {},
    }
    if not (text or "").strip():
        out["reason"] = "empty input"
        return out
    if not _candidates(prefer):
        out["reason"] = "no LLM provider configured"
        return out

    parsed, provider = _call(text, timeout, prefer)
    out["provider"] = provider
    if parsed is None:
        out["reason"] = "provider call failed or returned unparsable output"
        return out

    if not parsed.get("shareable"):
        out["reason"] = str(parsed.get("reason") or
                            "the model found no generalisable method")
        return out

    method = str(parsed.get("method") or "").strip()
    if len(method) < min_chars:
        out["reason"] = "the proposal was too short to be a method"
        return out

    if not _grounded(text, method, min_overlap):
        out["reason"] = ("the proposal introduced words that are not in "
                         "the note, so it is not grounded in it")
        return out

    # The rules dispose. The proposal has to pass the same detector set
    # that guards the write path -- including the first-person check,
    # because the whole point of the abstraction is the third person.
    from .visibility_classifier import classify_visibility
    verdict = classify_visibility(text=method, kind="lesson", hint=None,
                                  admin_allow_commons=True)
    if verdict.get("visibility") != "commons":
        out["reason"] = "the proposal still reads as private"
        out["signals"] = verdict.get("signals") or {}
        return out

    out["shareable"] = True
    out["text"] = method
    out["reason"] = str(parsed.get("reason") or "abstracted")
    return out


__all__ = ["abstract_to_shareable", "provider_available"]
