"""Publish independently verified snapshots of the public Recallry store."""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .metrics import MetricsStore
from .db import RecallryError, Database

STORE_FILES = ("recallry.db", "metrics.db")
STAMP = "%Y%m%dT%H%M%S%fZ"
BACKUP_ERRORS = (OSError, ValueError, sqlite3.Error)


class BackupError(ValueError):
    pass


def check_destination(base: Path, root: Path) -> Path:
    if not base.is_absolute():
        raise BackupError("The backup destination must be an absolute path.")
    base, root = base.resolve(), root.resolve()
    if base.is_relative_to(root) or root.is_relative_to(base):
        raise BackupError("Keep backups outside the Knowledge store and its ancestors.")
    if any(parent.name.startswith(("recallry-backup-", ".partial-")) for parent in (base, *base.parents)):
        raise BackupError("Choose a directory outside existing backup generations.")
    if any((parent / "data/recallry.db").is_file() for parent in (base, *base.parents)):
        raise BackupError("Choose a directory outside all Knowledge stores.")
    if any((parent / ".git").exists() for parent in (base, *base.parents)):
        raise BackupError("Choose a directory outside Git repositories.")
    return base


@contextmanager
def directory_lock(path: Path, *, shared=False, wait=True):
    if os.name == "nt":
        import msvcrt
        # A stable file on Windows supplies the byte range lock otherwise taken on the directory.
        lock = path / ".recallry-backup.lock"
        if path.is_symlink() or lock.is_symlink():
            raise BackupError("A backup lock must not be a symbolic link.")
        with lock.open("a+b") as handle:
            handle.seek(0)
            mode = msvcrt.LK_RLCK if shared else msvcrt.LK_LOCK
            if not wait:
                mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except PermissionError as exc:
                if not wait:
                    raise BlockingIOError("Backup is busy") from exc
                raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, mode if wait else mode | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _sync_directory(path):
    if os.name == "nt":
        return  # Opening a directory for fsync is POSIX-specific.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as out:
        tmp = Path(out.name)
        try:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(tmp, path)
            _sync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)


def _open(path: Path, *, immutable=False):
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True)


def _digest(connection):
    digest = hashlib.sha256()
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name")
    for (table,) in tables:
        digest.update(table.encode())
        quoted = '"' + table.replace('"', '""') + '"'
        rows = sorted(json.dumps(row, ensure_ascii=False, default=repr)
                      for row in connection.execute(f"SELECT * FROM {quoted}"))
        for row in rows:
            digest.update(row.encode())
    digest.update(str(connection.execute("PRAGMA user_version").fetchone()[0]).encode())
    return digest.hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(data):
    members = {}
    for name in STORE_FILES:
        path = data / name
        if path.is_symlink():
            raise BackupError(f"Refusing a linked database: {name}")
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            raise BackupError(f"Unreadable database: {name}")
        members[name] = (stat.st_dev, stat.st_ino)
    if "recallry.db" not in members:
        raise BackupError("The Knowledge database is missing.")
    return members


def _snapshot(data, stage):
    # All pinned read transactions overlap. If any DB changed while the set
    # was being opened, retry before copying any bytes.
    for _ in range(3):
        with ExitStack() as stack:
            members = _members(data)
            sources = {n: stack.enter_context(closing(_open(data / n))) for n in members}
            observers = {n: stack.enter_context(closing(_open(data / n))) for n in members}
            versions = {n: c.execute("PRAGMA data_version").fetchone() for n, c in observers.items()}
            for c in sources.values():
                c.execute("BEGIN")
                c.execute("SELECT count(*) FROM sqlite_master").fetchone()
            if members != _members(data) or any(
                c.execute("PRAGMA data_version").fetchone() != versions[n] for n, c in observers.items()
            ):
                continue
            files = {}
            for name, source in sources.items():
                path = stage / name
                with closing(sqlite3.connect(path)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise BackupError(f"Could not create a standalone SQLite file: {name}")
                path.chmod(0o600)
                # verify(stage) compares the published bytes with this pinned source.
                files[name] = {"logical_sha256": _digest(source), "file_sha256": _file_hash(path)}
            return files
    raise BackupError("Concurrent writes prevented a consistent snapshot. Retry on the next write.")


def manifest(directory):
    path = directory / "manifest.json"
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise BackupError("Backup manifest missing.")
    value = json.loads(path.read_text(encoding="utf-8"))
    files = value.get("files") if isinstance(value, dict) else None
    if (not isinstance(files, dict) or "recallry.db" not in files or set(files) - set(STORE_FILES)
            or not isinstance(value.get("source_root"), str)
            or not isinstance(value.get("created_at"), str)):
        raise BackupError("Invalid backup manifest")
    datetime.strptime(value["created_at"], STAMP)
    expected = {"manifest.json", *files}
    # Finder can add display metadata, including AppleDouble on removable disks.
    metadata = {".DS_Store", "._.DS_Store", *("._" + name for name in expected)}
    for path in directory.iterdir():
        if path.name in expected:
            continue
        if path.name not in metadata or path.is_symlink() or not path.is_file():
            raise BackupError("Unexpected files in backup generation.")
    for name, entry in files.items():
        db = directory / name
        if (db.is_symlink() or not db.is_file() or not isinstance(entry, dict)
                or not all(isinstance(entry.get(k), str) and len(entry[k]) == 64
                           for k in ("file_sha256", "logical_sha256"))):
            raise BackupError(f"Invalid backup manifest: {name}")
    return value


def verify(directory: Path) -> dict:
    value = manifest(directory)
    for name, entry in value["files"].items():
        path = directory / name
        if _file_hash(path) != entry["file_sha256"]:
            raise BackupError(f"File checksum mismatch: {name}")
        with closing(_open(path, immutable=True)) as con:
            if name == "recallry.db":
                try:
                    Database(path).validate_connection(con)
                except RecallryError as exc:
                    raise BackupError(str(exc)) from exc
            else:
                MetricsStore(path)._validate(con)
                if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError(f"Integrity check failed for {name}")
            if _digest(con) != entry["logical_sha256"]:
                raise BackupError(f"Logical checksum mismatch: {name}")
    return value


def snapshot_generation(root: Path, base: Path) -> dict:
    """Publish under an existing (and, for managed operations, locked) destination."""
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    target = base / f"recallry-backup-{stamp}-{uuid.uuid4().hex[:12]}"
    stage = Path(tempfile.mkdtemp(prefix=".partial-", dir=base))
    try:
        files = _snapshot(root.resolve() / "data", stage)
        value = {"created_at": stamp, "source_root": str(root.resolve()), "files": files}
        atomic_json(stage / "manifest.json", value)
        verify(stage)
        stage.rename(target)
        _sync_directory(base)
        return {"path": str(target), **value}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _restore_into_empty_stage(backup_dir: Path, root: Path) -> list[str]:
    """Copy a verified generation into an empty recovery stage."""
    if any((root / "data").iterdir()):
        raise BackupError("Recovery requires an empty data directory.")
    if backup_dir.name.startswith(".partial-"):
        raise BackupError("An incomplete backup cannot be recovered.")
    with directory_lock(backup_dir.parent, shared=True):
        value = verify(backup_dir)
        for name, entry in value["files"].items():
            with closing(_open(backup_dir / name, immutable=True)) as src, closing(
                sqlite3.connect(root / "data" / name)
            ) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or _digest(dst) != entry["logical_sha256"]:
                    raise BackupError(f"Recovered database verification failed: {name}")
        return list(value["files"])


def recover_copy(generation: Path, parent: Path, active_root: Path) -> Path:
    """Publish only a complete, independently readable recovery. Never overwrite a store."""
    parent = parent.resolve(strict=True)
    if (generation.is_symlink() or not parent.is_dir() or parent.is_relative_to(active_root.resolve())
            or parent.is_relative_to(generation.resolve()) or
            any((p / ".git").exists() or (p / "data/recallry.db").is_file() or
                p.name.startswith(("recallry-backup-", ".partial-")) for p in (parent, *parent.parents))):
        raise BackupError("Choose a recovery location outside active stores, backup generations and Git repositories.")
    stage = Path(tempfile.mkdtemp(prefix=".recallry-restore-", dir=parent))
    target = parent / ("Recallry-Recovered-" + stage.name.rsplit("-", 1)[-1])
    try:
        (stage / "data").mkdir(mode=0o700)
        _restore_into_empty_stage(generation, stage)
        Database(stage / "data" / "recallry.db").validate()
        # A new copy never inherits transmission consent or live-store permissions.
        (stage / "config.toml").write_text(
            'default_context_limit = 15\ndefault_context_content_chars = 800\n'
            'default_context_max_chars = 12000\ndefault_status = "candidate"\n', encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise BackupError("Recovery destination already exists.")
        stage.rename(target)
        _sync_directory(parent)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)
