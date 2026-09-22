from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


METRICS_SCHEMA_VERSION = 1
EVENT_TYPES = (
    "automatic_context",
    "candidate_created",
    "knowledge_promoted",
    "knowledge_rejected",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_origin(source: str | None) -> str:
    value = (source or "").lower()
    if "automatic-candidate" in value:
        return "automatic"
    if "historical-backfill" in value:
        return "backfill"
    if "user-approved" in value:
        return "user_approved"
    return "manual"


class MetricsStore:
    """Local operational telemetry isolated from the Knowledge database."""

    def __init__(self, path: Path):
        self.path = path

    def append(
        self, event_type: str, *, project_id: str | None = None,
        command: str, mode: str, knowledge_ids: tuple[str, ...] = (),
        included_count: int | None = None, injected_chars: int | None = None,
        truncated: bool | None = None, origin: str | None = None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported metrics event: {event_type}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            self._initialize(connection)
            cursor = connection.execute(
                """INSERT INTO metrics_events
                (occurred_at,event_type,project_id,command,mode,included_count,
                 injected_chars,truncated,origin)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now_utc(), event_type, project_id, command, mode, included_count,
                 injected_chars, None if truncated is None else int(truncated), origin),
            )
            event_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO metrics_event_knowledge(event_id,knowledge_id) VALUES (?,?)",
                ((event_id, item_id) for item_id in dict.fromkeys(knowledge_ids)),
            )

    def read_stats(self, *, project: str | None = None, days: int | None = None) -> dict:
        if not self.path.is_file():
            return _empty_stats()
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            self._validate(connection)
            collection_since = connection.execute(
                "SELECT value FROM metrics_meta WHERE key='collection_started_at'"
            ).fetchone()[0]
            clauses: list[str] = []
            values: list[object] = []
            if project is not None:
                clauses.append("e.project_id = ?")
                values.append(project)
            if days is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                clauses.append("e.occurred_at >= ?")
                values.append(cutoff.isoformat(timespec="seconds"))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            row = connection.execute(
                f"""SELECT
                    sum(CASE WHEN e.event_type='automatic_context' THEN 1 ELSE 0 END) automatic_reads,
                    coalesce(sum(CASE WHEN e.event_type='automatic_context' THEN e.included_count ELSE 0 END),0) retrievals,
                    count(DISTINCT CASE WHEN e.event_type='automatic_context' THEN e.project_id END) projects_active,
                    sum(CASE WHEN e.event_type='candidate_created' THEN 1 ELSE 0 END) candidates_created,
                    sum(CASE WHEN e.event_type='knowledge_promoted' THEN 1 ELSE 0 END) candidates_promoted,
                    sum(CASE WHEN e.event_type='knowledge_rejected' THEN 1 ELSE 0 END) candidates_rejected,
                    coalesce(sum(CASE WHEN e.event_type='automatic_context' THEN e.injected_chars ELSE 0 END),0) injected_chars
                FROM metrics_events e{where}""",
                values,
            ).fetchone()
            unique_used = connection.execute(
                f"""SELECT count(DISTINCT ek.knowledge_id)
                FROM metrics_events e
                JOIN metrics_event_knowledge ek ON ek.event_id=e.id
                {where + (' AND ' if where else ' WHERE ')}e.event_type='automatic_context'""",
                values,
            ).fetchone()[0]
        result = {key: int(row[key] or 0) for key in row.keys()}
        result["unique_used"] = int(unique_used or 0)
        result["collection_since"] = collection_since
        result["estimated_injected_tokens"] = (result["injected_chars"] + 3) // 4
        return result

    def _initialize(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not objects and version == 0:
            connection.executescript(
                """CREATE TABLE metrics_meta (
                       key TEXT PRIMARY KEY NOT NULL,
                       value TEXT NOT NULL
                   );
                   CREATE TABLE metrics_events (
                       id INTEGER PRIMARY KEY,
                       occurred_at TEXT NOT NULL,
                       event_type TEXT NOT NULL CHECK (event_type IN
                           ('automatic_context','candidate_created','knowledge_promoted','knowledge_rejected')),
                       project_id TEXT,
                       command TEXT NOT NULL,
                       mode TEXT NOT NULL,
                       included_count INTEGER CHECK (included_count IS NULL OR included_count >= 0),
                       injected_chars INTEGER CHECK (injected_chars IS NULL OR injected_chars >= 0),
                       truncated INTEGER CHECK (truncated IS NULL OR truncated IN (0,1)),
                       origin TEXT
                   );
                   CREATE TABLE metrics_event_knowledge (
                       event_id INTEGER NOT NULL REFERENCES metrics_events(id) ON DELETE CASCADE,
                       knowledge_id TEXT NOT NULL,
                       PRIMARY KEY (event_id, knowledge_id)
                   );
                   CREATE INDEX idx_metrics_events_type_time ON metrics_events(event_type, occurred_at);
                   CREATE INDEX idx_metrics_events_project_time ON metrics_events(project_id, occurred_at);
                   CREATE INDEX idx_metrics_knowledge_id ON metrics_event_knowledge(knowledge_id);
                """
            )
            connection.executemany(
                "INSERT INTO metrics_meta(key,value) VALUES (?,?)",
                (("app_id", "recallry-metrics"),
                 ("schema_version", str(METRICS_SCHEMA_VERSION)),
                 ("collection_started_at", now_utc())),
            )
            connection.execute(f"PRAGMA user_version = {METRICS_SCHEMA_VERSION}")
        else:
            self._validate(connection)

    def _validate(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != METRICS_SCHEMA_VERSION:
            raise sqlite3.DatabaseError("unsupported metrics database schema")
        metadata = dict(connection.execute("SELECT key,value FROM metrics_meta"))
        if metadata.get("app_id") != "recallry-metrics":
            raise sqlite3.DatabaseError("unrecognized metrics database")
        required = {"metrics_meta", "metrics_events", "metrics_event_knowledge"}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if not required.issubset(tables):
            raise sqlite3.DatabaseError("incomplete metrics database schema")


def _empty_stats() -> dict:
    return {
        "collection_since": None,
        "automatic_reads": 0,
        "retrievals": 0,
        "unique_used": 0,
        "projects_active": 0,
        "candidates_created": 0,
        "candidates_promoted": 0,
        "candidates_rejected": 0,
        "injected_chars": 0,
        "estimated_injected_tokens": 0,
    }


def render_stats(payload: dict, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    metrics = payload["metrics"]
    current = payload["current_knowledge"]
    since = metrics["collection_since"] or "Not started"
    return (
        "Recallry Metrics\n"
        "----------------\n"
        f"Collection since: {since}\n"
        f"Automatic reads: {metrics['automatic_reads']}\n"
        f"Knowledge retrievals: {metrics['retrievals']}\n"
        f"Unique Knowledge used: {metrics['unique_used']}\n"
        f"Projects active: {metrics['projects_active']}\n\n"
        f"Candidates created: {metrics['candidates_created']}\n"
        f"Candidates promoted: {metrics['candidates_promoted']}\n"
        f"Candidates rejected: {metrics['candidates_rejected']}\n\n"
        f"Injected context chars: {metrics['injected_chars']}\n"
        f"Estimated injected tokens: {metrics['estimated_injected_tokens']}\n\n"
        "Current Knowledge:\n"
        f"Verified: {current['verified']}\n"
        f"Candidate: {current['candidate']}\n"
        f"Rejected: {current['rejected']}\n"
        f"Deprecated: {current['deprecated']}\n"
    )
