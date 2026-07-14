"""Per-install instance UUID. Persisted in a file under ${ONELO_INSTANCE_DIR}
(default ~/.onelo). Override with env ONELO_INSTANCE_ID for containerized deploys
where the filesystem is ephemeral."""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def _instance_dir() -> Path:
    override = os.environ.get("ONELO_INSTANCE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".onelo"


def get_instance_id() -> str:
    env = os.environ.get("ONELO_INSTANCE_ID")
    if env:
        return env
    d = _instance_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "onelo-instance-id"
    if f.exists():
        existing = f.read_text().strip()
        if existing:
            return existing
    new_id = str(uuid.uuid4())
    f.write_text(new_id)
    return new_id
