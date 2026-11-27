"""Opt-in unattended review in the existing store config (no new database)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import tempfile
import tomllib
import uuid

from .db import RecallryError

CONSENT_VERSION = "unattended-review-v1-1"
PROVIDERS = ("claude", "codex")


@dataclass(frozen=True)
class ReviewSettings:
    enabled: bool = False
    threshold: int = 5
    provider: str = "claude"
    consent: str = ""
    epoch: str = ""

    @property
    def active(self):
        return self.enabled and self.consent == CONSENT_VERSION and bool(self.epoch)


def load_settings(root: Path) -> ReviewSettings:
    raw = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    section = raw.get("automatic_review", {})
    if not isinstance(section, dict) or set(section) - set(ReviewSettings.__dataclass_fields__):
        raise RecallryError("invalid automatic review settings")
    settings = ReviewSettings(**section)
    if (type(settings.enabled) is not bool or type(settings.threshold) is not int
            or settings.threshold < 1 or settings.provider not in PROVIDERS
            or not isinstance(settings.consent, str) or not isinstance(settings.epoch, str)):
        raise RecallryError("invalid automatic review settings")
    return settings


def project_writable(project_root: Path) -> bool:
    """Machine-readable veto, additional to the router's inherited no-write gate."""
    if os.environ.get("RECALLRY_READONLY"):
        return False
    path = project_root / ".recallry.toml"
    if path.is_symlink():
        return False
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return raw.get("knowledge_writes", True) is True


@contextmanager
def store_lock(root: Path, name: str, *, blocking=True):
    # OS advisory locks survive atomic config replacement, release on crashes,
    # and cover separate CLI processes. These files contain no Knowledge.
    try:
        import fcntl
    except ImportError as exc:
        raise RecallryError("automatic review locking is unavailable on this platform") from exc
    fd = os.open(root / "data" / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def save_settings(root: Path, *, enabled, threshold, provider, consent, expected_epoch):
    if os.environ.get("RECALLRY_READONLY"):
        raise RecallryError("automatic review settings unavailable in inherited read-only mode")
    if (type(enabled) is not bool or type(threshold) is not int or threshold < 1
            or provider not in PROVIDERS or type(consent) is not bool):
        raise RecallryError("review threshold must be a positive integer; select a supported provider")
    if enabled and not consent:
        raise RecallryError("explicit unattended transmission consent is required")
    with store_lock(root, ".automatic-review-settings.lock"):
        before = load_settings(root)
        if before.epoch != expected_epoch:
            raise RecallryError("review settings changed; reopen settings")
        after = ReviewSettings(enabled, threshold, provider, CONSENT_VERSION if enabled else "", uuid.uuid4().hex)
        path = root / "config.toml"
        if path.is_symlink():
            raise RecallryError("config must not be a symbolic link")
        original = path.read_text(encoding="utf-8")
        # Preserve every other existing setting and comment. Only our own table
        # is replaced; dotted/inline alternate encodings are refused, not erased.
        pattern = r"(?ms)^\[automatic_review\][ \t]*(?:#[^\n]*)?\n.*?(?=^\[|\Z)"
        clean, count = re.subn(pattern, "", original)
        if "automatic_review" in tomllib.loads(clean) or count > 1:
            raise RecallryError("unsupported automatic_review table layout")
        values = asdict(after)
        import json
        section = "\n[automatic_review]\n" + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in values.items())
        fd, temporary = tempfile.mkstemp(prefix=".review-config-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(clean.rstrip() + "\n" + section)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return after
