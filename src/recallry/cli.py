from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from importlib.resources import files
import sqlite3
import sys

from .connect import connect_project, validate_project_id
from .config import load_config, resolve_recallry_root
from .context import generate_context
from .db import RecallryError, Database, SchemaValidationError
from .metrics import MetricsStore, render_stats, source_origin
from .models import CATEGORIES, SCOPES, STATUSES, Knowledge
from .project_config import ProjectConfigError, load_project_identity
from .search import search


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="recallry", description="Local External Intelligence store")
    result.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize directories and database")

    add = commands.add_parser("add", help="Add Knowledge")
    add.add_argument("--title", required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--scope", choices=SCOPES, default="global")
    add.add_argument("--project")
    add.add_argument("--category", default="other", help=f"Suggested: {', '.join(CATEGORIES)}")
    add.add_argument("--source")

    find = commands.add_parser("search", help="Search Knowledge")
    find.add_argument("query")
    find.add_argument("--limit", type=positive_int, default=50)
    search_scope = find.add_mutually_exclusive_group()
    search_scope.add_argument("--project")
    search_scope.add_argument("--all-projects", action="store_true")

    listing = commands.add_parser("list", help="List Knowledge")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--project")
    listing.add_argument("--scope", choices=SCOPES)
    listing.add_argument("--category")

    show = commands.add_parser("show", help="Show one Knowledge item")
    show.add_argument("id")
    for name in ("promote", "reject", "deprecate"):
        action = commands.add_parser(name, help=f"{name.title()} Knowledge")
        action.add_argument("id")

    connect = commands.add_parser("connect", help="Connect a project to Recallry")
    connect.add_argument("project_path", type=Path)
    connect.add_argument("--project-id", required=True)
    connect.add_argument("--dry-run", action="store_true")

    context = commands.add_parser("context", help="Generate Markdown context")
    context_project = context.add_mutually_exclusive_group()
    context_project.add_argument("--project")
    context_project.add_argument("--project-root", type=Path)
    context.add_argument("--task", default="")
    context.add_argument("--limit", type=positive_int)
    context.add_argument("--verified-only", action="store_true")
    context.add_argument("--automatic", action="store_true")
    context.add_argument(
        "--format", choices=("markdown", "json", "json-compact"), default="markdown",
        help="Output Markdown (default), full JSON, or JSON without knowledge content fields",
    )
    stats = commands.add_parser("stats", help="Show local operational metrics")
    stats.add_argument("--project")
    stats.add_argument("--days", type=positive_int)
    stats.add_argument("--json", action="store_true")
    return result


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _summary(item: Knowledge) -> str:
    excerpt = " ".join(item.content.split())
    if len(excerpt) > 80:
        excerpt = excerpt[:77] + "..."
    return "\t".join((item.id[:12], item.status, item.scope, item.project or "-",
                       item.category, item.title, excerpt))


def _print_items(items: list[Knowledge]) -> None:
    if not items:
        print("No Knowledge found.")
        return
    print("ID\tstatus\tscope\tproject\tcategory\ttitle\tcontent")
    for item in items:
        print(_summary(item))


def _print_full(item: Knowledge) -> None:
    for name in (
        "id", "status", "scope", "project", "category", "title", "content", "source",
        "created_at", "updated_at", "verified_at", "use_count",
    ):
        value = getattr(item, name)
        print(f"{name}: {value if value is not None else '-'}")


def run(args: argparse.Namespace) -> int:
    if _read_only_enabled() and args.command not in {"context", "search", "show", "list", "stats"}:
        raise RecallryError("write operation unavailable in read-only mode")
    root = resolve_recallry_root(args.root, os.environ)
    database = Database(root / "data" / "recallry.db")
    metrics = MetricsStore(root / "data" / "metrics.db")
    if args.command == "init":
        # Validate a pre-existing DB before provisioning any resources.
        if database.path.is_file() and database.path.stat().st_size:
            database.validate()
        resources = files("recallry").joinpath("resources")
        defaults = ["config.toml", "templates/recallry-claude-router.md",
                    "templates/recallry-codex-router.md"]
        root.mkdir(parents=True, exist_ok=True)
        for relative in defaults:
            destination = root / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(resources.joinpath(relative).read_bytes())
        load_config(root / "config.toml")
        for relative in ("data", "knowledge/global", "knowledge/projects", "candidates", "exports"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        created = database.initialize()
        print("Recallry initialized." if created else "Recallry is already initialized; existing data preserved.")
        return 0
    config = load_config(root / "config.toml")
    if not database.path.is_file():
        raise RecallryError("Database is not initialized. Run 'recallry init' first.")
    database.validate()

    if args.command == "add":
        item = database.add(
            title=args.title, content=args.content, scope=args.scope, project=args.project,
            category=args.category, status=config.default_status, source=args.source,
        )
        _record_metrics(metrics, "candidate_created", project_id=item.project,
                        command="add", mode="manual", knowledge_ids=(item.id,),
                        origin=source_origin(item.source))
        print(f"Added {item.id} ({item.status}).")
    elif args.command == "search":
        _print_items(search(database, args.query, args.limit,
                            project=args.project, all_projects=args.all_projects))
    elif args.command == "list":
        _print_items(database.list(status=args.status, project=args.project,
                                   scope=args.scope, category=args.category))
    elif args.command == "show":
        _print_full(database.get(args.id))
    elif args.command in ("promote", "reject", "deprecate"):
        item, changed = database.transition(args.id, args.command)
        if changed and args.command in {"promote", "reject"}:
            _record_metrics(
                metrics,
                "knowledge_promoted" if args.command == "promote" else "knowledge_rejected",
                project_id=item.project, command=args.command, mode="manual",
                knowledge_ids=(item.id,),
            )
        print(f"{item.id} is now {item.status}." if changed else f"{item.id} is already {item.status}; no changes made.")
    elif args.command == "connect":
        project_id = validate_project_id(args.project_id)
        if not args.dry_run:
            context_result = generate_context(
                database, project=project_id, task="Verify Recallry project connection",
                limit=8, content_chars=600, max_chars=6000, verified_only=True,
                increment_usage=False,
            )
            if any(
                entry.item.status != "verified"
                or (entry.item.scope == "project" and entry.item.project != project_id)
                for entry in context_result.included
            ):
                raise RecallryError("Connection check failed: Knowledge boundary violation")
        result = connect_project(root, args.project_path, args.project_id, dry_run=args.dry_run)
        check = "not run (dry-run)"
        if not args.dry_run:
            identity = load_project_identity(result.project_root)
            if identity.project_id != project_id:
                raise RecallryError("Connection check failed: project identity mismatch")
            check = "ok"
        prefix = "Dry run for" if args.dry_run else "Connected project"
        print(f"{prefix}: {result.project_id}")
        print(f"Path: {result.project_root}")
        print(f".recallry.toml: {result.config.status}")
        print(f"CLAUDE.md: {result.claude.status}")
        print(f"AGENTS.md: {result.codex.status}")
        print(f"Connection check: {check}")
    elif args.command == "context":
        project = args.project
        if args.project_root is not None:
            project = load_project_identity(args.project_root).project_id
        if args.automatic and args.project_root is None:
            raise ProjectConfigError("--automatic requires --project-root with .recallry.toml")
        automatic = args.automatic
        result = generate_context(
            database, project=project, task=args.task,
            limit=min(args.limit or 8, 8) if automatic else (args.limit or config.default_context_limit),
            content_chars=600 if automatic else config.default_context_content_chars,
            max_chars=6000 if automatic else config.default_context_max_chars,
            verified_only=args.verified_only or automatic,
            increment_usage=not (_read_only_enabled() or automatic),
        )
        if automatic:
            _record_metrics(
                metrics, "automatic_context", project_id=project, command="context",
                mode="automatic",
                knowledge_ids=tuple(entry.item.id for entry in result.included),
                included_count=len(result.included), injected_chars=len(result.markdown),
                truncated=result.truncated,
            )
        if args.format in ("json", "json-compact"):
            payload = _context_payload(result, project, args.task)
            if args.format == "json-compact":
                for item in payload["knowledge"]:
                    del item["content"]
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(result.markdown, end="")
    elif args.command == "stats":
        counts = {status: 0 for status in STATUSES}
        for item in database.list():
            counts[item.status] += 1
        payload = {
            "filters": {"project": args.project, "days": args.days},
            "metrics": metrics.read_stats(project=args.project, days=args.days),
            "current_knowledge": counts,
        }
        print(render_stats(payload, as_json=args.json), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (RecallryError, ValueError, OSError, sqlite3.Error) as exc:
        if args.command == "context" and getattr(args, "format", None) in ("json", "json-compact"):
            if isinstance(exc, ProjectConfigError):
                code = "project_config_error"
            elif isinstance(exc, SchemaValidationError):
                code = "schema_invalid"
            else:
                code = "recallry_unavailable"
            print(json.dumps({
                "status": "error",
                "error": {"code": code, "message": str(exc)},
            }, ensure_ascii=False))
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _read_only_enabled() -> bool:
    return os.environ.get("RECALLRY_READONLY", "").lower() in {"1", "true", "yes", "on"}


def _metrics_enabled() -> bool:
    return os.environ.get("RECALLRY_METRICS", "").lower() in {"1", "true", "yes", "on"}


def _record_metrics(store: MetricsStore, event_type: str, **fields) -> None:
    if not _metrics_enabled():
        return
    try:
        store.append(event_type, **fields)
    except (OSError, sqlite3.Error, ValueError):
        # Telemetry is auxiliary. Its failure must never change the primary command result.
        return


def _context_payload(result, project: str | None, task: str) -> dict:
    knowledge = []
    for entry in result.included:
        item = entry.item
        knowledge.append({
            "id": item.id,
            "status": item.status,
            "scope": item.scope,
            "project": item.project,
            "category": item.category,
            "title": item.title,
            "content": entry.content,
        })
    return {
        "project_id": project,
        "task": task,
        "status": "ok",
        "included_count": len(knowledge),
        "included_chars": len(result.markdown),
        "truncated": result.truncated,
        "knowledge": knowledge,
        "markdown": result.markdown,
    }
