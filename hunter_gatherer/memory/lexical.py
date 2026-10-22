"""Conservative lexical normalization for derived search signals only.

Original text, source spans, citations and identifiers are never rewritten.
This is an English plural heuristic with Unicode accent folding, not a general
lemmatizer. Unrecognized inflections still need another wording or semantic
search. Digit-bearing tokens and punctuation-bearing identifiers keep their
literal spelling apart from case folding.
"""
from __future__ import annotations

import re
import unicodedata

TOKENIZER_VERSION = "accent-plurals-v1"
_INVARIANT = frozenset({"news", "series", "species", "https", "bios", "hsts", "headquarters"})


def normalize_word(word: str) -> str:
    folded = word.casefold()
    # Never turn model/version identifiers into ordinary word stems.
    if not word.isalpha():
        return folded
    folded = "".join(char for char in unicodedata.normalize("NFKD", folded)
                     if not unicodedata.combining(char))
    if (not re.fullmatch(r"[a-z]+", folded) or len(folded) <= 3
            or folded in _INVARIANT or folded.endswith(("ss", "us", "is", "ics"))):
        return folded
    if folded.endswith("ies") and len(folded) > 4:
        # -ie words are ambiguous (ties/pies), so short forms stay literal.
        return folded[:-3] + "y"
    if folded.endswith(("sses", "xes", "zes", "ches", "shes")):
        return folded[:-2]
    if folded.endswith("s"):
        return folded[:-1]
    return folded


def token_words(text: str, *, stopwords: frozenset[str] = frozenset()) -> list[str]:
    """Normalized word tokens; quantities remain available to query helpers."""
    # Check function words before stemming ("does" must not become "doe").
    return [normalize_word(word) for word in re.findall(r"\w+", text)
            if word.casefold() not in stopwords]


def search_variants(term: str) -> set[str]:
    """Exact FTS spellings for a normalized term, with no prefix wildcards."""
    variants = {term}
    if (not re.fullmatch(r"[a-z]+", term) or len(term) <= 2
            or term in _INVARIANT or term.endswith(("us", "is", "ics"))):
        return variants
    variants.add(term + "s")
    if term.endswith(("s", "x", "z", "ch", "sh")):
        variants.add(term + "es")
    if len(term) > 2 and term.endswith("y") and term[-2] not in "aeiou":
        variants.add(term[:-1] + "ies")
    return variants
