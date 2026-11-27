"""Event-driven opt-in review. Enrollment and attempts live in the existing source audit.

Only new candidates enrolled during the current consent epoch are eligible.
An attempt is durably consumed before inference; crashes/failures never retry it.
The existing local evidence and human veto gates are rechecked before promotion.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
import uuid

from . import learning as rules
from .db import RecallryError, now_utc
from .project_config import load_project_identity
from .review_providers import MAX_REVIEW_STDIN_BYTES, get_provider
from .review_settings import load_settings, project_writable, store_lock

POLICY = "unattended-review-v1"
MAX_BATCH = 20


def enrollment(database, project_root, proposal, evidence, reason):
    """Called only while creating a NEW candidate, never for legacy or duplicates."""
    if (not reason or reason in rules._RESTRICTED_REASONS
            or proposal.get("review_required") or proposal["category"] == "warning"
            or reason not in {"ordinary_uncertainty", "unverifiable_artifact", "missing_evidence",
                              "unsupported_claim", "possible_duplicate"}):
        return None
    try:
        settings = load_settings(database.path.parent.parent)
        if not settings.active or not project_writable(project_root):
            return None
        # Round-trip bounds and isolates mutable caller-owned references.
        if len(json.dumps(evidence).encode()) > 32 * 1024:
            return None
        return {"epoch": settings.epoch, "project_root": str(project_root),
                "project": load_project_identity(project_root).project_id,
                "evidence": json.loads(json.dumps(evidence))}
    except (OSError, ValueError, RecallryError):
        return None  # invalid config cannot enable unattended processing


def _ticket(item, project_root, project, settings):
    if item.status != "candidate" or rules._restriction(item):
        return None
    _original, events, invalid = rules.learning_history(item.source)
    if invalid or not events or any(e.get("policy") == POLICY for e in events):
        return None
    ticket = events[0].get("review_enrollment")
    if (not isinstance(ticket, dict) or ticket.get("epoch") != settings.epoch
            or ticket.get("project_root") != str(project_root) or ticket.get("project") != project
            or not isinstance(ticket.get("evidence"), list)):
        return None
    if item.scope == "project" and item.project != project:
        return None
    return ticket


def _visible(db, project):
    return db.list(scope="global") + db.list(scope="project", project=project)


def _proposal(item):
    return {key: getattr(item, key) for key in ("title", "content", "category", "scope")}


def _event(**fields):
    return {"policy": POLICY, "evidence": [], "evaluated_at": now_utc(),
            "relevant_progress": False, **fields}


def _append(db, item, event):
    return db.annotate_candidate(item.id, expected_source=item.source,
                                 source=rules._pending_source(item, event))


def _valid_response(response, ids):
    if not isinstance(response, dict) or set(response) != {"reviews"}:
        raise ValueError("invalid provider response")
    reviews = response["reviews"]
    if not isinstance(reviews, list) or len(reviews) != len(ids):
        raise ValueError("missing or extra review")
    results = {}
    for review in reviews:
        if (not isinstance(review, dict) or set(review) != {"id", "decision"}
                or not isinstance(review["id"], str) or review["id"] not in ids
                or review["id"] in results or review["decision"] not in ("approve", "hold", "human_review")):
            raise ValueError("invalid review identity or decision")
        results[review["id"]] = review["decision"]
    return results


def maybe_review(database, project_root: Path):
    """One bounded batch per learn event; never schedules work or retries."""
    if os.environ.get("RECALLRY_READONLY"):
        return {"state": "read_only"}
    root = database.path.parent.parent
    try:
        project_root = project_root.resolve(strict=True)
        if not project_writable(project_root):
            return {"state": "project_no_write"}
        settings = load_settings(root)
        if not settings.active:
            return {"state": "disabled"}
        # Nonblocking across threads/processes also prevents recursive calls.
        database.notify_saved()  # the learned candidate is durable before another transaction starts
        with store_lock(root, ".automatic-review-run.lock", blocking=False):
            return _run(database, project_root, settings)
    except BlockingIOError:
        return {"state": "busy"}
    except (OSError, ValueError, RecallryError):
        return {"state": "unavailable"}


def _run(database, project_root, settings):
    root = database.path.parent.parent
    project = load_project_identity(project_root).project_id
    attempt = uuid.uuid4().hex
    with store_lock(root, ".automatic-review-settings.lock"), database.transaction() as transaction:
        if load_settings(root) != settings or not project_writable(project_root):
            return {"state": "settings_changed"}
        visible = _visible(transaction, project)
        pending = []
        for item in visible:
            ticket = _ticket(item, project_root, project, settings)
            if ticket and rules._conflict(visible, _proposal(item), excluding=item.id) != "possible_verified_conflict":
                pending.append((item, ticket))
        if len(pending) < settings.threshold:
            return {"state": "below_threshold"}
        # Oldest first, bounded count and total input bytes. Do not rewrite backlog.
        batch, payload_items = [], []
        for item, ticket in sorted(pending, key=lambda pair: (pair[0].created_at, pair[0].id))[:MAX_BATCH]:
            entry = {"id": item.id, **_proposal(item)}
            # Match Claude's UTF-8 stdin, including the outer object and JSON escaping.
            payload = {"candidates": payload_items + [entry]}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_REVIEW_STDIN_BYTES:
                break
            payload_items.append(entry)
            marked = _append(transaction, item, _event(attempt=attempt, state="started", provider=settings.provider,
                epoch=settings.epoch, reason="unattended_attempt_consumed",
                payload_sha256=rules.digest(json.dumps(entry, sort_keys=True).encode())))
            batch.append((marked, ticket))
    database.notify_saved()  # protect the consumed attempt before calling a provider
    if not batch:
        return {"state": "empty_batch"}

    @contextmanager
    def launch_guard():
        with store_lock(root, ".automatic-review-settings.lock"):
            if load_settings(root) != settings or not project_writable(project_root):
                raise ValueError("consent changed")
            yield

    decisions, error = {}, None
    try:
        if load_settings(root) != settings or not project_writable(project_root):
            raise ValueError("consent changed")
        # Providers receive claims only; never store paths, evidence commands,
        # repository instructions, prior source logs, or another project's items.
        provider = get_provider(settings.provider, launch_guard=launch_guard)
        response = provider.review({"candidates": payload_items})
        decisions = _valid_response(response, {item.id for item, _ in batch})
    except Exception:
        # Do not persist provider stderr/content, credentials, or untrusted errors.
        error = "provider_failed_or_invalid_response"
    promoted = []
    # Serialize final validation/commit with consent saves. OFF remains writable
    # while inference runs, but cannot race a promotion commit after revocation.
    with store_lock(root, ".automatic-review-settings.lock"), database.transaction() as transaction:
        if load_settings(root) != settings or not project_writable(project_root):
            return {"state": "settings_changed", "promoted": []}
        visible = _visible(transaction, project)
        for snapshot, ticket in batch:
            item = transaction.get(snapshot.id)
            # Concurrent manual or evidence-based changes win. No stale approval is applied.
            if item != snapshot:
                continue
            proposal = _proposal(item)
            refs, problem = rules._support(project_root, proposal, ticket["evidence"], project)
            choice = decisions.get(item.id, "hold")
            reason = (rules._restriction(item) or error
                      or ("human_review_requested" if choice == "human_review" else None)
                      or rules._conflict(visible, proposal, excluding=item.id)
                      or problem or ("ai_held" if choice != "approve" else None))
            # Global proposals still need a scope-matched committed user decision.
            if item.scope == "global" and not any(ref.get("kind") == "user_decision" for ref in refs):
                reason = reason or "global_requires_user_decision"
            decision = "hold" if reason else "promote"
            _append(transaction, item, _event(attempt=attempt, state="finished", provider=settings.provider,
                epoch=settings.epoch, decision=decision, ai_decision=choice, evidence=refs,
                reason=reason or "ai_review_and_independent_evidence", evidence_problem=problem,
                review_required=choice == "human_review"))
            if decision == "promote":
                _updated, changed = transaction.transition(item.id, "promote")
                if changed:
                    promoted.append(item.id)
                    visible = _visible(transaction, project)
    return {"state": "failed" if error else "reviewed", "attempt": attempt,
            "reviewed": len(batch), "promoted": promoted}
