from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ProjectConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    config_path: Path


def load_project_identity(project_root: Path) -> ProjectIdentity:
    config_path = project_root.resolve() / ".recallry.toml"
    if not config_path.is_file():
        raise ProjectConfigError(f"Project config not found: {config_path}")
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"Invalid project config: {exc}") from exc
    project_id = raw.get("project_id")
    if not isinstance(project_id, str):
        raise ProjectConfigError("project_id must be a non-empty string")
    project_id = project_id.strip()
    if not project_id:
        raise ProjectConfigError("project_id must be a non-empty string")
    return ProjectIdentity(project_id=project_id, config_path=config_path)
