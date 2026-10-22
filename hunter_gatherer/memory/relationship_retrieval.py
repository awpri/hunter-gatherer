"""Bounded exact-source relationship navigation; no identity merges or model calls."""
from __future__ import annotations

import re
import unicodedata
from collections import deque

from hunter_gatherer.memory.relationship_model import (
    adjacent_edges, entities_for_source, get_entity_record, named_entity_seeds,
)

MAX_CANDIDATES = 32
_SCOPE = ('Authored/inferred relationships and structural source links are navigation hints, not verified support or identity. '
          'Read exact sources; missing dates/compatibility remain unknown. Truncation is not absence.')


def phrase_candidates(query):
    """At most 64 exact name phrases; bounded even for a 4000-character query."""
    words = re.findall(r"[\w]+(?:[-’'][\w]+)*", query, flags=re.UNICODE)[:20]
    quoted = re.findall(r'["“]([^"”]{1,200})["”]', query)[:8]
    capitalized = re.findall(r'\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3}', query)[:12]
    technical = [word.strip('?!,:;()[]"') for word in query.split()[:20]
                 if any(char in word for char in ('+', '#', '.'))]
    # Single-token aliases must not be crowded out by earlier long n-grams.
    phrases = [*quoted, *technical, *capitalized, *words]
    for size in (4, 3, 2, 1):
        phrases.extend(' '.join(words[i:i+size]) for i in range(len(words)-size+1))
    return list(dict.fromkeys(p for p in phrases if 0 < len(p) <= 200))[:64]


def _citations(record):
    return list(dict.fromkeys(ref['citation'] for ref in record['source_references']))


def _identity(record):
    return {'record_id': record['record_id'], 'label': record.get('label', record['derived_text'])[:200]}


def _target_identity(record):
    """Carry only an entity's explicit validated label witnesses, without reads.

    Alias/attribute bindings and entity-wide references cannot substitute for
    the target label. Older entities remain usable without a guessed witness.
    """
    target = _identity(record)
    if record['kind'] == 'entity':
        citations = list(dict.fromkeys(citation
            for binding in record.get('field_sources', []) if binding['field'] == 'label'
            for citation in binding['citations']))[:8]
        if citations:
            target['identity_citations'] = citations
    return target


def _field_words(value):
    """Normalize literal identifier words using ordinary query stopwords."""
    from hunter_gatherer.memory.query_focus import query_terms
    value = unicodedata.normalize('NFKC', value)
    spaced = []
    for index, char in enumerate(value):
        previous = value[index-1] if index else ''
        following = value[index+1] if index+1 < len(value) else ''
        if index and char.isupper() and (previous.islower() or previous.isdigit()
                or (previous.isupper() and following.islower())):
            spaced.append(' ')
        spaced.append(char)
    normalized = re.sub(r"[\W_]+", ' ', ''.join(spaced).casefold())
    return query_terms(normalized)


def _target_route(record, citation, query):
    """Expose literal property routes only for an exact bound target source.

    Values, names, aliases and entity-wide references cannot establish a field
    route. The record and its citation bindings have already been validated.
    This pure metadata projection does not establish source entailment.
    """
    target = _target_identity(record)
    if record['kind'] == 'entity':
        words = _field_words(query)
        keys = list(dict.fromkeys(binding['key']
            for binding in record.get('field_sources', [])
            if binding['field'] == 'attribute' and citation in binding['citations']
            and words & _field_words(binding['key'])))[:16]
        if keys:
            target['matched_fields'] = keys
    return target


def _seed_source_citations(record, phrases):
    """Only explicit bindings can say which source establishes a name/alias.

    Older records still seed discovery; never guess an alias witness from all
    sources associated with the same entity.
    """
    normalize = lambda value: ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    names = {normalize(phrase) for phrase in phrases}
    citations = []
    for binding in record.get('field_sources', []):
        value = record.get('label') if binding['field'] == 'label' else (
            binding.get('key') if binding['field'] == 'alias' else None)
        if value is not None and normalize(value) in names:
            citations.extend(binding['citations'])
    return list(dict.fromkeys(citations))[:8]


def _step(edge, origin, constraints=None):
    result = {'record_id': edge['record_id'], 'predicate': edge['relation']['predicate'],
              'direction': 'outgoing' if edge['subject_id'] == origin else 'incoming',
              'source_citations': _citations(edge),
              'support': 'authored' if edge.get('assertion_status') == 'asserted' else 'inferred',
              'review_status': edge.get('review_status', 'unreviewed')}
    for name in ('assertion_status',):
        if name in edge:
            result[name] = edge[name]
    dates = {key: edge[key] for key in ('valid_from', 'valid_to') if key in edge}
    if dates:
        result['relation_validity'] = dates
    if constraints:
        from hunter_gatherer.memory.relationship_constraints import evaluate_constraints
        # An edge's dates describe that assertion. Interface attributes belong
        # to the target entity and must not be inferred from an edge predicate.
        if constraints.get('as_of'):
            result['applicability'] = evaluate_constraints(edge, {'as_of': constraints['as_of']})
    return result


def _support(path):
    if not path['steps']:
        return 'structural'
    return 'authored' if all(step['support'] == 'authored' for step in path['steps']) else 'inferred'


def _originals(record):
    from hunter_gatherer.memory.original_file_reference import original_file_citation
    from hunter_gatherer.memory.original_page_reference import original_page_citation
    refs = []
    for ref in record.get('source_refs', []):
        if 'original_content_hash' in ref:
            refs.append(original_page_citation(ref) if 'page' in ref else original_file_citation(ref))
    return refs


def relationship_candidates(store, query, seed_citations=(), *, include_sensitive=False,
                            predicates=None, direction="both", constraints=None):
    from hunter_gatherer.memory.relationship_model import validated_record_snapshot
    with validated_record_snapshot(store):
        return _relationship_candidates(store, query, seed_citations,
            include_sensitive=include_sensitive, predicates=predicates,
            direction=direction, constraints=constraints)


def _relationship_candidates(store, query, seed_citations=(), *, include_sensitive=False,
                             predicates=None, direction="both", constraints=None):
    """Return <=32 exact chunks and paths from <=16 seeds, <=2 hops and <=64 edges.

    Initial passage identities are cursor-pinned by the caller. Every graph read
    revalidates source/dependency visibility through relationship_model. Scoped
    lexical search never invokes an extra embedder or model.
    """
    from hunter_gatherer.memory.documents import DocumentArchive
    from hunter_gatherer.memory.relationship_constraints import validate_constraints, evaluate_constraints
    from hunter_gatherer.memory.relationship_selection import path_order, rank_candidates
    constraints = validate_constraints(constraints)
    available = store._sqlite.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_entity_names'").fetchone()
    if not available:
        return [], None
    archive = DocumentArchive(store)
    seeds = {}; stats = {'hops': 2, 'entity_limit': 16, 'edge_limit': 64,
                         'candidate_limit': MAX_CANDIDATES, 'source_hydration_limit': 64, 'truncated': False}
    def diagnostic(info):
        stats['truncated'] |= bool(info.get('truncated') or info.get('result_limit_reached'))
    phrases = phrase_candidates(query)
    for i in range(0, len(phrases), 8):
        info = {}
        for record in named_entity_seeds(store, phrases[i:i+8], limit=16, diagnostics=info,
                                         include_sensitive=include_sensitive):
            seeds.setdefault(record['record_id'], (record, 'name_or_alias'))
            if len(seeds) == 16:
                stats['truncated'] = True
                break
        diagnostic(info)
        if len(seeds) >= 16:
            break
    for citation in seed_citations[:4]:
        match = re.fullmatch(r'\[doc:([^#]+)#([^\]]+)\]', citation)
        if not match:
            continue
        info = {}
        for record in entities_for_source(store, chunk_id=match[2], limit=8, diagnostics=info,
                                          include_sensitive=include_sensitive):
            if len(seeds) >= 16:
                stats['truncated'] = True
                break
            seeds.setdefault(record['record_id'], (record, 'passage'))
        diagnostic(info)
    if not seeds:
        return [], None
    candidates = {}; records = {}; queue = deque(); hydrated = {}
    hydration_count = 0

    def remember_document(record, path, document_id=None):
        routes = records.setdefault(document_id or record['document_id'], [])
        if not any(previous['record_id'] == record['record_id'] and route == path for previous, route in routes):
            if len(routes) < 4:
                routes.append((record, path))
            else:
                stats['truncated'] = True

    def remember_candidate(exact, detail):
        citation = exact['citation']
        if citation not in candidates:
            if len(candidates) >= MAX_CANDIDATES:
                stats['truncated'] = True
                return
            candidates[citation] = ({**exact, 'kind': 'source', 'claim_support': 'review_required'}, [])
        paths = candidates[citation][1]
        if detail not in paths:
            paths.append(detail)
            paths.sort(key=path_order)
            if len(paths) > 4:
                paths.pop()
                stats['truncated'] = True

    def add_sources(record, path, *, reason=None):
        """Hydrate local anchors and exact cross-source references, never derived text."""
        nonlocal hydration_count
        references = list(record['source_references'][:4])
        if len(record['source_references']) > 4:
            stats['truncated'] = True
        for ref in record.get('source_refs', []):
            if 'original_content_hash' in ref:
                continue
            # Validation already pins the source version/hash. Document/page
            # references scope text search; only a chunk ref is an exact passage.
            remember_document(record, path, ref['document_id'])
            if ref.get('chunk_id'):
                references.append({'chunk_id': ref['chunk_id'],
                    'citation': f"[doc:{ref['document_id']}#{ref['chunk_id']}]"})
        for reference in references:
            if reference['chunk_id'] not in hydrated:
                if hydration_count >= 64:
                    stats['truncated'] = True
                    break
                hydration_count += 1
                hydrated[reference['chunk_id']] = archive.get_chunk(reference['chunk_id'], include_sensitive=include_sensitive)
            exact = hydrated[reference['chunk_id']]
            if (exact.get('citation') != reference['citation'] or
                    reference.get('quote', '') not in exact.get('quote', '')):
                continue
            citation = exact['citation']
            if citation in candidates or len(candidates) < MAX_CANDIDATES:
                detail = {**path, 'support': _support(path),
                    'target': {**_target_route(record, citation, query), 'citation': citation,
                        'document_id': exact['document_id'], 'chunk_id': exact['chunk_id']},
                    'reason': reason or ('typed_relationship' if path['steps'] else 'entity_source'),
                    'source_citations': _citations(record)}
                originals = _originals(record)
                if originals:
                    detail['original_citations'] = originals
                if constraints:
                    detail['applicability'] = evaluate_constraints(record, constraints)
                remember_candidate(exact, detail)
            elif citation not in candidates:
                stats['truncated'] = True
    for record, matched in seeds.values():
        seed = {**_identity(record), 'match': matched}
        if matched == 'name_or_alias':
            witnesses = _seed_source_citations(record, phrases)
            if witnesses:
                seed['source_citations'] = witnesses
        queue.append((record, {'seed': seed, 'steps': []}, 0))
    visited = set(); edge_count = 0
    while queue:
        record, path, depth = queue.popleft()
        rid = record['record_id']
        remember_document(record, path)
        add_sources(record, path)
        if rid in visited:
            continue
        visited.add(rid)
        if depth == 2 or edge_count >= 64:
            if depth == 2:
                stats['hop_limit_reached'] = True
            continue
        info = {}; remaining = 64-edge_count
        edges = adjacent_edges(store, [rid], predicates=predicates, direction=direction, limit=min(16, remaining),
                               include_sensitive=include_sensitive, diagnostics=info)
        diagnostic(info); edge_count += len(edges)
        for edge in edges:
            edge_path = {**path, 'steps': [*path['steps'], _step(edge, rid, constraints)]}
            add_sources(edge, edge_path, reason='relationship_source')
            remember_document(edge, edge_path)
            neighbor = edge['object_id'] if edge['subject_id'] == rid else edge['subject_id']
            # Do not follow a cycle back to this path's original seed. A source
            # reached by another bounded route may still have a better witness.
            if neighbor == path['seed']['record_id']:
                continue
            target = get_entity_record(store, neighbor, include_sensitive=include_sensitive)
            if target and depth < 2:
                queue.append((target, edge_path, depth+1))
        if len(queue) > 64:
            queue = deque(list(queue)[:64]); stats['truncated'] = True
    # Graph-first document scope followed by text ranking. Structural anchors
    # remain useful partial evidence even if wording is absent in scoped search.
    docs = list(records)[:16]
    if len(records) > 16:
        stats['truncated'] = True
    if docs:
        response = archive.search_bounded(query, top_k=5, document_ids=docs,
            include_sensitive=include_sensitive, lexical_only=True)
        for hit in response['hits']:
            for record, path in records[hit['document_id']]:
                # Scoped text can be another chunk in the same source, not the
                # relationship anchor itself. Keep that distinction explicit.
                detail = {**path, 'support': _support(path), 'target': {**_target_route(record, hit['citation'], query), 'citation': hit['citation'], 'document_id': hit['document_id'], 'chunk_id': hit['chunk_id']},
                          'reason': 'scoped_text_in_related_document', 'source_citations': _citations(record)}
                if constraints:
                    detail['applicability'] = evaluate_constraints(record, constraints)
                remember_candidate(hit, detail)
    ordered = rank_candidates(store, query, candidates, include_sensitive=include_sensitive)
    stats.update(seeds=len(seeds), edges_visited=edge_count, entities_visited=len(visited), source_hydrations=hydration_count)
    return ordered, {'status': 'bounded_navigation_hints', 'paths': [], 'scope': _SCOPE, 'truncated': stats['truncated'], 'matched_seed_ids': [rid for rid, (_, match) in seeds.items() if match == 'name_or_alias'], 'limits': stats}
