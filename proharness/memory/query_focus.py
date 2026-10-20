"""Cheap lexical coverage signals; never eligibility or answerability judgments."""
import math


def validate_relevance_policy(value):
    if not isinstance(value, str) or value not in {"legacy", "coverage"}:
        raise ValueError("relevance_policy must be legacy or coverage")


def query_terms(query):
    from proharness.memory.documents import _tokens
    return set(_tokens(query))


def idf(term_frequency, n):
    if type(n) is not int or type(term_frequency) is not int or not 0 <= term_frequency <= n:
        raise ValueError("document counts must be integers with 0 <= frequency <= n")
    return math.log(1 + (n - term_frequency + .5) / (term_frequency + .5))


def query_idfs(terms, frequencies, count):
    return {term: idf(frequencies.get(term, 0), count) for term in sorted(set(terms))}


def weighted_coverage(idfs, matched):
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value < 0 for value in idfs.values()):
        raise ValueError("IDFs must be finite nonnegative numbers")
    denominator = sum(idfs.values())
    return sum(idfs[term] for term in sorted(set(matched)) if term in idfs) / denominator if denominator else 0.0


def coverage(terms, present_terms, frequencies, n):
    return weighted_coverage(query_idfs(terms, frequencies, n), present_terms)
