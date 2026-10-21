"""The six advertised tools complete a cited investigation without hidden tools."""
import asyncio
import json
from types import SimpleNamespace

from proharness.mcp.profiles import LIBRARY_TOOL_NAMES
from proharness.mcp.server import build_server
from proharness.mcp.tools import Harness
from proharness.memory.collection_compact import expand_collection_export
from proharness.memory.documents import DocumentArchive
from proharness.memory.store import MemoryStore


def server_for(store):
    harness = Harness(store=store, settings=store.settings, loop=SimpleNamespace(), cost=SimpleNamespace())
    return build_server(harness=harness, tool_profile="library")[0]


def invoke(server, name, **arguments):
    assert name in LIBRARY_TOOL_NAMES
    response = asyncio.run(server.call_tool(name, arguments))
    assert not response.is_error, response.content
    text = next(part.text for part in response.content if part.type == "text")
    payload = json.loads(text)
    if name != 'proharness_import_documents':
        assert response.structured_content == payload
    if 'budget' in payload:
        assert len(text.encode()) == payload['budget']['serialized_bytes']
        assert len(text.encode()) <= 4 * payload['budget']['max_tokens']
    return payload


def follow(server, route):
    return invoke(server, route['tool'], **route['arguments'])


def test_import_find_open_save_reopen_export_using_only_six_tools(store, tmp_path):
    server = server_for(store)
    assert {t.name for t in asyncio.run(server.list_tools())} == set(LIBRARY_TOOL_NAMES)
    path = tmp_path / 'greenhouse.md'
    prose = 'Rowan repaired the Cedar greenhouse valve. The log records two volunteer hours. No water savings measurement was recorded.'
    path.write_text('# Greenhouse maintenance\n\n' + prose)
    preview = invoke(server, 'proharness_import_documents', action='preview', paths=[str(path)])
    assert preview['file_count'] == 1
    plan = invoke(server, 'proharness_import_documents', action='create', paths=[str(path)], idempotency_key='single-file')
    completed = invoke(server, 'proharness_import_documents', action='run', job_id=plan['job_id'])
    assert completed['complete'] and completed['counts']['succeeded'] == 1
    packet = invoke(server, 'proharness_find', query='Rowan greenhouse contribution', max_tokens=4096)
    assert packet['work_trace']['retrieval_passes'] == 1
    passage = packet['supporting_passages'][0]
    citation = passage['citation']
    opened = invoke(server, 'proharness_open', refs=[citation])
    assert prose in opened['sources'][0]['quote']
    context = invoke(server, 'proharness_open', refs=[citation], view='context')
    assert prose in json.dumps(context)
    outline = invoke(server, 'proharness_open', refs=[citation], view='outline')
    section = next(s for s in outline['sections'] if 'followup' in s)
    assert prose in json.dumps(follow(server, section['followup']))
    ref = {'document_id': passage['document_id'], 'chunk_id': passage['chunk_id']}
    content = {'name': 'Greenhouse contribution', 'purpose': packet['query'], 'references': [ref],
        'unresolved': ['No measured water savings in this source.'], 'notes': 'Do not infer additional achievements.'}
    saved = invoke(server, 'proharness_collection', action='save', collection=content, idempotency_key='save-one')
    assert saved['revision'] == 1
    assert invoke(server, 'proharness_collection', action='save', collection=content, idempotency_key='save-one') == saved
    with MemoryStore(store.settings, embedder=store.embedder) as reopened:
        fresh = server_for(reopened)
        assert invoke(fresh, 'proharness_collection', action='get', collection_id=saved['id']) == saved
        handoff = invoke(fresh, 'proharness_collection', action='export', collection_id=saved['id'])
        exported = expand_collection_export(handoff['export'])
        assert prose in exported['content']['evidence'][0]['quote']
        assert exported['content']['evidence'][0]['citation'] == citation
        assert exported['unresolved'] == content['unresolved']
        refs = follow(fresh, handoff['followup'])
        assert refs['references'][0]['citation'] == citation
        assert invoke(fresh, 'proharness_collection', action='list')['collections'][0]['id'] == saved['id']


def test_memory_correction_history_and_sensitive_mutation_guard(store):
    server = server_for(store)
    remembered = invoke(server, 'proharness_memory', action='remember', text='Rowan plans a May race.', sensitive=True)
    identity = remembered['id']
    refused = asyncio.run(server.call_tool('proharness_memory', {'action': 'replace', 'memory_id': identity, 'text': 'A June race.'}))
    assert refused.is_error
    assert store.get(identity).is_retrievable()
    replaced = invoke(server, 'proharness_memory', action='replace', memory_id=identity,
                      text='Rowan plans a June race.', include_sensitive=True)
    new_id = replaced['memory']['id']
    history = invoke(server, 'proharness_memory', action='history', memory_id=new_id, include_sensitive=True)
    assert 'May race' in json.dumps(history) and 'June race' in json.dumps(history)
    hidden = invoke(server, 'proharness_memory', action='history', memory_id=new_id)
    assert 'May race' not in json.dumps(hidden) and 'June race' not in json.dumps(hidden)
    invoke(server, 'proharness_memory', action='forget', memory_id=new_id, include_sensitive=True)
    assert invoke(server, 'proharness_open', refs=[f'[mem:{new_id}]'], include_sensitive=True)['statuses'] == ['unavailable']
    assert invoke(server, 'proharness_memory', action='profile')['count'] == 0


def test_rejected_memory_arguments_explain_recovery_without_mutation(store):
    server = server_for(store)
    remembered = invoke(server, 'proharness_memory', action='remember', text='The fictional trial starts Monday.')
    identity = remembered['id']
    rejected = asyncio.run(server.call_tool('proharness_memory', {
        'action': 'replace', 'memory_id': identity, 'text': 'The fictional trial starts Friday.',
        'importance': 8, 'source': 'a source value that must not appear in the error',
    }))
    assert rejected.is_error
    message = ' '.join(part.text for part in rejected.content if part.type == 'text')
    assert "memory action 'replace': importance, source" in message
    assert 'Allowed arguments: include_sensitive, memory_id, metadata, text' in message
    assert 'No changes were made' in message
    assert 'Friday' not in message and 'must not appear' not in message
    assert store.get(identity).is_retrievable()
    assert store.get(identity).text == 'The fictional trial starts Monday.'
    corrected = invoke(server, 'proharness_memory', action='replace', memory_id=identity,
                       text='The fictional trial starts Friday.')
    assert corrected['replaced']


def test_outline_not_blocked_by_quote_budget(store, tmp_path):
    path = tmp_path / 'long.txt'
    path.write_text('Greenhouse notes ' * 490)
    document = DocumentArchive(store).ingest(str(path))
    row = store._sqlite.execute('SELECT id FROM archive_chunks WHERE document_id=? ORDER BY start', (document['document_id'],)).fetchone()
    citation = f"[doc:{document['document_id']}#{row[0]}]"
    server = server_for(store)
    assert invoke(server, 'proharness_open', refs=[citation], max_tokens=512)['statuses'] == ['omitted']
    outline = invoke(server, 'proharness_open', refs=[citation], view='outline', max_tokens=1024)
    assert outline['sections']


def test_collection_refs_budget_continuation_privacy_and_revision_conflicts(store, tmp_path):
    from proharness.memory.original_file_reference import original_file_citation
    path = tmp_path / 'original.txt'
    path.write_text('An exact original document for a collection.')
    doc = DocumentArchive(store).ingest(str(path))
    row = store._sqlite.execute('SELECT version,content_hash FROM archive_documents WHERE id=?', (doc['document_id'],)).fetchone()
    ref = {'document_id': doc['document_id'], 'version': row[0], 'original_content_hash': row[1]}
    server = server_for(store)
    content = {'name': 'Source pages', 'references': [ref] * 15}
    saved = invoke(server, 'proharness_collection', action='save', collection=content, idempotency_key='many-refs')
    page = invoke(server, 'proharness_collection', action='refs', collection_id=saved['id'], revision=1, max_tokens=1024)
    seen = []
    while True:
        seen.extend(page['references'])
        assert all(r['citation'] == original_file_citation(ref) for r in page['references'])
        assert all(r['followup']['tool'] == 'proharness_open' for r in page['references'])
        if page['next_offset'] is None:
            break
        page = follow(server, page['followup'])
    assert [r['index'] for r in seen] == list(range(15))
    second = invoke(server, 'proharness_collection', action='save', collection={**content, 'notes': 'Inspected.'},
        collection_id=saved['id'], expected_revision=1, idempotency_key='revision-two')
    assert second['revision'] == 2
    conflict = asyncio.run(server.call_tool('proharness_collection', {'action': 'save', 'collection': content,
        'collection_id': saved['id'], 'expected_revision': 1, 'idempotency_key': 'stale'}))
    assert conflict.is_error and conflict.structured_content['error_code'] == 'revision_conflict'
    # Hiding a dependency affects old revisions and exports as well.
    store._sqlite.execute('UPDATE archive_documents SET sensitive=1 WHERE id=?', (doc['document_id'],))
    store._sqlite.commit()
    hidden = invoke(server, 'proharness_collection', action='export', collection_id=saved['id'], revision=1)
    assert 'An exact original document' not in json.dumps(hidden)
    assert invoke(server, 'proharness_open', refs=[original_file_citation(ref)])['statuses'] == ['unavailable']


def test_page_identity_and_native_image_delivery(store, tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from proharness.memory.original_page_reference import original_page_citation
    path = tmp_path / 'page.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=80, height=80)
    with path.open('wb') as output:
        writer.write(output)
    doc = DocumentArchive(store).ingest(str(path))
    row = store._sqlite.execute('SELECT version,content_hash FROM archive_documents WHERE id=?', (doc['document_id'],)).fetchone()
    reference = {'document_id': doc['document_id'], 'version': row[0], 'original_content_hash': row[1], 'page': 1}
    monkeypatch.setattr('proharness.memory.document_page_image.document_page_image',
                        lambda *a, **k: {'source_reference': dict(reference), '_image_png': b'fixture PNG'})
    server = server_for(store)
    response = asyncio.run(server.call_tool('proharness_open', {'refs': [original_page_citation(reference)], 'view': 'page'}))
    assert not response.is_error and [p.type for p in response.content] == ['text', 'image']
    assert '_image_png' not in json.dumps(response.structured_content)
    monkeypatch.setattr('proharness.memory.document_page_image.document_page_image',
                        lambda *a, **k: {'source_reference': {**reference, 'page': 2}, '_image_png': b'wrong pixels'})
    refused = asyncio.run(server.call_tool('proharness_open', {'refs': [original_page_citation(reference)], 'view': 'page'}))
    assert refused.is_error and all(p.type != 'image' for p in refused.content)
    assert refused.structured_content['error_code'] == 'source_identity_mismatch'


def test_profile_public_guidance_respects_original_budget(store):
    from proharness.memory.schemas import Memory

    for i in range(4):
        store.add(Memory(text=f'Fictional preference {i}: ' + 'A plain fact. ' * 80))
    profile = invoke(server_for(store), 'proharness_memory', action='profile', max_tokens=512)
    assert profile['follow_up'] == 'Use proharness_find; inspect exact citations with proharness_open.'


def test_context_and_outline_carry_originating_pdf_page_route(store, tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / 'text.txt'
    path.write_text('The fictional pilot has a target, not a measured outcome.')
    archive = DocumentArchive(store)
    document = archive.ingest(str(path))
    document_id = document['document_id']
    # Metadata-only fixture: the unchecked hint must not read/render original bytes.
    store._sqlite.execute("UPDATE archive_documents SET media_type='application/pdf' WHERE id=?", (document_id,))
    store._sqlite.execute('UPDATE archive_sections SET page=1 WHERE document_id=?', (document_id,))
    store._sqlite.commit()
    chunk = archive._chunk_rows(False, document_ids=[document_id])[0]
    citation = f"[doc:{document_id}#{chunk['id']}]"
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: (_ for _ in ()).throw(AssertionError('original byte read')))
    server = server_for(store)
    for view in ['context', 'outline']:
        output = invoke(server, 'proharness_open', refs=[citation], view=view)
        hint = output['original_page']
        assert hint['availability'] == 'unchecked'
        assert hint['expected_source_reference']['document_id'] == document_id
        route = hint['followup']
        assert route['tool'] == 'proharness_open' and route['arguments']['view'] == 'page'
        assert route['arguments']['refs'][0].startswith(f'[page:{document_id}@1:')
        invoke(server, 'proharness_open', refs=[citation], view=view, max_tokens=512)
    store._sqlite.execute('UPDATE archive_documents SET sensitive=1 WHERE id=?', (document_id,))
    store._sqlite.commit()
    for view in ['context', 'outline']:
        hidden = invoke(server, 'proharness_open', refs=[citation], view=view)
        assert hidden['statuses'] == ['unavailable'] and 'original_page' not in hidden


def test_find_cursor_is_callable_through_the_six_tool_sdk(store):
    from proharness.memory.schemas import Memory
    expected = {f"[mem:{store.add(Memory(text=f'Copper sensor calibration {n}.')).id}]" for n in range(9)}
    server = server_for(store)
    response = invoke(server, 'proharness_find', query='copper sensor')
    seen = set()
    for _ in range(12):
        for key in ('relevant_memories', 'omitted_references'):
            seen.update(item['citation'] for item in response[key] if item.get('citation', '').startswith('[mem:'))
        cursor = response['continuation']['next_cursor']
        if cursor is None:
            break
        response = invoke(server, 'proharness_find', cursor=cursor, include_sensitive=False)
    assert cursor is None
    assert seen == expected
