from __future__ import annotations

import sqlite3

from src.ops.agent_session_metrics import collect_recent_agent_sessions


def test_collect_recent_agent_sessions_empty() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE agent_usage (
            id INTEGER PRIMARY KEY,
            request_id TEXT, user_id TEXT, session_id TEXT,
            endpoint TEXT, model_id TEXT, phase TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            cost_usd REAL, latency_ms INTEGER, status TEXT,
            created_at TEXT
        );
        CREATE TABLE agent_messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
            content_text TEXT, parts_json TEXT, created_at TEXT
        );
        CREATE TABLE chat_titles (
            session_id TEXT PRIMARY KEY, platform TEXT, user_id TEXT,
            title TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    out = collect_recent_agent_sessions(conn, limit=10, days=7)
    assert out["available"] is True
    assert out["sessions"] == []


def test_phase_token_usage_grouped() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE agent_usage (
            id INTEGER PRIMARY KEY, request_id TEXT, user_id TEXT, session_id TEXT,
            endpoint TEXT, model_id TEXT, phase TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            cost_usd REAL, latency_ms INTEGER, status TEXT, created_at TEXT
        );
        CREATE TABLE agent_messages (
            id TEXT, session_id TEXT, role TEXT, content_text TEXT,
            parts_json TEXT, created_at TEXT
        );
        CREATE TABLE chat_titles (session_id TEXT PRIMARY KEY, platform TEXT, user_id TEXT, title TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_sessions (
            session_id TEXT PRIMARY KEY, platform TEXT, user_id TEXT,
            metadata_json TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO agent_usage VALUES
        (1,'r1','u1','s1','chat','research-model','research',100,50,1000,200,0.01,100,'ok',strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        (2,'r1','u1','s1','chat','response-model','response',200,80,0,0,0.02,200,'ok',strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        (3,'r1','u1','s1','chat','response-model','aggregate',300,130,1000,200,0.03,300,'ok',strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """
    )
    conn.execute(
        """
        INSERT INTO agent_sessions VALUES
        ('s1','finance','u1','{"params":{"mode":"deep"}}',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """
    )
    out = collect_recent_agent_sessions(conn, limit=10, days=7)
    session = out["sessions"][0]
    assert session["token_usage"]["research"]["input_uncached"] == 100
    assert session["token_usage"]["research"]["cache_read"] == 1000
    assert session["token_usage"]["research"]["model_id"] == "research-model"
    assert session["token_usage"]["response"]["input_uncached"] == 200
    assert session["token_usage"]["response"]["model_id"] == "response-model"
    assert session["mode"] == "deep"
