"""写入闸门: 执行日志不该进长期记忆 (v1.16.91).

背景: 治理体检 (tests/governance_audit.py) 统计出 admin 层有 74 条活 fact
的正文就是一条命令 —— "python.exe -c \\"", "powershell -NoProfile
-Command \\"", 常常截断在引号中间。根因是写入门槛只用**长度**打分:
超过 200 字符的 neutral/success 内容拿 0.6 分, 而 0.5 分以上就进 public
tier。一段 shell 转录因此被当成知识存了下来。

修法: `astor_should_skip_auto_observe` 增加 "execution_log" 判据。

**判据是位置不是词汇** —— 真正的 fact 先说规则, 后提命令
("发微信的正确路径是 scripts/send_to_platform.py weixin --to <chat_id>"),
执行日志以命令开头。

这些测试里最重要的两条是**反例**: 第一版闸门把 "<ASTOR_RUNTIME_DIR> 优先" 和
"make pocket 主仓位 IDLE" 也判成噪声, 那两条是有价值的知识。真实语料
6640 条上的验收标准: 命中 36 条全是命令转录, 且以路径 / git / make /
curl / uv 开头的 fact 一条都不能被拦。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.forge.extractor import (  # noqa: E402
    _EXEC_LOG_START,
    astor_should_skip_auto_observe,
)


def _skip(text):
    return astor_should_skip_auto_observe(text)[0]


def _reason(text):
    return astor_should_skip_auto_observe(text)[1]


# ---------------------------------------------------------------- 噪声样本
def test_venv_python_invocation_is_skipped():
    text = ('/c/Users/someone/AppData/Local/hermes/hermes-agent/.venv/Scripts/'
            'python.exe -c "import sys; print(sys.executable)"')
    assert _skip(text)
    assert _reason(text) == "execution_log"


def test_lesson_tagged_powershell_is_skipped():
    text = '[LESSON] powershell -NoProfile -Command "Get-Process | Where-Object"'
    assert _skip(text)
    assert _reason(text) == "execution_log"


def test_powershell_command_variant_is_skipped():
    text = '[LESSON] powershell -Command "Stop-Process -Id 51736 -Force"'
    assert _skip(text)
    assert _reason(text) == "execution_log"


def test_python_heredoc_is_skipped():
    assert _skip("[LESSON] python - <<'EOF'\nprint(\"x\")\nEOF")


def test_drive_path_to_executable_is_skipped():
    assert _skip('[LESSON] <ASTOR_RUNTIME_DIR> -c "import os"')


# ---------------------------------------------------------------- 反例
def test_drive_path_rule_is_kept():
    """A path IS knowledge here. This one killed the first version."""
    text = ("<ASTOR_RUNTIME_DIR> 优先 + Open vault > 合 wiki (locked 2026-08-01): "
            "所有 AI 相关数据 / 工具 / 安装 / wikis 优先 <ASTOR_RUNTIME_DIR>")
    assert not _skip(text)
    assert _reason(text) == ""


def test_make_status_line_is_kept():
    """A sentence that begins with a command word is still a sentence."""
    text = "make pocket 主仓位处于 IDLE 状态,$1500 USD,无 active entry,8 天没 check"
    assert not _skip(text)


def test_git_commit_description_is_kept():
    text = ("git commit 5320290 是 runtime v1.11 LLM extract, "
            "8c79deb 是 public-source PII cleanup")
    assert not _skip(text)


def test_uv_behaviour_note_is_kept():
    text = ("uv venv creates virtual environments without pip by default; "
            "packages must be installed with uv pip install")
    assert not _skip(text)


def test_source_vs_runtime_paths_are_kept():
    text = ("<ASTOR_RUNTIME_DIR> 是 source 目录,<ASTOR_RUNTIME_DIR> "
            "是 runtime 目录,改 server.py 必须 sync")
    assert not _skip(text)


def test_rule_that_mentions_a_command_later_is_kept():
    text = ("Hermes 发送微信/wechat 通知的正确路径:scripts/send_to_platform.py "
            "weixin --to <chat_id>,不走 agent 自己拼 iLink API")
    assert not _skip(text)


# ---------------------------------------------------------------- 既有行为
def test_greetings_still_skipped():
    # long enough to get past the length check, so the prefix is what fires
    assert _skip("ok thanks a lot for your help, that was really useful")
    assert _reason("ok thanks a lot for your help, that was really useful") == "noise_prefix"


def test_empty_and_short_still_skipped():
    assert _reason("") == "empty"
    assert _reason("hi") == "too_short"


def test_normal_knowledge_is_kept():
    text = ("用户偏好:先 sync source repo 再跑测试,发现优化点就立即迭代, "
            "朝 70% 目标持续推进")
    assert not _skip(text)


def test_exec_log_pattern_is_anchored_at_the_start():
    """The same command later in a sentence is fine."""
    assert not _EXEC_LOG_START.match(
        "we run powershell -Command for that")
    assert _EXEC_LOG_START.match("powershell -Command \"x\"")
