@echo off

setlocal enabledelayedexpansion

rem 2026-09-01: removed hardcoded D:\AI\Astor-Memory-Runtime — use env var or default ~/.astor

rem This script is for local admin/dev use. For users, astor installs to %USERPROFILE%/.astor by default.

if "%ASTOR_DIR%"=="" set "ASTOR_DIR=%USERPROFILE%\.astor"

set PYTHONPATH=%ASTOR_DIR%

rem 2026-08-26 (M3 fix): load OPENROUTER env from hermes .env so

rem astor_llm_extract default primary='openai' works out-of-the-box.

rem OpenRouter provides OpenAI-compatible /chat/completions, so this is

rem the cheapest LLM-extract path that actually works in production.

for /f "usebackq tokens=1,* delims==" %%a in (`findstr /r "OPENROUTER_API_KEY=" "C:\Users\TheNuts\AppData\Local\hermes\.env"`) do (

    set "OPENROUTER_API_KEY=%%b"

)

rem Echo what we loaded (debug)

echo Loaded OPENROUTER_API_KEY (first 15 chars): !OPENROUTER_API_KEY:~0,15!

set "OPENAI_API_KEY=!OPENROUTER_API_KEY!"

set "OPENAI_BASE_URL=https://openrouter.ai/api/v1"

echo OPENAI_API_KEY first 15 chars: !OPENAI_API_KEY:~0,15!

rem 2026-08-27: pin the LLM model so the 'openai' provider hits OpenRouter

rem with gemini-3.7-flash (cheaper + faster than gpt-4o-mini default).

if "%ASTOR_LLM_MODEL%"=="" set "ASTOR_LLM_MODEL=google/gemini-3.7-flash"

echo ASTOR_LLM_MODEL: !ASTOR_LLM_MODEL!

rem 2026-08-27: enable LLM rerank by default. Toggle ASTOR_RERANK in .env to disable.
if "%ASTOR_RERANK%"=="" set "ASTOR_RERANK=1"
echo ASTOR_RERANK: !ASTOR_RERANK!
rem v1.14.6 (2026-09-08): bm25_weight=0.6 winner (100 query sweep: mrr=0.910 hit=1.000).
rem Pre-reembed winner was 0.4 — embedding model switch changed winner.
rem S6 verified: 100 query sweep, high_bm25_bm25.6 wins mrr=0.910 hit=1.000.
if "%ASTOR_BM25_WEIGHT%"=="" set "ASTOR_BM25_WEIGHT=0.6"
echo ASTOR_BM25_WEIGHT: !ASTOR_BM25_WEIGHT!
rem v1.14.6 (S2 ship): dual-model merge OFF — e5-large 100% coverage, bge-base deleted.
if "%ASTOR_DUAL_MODEL%"=="" set "ASTOR_DUAL_MODEL=0"
echo ASTOR_DUAL_MODEL: !ASTOR_DUAL_MODEL!
rem v1.15.36 (Ship P1.1): enable Hindsight-style mental_models read endpoint by default.
if "%ASTOR_MENTAL_MODELS%"=="" set "ASTOR_MENTAL_MODELS=1"
echo ASTOR_MENTAL_MODELS: !ASTOR_MENTAL_MODELS!
"D:\AI\PY-311\Scripts\pythonw.exe" -u -m astor_memory.server --host 127.0.0.1 --port 7803