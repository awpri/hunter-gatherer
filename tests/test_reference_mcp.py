"""Exercise the actual MCP SDK registration and invocation on synthetic data."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from proharness.mcp.server import build_server
from proharness.mcp.tools import Harness
from proharness.memory.schemas import Memory


@pytest.fixture
def reference_server(store, settings):
    harness = Harness(loop=SimpleNamespace(), store=store, cost=SimpleNamespace(), settings=settings)
    server, _ = build_server(harness=harness)
    assert any(t.name == "proharness_fetch" for t in asyncio.run(server.list_tools()))
    return server


def invoke(server, arguments):
    result = asyncio.run(server.call_tool('proharness_fetch', arguments))
    assert not result.is_error
    texts = [c.text for c in result.content if c.type == 'text']
    return json.loads(texts[0])


def test_reference_sdk_registration_schema(reference_server):
    tools = asyncio.run(reference_server.list_tools())
    tool = next(t for t in tools if t.name == 'proharness_fetch')
    schema = tool.input_schema
    assert schema['required'] == ['refs']
    refs = schema['properties']['refs']
    assert refs['type'] == 'array'
    assert refs['minItems'] == 1 and refs['maxItems'] == 32
    assert refs['items']['type'] == 'string'
    budget = schema['properties']['max_tokens']
    assert budget['minimum'] == 512 and budget['maximum'] == 16384
    assert budget['default'] == 4096
    assert schema['properties']['include_sensitive']['default'] is False


def test_reference_sdk_call_defaults_and_read_only(reference_server, store):
    memory = store.add(Memory(text='Synthetic source 🧪'))
    private = store.add(Memory(text='Synthetic secret', metadata={'sensitive': True}))
    changes = store._sqlite.total_changes
    counts = [(m.access_count, m.last_accessed) for m in (store.get(memory.id), store.get(private.id))]
    result = invoke(reference_server, {'refs': [memory.id, f'[mem:{memory.id[:6]}]', private.id, 'bad']})
    assert result['statuses'] == ['available', 'available', 'unavailable', 'invalid']
    assert result['sources'][0]['text'] == memory.text
    assert result['sources'][0]['indices'] == [0, 1]
    assert result['budget']['max_tokens'] == 4096
    assert invoke(reference_server, {'refs': [private.id], 'include_sensitive': True})['statuses'] == ['available']
    assert store._sqlite.total_changes == changes
    assert [(m.access_count, m.last_accessed) for m in (store.get(memory.id), store.get(private.id))] == counts


@pytest.mark.parametrize('arguments', [
    {'refs': []}, {'refs': ['a']*33}, {'refs': 'wrong'},
    {'refs': ['a'], 'max_tokens': 511}, {'refs': ['a'], 'max_tokens': 16385},
    {'refs': ['a'], 'max_tokens': True}, {'refs': ['a'], 'include_sensitive': 1},
])
def test_reference_sdk_rejects_contract_violations(reference_server, arguments):
    # SDK validation can raise or produce an MCP error depending on SDK version.
    try:
        result = asyncio.run(reference_server.call_tool('proharness_fetch', arguments))
    except (ValueError, TypeError, ToolError):
        return
    assert result.is_error


@pytest.mark.parametrize('nrefs', [1, 32])
def test_sdk_serialized_budget_includes_store(reference_server, store, nrefs):
    from proharness.mcp.tools import serialize_result
    memory = store.add(Memory(text='🧪'*4000))
    short = store.add(Memory(text='small source'))
    refs = ([memory.id, short.id, '0'*32, 'bad'] * 8)[:nrefs]
    raw = asyncio.run(reference_server.call_tool('proharness_fetch', {'refs': refs, 'max_tokens': 512}))
    assert not raw.is_error
    text = next(c.text for c in raw.content if c.type == 'text')
    result = json.loads(text)
    assert 'store' in result
    assert len(text.encode('utf-8')) == result['budget']['serialized_bytes']
    assert len(serialize_result(result).encode('utf-8')) <= 512*4
    assert result['statuses'][0] == 'omitted'
    assert len(result['statuses']) == nrefs
