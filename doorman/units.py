"""Unit names + whitespace-tolerant matching.

No real organisation data lives in this file -- the unit list comes from the
site config (see doorman/config.py; local/config.json, never committed).

Design rule: the upstream `Description` field is written by several parties and
the unit name appears with an inconsistent number of spaces ("Foo  1st Unit" vs
"Foo 1st Unit"). We never require the source to be clean -- everything that
reads a description normalizes first, and everything that filters matches
either form.

    canon("Foo  1st   Unit") == "Foo 1st Unit"
    match_unit("foo  1st unit (Treasurer)") == "Foo 1st Unit"
"""
import re

from .config import settings

MULTI_SEP = re.compile(r"\s*\|\s*")           # "A (x)  |  B (y)"

#: canonical unit names for this site
UNITS = list(settings.units)
#: misspellings/shorthand seen in the wild -> canonical unit
ALIASES = dict(settings.aliases)
#: town -> unit, ONLY where that town has exactly one unit
UNAMBIGUOUS_TOWN = {k.casefold(): v for k, v in settings.one_unit_towns.items()}
#: towns needing a unit number before we will claim a match
AMBIGUOUS_TOWNS = tuple(t.casefold() for t in settings.numbered_towns)


def canon(s):
    """Collapse whitespace runs to single spaces and strip. Every description
    passes through this before being compared, stored or displayed."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def _key(s):
    return canon(s).casefold()


_BY_KEY = {_key(u): u for u in UNITS}
_BY_KEY.update({_key(k): v for k, v in ALIASES.items()})

# Longest-first so "Foo 1st Unit" wins over a bare "Foo ... Unit".
_PATTERN = re.compile(
    "(" + "|".join(
        re.escape(canon(u)).replace(r"\ ", r"\s+")
        for u in sorted(set(UNITS) | set(ALIASES), key=len, reverse=True)
    ) + ")", re.I) if (UNITS or ALIASES) else re.compile(r"(?!x)x")

_TOWN_RE = (re.compile(r"\b(" + "|".join(map(re.escape, UNAMBIGUOUS_TOWN)) + r")\b", re.I)
            if UNAMBIGUOUS_TOWN else re.compile(r"(?!x)x"))


def match_unit(text):
    """First canonical unit named in `text`, or None. Space- and
    case-insensitive, so both spacing variants resolve to one value."""
    m = _PATTERN.search(text or "")
    return _BY_KEY.get(_key(m.group(1))) if m else None


def match_units(text):
    """Every canonical unit named in `text`, in order, de-duplicated."""
    out = []
    for m in _PATTERN.finditer(text or ""):
        u = _BY_KEY.get(_key(m.group(1)))
        if u and u not in out:
            out.append(u)
    return out


def match_unit_fuzzy(text):
    """match_unit(), then fall back to a bare town name where that town has
    exactly one unit. Returns None for ambiguous towns -- the caller must ask
    rather than guess, because the thing being assigned is building access."""
    u = match_unit(text)
    if u:
        return u
    m = _TOWN_RE.search(text or "")
    return UNAMBIGUOUS_TOWN[m.group(1).casefold()] if m else None


def parse_description(text):
    """Split a description into [(unit_or_None, calling_or_None, raw_part)],
    handling the multi-calling form 'A (x)  |  B (y)'."""
    parts = []
    for raw in (MULTI_SEP.split(canon(text)) if canon(text) else []):
        unit = match_unit(raw)
        m = re.search(r"\(([^)]*)\)", raw)
        calling = canon(m.group(1)) if m else None
        if not calling and unit:
            calling = canon(_PATTERN.sub("", raw, count=1)) or None
        parts.append((unit, calling, raw))
    return parts


def normalize_description(text):
    """Canonical rendering: collapse whitespace, re-spell units canonically."""
    return _PATTERN.sub(
        lambda m: _BY_KEY.get(_key(m.group(1)), canon(m.group(1))), canon(text))


def same_unit(a, b):
    """True if two descriptions name the same unit, whatever the spacing."""
    ua, ub = match_unit(a), match_unit(b)
    return ua is not None and ua == ub


def needs_manual_unit(text):
    """True when a human must pick the unit: no match, or an ambiguous town
    named without its number."""
    return bool(canon(text)) and not match_unit_fuzzy(text)
