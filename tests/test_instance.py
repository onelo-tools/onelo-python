"""Tests for instance_id helper — file persistence + env override."""
import os
import re
import uuid

from onelo._instance import get_instance_id


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_creates_new_uuid_on_first_call(monkeypatch, tmp_path):
    monkeypatch.delenv("ONELO_INSTANCE_ID", raising=False)
    monkeypatch.setenv("ONELO_INSTANCE_DIR", str(tmp_path))
    iid = get_instance_id()
    assert UUID_RE.match(iid)


def test_persists_across_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("ONELO_INSTANCE_ID", raising=False)
    monkeypatch.setenv("ONELO_INSTANCE_DIR", str(tmp_path))
    a = get_instance_id()
    b = get_instance_id()
    assert a == b


def test_env_override_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("ONELO_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("ONELO_INSTANCE_ID", "fixed-id-from-env")
    assert get_instance_id() == "fixed-id-from-env"


def test_creates_dir_if_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ONELO_INSTANCE_ID", raising=False)
    nested = tmp_path / "nested" / "deeper"
    monkeypatch.setenv("ONELO_INSTANCE_DIR", str(nested))
    iid = get_instance_id()
    assert UUID_RE.match(iid)
    assert nested.exists()
