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
from .automatic_context import generate_automatic_context
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

    learn = commands.add_parser("learn", help="Evaluate a proposal using independent local evidence")
    learn.add_argument("--project-root", type=Path, required=True)
    learn.add_argument("--input", type=Path, required=True, help="Local evidence proposal JSON file")

    review = commands.add_parser("review", help="Manage consent for unattended AI review")
    actions = review.add_subparsers(dest="review_action", required=True)
    actions.add_parser("status", help="Show the current store's review settings")
    actions.add_parser("enable", help="Read the disclosure and grant consent interactively")
    actions.add_parser("disable", help="Turn off unattended AI review and revoke consent")

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


def _review(root: Path, action: str) -> int:
    from .review_settings import load_settings, save_settings

    if action != "status" and os.environ.get("RECALLRY_READONLY"):
        raise RecallryError("review settings cannot be changed in inherited read-only mode")
    database = Database(root / "data" / "recallry.db")
    if not (root / "config.toml").is_file() or not database.path.is_file():
        raise RecallryError("Store is not initialized. Run 'recallry init' first.")
    settings = load_settings(root)
    provider_label = {"claude": "Claude Code", "codex": "Codex"}[settings.provider]
    if action == "status":
        print(f"Store: {root}")
        print(f"Automatic review: {'ON' if settings.active else 'OFF'}")
        print(f"Provider: {provider_label}")
        print(f"Threshold: {settings.threshold} new eligible candidates")
        print(f"Consent: {'current' if settings.active else 'no active consent'}")
        return 0

    # Apply the same public-store identity and schema checks as other mutations.
    database.validate()
    enable = action == "enable"
    if enable:
        from .backup_policy import PersonalBackups
        from .review_providers import get_provider

        if settings.active:
            print("Automatic review is already ON.")
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RecallryError("review enable requires an interactive terminal for informed consent")
        if settings.provider != "claude":
            raise RecallryError(f"Unattended review is unavailable for {provider_label}.")
        print(f"Enable unattended Claude Code review for store {root}?")
        print(f"When {settings.threshold} new eligible candidates accumulate, Recallry may send "
              "their IDs, titles, content, categories and scope to Anthropic without asking again.")
        print("Claude receives the review instructions and normal request metadata. "
              "Reviews use your claude.ai subscription allowance.")
        print("Existing candidates are not enrolled. Local evidence checks still apply. "
              "Sensitive or conflicting claims require human review.")
        print("Run 'recallry review disable' to revoke consent for future sends. "
              "Content already sent cannot be recalled.")
        try:
            authorized = input("Type ENABLE to consent, or press Enter to cancel: ") == "ENABLE"
        except (EOFError, KeyboardInterrupt):
            authorized = False
        if not authorized:
            raise RecallryError("consent was not granted")
        readiness = get_provider(settings.provider).availability(check=True)
        if not readiness["available"]:
            raise RecallryError(readiness["message"])
        PersonalBackups(root).take("before")
    elif not settings.enabled and not settings.consent:
        print("Automatic review is already OFF. No active consent.")
        return 0
    # Revocation must work even if the provider or backup destination is offline.
    save_settings(root, enabled=enable, threshold=settings.threshold, provider=settings.provider,
                  consent=enable, expected_epoch=settings.epoch)
    print("Automatic review is ON." if enable else "Automatic review is OFF. Consent revoked.")
    return 0


def run(args: argparse.Namespace) -> int:
    root = resolve_recallry_root(args.root, os.environ)
    if args.command == "review":
        return _review(root, args.review_action)
    if args.command == "learn" and os.environ.get("RECALLRY_READONLY"):
        raise RecallryError("learning unavailable in inherited read-only mode")
    if _read_only_enabled() and args.command not in {"context", "search", "show", "list", "stats"}:
        raise RecallryError("write operation unavailable in read-only mode")
    def after_save():
        from .backup_policy import PersonalBackups
        from .backup import BACKUP_ERRORS
        try:
            PersonalBackups(root).take("write")
        except BACKUP_ERRORS as exc:
            print(f"Knowledge was saved; automatic backup failed: {exc}", file=sys.stderr)

    mutation = args.command in {"add", "learn", "promote", "reject", "deprecate"}
    database = Database(root / "data" / "recallry.db", on_saved=after_save if mutation else None)
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
    try:
        return _execute(args, root, config, database, metrics)
    finally:
        database.notify_saved()


def _execute(args, root, config, database, metrics) -> int:
    if args.command == "add":
        item = database.add(
            title=args.title, content=args.content, scope=args.scope, project=args.project,
            category=args.category, status=config.default_status, source=args.source,
        )
        _record_metrics(metrics, "candidate_created", project_id=item.project,
                        command="add", mode="manual", knowledge_ids=(item.id,),
                        origin=source_origin(item.source))
        print(f"Added {item.id} ({item.status}).")
    elif args.command == "learn":
        from .learning import learn, read_request

        decision = learn(database, args.project_root, read_request(args.input))
        if decision.get("created"):
            item = database.get(decision["item_id"])
            _record_metrics(metrics, "candidate_created", project_id=item.project, command="learn",
                            mode="automatic", knowledge_ids=(item.id,), origin="automatic")
        promoted = list(decision.get("automatic_review", {}).get("promoted", []))
        if decision.get("decision") == "promote":
            promoted.append(decision["item_id"])
        for identifier in promoted:
            item = database.get(identifier)
            _record_metrics(metrics, "knowledge_promoted", project_id=item.project, command="learn",
                            mode="automatic", knowledge_ids=(identifier,))
        print(json.dumps({"status": "ok", **decision}, ensure_ascii=False))
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
        if automatic:
            result = generate_automatic_context(database, project=project, task=args.task)
        else:
            result = generate_context(
                database, project=project, task=args.task,
                limit=args.limit or config.default_context_limit,
                content_chars=config.default_context_content_chars,
                max_chars=config.default_context_max_chars,
                verified_only=args.verified_only,
                increment_usage=not _read_only_enabled(),
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
