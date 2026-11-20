"""fzf-style extended search (``-e``) with smartcase.

A query is a space separated AND of clauses; ``|`` separates OR alternatives
inside one clause. Each alternative may be negated with ``!`` and anchored with
a leading ``^`` or a trailing ``$``. Matching is substring based, preserves the
input row order and reports the character spans that matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    text: str
    negated: bool = False
    prefix: bool = False
    suffix: bool = False

    @property
    def case_sensitive(self) -> bool:
        return any(character.isupper() for character in self.text)


def _parse_term(raw: str) -> Term:
    negated = raw.startswith("!")
    if negated:
        raw = raw[1:]
    prefix = raw.startswith("^")
    if prefix:
        raw = raw[1:]
    suffix = raw.endswith("$") and len(raw) > 1
    if suffix:
        raw = raw[:-1]
    if raw.startswith("'"):
        raw = raw[1:]
    return Term(raw, negated, prefix, suffix)


def parse(query: str) -> list[list[Term]]:
    normalized = re.sub(r"\s*\|\s*", "|", query)
    groups: list[list[Term]] = []
    for token in normalized.split():
        terms = [_parse_term(alt) for alt in token.split("|") if alt]
        if terms:
            groups.append(terms)
    return groups


def _find(term: Term, text: str) -> tuple[int, int] | None:
    haystack = text if term.case_sensitive else text.lower()
    needle = term.text if term.case_sensitive else term.text.lower()
    if term.prefix and term.suffix:
        return (0, len(needle)) if haystack == needle else None
    if term.prefix:
        return (0, len(needle)) if haystack.startswith(needle) else None
    if term.suffix:
        start = len(haystack) - len(needle)
        return (start, len(haystack)) if start >= 0 and haystack.endswith(needle) else None
    start = haystack.find(needle)
    return None if start < 0 else (start, start + len(needle))


def match_spans(query: str, text: str) -> list[tuple[int, int]] | None:
    """Return the matched spans, or None when the query does not match."""

    return match_parsed(parse(query), text)


def match_parsed(groups: list[list[Term]], text: str) -> list[tuple[int, int]] | None:
    """Like :func:`match_spans` for a query already split by :func:`parse`."""

    spans: list[tuple[int, int]] = []
    for group in groups:
        clause_hit = False
        clause_spans: list[tuple[int, int]] = []
        for term in group:
            found = _find(term, text)
            if term.negated:
                if found is None:
                    clause_hit = True
                continue
            if found is not None:
                clause_hit = True
                clause_spans.append(found)
        if not clause_hit:
            return None
        spans.extend(clause_spans)
    return spans


def matches(query: str, text: str) -> bool:
    return match_spans(query, text) is not None


def score_match(
    spans: list[tuple[int, int]] | None,
    text: str,
    *,
    is_open: bool = False,
    is_current: bool = False,
    tab_match: bool = False,
) -> int:
    """Rank a matched row, rating currently open workspaces higher most of the time.

    Returns a tier score (higher sorts first, ties preserve frecency order):
    - 4: current project with direct basename/alias match
    - 3: open workspace with direct basename/alias match
    - 2: closed directory with an exact basename match (beats weak open matches)
    - 1: open/current workspace matched only via parent path or a child tab
    - 0: closed directory with a non-exact match
    """

    exact_basename = False
    strong_match = False
    if spans:
        suffix_start = text.rfind("  [")
        if suffix_start != -1 and text.endswith("]"):
            body_end = suffix_start
        else:
            suffix_start = -1
            body_end = len(text)
        dir_end = text.rfind("/", 0, body_end) + 1
        for start, end in spans:
            if start == dir_end and end == body_end and body_end > dir_end:
                exact_basename = True
                strong_match = True
            elif start >= dir_end and end <= body_end:
                strong_match = True
            elif suffix_start != -1 and start >= suffix_start:
                strong_match = True

    if is_current:
        return 4 if strong_match else 1
    if is_open:
        return 3 if strong_match else 1
    if exact_basename and not tab_match:
        return 2
    return 0
