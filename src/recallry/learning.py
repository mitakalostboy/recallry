"""Evidence-gated task-completion learning, without inference or executable evidence.

Only claim proposals are accepted, never an ID/status/action supplied by an agent.
Independent local checks establish source identity, exact support and unchanged
committed artifacts. They do not establish arbitrary semantic truth. Uncertain,
high-impact and conflicting material stays candidate for human review.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import unicodedata

from .db import RecallryError, Database, now_utc
from .connect import validate_project_id
from .models import CATEGORIES
from .project_config import load_project_identity

POLICY = "evidence-gated-v1"
SOURCE_PREFIX = "agent-suggested automatic-candidate " + POLICY + " "
PENDING_POLICY = "pending-knowledge-v2"
PENDING_PREFIX = "agent-suggested automatic-candidate " + PENDING_POLICY + " "
MAX_ARTIFACT = 1024 * 1024

# These gates are deliberately conservative and are not a general semantic risk
# classifier. The router must also hold important/ambiguous proposals. Source
# support is checked independently of any model-supplied label or confidence.
HIGH_IMPACT = re.compile(
    r"\b(trad(?:e|ing)|orders?|positions?|financial|money|funds?|capital|portfolio|"
    r"invest\w*|leverage|profit|loss|risk|payment|billing|tax|bank|stake|wager|"
    r"secur\w*|auth\w*|access|permission\w*|password|secret\w*|credential\w*|"
    r"token|encrypt\w*|decrypt\w*|rbac|acl|sudo|chmod|chown|firewall|"
    r"destruct\w*|destroy\w*|eras\w*|delet\w*|remov\w*|drop|truncate|wipe|purge|overwrite|reset|"
    r"production|deploy\w*|migrat\w*|backup\w*|recover\w*|restor\w*|rollback|"
    r"safety|live[- ]?data|predict\w*|forecast\w*|pit|point-in-time|leakage|look-ahead)\b|"
    r"model.{0,20}(?:adopt|approv|selection)|rm\s+-|rm\(|unlink\(|"
    r"売買|取引|発注|注文|金融|資金|資産|投資|損失|利益|口座|課金|決済|税|"
    r"セキュリティ|認証|認可|権限|アクセス|秘密|資格情報|パスワード|暗号|"
    r"破壊|削除|消去|上書き|本番|デプロイ|移行|バックアップ|復旧|復元|安全|"
    r"予測|モデル.{0,10}(採用|導入)|先読み|リーク|リーケージ|時点整合",
    re.I,
)
INJECTION = re.compile(
    r"ignore.{0,40}(previous|instruction)|system\s*(prompt|message)|"
    r"developer\s*message|you are (?:an? |the )?(?:ai|assistant)|"
    r"(?:recallry\s+)?(?:promote|deprecate|reject)\b|mark.{0,20}verified|"
    r"<\|(?:system|assistant)|\[INST\]|mcp__|"
    r"指示.{0,15}(無視|従)|システムプロンプト|自動.{0,8}(承認|昇格)|"
    r"検証済み.{0,8}(扱|変更)|秘密.{0,10}送信", re.I,
)
SPECULATIVE = re.compile(r"\b(maybe|perhaps|might|probably|speculat\w*|todo)\b|かもしれ|推測|未確認|おそらく", re.I)
TRANSIENT = re.compile(r"\b(currently (?:broken|fails?|missing)|not (?:yet )?implemented|tests? passed|task complete)\b|"
                       r"作業完了|テスト.{0,10}件.{0,5}成功|現時点.{0,12}(未実装|不具合)", re.I)


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RecallryError(f"{name} must be nonempty text (up to {maximum} characters)")
    if any(unicodedata.category(c) == "Cf" for c in value):
        raise RecallryError(f"{name} contains hidden format characters")
    return value.strip()


def read_request(path: Path):
    with path.open("rb") as handle:
        raw = handle.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RecallryError("learning request is too large")
    def unique(pairs):
        value = {}
        for key, field in pairs:
            if key in value:
                raise RecallryError("duplicate JSON field")
            value[key] = field
        return value
    return json.loads(raw, object_pairs_hook=unique)


def _git(project_root, *args):
    # Read objects only: no diff drivers, hooks, credential prompts, lazy fetch,
    # inherited Git redirections or writes to Git administrative state.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
               GIT_ALLOW_PROTOCOL="", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["/usr/bin/git", "--no-optional-locks", "--literal-pathspecs",
                             "-c", "core.fsmonitor=false", "-C", str(project_root), *args],
                            stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=5, check=False)
    if result.returncode:
        raise ValueError("committed artifact is unavailable")
    return result.stdout


def _artifact(project_root, evidence):
    """Read a bounded regular file without following any path component links."""
    path = evidence.get("path")
    if (not isinstance(path, str) or not path or len(path) > 500 or "\\" in path
            or any(part.startswith(".") for part in path.split("/"))
            or PurePosixPath(path).is_absolute() or str(PurePosixPath(path)) != path):
        raise ValueError("invalid evidence path")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("safe local evidence access unavailable")
    fd = os.open(project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT:
                raise ValueError("invalid evidence file")
            raw = handle.read(MAX_ARTIFACT + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_ARTIFACT or digest(raw) != evidence.get("sha256"):
        raise ValueError("artifact hash mismatch")
    top = Path(os.fsdecode(_git(project_root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != project_root:
        raise ValueError("evidence project must be the repository root")
    commit = _git(project_root, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("invalid artifact revision")
    entries = _git(project_root, "ls-tree", "-z", commit, "--", path).split(b"\0")
    if len(entries) != 2 or not entries[0].startswith((b"100644 blob ", b"100755 blob ")):
        raise ValueError("artifact is not a committed regular file")
    header, stored_path = entries[0].split(b"\t", 1)
    if stored_path != os.fsencode(path):
        raise ValueError("artifact path mismatch")
    blob_id = header.split()[2].decode()
    size = int(_git(project_root, "cat-file", "-s", blob_id))
    if size != len(raw) or size > MAX_ARTIFACT:
        raise ValueError("artifact changed since commit")
    if _git(project_root, "cat-file", "blob", blob_id) != raw:
        raise ValueError("artifact is uncommitted or modified")
    return raw.decode("utf-8"), {"path": path, "sha256": digest(raw), "commit": commit,
                                  "kind": evidence["kind"]}


def _support(project_root, proposal, evidence, project):
    """Return audited references, never a model's confidence/approved flag."""
    if not evidence:
        return [], "missing_evidence"
    references = []
    for entry in evidence:
        if (not isinstance(entry, dict) or not isinstance(entry.get("kind"), str)
                or entry["kind"] not in {"artifact", "user_decision"}):
            return references, "unsupported_evidence_kind"
        expected = {"kind", "path", "sha256", "quote"} if entry["kind"] == "artifact" else {
            "kind", "path", "sha256", "record"}
        if set(entry) != expected:
            return references, "ambiguous_evidence"
        try:
            text, reference = _artifact(project_root, entry)
            if entry["kind"] == "artifact":
                quote = entry["quote"]
                # No arbitrary paraphrase receives verified status: every byte
                # of the saved assertion must have independent source support.
                if not isinstance(quote, str) or quote.strip() != proposal["content"] or quote not in text:
                    return references, "unsupported_claim"
                if proposal["scope"] == "global":
                    return references, "project_artifact_not_global"
                reference["quote_sha256"] = digest(quote.encode())
            else:
                # A user-role flag submitted by the generating agent is not
                # evidence. The entire confirmed decision must already exist in
                # the unchanged committed project artifact, with its scope.
                records = json.loads(text)
                index = entry["record"]
                if type(index) is not int or not isinstance(records, list) or not 0 <= index < len(records):
                    return references, "invalid_user_record"
                record = records[index]
                if (not isinstance(record, dict) or record.get("confirmed") is not True
                        or record.get("role") != "user"
                        or any(record.get(key) != proposal[key] for key in ("title", "content", "scope", "category"))
                        or record.get("project") != (project if proposal["scope"] == "project" else None)):
                    return references, "unconfirmed_user_record"
                reference["record"] = index
                reference["record_sha256"] = digest(json.dumps(record, sort_keys=True, ensure_ascii=False).encode())
            references.append(reference)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError):
            return references, "unverifiable_artifact"
    return references, None


def _related(left, right):
    a, b = normalized(left), normalized(right)
    if a == b or a in b or b in a:
        return True
    # Conservative relatedness: matched titles/subjects, substantial common
    # words, or Japanese character bigrams. This is a hold gate, not ranking.
    def terms(text):
        words = set(re.findall(r"[a-z0-9_./-]{3,}", text)) - {"the", "and", "for", "use", "with", "this"}
        for run in re.findall(r"[一-龯ぁ-んァ-ヶ]+", text):
            words.update(run[i:i + 2] for i in range(len(run) - 1))
        return words
    aa, bb = terms(a), terms(b)
    shared = aa & bb
    return bool(shared) and (len(shared) >= 2 and len(shared) / max(1, min(len(aa), len(bb))) >= .5)


def learning_history(source):
    """Recover exact original provenance and local evidence events, never instructions."""
    original, events = source, []
    try:
        if source and source.startswith(PENDING_PREFIX):
            wrapper = json.loads(source[len(PENDING_PREFIX):])
            if (not isinstance(wrapper, dict) or wrapper.get("policy") != PENDING_POLICY
                    or not isinstance(wrapper.get("events"), list)
                    or not all(isinstance(event, dict) for event in wrapper["events"])
                    or not isinstance(wrapper.get("original_source"), (str, type(None)))):
                raise ValueError("invalid pending audit")
            original, events = wrapper["original_source"], wrapper["events"]
            if original and original.startswith(PENDING_PREFIX):
                raise ValueError("nested pending audit")
        if original and original.startswith(SOURCE_PREFIX):
            initial = json.loads(original[len(SOURCE_PREFIX):])
            if not isinstance(initial, dict) or initial.get("policy") != POLICY or not isinstance(initial.get("evidence"), list):
                raise ValueError("invalid initial audit")
            events = [initial, *events]
        if any(not isinstance(event.get("evidence"), list)
               or not all(isinstance(ref, dict) for ref in event["evidence"]) for event in events):
            raise ValueError("invalid evidence history")
        return original, events, False
    except (ValueError, TypeError, RecursionError):
        return source, [], True


_RESTRICTED_REASONS = {"untrusted_instruction", "human_review_high_impact", "human_review_requested",
                       "possible_verified_conflict", "invalid_learning_audit"}


def _restriction(item):
    material = unicodedata.normalize("NFKC", item.title + "\n" + item.content)
    if HIGH_IMPACT.search(material) or INJECTION.search(material) or item.category == "warning":
        return "human_review_high_impact"
    original, events, invalid = learning_history(item.source)
    if invalid:
        return "invalid_learning_audit"
    for event in events:
        if event.get("reason") in _RESTRICTED_REASONS or event.get("review_required") is True:
            return event.get("reason") if event.get("reason") in _RESTRICTED_REASONS else "human_review_requested"
    if not events and original and re.search(r"\b(conflict|contradict\w*)\b|矛盾|競合", original, re.I):
        return "possible_verified_conflict"
    return None


def _proofs(references):
    # Advancing HEAD without changing a proof is not a new evidence event.
    return sorted({json.dumps({key: value for key, value in entry.items() if key != "commit"},
                              sort_keys=True, ensure_ascii=False) for entry in references})


def _evidence_state(evidence, references, problem):
    submitted = sorted({json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in evidence})
    return digest(json.dumps([submitted, _proofs(references), problem], ensure_ascii=False).encode())


def _support_ids(references, *, with_hash=False):
    # For an already identical claim, a new quote boundary/record index or an
    # unrelated edit to the same artifact is not a new independent source.
    keys = ("kind", "path", "sha256") if with_hash else ("kind", "path")
    return {json.dumps({key: reference.get(key) for key in keys}, sort_keys=True) for reference in references}


def _unchanged(events, state, references, problem, request_hash):
    # Inference attempts are not new independent evidence events. No-op
    # detection must still compare the last local proof, including in flight.
    events = [event for event in events if event.get("policy") != "unattended-review-v1"]
    if not events:
        return False  # first independently checked proof for an explicitly matched legacy item
    previous = events[-1]
    if "evidence_state" in previous:
        return (previous["evidence_state"] == state or (
            bool(references) and problem is None and previous.get("evidence_problem") is None
            and _support_ids(previous["evidence"], with_hash=True) == _support_ids(references, with_hash=True)))
    # An older audit may lack raw attempted evidence. Its verified references and exact
    # request digest suffice for no-ops; an unknown first event is checked once.
    old_proofs = _proofs(previous.get("evidence", []))
    if references and problem is None and old_proofs == _proofs(references):
        return True
    return (previous.get("request_sha256") == request_hash and not references
            and not old_proofs and previous.get("reason") == problem)


def _pending_source(item, event):
    _original, _events, invalid = learning_history(item.source)
    if item.source and item.source.startswith(PENDING_PREFIX) and not invalid:
        wrapper = json.loads(item.source[len(PENDING_PREFIX):])
        wrapper["events"].append(event)
    else:
        wrapper = {"policy": PENDING_POLICY, "original_source": item.source, "events": [event]}
    return PENDING_PREFIX + json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))


def _conflict(visible, proposal, *, excluding=None):
    # Verified conflicts take precedence over candidate similarity regardless of
    # list ordering, so an explicit conflict remains durably restricted.
    for status, reason in (("verified", "possible_verified_conflict"), ("candidate", "possible_duplicate")):
        for item in visible:
            if item.id != excluding and item.status == status and (
                    _related(item.title, proposal["title"]) or _related(item.content, proposal["content"])):
                return reason
    return None


def _reconsider(transaction, item, visible, proposal, evidence, project_root, project, reason, request_hash):
    _original, events, invalid = learning_history(item.source)
    if invalid:
        return {"decision": "hold", "reason": "invalid_learning_audit", "item_id": item.id,
                "status": item.status, "created": False, "reevaluated": False, "policy": POLICY}
    # Independent local proof preflight discovers commitment/availability changes
    # even when the request bytes are unchanged. It never invokes inference.
    references, problem = _support(project_root, proposal, evidence, project)
    state = _evidence_state(evidence, references, problem)
    new_veto = proposal.get("review_required") and not _restriction(item)
    if not new_veto and _unchanged(events, state, references, problem, request_hash):
        return {"decision": "skip", "reason": "unchanged_evidence", "item_id": item.id,
                "status": item.status, "created": False, "reevaluated": False, "policy": POLICY}
    old_proofs = {proof for event in events for proof in _support_ids(event["evidence"])}
    relevant = bool(_support_ids(references) - old_proofs)
    conflict = _conflict(visible, proposal, excluding=item.id) if relevant else None
    reason = (_restriction(item) or reason
              or (conflict if conflict == "possible_verified_conflict" else None) or problem or conflict)
    if not relevant:
        reason = reason or "no_new_independent_evidence"
    decision = "hold" if reason else "promote"
    reason = reason or "new_independent_evidence_same_claim"
    timestamp = now_utc()
    event = {"policy": PENDING_POLICY, "decision": decision, "reason": reason, "evidence": references,
             "evidence_state": state, "request_sha256": request_hash, "evaluated_at": timestamp,
             "evidence_problem": problem, "relevant_progress": relevant,
             "review_required": proposal.get("review_required", False)}
    transaction.annotate_candidate(item.id, expected_source=item.source, source=_pending_source(item, event),
                                   progress_at=timestamp if relevant else None)
    if decision == "promote":
        item, _changed = transaction.transition(item.id, "promote")
    return {"decision": decision, "reason": reason, "item_id": item.id, "status": item.status,
            "created": False, "reevaluated": relevant, "policy": POLICY, "pending_policy": PENDING_POLICY,
            "evidence": references}


def learn(database: Database, project_root: Path, request):
    from .review_settings import project_writable
    if not project_writable(project_root):
        raise RecallryError("automatic learning unavailable in read-only/no-write project")
    result = _learn(database, project_root, request)
    # No-op/duplicate events do not poll or review a queue. Only a real creation
    # or new evidence event may trigger opt-in review.
    if result.get("created") or result.get("reevaluated"):
        from .automatic_review import maybe_review
        result["automatic_review"] = maybe_review(database, project_root)
        # The current candidate can have been promoted in the batch.
        if result.get("item_id"):
            result["status"] = database.get(result["item_id"]).status
    return result


def _learn(database: Database, project_root: Path, request):
    # Stricter than manual CLI legacy truthiness: the router's inherited
    # nonempty read-only contract must also hold for direct API callers.
    if os.environ.get("RECALLRY_READONLY"):
        raise RecallryError("automatic learning unavailable in inherited read-only mode")
    project_root = project_root.resolve(strict=True)
    if (project_root / ".recallry.toml").is_symlink():
        raise RecallryError("project identity must not be a symbolic link")
    project = validate_project_id(load_project_identity(project_root).project_id)
    required = {"title", "content", "scope", "category", "usefulness", "evidence"}
    if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {"review_required", "review_uncertain"}:
        raise RecallryError("learning request requires title/content/scope/category/usefulness/evidence and optional review_required")
    if type(request.get("review_required", False)) is not bool:
        raise RecallryError("review_required must be boolean")
    if type(request.get("review_uncertain", False)) is not bool:
        raise RecallryError("review_uncertain must be boolean")
    proposal = {key: _text(request[key], key, 300 if key == "title" else 4000)
                for key in ("title", "content", "usefulness")}
    proposal.update(scope=request["scope"], category=request["category"])
    proposal["review_required"] = request.get("review_required", False)
    if proposal["scope"] not in ("global", "project") or proposal["category"] not in CATEGORIES:
        raise RecallryError("invalid learning scope or category")
    evidence = request["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 4:
        raise RecallryError("evidence must be an array of up to four references")
    evidence = sorted(evidence, key=lambda entry: json.dumps(entry, sort_keys=True, ensure_ascii=False))
    item_project = project if proposal["scope"] == "project" else None
    material = unicodedata.normalize("NFKC", proposal["title"] + "\n" + proposal["content"])
    reason = None
    if INJECTION.search(material):
        reason = "untrusted_instruction"
    elif HIGH_IMPACT.search(material):
        reason = "human_review_high_impact"
    elif request.get("review_required") or proposal["category"] == "warning":
        reason = "human_review_requested"
    low_value = None
    if SPECULATIVE.search(material) or TRANSIENT.search(material):
        low_value = "speculative_or_transient"
    elif len(proposal["content"]) < 12 or len(proposal["usefulness"]) < 15:
        low_value = "insufficient_reuse_value"
    request_hash = digest(json.dumps(request, sort_keys=True, ensure_ascii=False).encode())
    if proposal["scope"] == "global" and (project.casefold() in normalized(material)
                                             or re.search(r"[\\/]|\.[a-z]{1,5}\b|\b[A-Za-z]+_[A-Za-z_]+\b", material)):
        reason = reason or "global_contains_project_details"
    with database.transaction() as transaction:
        # Global sees only global; project sees global and its own project.
        visible = transaction.list(scope="global")
        if proposal["scope"] == "project":
            visible += transaction.list(scope="project", project=item_project)
        matches = [item for item in visible if normalized(item.content) == normalized(proposal["content"])]
        if matches:
            verified = next((item for item in matches if item.status == "verified"), None)
            exact = [item for item in matches if item.status == "candidate" and
                     all(getattr(item, key) == proposal[key] for key in ("title", "content", "category", "scope"))
                     and item.project == item_project]
            if not verified and len(exact) == 1:
                item = exact[0]
                return _reconsider(transaction, item, visible, proposal, evidence, project_root, project,
                                   reason or low_value or ("ordinary_uncertainty" if request.get("review_uncertain") else None), request_hash)
            item = verified or matches[0]
            return {"decision": "skip", "reason": "existing_knowledge", "item_id": item.id,
                    "status": item.status, "created": False, "reevaluated": False, "policy": POLICY}
        conflict = _conflict(visible, proposal)
        if not reason and not conflict and low_value:
            return {"decision": "skip", "reason": low_value, "item_id": None, "created": False, "policy": POLICY}
        # A vague ordinary assertion with no identifiable evidence lead adds no
        # future review value. Important/conflicting unresolved claims survive.
        evidence_lead = any(isinstance(entry, dict) and entry.get("kind") in ("artifact", "user_decision")
                            and isinstance(entry.get("path"), str) and entry["path"] for entry in evidence)
        if not reason and not conflict and not evidence_lead:
            return {"decision": "skip", "reason": "no_independent_evidence_lead", "item_id": None,
                    "created": False, "policy": POLICY}
        references, evidence_reason = _support(project_root, proposal, evidence, project)
        reason = (reason or (conflict if conflict == "possible_verified_conflict" else None)
                  or low_value or evidence_reason or conflict
                  or ("ordinary_uncertainty" if request.get("review_uncertain") else None))
        # All accepted writes, including audit metadata, roll back together.
        decision = "hold" if reason else "promote"
        reason = reason or "unchanged_committed_evidence_exact_support"
        audit = {"policy": POLICY, "decision": decision, "reason": reason, "evidence": references,
                 "request_sha256": request_hash, "evidence_state": _evidence_state(evidence, references, evidence_reason),
                 "evidence_problem": evidence_reason, "evaluated_at": now_utc(), "relevant_progress": bool(references),
                 "review_required": proposal["review_required"]}
        from .automatic_review import enrollment
        ticket = enrollment(database, project_root, proposal, evidence, reason if decision == "hold" else None)
        if ticket:
            audit["review_enrollment"] = ticket
        item = transaction.add(title=proposal["title"], content=proposal["content"],
                               scope=proposal["scope"], project=item_project, category=proposal["category"],
                               source=SOURCE_PREFIX + json.dumps(audit, ensure_ascii=False, separators=(",", ":")))
        if decision == "promote":
            item, _changed = transaction.transition(item.id, "promote")
        return {"decision": decision, "reason": reason, "item_id": item.id, "status": item.status,
                "created": True, "policy": POLICY, "pending_policy": PENDING_POLICY, "evidence": references}
