"""Fix F2 top-5 retrieval for automatic verified context only.

Fix F2 Variant A with project boost; keep every eligible top-five candidate.
The manual E0 selector and production renderer remain in context.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from .context import (
    ContextResult, _ASCII_TOKEN_CHARS, _ASCII_TRIM_CHARS,
    _fetch_candidates, _is_cjk, _render_context,
)
from .db import Database
from .models import Knowledge

CANDIDATE_LIMIT = 5
PROJECT_SCOPE_BOOST = 1
_GLOBAL_MIN_DISTINCT = 2
_PROJECT_MIN_DISTINCT = 1
_HIRAGANA_LO, _HIRAGANA_HI = 0x3040, 0x309F

_ENGLISH_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is",
    "are", "was", "were", "this", "that", "these", "those", "with", "as",
    "by", "at", "be", "it", "its", "from", "not", "do", "does", "did",
    "if", "then", "than", "so", "but", "into", "onto", "about", "can",
    "will", "should", "would", "there", "here", "i", "we", "you", "my",
    "our", "want", "please",
})


def _has_hiragana(token: str) -> bool:
    return any(_HIRAGANA_LO <= ord(ch) <= _HIRAGANA_HI for ch in token)


def _term_matches(term: str, text: str) -> bool:
    """Match CJK substrings and ASCII token boundaries, as in frozen Fix F2."""
    if any(_is_cjk(ch) for ch in term):
        return term in text
    idx = text.find(term)
    while idx != -1:
        before_ok = idx == 0 or text[idx - 1] not in _ASCII_TOKEN_CHARS
        after = idx + len(term)
        after_ok = after >= len(text) or text[after] not in _ASCII_TOKEN_CHARS
        if before_ok and after_ok:
            return True
        idx = text.find(term, idx + 1)
    return False


def _cjk_script(ch: str) -> str:
    """Script transitions delimit evidence spans inside a contiguous CJK run."""
    cp = ord(ch)
    if 0x3040 <= cp <= 0x309F:
        return "hiragana"
    if (0x30A0 <= cp <= 0x30FF) or (0x31F0 <= cp <= 0x31FF) or (0xFF65 <= cp <= 0xFF9F):
        return "katakana"
    if 0xAC00 <= cp <= 0xD7A3:
        return "hangul"
    return "kanji"  # CJK ideograph + compatibility ideograph ranges


def _tokenize_word_with_spans(word: str, start_span_id: int) -> tuple[list[tuple[str, int]], int]:
    """Tag same-script bigrams with one span; each script boundary gets its own."""
    tokens: list[tuple[str, int]] = []
    run: list[str] = []
    run_kind: str | None = None
    span_id = start_span_id

    def flush() -> None:
        nonlocal span_id
        if not run:
            return
        if run_kind == "ascii":
            trimmed = "".join(run).strip(_ASCII_TRIM_CHARS)
            if trimmed:
                tokens.append((trimmed, span_id))
                span_id += 1
        elif run_kind == "cjk":
            if len(run) == 1:
                tokens.append((run[0], span_id))
                span_id += 1
            else:
                groups = [0]
                for i in range(1, len(run)):
                    same = _cjk_script(run[i]) == _cjk_script(run[i - 1])
                    groups.append(groups[-1] if same else groups[-1] + 1)
                group_span_id: dict[tuple, int] = {}
                for i in range(len(run) - 1):
                    key = ("same", groups[i]) if groups[i] == groups[i + 1] else ("boundary", i)
                    if key not in group_span_id:
                        group_span_id[key] = span_id
                        span_id += 1
                    tokens.append((run[i] + run[i + 1], group_span_id[key]))
        run.clear()

    for ch in word:
        if _is_cjk(ch):
            if run_kind != "cjk":
                flush()
                run_kind = "cjk"
            run.append(ch)
        elif ch in _ASCII_TOKEN_CHARS:
            if run_kind != "ascii":
                flush()
                run_kind = "ascii"
            run.append(ch)
        else:
            flush()
            run_kind = None
    flush()
    return tokens, span_id


def _tokenize_with_spans(task: str) -> list[tuple[str, int]]:
    lowered = task.lower()
    if not any(_is_cjk(ch) for ch in lowered):
        return [(term, i) for i, term in enumerate(t for t in lowered.split() if t)]
    tokens: list[tuple[str, int]] = []
    span_id = 0
    for word in lowered.split():
        word_tokens, span_id = _tokenize_word_with_spans(word, span_id)
        tokens.extend(word_tokens)
    return tokens


def fixf2_spans(task: str) -> dict[int, list[str]]:
    """Filter hiragana/stopwords, then merge spans with identical surviving term sets."""
    positional: dict[int, list[str]] = {}
    for term, span_id in _tokenize_with_spans(task):
        if any(_is_cjk(ch) for ch in term):
            if _has_hiragana(term):
                continue
        elif term in _ENGLISH_STOPWORDS:
            continue
        positional.setdefault(span_id, []).append(term)

    merged: dict[frozenset, list[str]] = {}
    for terms in positional.values():
        if not terms:
            continue
        merged.setdefault(frozenset(terms), terms)
    return dict(enumerate(merged.values()))


def fixf2_matched_span_count(item: Knowledge, spans: dict[int, list[str]]) -> int:
    """Each matched span contributes at most one evidence credit."""
    text = f"{item.title} {item.content} {item.category}".lower()
    return sum(
        1 for terms in spans.values()
        if any(_term_matches(term, text) for term in terms)
    )


@dataclass(frozen=True)
class ScoredCandidate:
    item: Knowledge
    span_count: int
    rank_score: int
    boosted: bool


def automatic_candidates(
    database: Database, *, project: str | None, task: str,
) -> list[ScoredCandidate]:
    """Fixed top-5 Fix F2 Variant A; boost affects rank, never eligibility."""
    spans = fixf2_spans(task)
    items = _fetch_candidates(database, project=project, verified_only=True)
    eligible = []
    for item in items:
        span_count = fixf2_matched_span_count(item, spans)
        threshold = _PROJECT_MIN_DISTINCT if item.scope == "project" else _GLOBAL_MIN_DISTINCT
        if span_count < threshold:
            continue
        boost = PROJECT_SCOPE_BOOST if item.scope == "project" else 0
        eligible.append(ScoredCandidate(item, span_count, span_count + boost, boost > 0))
    # Frozen tie-breaks. SQL's id-ascending order survives equal keys via stable sort.
    eligible.sort(
        key=lambda s: (
            s.rank_score + (1 if s.item.status == "verified" else 0),
            1 if s.item.status == "verified" else 0,
            s.rank_score,
            s.item.updated_at,
        ),
        reverse=True,
    )
    return eligible[:CANDIDATE_LIMIT]


def select_automatic_context(
    database: Database, *, project: str | None, task: str,
) -> list[Knowledge]:
    return [candidate.item for candidate in
            automatic_candidates(database, project=project, task=task)]


def generate_automatic_context(
    database: Database, *, project: str | None, task: str,
) -> ContextResult:
    """Read-only automatic path, under the unchanged production rendering budget."""
    return _render_context(
        select_automatic_context(database, project=project, task=task),
        project=project, task=task, content_chars=600, max_chars=6000,
    )
