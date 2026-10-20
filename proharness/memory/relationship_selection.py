"""Order bounded, already validated relationship sources using existing signals.

This changes discovery order, not eligibility or claim support. In particular,
identity, qualification and original-source passages are never discarded for
missing words from the question. No model calls, source writes or public vectors.
"""
from collections import Counter

from proharness.memory.query_focus import query_idfs, query_terms, weighted_coverage
from proharness.memory.retrieval_context import score_existing_chunks


def path_witnesses(path):
    """Exact named-seed, target-identity and edge witnesses, never all entity refs."""
    return list(dict.fromkeys([
        *path['seed'].get('source_citations', []),
        *path.get('target', {}).get('identity_citations', []),
        *(ref for step in path['steps'] for ref in step.get('source_citations', []))]))


def bound_source_matches(graph_items):
    """Sources explicitly bound to a named subject or requested property.

    Other connections remain leads, not automatic quotes. This is an authored
    field-route signal, not claim verification or a test of library absence.
    Ordinary text/semantic retrieval remains independent of these annotations.
    """
    return {hit['citation'] for hit, path in graph_items
            if path['seed'].get('match') == 'name_or_alias'
            and (path['target'].get('matched_fields')
                 or hit['citation'] in path['seed'].get('source_citations', []))}


def path_order(path):
    """Prefer a non-mismatching witness, then a named, short recorded route.

    This selects one separate assertion; it never merges competing attributes.
    Unknown applicability remains unknown, even when preferred to a mismatch.
    """
    mismatch = path.get('applicability', {}).get('status') == 'mismatch' or any(
        step.get('applicability', {}).get('status') == 'mismatch' for step in path['steps'])
    return (mismatch, path['seed'].get('match') != 'name_or_alias', len(path['steps']),
            path['seed']['record_id'], tuple(step['record_id'] for step in path['steps']),
            path['target']['record_id'], path['reason'])


def rank_candidates(store, query, candidates, *, include_sensitive=False):
    """Return every admitted candidate, with the best bounded witness retained.

    candidates maps exact citations to (hit, alternative paths). Existing ordinary
    retrieval can provide either the original or its registered expanded query
    vector. An unavailable/mismatched vector leaves only literal ordering.
    """
    if len(candidates) > 32:
        raise ValueError('Relationship selection accepts at most 32 exact sources')
    items = list(candidates.values())
    similarities = score_existing_chunks(store, query,
        [hit['chunk_id'] for hit, _ in items], include_sensitive=include_sensitive)
    bodies = [query_terms(hit['quote']) for hit, _ in items]
    metadata = [query_terms(' '.join((str(hit.get('title', '')),
                str(hit.get('location', {}).get('section') or '')))) for hit, _ in items]
    frequencies = Counter(term for body in bodies for term in body)
    weights = query_idfs(query_terms(query), frequencies, len(items))
    ranked = []
    for (hit, paths), body, meta in zip(items, bodies, metadata, strict=True):
        similarity = similarities.get(hit['chunk_id'])
        score = weighted_coverage(weights, body)
        if similarity is not None:
            score += .45 * max(0., similarity)
            hit = {**hit, 'explanation': {**hit.get('explanation', {}),
                   'semantic_similarity': similarity}}
        path = min(paths, key=path_order)
        ranked.append((score, weighted_coverage(weights, meta), hit, path))
    # Stable ties preserve the bounded discovery order. Scores are private
    # ordering hints, not calibrated relevance/compatibility probabilities.
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    by_citation = {hit['citation']: (hit, path) for _, _, hit, path in ranked}
    ordered = {}
    for _, _, hit, path in ranked:
        ordered.setdefault(hit['citation'], (hit, path))
        # A route's exact witnesses belong near its terminal source, even when
        # their wording is about identity rather than the requested property.
        # Entity-wide references are deliberately not treated as dependencies.
        for citation in path_witnesses(path):
            if citation in by_citation:
                ordered.setdefault(citation, by_citation[citation])
    return list(ordered.values())


def evidence_bundles(graph_items, ordinary_citations, *, seen=(), delivered=()):
    """Bind graph-only bodies to exact witnesses from the chosen bounded route.

    Ordinary retrieval is an independent route to a passage, so it does not need
    a graph witness. Previously returned references are not evidence bodies.
    Missing witness sets keep exact Open leads instead of pretending
    that the route has been delivered. No new source reads or relevance claims.
    """
    available = {hit['citation'] for hit, _ in graph_items} | set(ordinary_citations)
    seen, delivered = set(seen), set(delivered)
    bundles, deferred = {}, {}
    for hit, path in graph_items:
        citation = hit['citation']
        if citation in ordinary_citations or citation in seen:
            continue
        witnesses = [ref for ref in path_witnesses(path) if ref != citation and ref not in delivered]
        if any(ref not in available or ref in seen for ref in witnesses) or len(witnesses) > 8:
            deferred[citation] = 'route_witness_requires_open'
        elif witnesses:
            bundles[citation] = witnesses
    for citation in bundles:
        pending, closure = [citation], set()
        while pending and len(closure) <= 32:
            member = pending.pop()
            if member not in closure:
                closure.add(member)
                pending.extend(bundles.get(member, []))
        if len(closure) > 32:
            deferred[citation] = 'route_witness_requires_open'
    # A citation cycle is valid: an alias and label can witness one another's
    # routes. The allocator treats its bounded closure as one atomic group.
    # Only unavailable witnesses propagate deferral to their dependents.
    changed = True
    while changed:
        invalid = {citation for citation, witnesses in bundles.items()
                   if citation in deferred or any(ref in deferred for ref in witnesses)}
        changed = bool(invalid)
        for citation in invalid:
            bundles.pop(citation)
            deferred[citation] = 'route_witness_requires_open'
    return bundles, deferred
