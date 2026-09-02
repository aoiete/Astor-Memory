"""
Tests for astor_memory.forge.pattern_detector.

v1.13.0 (2026-09-02): initial ship.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from astor_memory.forge.pattern_detector import (
    astor_detect_success_pattern,
    astor_score_success_strength,
    astor_count_similar_success_facts,
    astor_promote_recurring_success,
    DEFAULT_RECURRENCE_THRESHOLD,
)


# --- astor_detect_success_pattern ---

def test_detect_chinese_success_phrase_搞定了():
    assert astor_detect_success_pattern("搞定了") is True


def test_detect_chinese_success_phrase_成功了():
    assert astor_detect_success_pattern("成功了") is True


def test_detect_chinese_success_phrase_跑通了():
    assert astor_detect_success_pattern("跑通了") is True


def test_detect_chinese_success_phrase_in_sentence():
    assert astor_detect_success_pattern("这个 cron 配置对了，今天跑通了") is True


def test_detect_chinese_zhe_zhao():
    assert astor_detect_success_pattern("这招可以") is True


def test_detect_english_this_works():
    assert astor_detect_success_pattern("this works perfectly") is True


def test_detect_english_shipped():
    assert astor_detect_success_pattern("shipped it yesterday") is True


def test_detect_english_nailed_it():
    assert astor_detect_success_pattern("nailed it") is True


def test_detect_english_figured_out():
    assert astor_detect_success_pattern("I figured out the bug") is True


def test_detect_english_got_it_working():
    assert astor_detect_success_pattern("got it working") is True


def test_detect_english_that_worked():
    assert astor_detect_success_pattern("that worked") is True


def test_detect_no_match_negative_sentiment():
    assert astor_detect_success_pattern("今天天气不太好") is False


def test_detect_no_match_general_question():
    assert astor_detect_success_pattern("how do I fix this?") is False


def test_detect_no_match_error_pattern():
    assert astor_detect_success_pattern("failed again, what went wrong?") is False


def test_detect_empty_string():
    assert astor_detect_success_pattern("") is False


def test_detect_whitespace_only():
    assert astor_detect_success_pattern("   \n\t  ") is False


def test_detect_case_insensitive():
    assert astor_detect_success_pattern("THIS WORKS") is True
    assert astor_detect_success_pattern("This Works") is True


# --- astor_score_success_strength ---

def test_score_zero_no_match():
    assert astor_score_success_strength("天气不太好") == 0.0


def test_score_half_single_phrase():
    assert astor_score_success_strength("搞定了") == 0.5


def test_score_three_quarters_two_phrases():
    assert astor_score_success_strength("搞定了 this works") == 0.75


def test_score_full_three_phrases():
    text = "搞定了 this works shipped"
    assert astor_score_success_strength(text) == 1.0


def test_score_empty():
    assert astor_score_success_strength("") == 0.0


def test_score_whitespace():
    assert astor_score_success_strength("  \n  ") == 0.0


# --- astor_count_similar_success_facts (with temp DB) ---

def _make_temp_bus_db() -> Path:
    """Create a temp bus.db with the minimal memory_canonical + audit_log schema."""
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "bus.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            event_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            confidence REAL NOT NULL DEFAULT 0.7,
            importance REAL NOT NULL DEFAULT 0.5,
            tags TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            keywords TEXT NOT NULL DEFAULT '[]',
            context TEXT NOT NULL DEFAULT '',
            promoted_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            promoted_by TEXT,
            last_confirmed_at DATETIME,
            last_confirmed_session TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            tombstoned INTEGER NOT NULL DEFAULT 0,
            tombstoned_at DATETIME,
            expires_at DATETIME,
            scene TEXT NOT NULL DEFAULT 'casual',
            revision INTEGER NOT NULL DEFAULT 1,
            parent_revision_id INTEGER,
            superseded_by INTEGER,
            origin_session_id TEXT,
            verdict TEXT NOT NULL DEFAULT 'settled',
            scope_type TEXT NOT NULL DEFAULT 'long_term',
            user_id TEXT,
            session_id TEXT,
            tier TEXT NOT NULL DEFAULT 'public',
            stable_id TEXT UNIQUE
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            event TEXT NOT NULL,
            actor TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            old_state TEXT,
            new_state TEXT,
            reason TEXT,
            metadata TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _insert_fact(
    db_path: Path,
    content: str,
    tier: str = 'private',
    user_id: str | None = None,
    tags: str = '[]',
    metadata: str = '{}',
) -> int:
    conn = sqlite3.connect(str(db_path))
    # Use unique candidate_id + event_id per insert to avoid UNIQUE conflicts.
    cur = conn.execute(
        "SELECT COALESCE(MAX(candidate_id), 0) + 1 FROM memory_canonical"
    )
    next_cid = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT COALESCE(MAX(event_id), 0) + 1 FROM memory_canonical"
    )
    next_eid = cur.fetchone()[0]
    cur = conn.execute(
        """INSERT INTO memory_canonical
           (candidate_id, event_id, namespace, content, tags, metadata, tier, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (next_cid, next_eid, 'test', content, tags, metadata, tier, user_id),
    )
    fact_id = cur.lastrowid
    conn.commit()
    conn.close()
    return fact_id


def test_count_similar_success_facts_empty_db(tmp_path):
    db_path = _make_temp_bus_db()
    assert astor_count_similar_success_facts(
        "搞定了 cron 配置",
        tier='public',
        db_path=db_path,
    ) == 0


def test_count_similar_success_facts_with_match(tmp_path):
    db_path = _make_temp_bus_db()
    _insert_fact(
        db_path,
        "搞定了 cron 配置，今天跑通了",
        tier='public',
        tags=json.dumps(['outcome:success']),
    )
    _insert_fact(
        db_path,
        "搞定了 cron 配置，部署成功",
        tier='public',
        tags=json.dumps(['outcome:success']),
    )
    count = astor_count_similar_success_facts(
        "搞定了 cron 配置，今天跑通了",
        tier='public',
        db_path=db_path,
    )
    assert count == 2


def test_count_ignores_non_success_tags(tmp_path):
    db_path = _make_temp_bus_db()
    _insert_fact(
        db_path,
        "搞定了 cron 配置",
        tier='public',
        tags='[]',
    )
    count = astor_count_similar_success_facts(
        "搞定了 cron 配置",
        tier='public',
        db_path=db_path,
    )
    assert count == 0


def test_count_ignores_different_tier(tmp_path):
    db_path = _make_temp_bus_db()
    _insert_fact(
        db_path,
        "搞定了 cron 配置",
        tier='private',
        user_id='alice',
        tags=json.dumps(['outcome:success']),
    )
    count = astor_count_similar_success_facts(
        "搞定了 cron 配置",
        tier='public',
        db_path=db_path,
    )
    assert count == 0


def test_count_ignores_tombstoned(tmp_path):
    db_path = _make_temp_bus_db()
    fact_id = _insert_fact(
        db_path,
        "搞定了 cron 配置",
        tier='public',
        tags=json.dumps(['outcome:success']),
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE memory_canonical SET tombstoned = 1 WHERE id = ?",
        (fact_id,),
    )
    conn.commit()
    conn.close()

    count = astor_count_similar_success_facts(
        "搞定了 cron 配置",
        tier='public',
        db_path=db_path,
    )
    assert count == 0


def test_count_empty_content():
    assert astor_count_similar_success_facts("", tier='public') == 0
    assert astor_count_similar_success_facts("   ", tier='public') == 0


# --- astor_promote_recurring_success ---

def test_promote_idempotent_already_public(tmp_path):
    db_path = _make_temp_bus_db()
    fact_id = _insert_fact(
        db_path,
        "搞定了 cron 配置",
        tier='public',
        tags=json.dumps(['outcome:success']),
    )
    result = astor_promote_recurring_success(fact_id, db_path=db_path, actor='test')
    assert result is False
    conn = sqlite3.connect(str(db_path))
    tier = conn.execute("SELECT tier FROM memory_canonical WHERE id = ?", (fact_id,)).fetchone()[0]
    conn.close()
    assert tier == 'public'


def test_promote_requires_threshold(tmp_path):
    db_path = _make_temp_bus_db()
    fact_id = _insert_fact(
        db_path,
        "搞定了 cron 配置今天跑通了",
        tier='private',
        tags=json.dumps(['outcome:success']),
    )
    # Only 1 fact (self) -> below threshold of 3
    result = astor_promote_recurring_success(fact_id, db_path=db_path, actor='test')
    assert result is False
    conn = sqlite3.connect(str(db_path))
    tier = conn.execute("SELECT tier FROM memory_canonical WHERE id = ?", (fact_id,)).fetchone()[0]
    conn.close()
    assert tier == 'private'


def test_promote_at_threshold_succeeds(tmp_path):
    db_path = _make_temp_bus_db()
    for content in [
        "搞定了 cron 配置今天跑通了",
        "搞定了 cron 配置部署成功",
        "搞定了 cron 配置跑通验证",
    ]:
        _insert_fact(
            db_path,
            content,
            tier='private',
            tags=json.dumps(['outcome:success']),
        )

    conn = sqlite3.connect(str(db_path))
    target_id = conn.execute(
        "SELECT id FROM memory_canonical WHERE content LIKE '%跑通了%' LIMIT 1"
    ).fetchone()[0]
    conn.close()

    result = astor_promote_recurring_success(
        target_id,
        db_path=db_path,
        actor='test',
        threshold=3,
    )
    assert result is True

    conn = sqlite3.connect(str(db_path))
    tier = conn.execute("SELECT tier FROM memory_canonical WHERE id = ?", (target_id,)).fetchone()[0]
    conn.close()
    assert tier == 'public'


def test_promote_writes_audit_log(tmp_path):
    db_path = _make_temp_bus_db()
    for content in [
        "搞定了 cron 配置今天跑通了",
        "搞定了 cron 配置部署成功",
        "搞定了 cron 配置跑通验证",
    ]:
        _insert_fact(
            db_path,
            content,
            tier='private',
            tags=json.dumps(['outcome:success']),
        )

    conn = sqlite3.connect(str(db_path))
    target_id = conn.execute(
        "SELECT id FROM memory_canonical WHERE content LIKE '%跑通了%' LIMIT 1"
    ).fetchone()[0]
    conn.close()

    result = astor_promote_recurring_success(
        target_id,
        db_path=db_path,
        actor='admin:admin',
        threshold=3,
    )
    assert result is True

    conn = sqlite3.connect(str(db_path))
    audit = conn.execute(
        """SELECT event, actor, target_id, old_state, new_state, reason
           FROM audit_log WHERE event = 'promote_recurring_success'"""
    ).fetchone()
    conn.close()
    assert audit is not None
    event, actor, target_id_db, old, new, reason = audit
    assert event == 'promote_recurring_success'
    assert actor == 'admin:admin'
    assert target_id_db == str(target_id)
    old_dict = json.loads(old)
    new_dict = json.loads(new)
    assert old_dict == {'tier': 'private'}
    assert new_dict['tier'] == 'public'
    assert new_dict['threshold'] == 3
    assert new_dict['recurrence_count'] == 3


def test_promote_fact_not_found(tmp_path):
    db_path = _make_temp_bus_db()
    result = astor_promote_recurring_success(999, db_path=db_path, actor='test')
    assert result is False


def test_promote_missing_db():
    result = astor_promote_recurring_success(
        1, db_path='/nonexistent/bus.db', actor='test'
    )
    assert result is False


def test_default_threshold_is_3():
    assert DEFAULT_RECURRENCE_THRESHOLD == 3