@echo off
setlocal enabledelayedexpansion
set ASTOR_DIR=<runtime_dir>
set PYTHONPATH=<runtime_dir>
rem 2026-08-26 (M3 fix): load OPENROUTER env from hermes .env so
rem astor_llm_extract default primary='openai' works out-of-the-box.
rem OpenRouter provides OpenAI-compatible /chat/completions, so this is
rem the cheapest LLM-extract path that actually works in production.
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /r "OPENROUTER_API_KEY=" "<home_dir>AppData\Local\hermes\.env"`) do (
    set "OPENROUTER_API_KEY=%%b"
)
rem Echo what we loaded (debug)
echo Loaded OPENROUTER_API_KEY (first 15 chars): !OPENROUTER_API_KEY:~0,15!
set "OPENAI_API_KEY=!OPENROUTER_API_KEY!"
set "OPENAI_BASE_URL=https://openrouter.ai/api/v1"
echo OPENAI_API_KEY first 15 chars: !OPENAI_API_KEY:~0,15!
rem 2026-08-27: enable LLM rerank by default. Toggle ASTOR_RERANK in .env to disable.
if "%ASTOR_RERANK%"=="" set "ASTOR_RERANK=1"
echo ASTOR_RERANK: !ASTOR_RERANK!
"<home_dir>AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe" -u -m astor_memory.server --host 127.0.0.1 --port 7803