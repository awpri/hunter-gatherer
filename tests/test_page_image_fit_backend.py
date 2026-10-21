"""Public fitting invariants independent of the renderer implementation."""
import importlib
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from test_document_page_image import ingest, png_bytes

backend = importlib.import_module('proharness.memory.document_page_image')


def fitted_result():
    return {'page_count': 2, 'width': 16, 'height': 24, '_image_png': png_bytes(),
            'render': {'renderer': 'pypdfium2', 'renderer_version': '5.0.0', 'pdfium_version': '144.0.0',
                       'pillow_version': '12.0.0', 'max_pixels': 4_000_000, 'additional_rotation_degrees': 0,
                       'intrinsic_rotation_degrees': 0, 'crop': 'PDF visible CropBox; no additional crop',
                       'background': 'white', 'forms': 'disabled', 'annotations': 'visible', 'ocr': False,
                       'scale': 1.0, 'requested_max_side': 2048, 'max_side': 1536,
                       'fit_to_byte_limit': True, 'downsampling_retry': True, 'attempt_count': 2,
                       'attempts': [{'max_side': 2048, 'width': 1366, 'height': 2048, 'outcome': 'image_too_large'},
                                    {'max_side': 1536, 'width': 16, 'height': 24, 'outcome': 'success'}],
                       'resolution_notice': 'do not trust this worker string'}}


@pytest.mark.parametrize('value', [None, 0, 1, 'true', 'false', [], {}])
def test_fitting_strict_boolean_before_io(store, monkeypatch, value):
    monkeypatch.setattr(backend, 'load_document_original', lambda *a, **kw: pytest.fail('invalid input reached disk'))
    assert backend.document_page_image(store, 'id', page=1, fit_to_byte_limit=value)['error_code'] == 'invalid_input'


def test_opt_in_forwarding_provenance_and_unchanged_original(store, tmp_path, monkeypatch):
    source, identity = ingest(store, tmp_path)
    original = source.read_bytes()
    calls = []
    def render(data, page, side, **kw):
        calls.append((data, page, side, kw))
        return fitted_result()
    monkeypatch.setattr(backend, '_worker_render', render)
    before = store._sqlite.total_changes
    result = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True)
    assert calls == [(original, 1, 2048, {'fit_to_byte_limit': True})]
    assert result['source_reference']['document_id'] == identity
    assert result['render']['max_side'] == 1536
    assert 'lower resolution' in result['render']['resolution_notice']
    assert 'do not trust' not in str(result)
    assert source.read_bytes() == original
    assert store._sqlite.total_changes == before


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(requested_max_side=1024),
    lambda r: r.update(fit_to_byte_limit=1),
    lambda r: r.update(attempt_count=True),
    lambda r: r.update(attempt_count=5),
    lambda r: r.update(downsampling_retry=False),
    lambda r: r.update(max_side=2048),
    lambda r: r.update(scale=float('nan')),
    lambda r: r.update(scale=True),
    lambda r: r.update(renderer_version='/private/path'),
    lambda r: r.update(forms='enabled'),
    lambda r: r.update(ocr=0),
    lambda r: r['attempts'][0].update(max_side=3072),
    lambda r: r['attempts'][0].update(outcome='success'),
    lambda r: r['attempts'][0].update(width=2049),
    lambda r: r['attempts'][1].update(width=17),
    lambda r: r['attempts'][1].update(outcome='image_too_large'),
])
def test_malformed_fit_history_rejected_before_output(store, tmp_path, monkeypatch, mutation):
    _, identity = ingest(store, tmp_path)
    result = fitted_result()
    mutation(result['render'])
    monkeypatch.setattr(backend, '_worker_render', lambda *a, **kw: deepcopy(result))
    response = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True)
    assert response['error_code'] == 'render_failed'
    assert '_image_png' not in response


@pytest.mark.parametrize('change', ['sensitive=1', 'deleted_at=42', 'version=2'])
@pytest.mark.parametrize('failed', [False, True])
def test_final_attempt_still_reauthorizes(store, tmp_path, monkeypatch, change, failed):
    _, identity = ingest(store, tmp_path)
    def render(*args, **kwargs):
        with sqlite3.connect(store.settings.sqlite_path) as db:
            db.execute(f'UPDATE archive_documents SET {change} WHERE id=?', (identity,))
        if failed:
            return {'error': 'size', 'error_code': 'image_too_large', 'http_status': 413, 'render': fitted_result()['render']}
        return fitted_result()
    monkeypatch.setattr(backend, '_worker_render', render)
    response = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True)
    assert response['error_code'] in ('document_unavailable', 'original_changed')
    assert 'render' not in response and '_image_png' not in response


def test_remaining_deadline_forwarded_once_and_expiry_rejects_output(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    clock = [100.0]
    monkeypatch.setattr(backend.time, 'monotonic', lambda: clock[0])
    def render(*a, **kw):
        assert kw == {'fit_to_byte_limit': True, 'timeout_seconds': 3.0}
        clock[0] = 104.0
        return fitted_result()
    monkeypatch.setattr(backend, '_worker_render', render)
    assert backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True, _deadline=103.0)['error_code'] == 'render_timeout'


def test_one_owned_process_deadline_and_cleanup(monkeypatch):
    paths, killed, commands, waits = [], [], [], []
    class Process:
        pid = 878787
        returncode = None
        def __init__(self, command, **kwargs):
            commands.append(command)
            paths.append(Path(command[3]).parent)
        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired('owned', timeout)
            return 1
    monkeypatch.setattr(Process, "poll", lambda self: None, raising=False)
    monkeypatch.setattr(Process, "kill", lambda self: killed.append(self.pid), raising=False)
    monkeypatch.setattr(backend.subprocess, 'Popen', Process)
    monkeypatch.setattr(backend.os, 'killpg', lambda pid, sig: killed.append(pid), raising=False)
    response = backend._worker_render(b'%PDF', 1, 2048, timeout_seconds=0.2, fit_to_byte_limit=True)
    assert response['error_code'] == 'render_timeout'
    assert len(commands) == 1 and commands[0][-1] == 'true'
    assert 0 <= waits[0] <= 0.2
    assert killed == [878787] and all(not p.exists() for p in paths)
