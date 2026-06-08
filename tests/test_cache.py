"""Unit tests for the shared JSON cache module."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.cache import age_hours, is_fresh, load_json, now_iso, save_json


def test_now_iso_round_trip():
    stamped = now_iso()
    parsed = datetime.fromisoformat(stamped)
    assert parsed.tzinfo is not None
    assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5


def test_age_hours_recent():
    two_hours_ago = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="seconds")
    assert 1.9 < age_hours(two_hours_ago) < 2.1


def test_age_hours_naive_assumed_utc():
    naive = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).replace(tzinfo=None).isoformat()
    assert 0.9 < age_hours(naive) < 1.1


def test_age_hours_unparseable():
    assert age_hours("not-a-date") == float("inf")
    assert age_hours(None) == float("inf")
    assert age_hours("") == float("inf")


def test_load_json_missing(tmp_path):
    assert load_json(tmp_path / "nope.json") is None


def test_load_json_corrupt(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid")
    assert load_json(path) is None


def test_load_json_non_dict(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert load_json(path) is None


def test_save_json_creates_parents(tmp_path):
    path = tmp_path / "deep" / "nested" / "out.json"
    save_json(path, {"a": 1})
    assert path.exists()
    assert json.loads(path.read_text()) == {"a": 1}


def test_save_json_trailing_newline(tmp_path):
    path = tmp_path / "out.json"
    save_json(path, {"a": 1}, trailing_newline=True)
    assert path.read_text().endswith("\n")
    save_json(path, {"a": 1}, trailing_newline=False)
    assert not path.read_text().endswith("\n")


def test_save_json_sort_keys(tmp_path):
    path = tmp_path / "sorted.json"
    save_json(path, {"b": 1, "a": 2}, sort_keys=True)
    body = path.read_text()
    assert body.index('"a"') < body.index('"b"')


def test_is_fresh_within_ttl(tmp_path):
    path = tmp_path / "c.json"
    save_json(path, {"fetched_at": now_iso(), "data": [1]})
    assert is_fresh(path, ttl_hours=1.0)


def test_is_fresh_expired(tmp_path):
    path = tmp_path / "c.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    save_json(path, {"fetched_at": old})
    assert not is_fresh(path, ttl_hours=1.0)


def test_is_fresh_missing(tmp_path):
    assert not is_fresh(tmp_path / "absent.json", ttl_hours=999.0)


def test_is_fresh_custom_field(tmp_path):
    path = tmp_path / "c.json"
    save_json(path, {"saved_at": now_iso()})
    assert is_fresh(path, ttl_hours=1.0, fetched_at_field="saved_at")
    assert not is_fresh(path, ttl_hours=1.0)  # default field absent
