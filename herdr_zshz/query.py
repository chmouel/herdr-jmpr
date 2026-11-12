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

    groups = parse(query)
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
