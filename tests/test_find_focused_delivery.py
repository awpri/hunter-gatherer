"""Weak bodies stay recoverable; useful detail, paraphrase and scope survive."""
from types import SimpleNamespace

from proharness.memory.documents import DocumentArchive
from proharness.memory.output_budget import output_bytes
from proharness.memory.schemas import Memory
from proharness.memory.simple_search import _focused_references, find


def test_focused_body_and_semantic_escape_hatch():
    packet = {'supporting_passages': [
        {'citation':'head','quote':'## Family records\n'},
        {'citation':'unrelated','quote':'Ada builds software.'},
        {'citation':'partial','quote':'Ada’s grandmother lived in Bristol. Name unrecorded.'},
        {'citation':'paraphrase','quote':'Her maternal ancestor was called Vera.', 'explanation':{'semantic_similarity':.78}},
        {'citation':'nan','quote':'A camera manual.', 'explanation':{'semantic_similarity':float('nan')}},
        {'citation':'required','quote':'The form preserves the original inscription.'},
    ], 'relevant_memories': [], 'requirement_checks':[{'selected_citations':['required']}]}
    result = _focused_references(packet, ["What is Ada's grandmother's name?"])
    assert result == {'head':'heading_only_navigation','unrelated':'weak_query_body_match','nan':'weak_query_body_match'}
    assert _focused_references({'supporting_passages':[{'citation':'name','quote':'Ada writes software.'}],
                                'relevant_memories':[]}, ['Who is Ada?']) == {}


def test_deferral_keeps_open_continuation_and_privacy(store, tmp_path):
    archive=DocumentArchive(store)
    file=tmp_path/'profile.txt'
    file.write_text('Ada builds open-source software for historical archives.')
    doc=archive.ingest(file,title='Ada profile')
    store.add(Memory(text='Ada studies history.'))
    store.settings.library_find_delivery_policy='focused'
    harness=SimpleNamespace(store=store,settings=store.settings)
    result=find(harness,"What is Ada's grandmother's name?",max_tokens=4096)
    assert not result['supporting_passages'] and not result['relevant_memories']
    ref=next(r for r in result['omitted_references'] if r.get('document_id')==doc['document_id'])
    assert ref['omission_reason']=='weak_query_body_match'
    assert ref['followups'][0]['arguments']['refs']==[ref['citation']]
    assert result['retrieval_assessment']['answerability']=='not_determined'
    assert output_bytes(result)<=4096*4
    store._sqlite.execute('UPDATE archive_documents SET sensitive=1 WHERE id=?',(doc['document_id'],))
    store._sqlite.commit()
    assert doc['document_id'] not in str(find(harness,"What is Ada's grandmother's name?"))
