"""Lossless, deterministic projection of an already authorized evidence packet.

This module never fetches sources or changes retrieval, privacy, or assessment.
"""
from __future__ import annotations

import json
from copy import deepcopy

_CONTENT = ('quote', 'location', 'context', 'context_location')
_ATTRIBUTION = ('document_id', 'chunk_id', 'title', 'author', 'document_date',
                'version', 'is_latest', 'latest_version', 'warnings', 'sensitive', 'source_uri')
_FORMAT = 'hg_evidence_compact_v1'
_READ_FORMATS = (_FORMAT, 'proharness_evidence_compact_v1')
_CONTRACT = (
    'Content occurrences reference content_registry by exact citation. Restore only '
    'the fields named by content_fields. For context_encoding=quote, context is quote; '
    'for context_encoding=split, context is context_before + quote + context_after. '
    'All other occurrence fields and packet fields retain their original meaning. '
    'Registry presence and source kinds imply neither truth nor claim support.'
)


def _canonical_payload(value):
    """Compare JSON value types exactly; NaN/Infinity are not lossless JSON."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('Evidence source payload must contain finite JSON values') from exc


def _occurrences(packet):
    for key in ('supporting_passages', 'source_leads'):
        yield from packet.get(key, [])
    for memory in packet.get('relevant_memories', []):
        yield from memory.get('provenance', {}).get('sources', [])


def _encode_context(content):
    if 'context' not in content or not isinstance(content.get('quote'), str):
        return content
    quote, context = content['quote'], content['context']
    if context == quote:
        content.pop('context')
        content['context_encoding'] = 'quote'
        return content
    location, surrounding = content.get('location'), content.get('context_location')
    if not isinstance(context, str) or not isinstance(location, dict) or not isinstance(surrounding, dict):
        return content
    coordinates = [location.get(k) for k in ('start', 'end')] + [surrounding.get(k) for k in ('start', 'end')]
    if not all(type(value) is int for value in coordinates):
        return content
    start, end, context_start, context_end = coordinates
    offset = start - context_start
    if (offset >= 0 and end - start == len(quote) and context_end - context_start == len(context)
            and context[offset:offset + len(quote)] == quote
            and all(location.get(k) == surrounding.get(k) for k in ('page', 'section'))):
        content.pop('context')
        content.update(context_encoding='split', context_before=context[:offset],
                       context_after=context[offset + len(quote):])
    return content


def _decode_content(content):
    result = deepcopy(content)
    encoding = result.pop('context_encoding', None)
    try:
        if encoding == 'quote':
            result['context'] = result['quote']
        elif encoding == 'split':
            result['context'] = result.pop('context_before') + result['quote'] + result.pop('context_after')
        elif encoding is not None:
            raise ValueError('Unknown compact context encoding')
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid compact context payload') from exc
    return result


def compact_evidence_packet(packet: dict) -> dict:
    """Keep every field exactly reconstructible; conflicting citations fail closed."""
    if not isinstance(packet, dict) or any(key in packet for key in ('content_registry', 'compact_format')):
        raise ValueError('Expected an unprojected evidence packet')
    result = deepcopy(packet)
    registry, attribution = {}, {}
    for occurrence in _occurrences(result):
        if not isinstance(occurrence, dict) or any(k in occurrence for k in ('content_ref', 'content_fields')):
            raise ValueError('Invalid evidence content occurrence')
        fields = [field for field in _CONTENT if field in occurrence]
        if not fields:
            continue
        citation = occurrence.get('citation')
        if not isinstance(citation, str) or not citation:
            raise ValueError('Content requires an exact citation')
        content = registry.setdefault(citation, {})
        source = attribution.setdefault(citation, {})
        for field in (*fields, *_ATTRIBUTION):
            if field not in occurrence:
                continue
            target = content if field in _CONTENT else source
            encoded = _canonical_payload(occurrence[field])
            if field in target:
                if _canonical_payload(target[field]) != encoded:
                    raise ValueError(f'Conflicting evidence payload for citation: {citation}')
            else:
                target[field] = deepcopy(occurrence[field])
        for field in fields:
            occurrence.pop(field)
        occurrence.update(content_ref=citation, content_fields=fields)
    result['content_registry'] = {citation: _encode_context(content) for citation, content in registry.items()}
    result['compact_format'] = {'version': _FORMAT, 'reconstruction_contract': _CONTRACT}
    return result


def expand_evidence_packet(packet: dict) -> dict:
    """Reconstruct the original packet for adapters and losslessness checks."""
    if (not isinstance(packet, dict) or not isinstance(packet.get('compact_format'), dict)
            or packet['compact_format'].get('version') not in _READ_FORMATS):
        raise ValueError('Unsupported compact evidence format')
    result = deepcopy(packet)
    registry = result.pop('content_registry', None)
    if not isinstance(registry, dict) or any(not isinstance(value, dict) for value in registry.values()):
        raise ValueError('Invalid compact content registry')
    result.pop('compact_format')
    decoded = {citation: _decode_content(content) for citation, content in registry.items()}
    for occurrence in _occurrences(result):
        if 'content_ref' not in occurrence:
            continue
        citation = occurrence.pop('content_ref')
        fields = occurrence.pop('content_fields', None)
        if occurrence.get('citation') != citation or citation not in decoded:
            raise ValueError('Invalid compact content reference')
        if (not isinstance(fields, list) or any(not isinstance(f, str) or f not in _CONTENT for f in fields)
                or len(set(fields)) != len(fields)):
            raise ValueError('Invalid compact content fields')
        for field in fields:
            if field in occurrence or field not in decoded[citation]:
                raise ValueError('Missing or colliding compact content field')
            occurrence[field] = deepcopy(decoded[citation][field])
    return result
