from __future__ import annotations

from .db import Database
from .models import Knowledge


def search(
    database: Database, query: str, limit: int = 50, *,
    project: str | None = None, all_projects: bool = False,
) -> list[Knowledge]:
    query = query.strip()
    if not query:
        return []
    if project is not None and all_projects:
        raise ValueError("project and all_projects cannot be used together")
    if all_projects:
        fts_scope, like_scope, scope_values = "", "", []
    elif project is not None:
        fts_scope = "AND (k.scope='global' OR (k.scope='project' AND k.project=?))"
        like_scope = "AND (scope='global' OR (scope='project' AND project=?))"
        scope_values = [project]
    else:
        fts_scope = "AND k.scope='global'"
        like_scope = "AND scope='global'"
        scope_values = []
    with database.connect() as connection:
        has_fts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        if has_fts:
            rows = connection.execute(
                f"""SELECT k.* FROM knowledge_fts f
                JOIN knowledge k ON k.rowid=f.rowid
                WHERE knowledge_fts MATCH ?
                {fts_scope}
                ORDER BY CASE k.status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                         bm25(knowledge_fts), k.updated_at DESC LIMIT ?""",
                (f'"{query.replace(chr(34), chr(34) * 2)}"', *scope_values, limit),
            ).fetchall()
            if rows:
                return [Knowledge(**dict(row)) for row in rows]
            # A valid zero-result FTS query still gets substring/tokenizer coverage via LIKE.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = connection.execute(
            f"""SELECT * FROM knowledge
            WHERE (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'
               OR category LIKE ? ESCAPE '\\' OR coalesce(project,'') LIKE ? ESCAPE '\\')
              {like_scope}
            ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                     CASE WHEN title LIKE ? THEN 0 ELSE 1 END, updated_at DESC LIMIT ?""",
            (pattern, pattern, pattern, pattern, *scope_values, pattern, limit),
        ).fetchall()
    return [Knowledge(**dict(row)) for row in rows]
