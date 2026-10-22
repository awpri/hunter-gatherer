"""Synthetic captionless PDF description → Find → exact bounded crop."""
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from test_document_page_image import pdf_bytes

from hunter_gatherer.memory.document_enrichment import (
    EnrichmentError,
    attach_document_enrichment,
    revoke_document_enrichment,
)
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.output_budget import output_bytes
from hunter_gatherer.memory.simple_open import open_sources
from hunter_gatherer.memory.simple_search import find
from hunter_gatherer.memory.visual_regions import discover_regions, region_image, resolve_region


def setup(store, tmp_path, bbox=None):
    path = tmp_path / 'captionless.pdf'
    path.write_bytes(pdf_bytes(pages=1))
    doc = DocumentArchive(store).ingest(str(path))
    saved = attach_document_enrichment(store, document_id=doc['document_id'], expected_version=doc['version'],
        expected_content_hash=doc['content_hash'], producer={'id':'synthetic-agent'}, idempotency_key='region',
        records=[{'key':'diagram','kind':'visual_region','text':'crimson geometric rocket schematic', 'anchors':[],
                  'region':{'page':1,'bbox':bbox or [0,0,.5,.5]}}])
    return doc, saved, saved['record_ids'][0], path


def test_captionless_find_open_actual_crop_and_revocation(store, tmp_path):
    pytest.importorskip('pypdfium2')
    doc, batch, record, _ = setup(store, tmp_path)
    item, = discover_regions(store, ['rocket'])
    assert item['verification'] == 'unverified' and 'quote' not in item
    assert item['source_reference']['original_content_hash'] == doc['content_hash']
    harness = SimpleNamespace(store=store, settings=store.settings)
    for budget in (2048,4096):
        found = find(harness, 'rocket', max_tokens=budget)
        assert found['visual_regions'][0]['citation'] == item['citation']
        assert output_bytes(found) <= budget * 4
    cropped = open_sources(harness, [item['citation']], view='region')
    assert '_image_png' in cropped, cropped
    whole = open_sources(harness, [item['citation']], view='page')
    crop_image = Image.open(BytesIO(cropped['_image_png']))
    page_image = Image.open(BytesIO(whole['_image_png']))
    expected = page_image.crop((0,0,(page_image.width+1)//2,(page_image.height+1)//2))
    assert crop_image.size == expected.size and crop_image.tobytes() == expected.tobytes()
    revoke_document_enrichment(store, document_id=doc['document_id'], batch_id=batch['batch_id'])
    assert not discover_regions(store,['rocket'])
    assert region_image(store,record)['error_code'] == 'region_unavailable'


@pytest.mark.parametrize('bbox', [[0,0,float('nan'),1],[0,0,float('inf'),1],[False,0,1,1],[0,0,0,1],[-.1,0,1,1],[0,0,2,1]])
def test_invalid_bounds(store,tmp_path,bbox):
    with pytest.raises(EnrichmentError):
        setup(store,tmp_path,bbox)


def test_visibility_hash_and_new_version_guards(store,tmp_path):
    doc,batch,record,path=setup(store,tmp_path)
    store._sqlite.execute('UPDATE archive_documents SET sensitive=1 WHERE id=?',(doc['document_id'],))
    store._sqlite.commit()
    assert resolve_region(store,record) is None
    assert resolve_region(store,record,True)
    store._sqlite.execute('UPDATE archive_documents SET content_hash=? WHERE id=?',('a'*64,doc['document_id']))
    store._sqlite.commit()
    assert resolve_region(store,record,True) is None


def test_new_version_deletion_original_corruption_and_page_range(store,tmp_path):
    doc,_,record,path=setup(store,tmp_path)
    original=store._sqlite.execute('SELECT original_path FROM archive_documents WHERE id=?',(doc['document_id'],)).fetchone()[0]
    from pathlib import Path
    managed=Path(original)
    if not managed.is_absolute():
        managed=Path(store.settings.sqlite_path).parent / managed
    data=managed.read_bytes()
    managed.write_bytes(b'corrupt')
    assert region_image(store,record)['error_code']=='original_integrity_failed'
    managed.write_bytes(data)
    path.write_bytes(pdf_bytes(pages=2))
    newer=DocumentArchive(store).ingest(str(path))
    assert newer['version']>doc['version'] and resolve_region(store,record) is None
    DocumentArchive(store).forget(newer['document_id'])
    assert region_image(store,record)['error_code']=='region_unavailable'


def test_rotated_cropbox_geometry_matches_displayed_page(store,tmp_path):
    pytest.importorskip('pypdfium2')
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject
    path=tmp_path/'rotated.pdf'
    reader=PdfReader(BytesIO(pdf_bytes(pages=1)))
    page=reader.pages[0]
    page.cropbox=RectangleObject([20,30,220,280])
    page.rotate(90)
    writer=PdfWriter()
    writer.add_page(page)
    with path.open('wb') as stream:
        writer.write(stream)
    doc=DocumentArchive(store).ingest(str(path))
    saved=attach_document_enrichment(store,document_id=doc['document_id'],expected_version=doc['version'],
        expected_content_hash=doc['content_hash'],producer={'id':'test'},idempotency_key='rotated',
        records=[{'key':'r','kind':'visual_region','text':'rotated artwork','anchors':[],
                  'region':{'page':1,'bbox':[.2,.1,.8,.7]}}])
    identity=saved['record_ids'][0]
    whole=region_image(store,identity,view='page')
    crop=region_image(store,identity)
    full=Image.open(BytesIO(whole['_image_png']))
    actual=Image.open(BytesIO(crop['_image_png']))
    import math
    expected=full.crop((math.floor(.2*full.width),math.floor(.1*full.height),math.ceil(.8*full.width),math.ceil(.7*full.height)))
    assert actual.size==expected.size and actual.tobytes()==expected.tobytes()
    assert crop['render']['intrinsic_rotation_degrees']==90


def test_six_tool_transport_import_find_and_open(store,tmp_path):
    import asyncio
    import json

    from test_simple_library_workflow import server_for

    from hunter_gatherer.mcp.profiles import LIBRARY_TOOL_NAMES
    pytest.importorskip('pypdfium2')
    path=tmp_path/'transport.pdf'
    path.write_bytes(pdf_bytes(pages=1))
    doc=DocumentArchive(store).ingest(str(path))
    server=server_for(store)
    assert len(asyncio.run(server.list_tools())) == len(LIBRARY_TOOL_NAMES) == 6
    attached=asyncio.run(server.call_tool('hg_import_documents', {'action':'enrich', 'idempotency_key':'wire',
        'enrichment':{'document_id':doc['document_id'],'expected_version':doc['version'],
        'expected_content_hash':doc['content_hash'],'producer':{'id':'wire'},'records':[
            {'key':'wire','kind':'visual_region','text':'orbital crimson rocket schematic','anchors':[],
             'region':{'page':1,'bbox':[0,0,.5,.5]}}]}}))
    assert not attached.is_error, attached.content
    found=asyncio.run(server.call_tool('hg_find',{'query':'orbital rocket','max_tokens':4096}))
    assert not found.is_error, found.content
    item=json.loads(found.content[0].text)['visual_regions'][0]
    opened=asyncio.run(server.call_tool('hg_open',item['followup']['arguments']))
    assert not opened.is_error, opened.content
    assert any(part.type=='image' for part in opened.content)
    metadata=json.loads(next(part.text for part in opened.content if part.type=='text'))
    assert metadata['citation']==item['citation'] and '_image_png' not in metadata
    from hunter_gatherer.server.routers.library_sources import CollectionInput
    with pytest.raises(ValueError):
        CollectionInput(name='Unsupported region',references=[{'region_id':item['record_id']}])


def test_more_than_eight_regions_continue_without_duplicates_at_short_budget(store,tmp_path):
    doc,_,_,_=setup(store,tmp_path)
    saved=attach_document_enrichment(store,document_id=doc['document_id'],expected_version=doc['version'],
        expected_content_hash=doc['content_hash'],producer={'id':'many'},idempotency_key='many-regions',
        records=[{'key':f'r{i}','kind':'visual_region','text':'rocket '+('diagram ' * 220)+str(i),'anchors':[],
                  'region':{'page':1,'bbox':[0,0,1,1]}} for i in range(12)])
    expected=set(saved['record_ids']) | {item['record_id'] for item in discover_regions(store,['rocket'],limit=256)}
    harness=SimpleNamespace(store=store,settings=store.settings)
    found=find(harness,'rocket',max_tokens=1024)
    seen=[]
    omitted=False
    for _ in range(30):
        assert output_bytes(found)<=1024*4
        for item in found.get('visual_regions',[]):
            seen.append(item['record_id'])
            assert item['followup']['arguments']['refs']==[item['citation']]
            omitted |= item.get('description_omitted',False)
        cursor=found['continuation']['next_cursor']
        if cursor is None:
            break
        found=find(harness,cursor=cursor,max_tokens=1024)
    else:
        pytest.fail('region continuation failed to terminate')
    assert len(seen)==len(set(seen)) and set(seen)==expected
    assert omitted
    assert found['visual_region_discovery']['remaining']==0
    assert 'not exhaustive' in found['visual_region_discovery']['scope']


def test_region_open_metadata_budget_excludes_native_image_and_marks_omitted_description(monkeypatch):
    from hunter_gatherer.memory import visual_regions
    png=b'large-native-image' * 1000
    monkeypatch.setattr(visual_regions,'region_image',lambda *args,**kwargs: {
        'citation':'[region:11111111-1111-4111-8111-111111111111]',
        'description':'long annotation '*200,'verification':'unverified','_image_png':png})
    packet=open_sources(SimpleNamespace(store=None),['[region:11111111-1111-4111-8111-111111111111]'],view='region',max_tokens=512)
    assert packet['_image_png'] is png
    assert packet['description_omitted'] and 'description' not in packet
    metadata={key:value for key,value in packet.items() if not key.startswith('_')}
    assert output_bytes(metadata)<=512*4
    assert packet['budget']['serialized_bytes']==output_bytes(metadata)
