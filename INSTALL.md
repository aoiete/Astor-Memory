# Installing Astor-Memory

This covers the parts of installation that fail *silently* — where a command
exits without a useful message and the only symptom is that something else
didn't work. Every failure described here has actually occurred.

---

## Quick start

```bash
git clone https://github.com/<repo-owner>/Astor-Memory.git
cd Astor-Memory

uv venv --python 3.11
# Windows:      .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate

uv pip install -e .

am preflight        # will this host run astor at all?
am doctor           # full health check
python -m astor_memory.server --host 127.0.0.1 --port 7803
```

**Run `am preflight` before anything else.** It takes about a second and it
catches the one failure that otherwise looks like astor being broken.

From PyPI instead of source:

```bash
pip install astor-memory
am init
am admin lock --user-id=admin
```

---

## Windows: the Visual C++ runtime

### Why this section exists

Astor semantic search runs through [onnxruntime], a native library.
**onnxruntime 1.21 and newer require the Visual C++ 2015-2022 Redistributable
(x64).** pip cannot install this — it is a Windows system component, and on a
machine that predates the VS2022 toolchain, importing onnxruntime does not
raise an exception. It faults:

```
$ python -c "import onnxruntime"
Segmentation fault          # exit 139, no traceback, no message

$ python -m astor_memory.server --port 7803
Segmentation fault          # dies before binding the port
```

No traceback appears because the crash is **inside a DLL** — control never
returns to the interpreter, so there is nothing for Python to catch and nothing
to print. From the outside it is indistinguishable from astor segfaulting.

Downgrading onnxruntime does not reliably fix it. The wheel is not the problem;
the runtime it loads against is. This has been reported against onnxruntime
1.22 through 1.31, and across Python 3.8 through 3.13 — see
[microsoft/onnxruntime#16116].

### Check

```bash
am preflight
```

```
[astor] host preflight
   python     : 3.11.16
   platform   : Windows 11 AMD64
   VC++ runtime: 14.51.36247.0  [ok]
   onnxruntime : 1.29.0  [ok]
   numpy       : 2.4.6  [ok]

[READY] the host can run astor. Next: `am doctor` for full health.
```

A current runtime reads **14.31 or newer** (the VS2022 generation). Anything
below that — the VS2019-era 14.2x, in particular 14.28.x — produces the crash
above.

### Fix it automatically

```bash
am preflight --fix
```

This downloads Microsoft's official x64 redistributable, checks that the
download is a real installer rather than an error page, runs it, and re-tests.
It touches nothing else — not astor, not your data directory, not the Python
environment. It will prompt for administrator rights (UAC).

Expect it to finish by asking you to **restart Windows**. That is not optional:
running processes keep the old DLLs mapped, so onnxruntime cannot import until
everything has restarted. `am preflight` says so rather than leaving you to
wonder whether it worked.

### Fix it manually

If you would rather not grant elevation to the CLI:

1. Download the **x64** package from
   <https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170>
   (the x86 build is a common wrong download and fails differently)
2. Install it
3. Restart Windows
4. `am preflight` again

Verify at any time:

```powershell
(Get-Item $env:SystemRoot\System32\vcruntime140_1.dll).VersionInfo.FileVersion
```

---

## If the runtime cannot be updated

Some managed environments lock the redistributable. `astor_memory.server` then
starts in **degraded mode** rather than refusing to start:

```
[DEGRADED] the native embedding stack is unavailable on this host.

  reason   : vcruntime140_1.dll is 14.28.29325.2; onnxruntime >= 1.21 needs
             14.31+ (VS2022 generation)
  effect   : semantic recall is disabled. Everything else works — the bus,
             exact lookup, the ACL, all SQLite tiers, the REST API.
```

In that mode astor keeps running and `/v1/health` reports the condition. Facts
can still be written and read by exact lookup; only natural-language queries
return nothing. It is strictly better than no server, but it is a workaround —
fixing the runtime restores full function.

---

## Linux and macOS

No native-runtime prerequisite; onnxruntime ships self-contained wheels.

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .
am preflight
am doctor
```

If `am preflight` shows onnxruntime failing to import there, the usual cause is
a partial wheel:

```bash
pip install --force-reinstall --no-cache-dir onnxruntime
```

---

## Exit codes

Both commands use distinct codes so a script can branch on them:

| Code | Meaning |
|------|---------|
| 0 | ready / healthy |
| 1 | `am doctor` only — running, but something needs attention |
| 2 | **blocked** — the Visual C++ runtime is too old; astor cannot start |

```bash
am preflight --fix || {
  echo "astor cannot start on this host; see above" >&2
  exit 1
}
```

---

## Troubleshooting

**`am` command not found**
The environment `Scripts`/`bin` directory is not on `PATH`. Use
`python -m astor_memory.cli.main preflight` as a fallback.

**Server exits immediately with no output**
Run it in the foreground and read the whole output:
`python -m astor_memory.server --host 127.0.0.1 --port 7803`. `am preflight`
reports the runtime version before the server attempts to import onnxruntime,
so checking there first preserves that diagnosis.

**`ModuleNotFoundError` from any astor command**
A dependency is mis-declared for a clean install. This is a packaging bug —
please report it with the exact package name.

**Port 7803 already in use**

```bash
# Linux/macOS
lsof -i :7803
# Windows
netstat -ano | findstr :7803
```

Two astor processes sharing one data directory corrupt each other SQLite
state. Stop the old one first.

**`database is locked`**
Almost always two processes on the same `--astor-dir`. Same check as above.

**Python version**
3.11 is what astor is developed and tested on. 3.10 and 3.12 work; 3.13 is
untested and fastembed onnxruntime pin differs there.

---

## Reporting a problem

Include these — together they are almost always sufficient:

```bash
am preflight
python -V
python -m pip list | grep -Ei 'onnxruntime|numpy|fastembed'
am doctor
```

On Windows also include:

```powershell
(Get-Item $env:SystemRoot\System32\vcruntime140_1.dll).VersionInfo.FileVersion
```

`/v1/health` reports the data directory by **name only**, never the full path,
so its output is safe to paste.

## See also

- [QUICKSTART.md](QUICKSTART.md) — 5-minute end-to-end demo
- [README.md](README.md) — overview + concepts
- [docs/architecture.md](docs/architecture.md) — 3-store x 3-tier design deep dive

[onnxruntime]: https://onnxruntime.ai/
[microsoft/onnxruntime#16116]: https://github.com/microsoft/onnxruntime/issues/16116
