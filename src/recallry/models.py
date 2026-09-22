from __future__ import annotations

from dataclasses import dataclass


SCOPES = ("global", "project")
STATUSES = ("candidate", "verified", "deprecated", "rejected")
CATEGORIES = ("rule", "bug", "decision", "lesson", "workflow", "architecture", "warning", "other")


@dataclass(frozen=True)
class Knowledge:
    id: str
    scope: str
    project: str | None
    title: str
    content: str
    status: str
    category: str
    source: str | None
    created_at: str
    updated_at: str
    verified_at: str | None
    use_count: int

