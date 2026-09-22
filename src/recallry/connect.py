from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

from .project_config import ProjectConfigError, load_project_identity


START_MARKER = b"<!-- RECALLRY:START -->"
END_MARKER = b"<!-- RECALLRY:END -->"
MAX_PROJECT_ID_LENGTH = 128
PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


class ConnectError(ValueError):
    pass


@dataclass(frozen=True)
class FileChange:
    path: Path
    status: str
    content: bytes | None = None


@dataclass(frozen=True)
class ConnectResult:
    project_root: Path
    project_id: str
    config: FileChange
    claude: FileChange
    codex: FileChange
    dry_run: bool


def validate_project_id(project_id: str) -> str:
    if not project_id:
        raise ConnectError("project_id must not be empty")
    if len(project_id) > MAX_PROJECT_ID_LENGTH:
        raise ConnectError(f"project_id must be at most {MAX_PROJECT_ID_LENGTH} characters")
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ConnectError(
            "project_id must match [a-z0-9][a-z0-9._-]* with no whitespace"
        )
    return project_id


def connect_project(
    recallry_root: Path, project_path: Path, project_id: str, *, dry_run: bool = False,
) -> ConnectResult:
    project_id = validate_project_id(project_id)
    project_root = _resolve_project_root(project_path)

    config = _plan_config(project_root, project_id)
    claude = _plan_router(
        project_root / "CLAUDE.md",
        recallry_root / "templates" / "recallry-claude-router.md",
        "- This project-level router supplements and does not override parent `CLAUDE.md`, "
        "repository rules, or higher-priority instructions.",
    )
    codex = _plan_router(
        project_root / "AGENTS.md",
        recallry_root / "templates" / "recallry-codex-router.md",
        "- This project-level router supplements and does not override parent `AGENTS.md`, "
        "repository rules, or higher-priority instructions.",
    )
    result = ConnectResult(project_root, project_id, config, claude, codex, dry_run)
    if not dry_run:
        for change in (config, claude, codex):
            if change.status != "unchanged":
                assert change.content is not None
                _atomic_write(change.path, change.content)
    return result


def _resolve_project_root(project_path: Path) -> Path:
    try:
        resolved = project_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConnectError(f"project path does not exist: {project_path}") from exc
    if not resolved.is_dir():
        raise ConnectError(f"project path is not a directory: {resolved}")
    return resolved


def _plan_config(project_root: Path, project_id: str) -> FileChange:
    path = project_root / ".recallry.toml"
    _reject_symlink(path)
    if path.exists():
        if not path.is_file():
            raise ConnectError(f"project config is not a regular file: {path}")
        try:
            existing = load_project_identity(project_root).project_id
        except ProjectConfigError as exc:
            raise ConnectError(str(exc)) from exc
        if existing != project_id:
            raise ConnectError(
                f"project_id conflict in {path}: existing {existing!r}, requested {project_id!r}"
            )
        return FileChange(path, "unchanged")
    return FileChange(path, "created", f'project_id = "{project_id}"\n'.encode())


def _plan_router(path: Path, template_path: Path, parent_rule: str) -> FileChange:
    _reject_symlink(path)
    if path.exists() and not path.is_file():
        raise ConnectError(f"router target is not a regular file: {path}")
    if not template_path.is_file():
        raise ConnectError(f"Recallry router template not found: {template_path}")
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConnectError(f"cannot read Recallry router template: {template_path}") from exc

    original = path.read_bytes() if path.exists() else b""
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    block = _managed_block(template, parent_rule, newline)
    updated = _replace_or_append_block(path, original, block, newline)
    if updated == original:
        return FileChange(path, "unchanged")
    return FileChange(path, "updated" if path.exists() else "created", updated)


def _managed_block(template: str, parent_rule: str, newline: bytes) -> bytes:
    lines = template.splitlines()
    if not lines:
        raise ConnectError("Recallry router template is empty")
    managed_lines = [lines[0], parent_rule, *lines[1:]]
    body = newline.join(line.encode("utf-8") for line in managed_lines)
    return START_MARKER + newline + body + newline + END_MARKER


def _replace_or_append_block(path: Path, original: bytes, block: bytes, newline: bytes) -> bytes:
    starts = original.count(START_MARKER)
    ends = original.count(END_MARKER)
    if starts == 0 and ends == 0:
        if not original:
            return block + newline
        if original.endswith(newline + newline):
            separator = b""
        elif original.endswith(newline):
            separator = newline
        else:
            separator = newline + newline
        return original + separator + block + newline
    if starts != 1 or ends != 1:
        raise ConnectError(f"invalid Recallry marker block in {path}")
    start = original.index(START_MARKER)
    end = original.index(END_MARKER)
    if end < start:
        raise ConnectError(f"invalid Recallry marker order in {path}")
    end += len(END_MARKER)
    return original[:start] + block + original[end:]


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ConnectError(f"refusing to replace symlink: {path}")


def _atomic_write(path: Path, content: bytes) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
