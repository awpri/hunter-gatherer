"""Unverified authored descriptions tied to exact original PDF geometry."""
from __future__ import annotations

import math
import re

from hunter_gatherer.memory import document_enrichment as enrichment
from hunter_gatherer.memory.document_originals import error
from hunter_gatherer.memory.library_health import _read_snapshot

_REGION = re.compile(r'\[region:([0-9a-f-]{36})\]')

def validate_region(region):
    enrichment._object('region', region, {'page', 'bbox'})
    enrichment._integer('region.page', region['page'], 1, 1_000_000)
    box = region['bbox']
    if (type(box) is not list or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
            or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1):
        enrichment._fail('bbox requires finite normalized [left,top,right,bottom] with positive area')

def parse_region_citation(citation):
    match = _REGION.fullmatch(citation) if type(citation) is str and len(citation) < 100 else None
    return match[1] if match else None

def _item(record):
    region = record['region']
    ref = {'document_id': record['document_id'], 'version': record['version'],
           'original_content_hash': record['content_hash'], **region}
    citation = f"[region:{record['record_id']}]"
    return {'kind': 'visual_region', 'record_id': record['record_id'], 'citation': citation,
            'description': record['derived_text'], 'verification': 'unverified', 'authorship': 'agent_authored',
            'source_reference': ref, 'followup': {'tool': 'hg_open',
                'arguments': {'refs': [citation], 'view': 'region'}},
            'content_policy': 'Discovery description is an unverified agent annotation, not a source quotation.'}

def resolve_region(store, record_id, include_sensitive=False):
    if type(include_sensitive) is not bool or not parse_region_citation(f'[region:{record_id}]'):
        return None
    with _read_snapshot(store) as db:
        if not enrichment._available(db, enrichment._TABLES):
            return None
        row = db.execute(enrichment._SELECT + "WHERE r.id=? AND r.kind='visual_region' AND b.revoked_at IS NULL", (record_id,)).fetchone()
        if row is None:
            return None
        try:
            source = enrichment._source(db, row['document_id'], include_sensitive, latest=True)
            record = enrichment._saved_record(db, row, source, include_sensitive=include_sensitive)
        except enrichment.EnrichmentError:
            return None
        return _item(record) if record else None

def discover_regions(store, queries, include_sensitive=False, limit=8):
    # Bounded FTS candidate scan; independently validate every returned reference.
    terms = sorted(set().union(*(enrichment._tokens(q) for q in queries)))[:128]
    if not terms:
        return []
    expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
    with _read_snapshot(store) as db:
        if not enrichment._available(db, enrichment._TABLES):
            return []
        ids = [row[0] for row in db.execute("SELECT r.id FROM document_enrichment_records r JOIN document_enrichment_fts f ON f.record_id=r.id JOIN document_enrichment_batches b ON b.id=r.batch_id WHERE document_enrichment_fts MATCH ? AND r.kind='visual_region' AND b.revoked_at IS NULL ORDER BY bm25(document_enrichment_fts),r.id LIMIT 256", (expression,))]
        results = []
        for identity in ids:
            item = resolve_region(store, identity, include_sensitive)
            if item:
                item['followup']['arguments']['include_sensitive'] = include_sensitive
                results.append(item)
                if len(results) == limit:
                    break
        return results

def region_image(store, record_id, *, include_sensitive=False, view='region'):
    if view not in {'region', 'page'}:
        return error('invalid_input', 'view must be region or page', 422)
    item = resolve_region(store, record_id, include_sensitive)
    if item is None:
        return error('region_unavailable', 'Region not found or excluded.', 404)
    from hunter_gatherer.memory.document_page_image import document_page_image
    ref = item['source_reference']
    result = document_page_image(store, ref['document_id'], page=ref['page'], include_sensitive=include_sensitive,
                                 **({'region_bbox': ref['bbox']} if view == 'region' else {}))
    if result.get('error'):
        return result
    expected = {k: v for k, v in ref.items() if k != 'bbox'}
    if result['source_reference'] != expected or resolve_region(store, record_id, include_sensitive) != item:
        return error('region_unavailable', 'Region source changed or excluded.', 404)
    return {**result, 'region_reference': ref, 'citation': item['citation'], 'view': view,
            'description': item['description'], 'verification': 'unverified'}
