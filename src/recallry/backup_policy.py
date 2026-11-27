"""Per-store individual backup settings. Reads never create files."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

from .backup import (
    BACKUP_ERRORS, STAMP, BackupError, atomic_json, snapshot_generation, check_destination,
    directory_lock, manifest, verify,
)

DEFAULTS = {"enabled": True, "frequency": "daily", "retention": 30, "destination": None}
INTERVALS = {"every": 0, "daily": 86400, "weekly": 604800}
MANAGED_NAME = re.compile(r"recallry-backup-\d{8}T\d{12}Z-[0-9a-f]{12}")


def _validate(policy):
    if (not isinstance(policy, dict) or type(policy.get("enabled")) is not bool
            or "destination" not in policy or policy.get("frequency") not in tuple(INTERVALS)
            or type(policy.get("retention")) is not int or policy["retention"] not in (7, 30, 90, 0)):
        raise BackupError("Invalid backup settings.")
    dest = policy.get("destination")
    if dest is not None and (not isinstance(dest, dict) or not isinstance(dest.get("path"), str)
                             or any(type(dest.get(k)) is not int for k in ("device", "inode"))):
        raise BackupError("Invalid backup destination.")


def _read(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)
    if not isinstance(value, dict):
        raise BackupError("Invalid backup settings or status record.")
    return value


def _status(path):
    state = _read(path, {})
    for key in ("last_success", "last_completed", "last_failure", "pending"):
        entry = state.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("at"), str):
            raise BackupError("Invalid backup status timestamp.")
        if datetime.fromisoformat(entry["at"]).tzinfo is None:
            raise BackupError("Backup status requires a time zone.")
        if key in ("last_success", "last_completed") and not isinstance(entry.get("path"), str):
            raise BackupError("Invalid destination recorded in backup status.")
    return state


class PersonalBackups:
    def __init__(self, root, *, default_base=None, support=None):
        self.root = Path(root).resolve()
        support = support or Path.home() / ".local/state/recallry"
        self.default_base = default_base or support / "backups"
        key = hashlib.sha256(str(self.root).encode()).hexdigest()
        self.state_dir = support / "backup-settings" / key

    def policy(self):
        value = _read(self.state_dir / "policy.json", DEFAULTS)
        _validate(value)
        return value

    def info(self):
        policy = self.policy()
        status_path = self.state_dir / "status.json"
        status = _status(status_path)
        if status.get("pending"):
            try:
                with directory_lock(self.state_dir, shared=True, wait=False):
                    status = _status(status_path)
                    if status.get("pending"):
                        status["last_failure"] = {"at": status["pending"]["at"],
                            "message": "Previous backup interrupted. Retry on the next write."}
            except BlockingIOError:
                status["running"] = True
        dest = policy["destination"]
        return {**policy, "destination": dest["path"] if dest else None,
                "base": str(dest["path"] if dest else self.default_base), **status}

    @contextmanager
    def _lock(self):
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        with directory_lock(self.state_dir):
            yield

    def configure(self, *, enabled, frequency, retention, destination="keep"):
        with self._lock():
            value = self.policy()
            if isinstance(destination, Path):
                path = self._custom_destination(destination)
                if not path.is_dir():
                    raise BackupError("Choose an existing directory.")
                stat = path.stat()
                value["destination"] = {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}
            elif destination is None:
                value["destination"] = None
            elif destination != "keep":
                raise BackupError("Invalid backup destination.")
            value.update(enabled=enabled, frequency=frequency, retention=retention)
            _validate(value)
            atomic_json(self.state_dir / "policy.json", value)

    def _custom_destination(self, path):
        path = check_destination(path, self.root)
        if path.is_relative_to(self.state_dir.parent.resolve()):
            raise BackupError("Choose a destination outside the backup settings directory.")
        return path

    def _base(self, policy):
        dest = policy["destination"]
        if dest:
            base = self._custom_destination(Path(dest["path"]))
            if not base.is_dir() or (base.stat().st_dev, base.stat().st_ino) != (dest["device"], dest["inode"]):
                raise BackupError("The backup destination is unavailable or has been replaced.")
        else:
            base = check_destination(self.default_base, self.root)
            base.mkdir(parents=True, mode=0o700, exist_ok=True)
        return base

    def take(self, reason, *, snapshot=snapshot_generation):
        if reason not in ("write", "manual", "before"):
            raise ValueError("unknown backup trigger")
        with self._lock():
            status_path = self.state_dir / "status.json"
            status = _status(status_path)
            now = datetime.now(timezone.utc)
            try:
                policy = self.policy()
                if reason == "write" and not policy["enabled"]:
                    return None
                base = self._base(policy)
                with directory_lock(base):
                    if reason == "write" and self._recent(policy, status, base, now):
                        return None
                    status["pending"] = {"at": now.isoformat(), "reason": reason}
                    atomic_json(status_path, status)
                    result = snapshot(self.root, base)
                    status.pop("pending")
                    completed = datetime.now(timezone.utc)
                    last = {"at": completed.isoformat(), "path": result["path"], "reason": reason}
                    status.update(last_success=last, last_failure=None)
                    if reason != "before":
                        status["last_completed"] = last
                    atomic_json(status_path, status)
                    self._prune(base, policy["retention"], completed, result["path"])
                return result
            except BACKUP_ERRORS as exc:
                status.pop("pending", None)
                status["last_failure"] = {"at": now.isoformat(), "message": str(exc)}
                atomic_json(status_path, status)
                raise

    @staticmethod
    def _recent(policy, status, base, now):
        last = status.get("last_completed")
        if not last or status.get("last_failure") or status.get("pending"):
            return False
        path = Path(last["path"])
        elapsed = (now - datetime.fromisoformat(last["at"])).total_seconds()
        return (path.parent == base and (path / "manifest.json").is_file()
                and 0 <= elapsed < INTERVALS[policy["frequency"]])

    def _prune(self, base, days, now, newest):
        if days == 0:
            return
        owned = []
        for path in base.glob("recallry-backup-*"):
            if not MANAGED_NAME.fullmatch(path.name):
                continue
            try:
                value = manifest(path)
                stamp = datetime.strptime(value["created_at"], STAMP).replace(tzinfo=timezone.utc)
                if value["source_root"] == str(self.root) and path.name.startswith("recallry-backup-" + value["created_at"] + "-"):
                    owned.append((stamp, path))
            except BACKUP_ERRORS:
                continue
        cutoff = now - timedelta(days=days)
        if not any(stamp < cutoff for stamp, _ in owned):
            return
        # Keep the new verified copy and one independently verified fallback.
        # Corrupt/unrecognized folders are never used to justify deleting a good one.
        good = 1
        for stamp, path in sorted(owned, reverse=True):
            if str(path) == newest or (good >= 2 and stamp >= cutoff):
                continue
            try:
                verify(path)
            except BACKUP_ERRORS:
                continue
            if good >= 2 and stamp < cutoff:
                shutil.rmtree(path)
            good += 1
