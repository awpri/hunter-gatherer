"""Conservative count-word proximity for ranking and display, never claim support.

This recognizes a requested count noun and an explicit nearby quantity. It does
not infer quantities, resolve contradictions, or alter retrieved source text.
The browser mirrors this policy for display without loading a language model.
"""
from __future__ import annotations

import re

from hunter_gatherer.memory.lexical import normalize_word


_QUESTION_END = set("can could may might must should shall will would do does did is are was were have has had each per at in on for from to of with and or a an the you your we our i my it they their bring take borrow submit buy receive hold contain get".split())
_NUMBERS = set("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million dozen".split())
_CURRENCY = set("usd eur gbp jpy cad aud chf cny inr krw rub dollar euro yen rupee won rouble yuan penny pence cent".split())
_GAP_BLOCKED = _QUESTION_END | set("by before after than that which who whose these those this its then now not never no bought sold received brought took holds contains checks weighs checked weighed return returns allowed available cost costs weigh kilogram gram kg g milligram mg litre liter ml meter metre cm mm km mile inch foot feet pound lb ounce oz hour minute second day week month year january february march april june july august september october november december jan feb mar apr jun jul aug sep oct nov dec".split())
_WORDS = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_IDENTIFIER = re.compile(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9]+(?:-[a-z0-9]+)*\b", re.I)
_UNIT_ATTRIBUTE = re.compile(
    r"^\s+(?:(?:may|can|must|will|would|usually|typically|normally)\s+)*"
    r"(?:costs?|weighs?|measures?|lasts?\b|"
    r"(?:is|are)\s+(?:priced|valued)|"
    r"(?:has|have)\s+(?:a\s+)?(?:weight|price|length|height|width|volume|duration))\b",
    re.I,
)


def quantity_target(query: str) -> str | None:
    """Return the count noun after 'how many'/'number of', if unambiguous enough."""
    prefix = re.search(r"\b(?:how\s+many|number\s+of)\s+", query, re.I)
    if prefix is None:
        return None
    words = []
    for word in _WORDS.finditer(query[prefix.end():]):
        raw = word.group().casefold()
        if raw in _QUESTION_END or len(words) == 4:
            break
        normalized = normalize_word(raw)
        words.append(normalized)
        # A regular plural ends the requested noun phrase. Do not accidentally
        # drift into a predicate or a later object/model identifier ("vials fit
        # rack ..."). The same rule allows leading adjectives before "items".
        if raw.endswith("s") and normalized != raw:
            return normalized
    return words[-1] if words else None


def quantity_match_score(text: str, query: str) -> int:
    """Return 1 for explicit count wording attached to the requested noun, else 0.

    It is a positive ordering/display signal, not an eligibility filter. A short
    adjective gap is allowed, while clauses, other plural nouns, measurements,
    dates, times and negated counts are conservatively rejected.
    """
    target = quantity_target(query)
    if target is None:
        return 0
    query_ids = {match.group().casefold() for match in _IDENTIFIER.finditer(query)}
    source_ids = {match.group().casefold() for match in _IDENTIFIER.finditer(text)}
    if query_ids and source_ids and not query_ids.intersection(source_ids):
        return 0
    if re.search(r"\b(?:not|never|cannot)\b|n['’]t\b|\bno\b(?!\s+(?:more|fewer|less)\s+than\b)", text, re.I):
        return 0
    tokens = list(_WORDS.finditer(text))
    for index, token in enumerate(tokens):
        if normalize_word(token.group()) != target:
            continue
        # A price or measurement of a unit is not an available/permitted count.
        # Inspect this noun's predicate only, so a separate price sentence does
        # not suppress an otherwise explicit count.
        if _UNIT_ATTRIBUTE.match(text[token.end():]):
            continue
        for quantity_index in range(max(0, index - 3), index):
            quantity = tokens[quantity_index]
            raw = quantity.group().casefold()
            numeric = bool(re.fullmatch(r"\d+(?:[.,]\d+)*", raw))
            if not numeric and raw not in _NUMBERS:
                continue
            if quantity_index and tokens[quantity_index - 1].group().casefold() in _CURRENCY:
                continue
            before, after = text[:quantity.start()], text[quantity.end():]
            if numeric and (re.search(r"[\w:/.$£€¥₹₩₽-]$", before) or re.match(r"[:/.-]\w", after)):
                continue
            gap = tokens[quantity_index + 1:index]
            if any(word.group().casefold() in _GAP_BLOCKED | _CURRENCY
                   or (normalize_word(word.group()) != word.group().casefold()
                       and word.group().casefold() not in _NUMBERS)
                   or re.search(r"(?:ed|ing)$", word.group(), re.I) for word in gap):
                continue
            # No punctuation or conjunction can bridge an unrelated number to
            # the requested noun. Hyphenated number/adjective phrases are okay.
            if any(not re.fullmatch(r"[\s-]+", text[left.end():right.start()])
                   for left, right in zip(tokens[quantity_index:index], tokens[quantity_index + 1:index + 1])):
                continue
            return 1
    return 0
