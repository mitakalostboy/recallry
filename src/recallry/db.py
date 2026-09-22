from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid

from .models import Knowledge, SCOPES, STATUSES

SCHEMA_VERSION = 1
APP_ID = "recallry"


class RecallryError(Exception):
    pass


class SchemaValidationError(RecallryError):
    pass


class NotFoundError(RecallryError):
    pass


class AmbiguousIdError(RecallryError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_or_zero = not self.path.exists() or self.path.stat().st_size == 0
        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RecallryError(f"Database integrity check failed: {integrity}")
            objects = connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if new_or_zero or (not objects and version == 0):
                connection.execute("PRAGMA journal_mode = WAL")
                self._create_schema(connection)
                return True

            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recallry_meta'"
            ).fetchone()
            if not meta_exists:
                raise RecallryError(
                    "Refusing to initialize: existing SQLite database is not recognized "
                    "as an Recallry database."
                )
            self._validate_schema(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            return False

    def validate(self) -> None:
        if not self.path.is_file():
            raise RecallryError("Database is not initialized. Run 'recallry init' first.")
        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RecallryError(f"Database integrity check failed: {integrity}")
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recallry_meta'"
            ).fetchone()
            if not meta_exists:
                raise SchemaValidationError(
                    "Existing SQLite database is not recognized as an Recallry database."
                )
            self._validate_schema(connection)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE recallry_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            )"""
        )
        connection.executemany(
            "INSERT INTO recallry_meta(key, value) VALUES (?, ?)",
            (("app_id", APP_ID), ("schema_version", str(SCHEMA_VERSION))),
        )
        connection.execute(
            """CREATE TABLE knowledge (
                id TEXT PRIMARY KEY NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
                project TEXT,
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                status TEXT NOT NULL CHECK (status IN ('candidate','verified','deprecated','rejected')),
                category TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                verified_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
                CHECK ((scope = 'global' AND project IS NULL) OR
                       (scope = 'project' AND length(trim(project)) > 0))
            )"""
        )
        connection.execute("CREATE INDEX idx_knowledge_status ON knowledge(status)")
        connection.execute("CREATE INDEX idx_knowledge_scope_project ON knowledge(scope, project)")
        connection.execute("CREATE INDEX idx_knowledge_category ON knowledge(category)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._create_fts(connection)

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise SchemaValidationError(
                f"Recallry database schema validation failed: user_version is {version}, "
                f"expected {SCHEMA_VERSION}."
            )
        self._validate_columns(connection, "recallry_meta", {
            "key": ("TEXT", 1, None, 1),
            "value": ("TEXT", 1, None, 0),
        })
        metadata = dict(connection.execute("SELECT key, value FROM recallry_meta").fetchall())
        expected_metadata = {"app_id": APP_ID, "schema_version": str(SCHEMA_VERSION)}
        for key, expected in expected_metadata.items():
            if metadata.get(key) != expected:
                raise SchemaValidationError(
                    f"Recallry database schema validation failed: invalid metadata '{key}'."
                )
        self._validate_columns(connection, "knowledge", {
            "id": ("TEXT", 1, None, 1),
            "scope": ("TEXT", 1, None, 0),
            "project": ("TEXT", 0, None, 0),
            "title": ("TEXT", 1, None, 0),
            "content": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, None, 0),
            "category": ("TEXT", 1, None, 0),
            "source": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
            "verified_at": ("TEXT", 0, None, 0),
            "use_count": ("INTEGER", 1, "0", 0),
        })
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge'"
        ).fetchone()
        normalized = " ".join(table_row[0].lower().split()) if table_row and table_row[0] else ""
        required_checks = (
            "scope in ('global', 'project')",
            "status in ('candidate','verified','deprecated','rejected')",
            "length(trim(title)) > 0",
            "length(trim(content)) > 0",
            "use_count >= 0",
            "scope = 'global' and project is null",
            "scope = 'project' and length(trim(project)) > 0",
        )
        if any(check not in normalized for check in required_checks):
            raise SchemaValidationError(
                "Recallry database schema validation failed: required CHECK constraint is missing."
            )
        expected_indexes = {
            "idx_knowledge_status": ("status",),
            "idx_knowledge_scope_project": ("scope", "project"),
            "idx_knowledge_category": ("category",),
        }
        indexes = {row[1]: row for row in connection.execute("PRAGMA index_list(knowledge)")}
        for name, expected_columns in expected_indexes.items():
            if name not in indexes:
                raise SchemaValidationError(
                    f"Recallry database schema validation failed: missing index '{name}'."
                )
            actual_columns = tuple(
                row[2] for row in connection.execute(f'PRAGMA index_info("{name}")')
            )
            if actual_columns != expected_columns:
                raise SchemaValidationError(
                    f"Recallry database schema validation failed: invalid index '{name}'."
                )

        fts = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        triggers = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'knowledge_%'"
            )
        }
        expected_triggers = {"knowledge_ai", "knowledge_ad", "knowledge_au"}
        if fts:
            if "using fts5" not in fts[0].lower() or not expected_triggers.issubset(triggers):
                raise SchemaValidationError(
                    "Recallry database schema validation failed: incomplete FTS schema."
                )
        elif triggers & expected_triggers:
            raise SchemaValidationError(
                "Recallry database schema validation failed: orphaned FTS trigger."
            )

        allowed_tables = {"recallry_meta", "knowledge"}
        allowed_triggers: set[str] = set()
        if fts:
            allowed_tables.update({
                "knowledge_fts", "knowledge_fts_config", "knowledge_fts_data",
                "knowledge_fts_docsize", "knowledge_fts_idx",
            })
            allowed_triggers = expected_triggers
        actual_tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        actual_named_indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != allowed_tables or actual_named_indexes != set(expected_indexes) or triggers != allowed_triggers:
            raise SchemaValidationError(
                "Recallry database schema validation failed: unknown database object."
            )

    def _validate_columns(
        self, connection: sqlite3.Connection, table: str,
        expected: dict[str, tuple[str, int, str | None, int]],
    ) -> None:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        actual = {row[1]: (row[2].upper(), row[3], row[4], row[5]) for row in rows}
        if actual != expected:
            raise SchemaValidationError(
                f"Recallry database schema validation failed: invalid table '{table}'."
            )

    def _create_fts(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                """CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                    title, content, category, project,
                    content='knowledge', content_rowid='rowid'
                )"""
            )
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" in str(exc).lower():
                return
            raise
        connection.execute(
            """CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
                    INSERT INTO knowledge_fts(rowid,title,content,category,project)
                    VALUES (new.rowid,new.title,new.content,new.category,coalesce(new.project,''));
                END"""
        )
        connection.execute(
            """CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts,rowid,title,content,category,project)
                    VALUES ('delete',old.rowid,old.title,old.content,old.category,coalesce(old.project,''));
                END"""
        )
        connection.execute(
            """CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge BEGIN
                    INSERT INTO knowledge_fts(knowledge_fts,rowid,title,content,category,project)
                    VALUES ('delete',old.rowid,old.title,old.content,old.category,coalesce(old.project,''));
                    INSERT INTO knowledge_fts(rowid,title,content,category,project)
                    VALUES (new.rowid,new.title,new.content,new.category,coalesce(new.project,''));
                END"""
        )

    def add(self, *, title: str, content: str, scope: str, project: str | None,
            category: str, status: str = "candidate", source: str | None = None) -> Knowledge:
        title, content = title.strip(), content.strip()
        project = project.strip() if project else None
        source = source.strip() if source else None
        if not title or not content:
            raise RecallryError("title and content must not be empty")
        if scope not in SCOPES:
            raise RecallryError(f"scope must be one of: {', '.join(SCOPES)}")
        if scope == "project" and not project:
            raise RecallryError("--project is required when scope is project")
        if scope == "global" and project:
            raise RecallryError("--project cannot be used when scope is global")
        if status != "candidate":
            raise RecallryError("New Knowledge must start as candidate; use promote to verify it")
        if not category:
            raise RecallryError("category must not be empty")
        item_id, timestamp = str(uuid.uuid4()), now_utc()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO knowledge
                (id,scope,project,title,content,status,category,source,created_at,updated_at,verified_at,use_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                (item_id, scope, project, title, content, status, category, source,
                 timestamp, timestamp, timestamp if status == "verified" else None),
            )
        return self.get(item_id)

    def _resolve_id(self, connection: sqlite3.Connection, identifier: str) -> str:
        escaped = identifier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = connection.execute(
            "SELECT id FROM knowledge WHERE id = ? OR id LIKE ? ESCAPE '\\' ORDER BY id",
            (identifier, escaped + "%"),
        ).fetchall()
        unique = {row[0] for row in rows}
        if not unique:
            raise NotFoundError(f"Knowledge not found: {identifier}")
        if len(unique) > 1:
            raise AmbiguousIdError(f"Ambiguous Knowledge ID: {identifier}")
        return unique.pop()

    def get(self, identifier: str) -> Knowledge:
        with self.connect() as connection:
            item_id = self._resolve_id(connection, identifier)
            row = connection.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()
        return Knowledge(**dict(row))

    def list(self, **filters: str | None) -> list[Knowledge]:
        clauses, values = [], []
        for name in ("status", "project", "scope", "category"):
            value = filters.get(name)
            if value is not None:
                clauses.append(f"{name} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge" + where + " ORDER BY created_at DESC, id", values
            ).fetchall()
        return [Knowledge(**dict(row)) for row in rows]

    def transition(self, identifier: str, action: str) -> tuple[Knowledge, bool]:
        targets = {"promote": "verified", "reject": "rejected", "deprecate": "deprecated"}
        target = targets[action]
        with self.connect() as connection:
            item_id = self._resolve_id(connection, identifier)
            row = connection.execute("SELECT status FROM knowledge WHERE id = ?", (item_id,)).fetchone()
            current = row[0]
            if current == target:
                return self.get(item_id), False
            allowed = {
                "promote": {"candidate"},
                "reject": {"candidate"},
                "deprecate": {"candidate", "verified"},
            }
            if current not in allowed[action]:
                raise RecallryError(f"Cannot {action} Knowledge with status '{current}'")
            timestamp = now_utc()
            verified = timestamp if target == "verified" else None
            connection.execute(
                "UPDATE knowledge SET status=?, updated_at=?, verified_at=? WHERE id=?",
                (target, timestamp, verified, item_id),
            )
        return self.get(item_id), True

    def increment_use_count(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.connect() as connection:
            connection.executemany(
                "UPDATE knowledge SET use_count = use_count + 1 WHERE id = ?",
                ((item_id,) for item_id in ids),
            )
