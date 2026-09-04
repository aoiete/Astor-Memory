@echo off

setlocal enabledelayedexpansion

rem 2026-09-01: removed hardcoded <runtime_dir> — use env var or default ~/.astor

rem This script is for local admin/dev use. For users, astor installs to %USERPROFILE%/.astor by default.

if "%ASTOR_DIR%"=="" set "ASTOR_DIR=%USERPROFILE%\.astor"
if "%PYTHON_BIN%"=="" set "PYTHON_BIN=python"
if "%HERMES_HOME%"=="" set "HERMES_HOME=%LOCALAPPDATA%\hermes"

set PYTHONPATH=%ASTOR_DIR%

rem 2026-08-26 (M3 fix): load OPENROUTER env from hermes .env so

rem astor_llm_extract default primary='openai' works out-of-the-box.

rem OpenRouter provides OpenAI-compatible /chat/completions, so this is

rem the cheapest LLM-extract path that actually works in production.

for /f "usebackq tokens=1,* delims==" %%a in (`findstr /r "OPENROUTER_API_KEY=" "%HERMES_HOME%\.env"`) do (

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

"%PYTHON_BIN%" -u -m astor_memory.server --host 127.0.0.1 --port 7803