"""Evidence Lens HTTP routes, with fake providers and both socket and in-memory transports."""

from __future__ import annotations

import io
import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from lens_fakes import evidence_id, seed
from test_ui_server import call, wait_job

from litforge.analysis import ask as answers
from litforge.analysis import gaps
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.matrix import MatrixColumn, create_matrix
from litforge.analysis.runs import start_run
from litforge.config import Config, ModelProfileConfig, Paths
from litforge.llm.budget import Pricing
from litforge.screening.llm import FakeLlm
from litforge.ui.server import App, make_handler

SECRET = 'PRIVATE_PROVIDER_TOKEN_123'
UNTRUSTED = '<img src=x onerror=alert(1)>'
COLUMN = {'key': 'gain', 'label': 'Gain', 'instruction': 'gain', 'type': 'number'}


class MemorySocket:
    """Run the actual stdlib request handler without needing permission to bind a port."""

    def __init__(self, request: bytes) -> None:
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, mode: str, *args: Any) -> io.BytesIO:
        return self.input if mode == 'rb' else io.BytesIO(self.output)

    def sendall(self, data: bytes) -> None:
        self.output.extend(data)


@dataclass
class LensServer:
    app: App
    fake: FakeLlm
    run_id: str
    matrix_id: str
    base: str | None = None

    def call(self, path: str, body: Any = None, origin: str | None = None) -> tuple[int, Any]:
        headers = {'Origin': origin} if origin else {}
        if self.base:
            return call(self.base, path, body, headers=headers)
        data = json.dumps(body).encode() if body is not None else b''
        method = 'POST' if body is not None else 'GET'
        request = (f'{method} {path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: {len(data)}\r\n' +
                   ''.join(f'{k}: {v}\r\n' for k, v in headers.items()) + '\r\n').encode() + data
        sock = MemorySocket(request)
        make_handler(self.app)(sock, ('127.0.0.1', 1234), object())  # type: ignore[arg-type]
        response = HTTPResponse(MemorySocket(bytes(sock.output)))  # type: ignore[arg-type]
        response.begin()
        content = response.read()
        if response.getheader('Content-Type', '').startswith('application/json'):
            return response.status, json.loads(content)
        return response.status, content

    def job(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        status, job = self.call(path, body)
        assert status == 200, job
        if self.base:
            return wait_job(self.base, job['id'])
        # A bounded join also catches background failures without binding sockets.
        import time
        for _ in range(200):
            _, job = self.call('/api/jobs/' + job['id'])
            if job['status'] != 'running':
                return job
            time.sleep(.01)
        raise AssertionError('job did not finish')


@pytest.fixture(params=['memory', 'http'])
def lens_server(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[LensServer]:
    config = Config()
    config.models.policy = 'ask_before_cloud'
    config.privacy.allow_cloud = True
    config.models.profiles['paid'] = ModelProfileConfig(
        provider='openai_compatible', model='remote', remote=True,
        pricing=Pricing(input_per_million_usd=1, output_per_million_usd=1))
    planner_response: dict[str, Any] = {
        'steps': [{'action': 'filter_papers', 'args': {'year_from': 2020}}],
    }
    planner_messages: list[dict[str, str]] = []

    def reply(messages: list[dict[str, str]]) -> dict[str, Any]:
        if 'Plan a research-library workflow' in messages[0]['content']:
            planner_messages[:] = messages
            return planner_response
        if 'Audit' in messages[0]['content']:
            return {'verdict': 'partial', 'note': UNTRUSTED}
        if 'Extract' in messages[0]['content']:
            return {'status': 'reported', 'value': 3.25, 'unit': 'ms',
                    'evidence_ids': [evidence_id(messages)], 'note': UNTRUSTED}
        return {'sentences': [{'text': 'Gain 3.25. ' + UNTRUSTED, 'kind': 'interpretation',
                               'evidence_ids': [evidence_id(messages)]}], 'insufficient': False}

    fake = FakeLlm(reply)
    app = App(Paths(tmp_path / 'data'), config,
              make_gateway=lambda c: ModelGateway(c, config, providers={'local': fake, 'paid': fake}))
    conn = app.db()
    try:
        seed(conn)
        conn.execute('UPDATE papers SET title=?', (UNTRUSTED,))
        answer = answers.ask(conn, app.make_gateway(conn), 'gain', 'proj')
        matrix = create_matrix(conn, 'proj', 'Studies ' + UNTRUSTED, 'gain', [MatrixColumn(**COLUMN)])
    finally:
        conn.close()
    server = LensServer(app, fake, answer.run_id, matrix.id)
    server.planner_response = planner_response
    server.planner_messages = planner_messages
    httpd = None
    try:
        if request.param == 'http':
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            server.base = f'http://127.0.0.1:{httpd.server_address[1]}'
        yield server
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()


def task_request(server: LensServer, task: str) -> tuple[str, dict[str, Any]]:
    if task == 'ask':
        return '/api/ask', {'task': task, 'question': 'gain', 'project': 'demo'}
    if task == 'audit':
        return '/api/audit', {'task': task, 'run_id': server.run_id}
    return '/api/matrices/' + server.matrix_id + '/extract', {'task': task, 'matrix_id': server.matrix_id}


@pytest.mark.parametrize('task', ['ask', 'audit', 'matrix_extract'])
def test_preview_and_remote_approval(lens_server: LensServer, task: str) -> None:
    server = lens_server
    path, body = task_request(server, task)
    body['profile'] = 'paid'
    before = server.fake.calls
    status, plan = server.call('/api/preview', body)
    assert status == 200 and plan['remote'] and plan['requires_approval']
    assert 'calls' not in plan  # only aggregate metadata is sent to the preview dialog
    assert plan['providers'] == ['openai_compatible'] and plan['models'] == ['remote']
    assert plan['evidence_ids_count'] == plan['number_of_calls'] == 1
    assert plan['estimated_input_tokens'] > 0 and plan['max_cost_usd'] > 0
    status, result = server.call(path, body)
    assert status == 403 and 'Approval' in result['error']
    assert server.fake.calls == before and not server.app.jobs
    assert server.call(path, {**body, 'approved': 'true'})[0] == 400
    job = server.job(path, {**body, 'approved': True})
    assert job['status'] == 'done', job
    assert server.fake.calls == before + 1


def test_console_plan_preview_approval_execution_and_validation(lens_server: LensServer) -> None:
    server = lens_server
    conn = server.app.db()
    conn.execute("UPDATE papers SET year=2022 WHERE id='p'")
    conn.close()
    before = server.fake.calls
    status, data = server.call('/api/console/plan', {'request': 'Select newer papers', 'project': 'demo'})
    assert status == 200
    assert data['plan']['steps'][0]['action'] == 'filter_papers'
    assert data['preview']['steps'][0]['papers_in_scope'] == 1
    assert data['preview']['steps'][0]['writes'] is False
    assert server.fake.calls == before + 1
    prompt = '\n'.join(message['content'] for message in server.planner_messages)
    assert 'Select newer papers' in prompt and UNTRUSTED not in prompt

    status, error = server.call('/api/console/execute', {'plan': data['plan'], 'project': 'demo'})
    assert status == 403 and 'Explicit execution approval' in error['error']
    assert server.call('/api/console/execute', {'plan': data['plan'], 'project': 'demo',
                                                'approved': 'true'})[0] == 403
    status, started = server.call('/api/console/execute', {'plan': data['plan'], 'project': 'demo',
                                                           'approved': True})
    assert status == 200 and started['run_id']
    for _ in range(100):
        status, run = server.call('/api/console/runs/' + started['run_id'])
        if run['status'] not in ('pending', 'running'):
            break
        import time
        time.sleep(.01)
    assert status == 200 and run['status'] == 'done' and len(run['results']) == 1
    assert server.call('/api/console/runs?project=demo')[1][0]['id'] == started['run_id']

    server.planner_response.clear()
    server.planner_response.update({'steps': [{'action': 'shell', 'args': {'command': 'id'}}]})
    status, rejected = server.call('/api/console/plan', {'request': 'unsafe action', 'project': 'demo'})
    assert status == 400 and 'validation_errors' in rejected
    assert 'unknown action' in str(rejected['validation_errors'])


def test_answer_audit_and_sentence_reviews(lens_server: LensServer) -> None:
    server = lens_server
    job = server.job('/api/ask', {'question': 'gain', 'project': 'demo'})
    assert job['status'] == 'done', job
    status, answer = server.call('/api/answers/' + job['run_id'])
    assert status == 200 and answer['evidence'][0]['title'] == UNTRUSTED
    sentence = answer['sentences'][0]
    assert sentence['needs_review'] and sentence['audit_focus']
    assert UNTRUSTED in sentence['text']
    job = server.job('/api/audit', {'run_id': answer['run_id']})
    assert job['status'] == 'done' and job['report']['sentences'][0]['audit_verdict'] == 'partial'
    _, queue = server.call('/api/review?project=demo')
    item = next(i for i in queue if i['target_id'] == sentence['id'])
    assert item['run_id'] == answer['run_id'] and item['evidence'][0]['page'] == 1
    assert item['question'] == 'gain'
    for action in ('accept', 'reject', 'edit'):
        assert server.call('/api/review', {'target_kind': 'sentence', 'target_id': sentence['id'],
                                          'action': action, 'new_value': 'Edited gain 3.25.',
                                          'reason': UNTRUSTED})[0] == 200
    _, updated = server.call('/api/answers/' + answer['run_id'])
    assert updated['sentences'][0]['text'] == 'Edited gain 3.25.'
    assert updated['sentences'][0]['review_status'] == 'edited'


@pytest.mark.parametrize('view', ['matrix', 'sentence_review', 'cell_review'])
def test_reused_evidence_keeps_source_run_page(lens_server: LensServer, view: str) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        conn.execute('UPDATE chunks SET text=?, page_end=2, bbox_json=? WHERE id=?',
                     ('alpha marker. beta marker.', json.dumps([
                         {'start': 0, 'end': 14, 'page': 1}, {'start': 14, 'end': 26, 'page': 2},
                     ]), 'cp'))
    finally:
        conn.close()

    # A flagged value keeps cited matrix cells in the review queue.
    fake = FakeLlm(lambda m: {'status': 'reported', 'value': 999,
                              'evidence_ids': [evidence_id(m)], 'note': ''})
    cases = []
    for idx, (term, page) in enumerate([('alpha', 1), ('beta', 2), ('alpha', 1)]):
        if view == 'sentence_review':
            job = server.job('/api/ask', {'question': term, 'project': 'demo'})
            assert job['status'] == 'done', job
            status, answer = server.call('/api/answers/' + job['run_id'])
            assert status == 200 and answer['evidence'][0]['page'] == page
            cases.append((answer['sentences'][0]['id'], answer['evidence'][0]['id'], page))
        else:
            server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': fake})
            status, matrix = server.call('/api/matrices', {
                'name': f'{term} {idx}', 'question': term, 'project': 'demo',
                'columns': [{**COLUMN, 'label': term, 'instruction': term}],
            })
            assert status == 200
            mid = matrix['id']
            job = server.job(f'/api/matrices/{mid}/extract', {})
            assert job['status'] == 'done', job
            status, exported = server.call(f'/api/matrices/{mid}/export?format=json')
            assert status == 200
            assert exported['evidence'][0]['page'] == page
            target = mid if view == 'matrix' else exported['cells'][0]['id']
            cases.append((target, exported['evidence'][0]['id'], page))

    assert len({eid for _, eid, _ in cases}) == 1  # One ledger span, three retrieval runs.
    status, queue = server.call('/api/review?project=demo')
    assert status == 200
    for target, eid, page in cases:
        if view == 'matrix':
            status, result = server.call('/api/matrices/' + target)
            assert status == 200
        else:
            result = next(item for item in queue if item['target_id'] == target)
        assert [(s['id'], s['page']) for s in result['evidence']] == [(eid, page)]


def test_matrix_create_extract_add_column_review_and_exports(lens_server: LensServer) -> None:
    server = lens_server
    status, matrix = server.call('/api/matrices', {'name': 'New matrix', 'question': 'gain',
                                                   'project': 'demo', 'columns': [COLUMN]})
    assert status == 200
    mid = matrix['id']
    _, listing = server.call('/api/matrices?project=demo')
    assert mid in {m['id'] for m in listing}
    job = server.job(f'/api/matrices/{mid}/extract', {})
    assert job['status'] == 'done', job
    assert job['report']['stats'] == {'extracted': 1, 'cached': 0, 'flagged': 0}
    status, matrix = server.call(f'/api/matrices/{mid}')
    assert status == 200 and matrix['papers'][0]['title'] == UNTRUSTED
    assert matrix['evidence'][0]['text'].startswith('Results:') and matrix['warnings'] == []
    cell = matrix['cells'][0]
    assert cell['status'] == 'reported' and cell['value'] == 3.25
    before = server.fake.calls
    cached = server.job(f'/api/matrices/{mid}/extract', {})
    assert cached['status'] == 'done'
    assert cached['report']['stats'] == {'extracted': 0, 'cached': 1, 'flagged': 0}
    assert server.fake.calls == before
    assert server.call(f'/api/matrices/{mid}/columns', {**COLUMN, 'key': 'other'})[0] == 200
    assert server.job(f'/api/matrices/{mid}/extract', {})['status'] == 'done'
    assert server.fake.calls == before + 1
    for action in ('accept', 'reject', 'edit'):
        assert server.call('/api/review', {'target_kind': 'cell', 'target_id': cell['id'], 'action': action,
                                          'new_value': 4.0})[0] == 200
    _, matrix = server.call(f'/api/matrices/{mid}')
    assert matrix['cells'][0]['value'] == 4.0 and matrix['cells'][0]['model_value'] == 3.25
    for fmt in ('md', 'csv', 'json'):
        status, content = server.call(f'/api/matrices/{mid}/export?format={fmt}')
        assert status == 200 and content
    assert b'gain_status' in server.call(f'/api/matrices/{mid}/export?format=csv')[1]


@pytest.mark.parametrize('task', ['ask', 'audit', 'matrix_extract'])
def test_blocked_plan_and_safe_provider_errors(lens_server: LensServer, task: str) -> None:
    server = lens_server
    path, body = task_request(server, task)
    before = server.fake.calls
    assert server.call(path, {**body, 'profile': 'missing'})[0] == 400
    assert server.fake.calls == before

    def broken(messages: list[dict[str, str]]) -> dict[str, Any]:
        raise RuntimeError(SECRET)

    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': FakeLlm(broken)})
    job = server.job(path, body)
    assert SECRET not in json.dumps(job)
    if task == 'matrix_extract':
        assert job['status'] == 'done' and job['report']['cells'][0]['status'] == 'error'
    else:
        assert job['status'] == 'failed' and 'Analysis failed' in job['error']


@pytest.mark.parametrize(('path', 'body'), [
    ('/api/ask', {'question': ''}), ('/api/audit', {'run_id': 'missing'}),
    ('/api/preview', {'task': 'unknown'}), ('/api/review', {'action': 'unknown'}),
    ('/api/matrices', {'name': 'bad', 'question': 'gain', 'columns': []}),
    ('/api/matrices/missing/columns', COLUMN), ('/api/matrices/missing/extract', {}),
    ('/api/answers/missing', None), ('/api/matrices/missing', None),
    ('/api/review?project=missing', None), ('/api/matrices?project=missing', None),
    ('/api/matrices/missing/export?format=md', None),
])
def test_invalid_and_missing_items(lens_server: LensServer, path: str, body: Any) -> None:
    status, result = lens_server.call(path, body)
    assert status in (400, 404) and 'error' in result


@pytest.mark.parametrize('path', ['/api/ask', '/api/audit', '/api/preview', '/api/review', '/api/matrices',
                                 '/api/matrices/id/columns', '/api/matrices/id/extract'])
def test_all_posts_require_same_origin(lens_server: LensServer, path: str) -> None:
    status, result = lens_server.call(path, {}, origin='https://attacker.example')
    assert status == HTTPStatus.FORBIDDEN and result['error'] == 'cross-origin request refused'


def test_preview_errors_are_sanitised(lens_server: LensServer, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)
    monkeypatch.setattr(answers, 'plan_calls', broken)
    status, body = lens_server.call('/api/preview', {'task': 'ask', 'question': 'gain'})
    assert status == 500 and SECRET not in json.dumps(body)


def test_matrix_validation_errors(lens_server: LensServer) -> None:
    server = lens_server
    assert server.call('/api/matrices', {'name': 'Bad', 'question': 'gain',
                                         'columns': [{**COLUMN, 'type': 'no'}]})[0] == 400
    path = '/api/matrices/' + server.matrix_id
    assert server.call(path + '/columns', COLUMN)[0] == 400  # duplicate key
    assert server.call(path + '/export?format=exe')[0] == 400
    assert server.call('/api/review', {'target_kind': 'sentence', 'target_id': 'missing', 'action': 'accept'})[0] == 404


def test_lens_assets(lens_server: LensServer) -> None:
    _, html = lens_server.call('/')
    _, js = lens_server.call('/static/app.js')
    assert b'data-view="ask"' in html and b'data-view="matrix"' in html
    assert b'id="lens-preview"' in html and b'Evidence Lens' in html
    lens_js = js.split(b'/* ---------- Evidence Lens ---------- */')[1].split(b'function init()')[0]
    assert b'innerHTML' not in lens_js and b'textContent' in lens_js
    assert b'/pdf#page=' in lens_js and b'body.approved = true' in lens_js


def test_empty_retrieval_needs_no_provider(lens_server: LensServer) -> None:
    server = lens_server
    before = server.fake.calls
    status, plan = server.call('/api/preview', {'task': 'ask', 'question': 'zzzzunfindable', 'project': 'demo',
                                                'profile': 'paid'})
    assert status == 200 and plan['number_of_calls'] == 0 and not plan['remote']
    job = server.job('/api/ask', {'question': 'zzzzunfindable', 'project': 'demo', 'profile': 'paid'})
    assert job['status'] == 'done' and job['report']['insufficient']
    assert job['report']['sentences'][0]['kind'] == 'unsupported'
    assert server.fake.calls == before


def test_invalid_model_response_is_safe(lens_server: LensServer) -> None:
    server = lens_server
    bad = FakeLlm(lambda m: {'sentences': SECRET, 'verdict': SECRET})
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': bad})
    for path, body in [('/api/ask', {'question': 'gain'}), ('/api/audit', {'run_id': server.run_id})]:
        job = server.job(path, body)
        assert job['status'] == 'failed' and SECRET not in json.dumps(job)


def test_review_errors_and_project_scope(lens_server: LensServer) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('other','other','now')")
    finally:
        conn.close()
    assert server.call('/api/review?project=other')[1] == []
    assert server.call('/api/matrices?project=other')[1] == []
    job = server.job('/api/matrices/' + server.matrix_id + '/extract', {})
    cell = job['report']['cells'][0]
    for body in [
        {'target_kind': 'cell', 'target_id': cell['id'], 'action': 'edit', 'new_value': 'not a number'},
        {'target_kind': 'cell', 'target_id': cell['id'], 'action': 'edit'},
    ]:
        assert server.call('/api/review', body)[0] == 400
    _, matrix = server.call('/api/matrices/' + server.matrix_id)
    assert matrix['cells'][0]['value'] == 3.25


def test_hybrid_remote_still_requires_ui_confirmation(lens_server: LensServer) -> None:
    server = lens_server
    server.app.config.models.policy = 'hybrid'
    status, result = server.call('/api/ask', {'question': 'gain', 'profile': 'paid'})
    assert status == 403 and result['preview']['remote'] and not server.app.jobs


def test_matrix_warnings_and_review_evidence(lens_server: LensServer) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        seed(conn, paper='q')
    finally:
        conn.close()

    def reply(messages: list[dict[str, str]]) -> dict[str, Any]:
        return {'status': 'reported', 'value': 3.25, 'unit': 's' if 'Title q' in messages[-1]['content'] else 'ms',
                'evidence_ids': [evidence_id(messages)], 'note': ''}

    fake = FakeLlm(reply)
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': fake})
    job = server.job('/api/matrices/' + server.matrix_id + '/extract', {})
    assert job['status'] == 'done'
    assert len(job['report']['warnings']) == 1 and 'units differ' in job['report']['warnings'][0]
    # Flag one cell through an uncited extraction, then inspect the project's queue.
    uncited = FakeLlm(lambda m: {'status': 'reported', 'value': 3.25, 'evidence_ids': [], 'note': ''})
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': uncited})
    job = server.job('/api/matrices/' + server.matrix_id + '/extract', {'force': True})
    _, queue = server.call('/api/review?project=demo')
    cells = [i for i in queue if i['target_kind'] == 'cell']
    assert len(cells) == 2 and cells[0]['matrix_id'] == server.matrix_id
    assert 'uncited_value' in cells[0]['flags']
    assert cells[0]['matrix_name'].startswith('Studies ')
    assert cells[0]['paper_title'] in (UNTRUSTED, 'Title q')
    assert cells[0]['column_label'] == 'Gain'


def test_model_profiles_are_local_first_and_public(lens_server: LensServer) -> None:
    config = lens_server.app.config
    config.models.profiles['paid'].base_url = 'https://private.example'
    config.models.profiles['paid'].api_key_env = SECRET
    config.models.profiles['offline'] = ModelProfileConfig(provider='ollama', model='other')
    status, profiles = lens_server.call('/api/model-profiles')
    assert status == 200
    assert [p['name'] for p in profiles] == ['local', 'offline', 'paid']
    assert profiles[0] == {'name': 'local', 'provider': 'ollama', 'model': config.models.local.model, 'remote': False}
    assert profiles[-1]['remote']
    assert all(set(p) == {'name', 'provider', 'model', 'remote'} for p in profiles)
    assert SECRET not in json.dumps(profiles) and 'private.example' not in json.dumps(profiles)


def test_phase2_claims_queue_and_screening(lens_server: LensServer) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        from litforge.screening.criteria import Criteria, save_criteria
        save_criteria(conn, 'proj', Criteria(question='gain'))
    finally:
        conn.close()
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        if 'Classify' in m[0]['content']:
            return {'relation': 'replicates', 'explanation': UNTRUSTED, 'possible_causes': []}
        if 'Screen' in m[0]['content']:
            return {'relevance': 4, 'inclusion': [], 'exclusion': [], 'reason': UNTRUSTED}
        return {'claims': [{'text': UNTRUSTED, 'type': 'finding', 'polarity': 'positive',
                            'evidence_ids': [evidence_id(m)]}]}
    fake = FakeLlm(reply)
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': fake, 'paid': fake})
    status, plan = server.call('/api/preview', {'task': 'claim_extract', 'project': 'demo', 'profile': 'paid'})
    assert status == 200 and plan['requires_approval']
    assert server.call('/api/claims/extract', {'project': 'demo', 'profile': 'paid'})[0] == 403
    job = server.job('/api/claims/extract', {'project': 'demo'})
    assert job['status'] == 'done', job
    assert server.call('/api/claims/cluster', {'project': 'demo'})[0] == 200
    status, data = server.call('/api/claims?project=demo')
    assert status == 200 and data['claims'][0]['text'] == UNTRUSTED
    assert data['evidence'][0]['text'].startswith('Results')
    cid = data['claims'][0]['id']
    assert server.call('/api/claims/review', {'id': cid, 'action': 'accept'})[0] == 200
    assert server.call('/api/claims/landscape?project=demo')[1][0]['single_source']
    assert server.job('/api/claims/relate', {'project': 'demo'})['status'] == 'done'
    job = server.job('/api/screen-fulltext', {'project': 'demo'})
    assert job['status'] == 'done', job
    status, queue = server.call('/api/queue?project=demo')
    assert status == 200 and queue[0]['stage'] == 'S2' and queue[0]['reason']
    assert server.call('/api/marks', {'project': 'demo', 'paper_ids': ['p'], 'mark': 'relevant'})[0] == 200
    assert server.call('/api/queue?project=demo')[1] == []
    _, paper = server.call('/api/papers/p?project=demo')
    assert paper['fulltext_screening']['decision'] == 'include'
    assert paper['screening_history'][-1]['source'] == 'human'


def test_asset_crop_and_analysis_endpoints_require_approval(lens_server: LensServer) -> None:
    server = lens_server
    crop = b'only this crop'
    parsed = server.app.paths.root / 'parsed'
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / 'crop.png').write_bytes(crop)
    conn = server.app.db()
    try:
        conn.execute(
            """INSERT INTO assets(id,parse_run_id,kind,label,caption,page,bbox_json,path,data_json,quality)
               VALUES ('asset-ui','rp','table','Table 1','Parameter estimates',1,'[]','parsed/crop.png',NULL,
               'verified')"""
        )
    finally:
        conn.close()

    class VisionFake:
        def __init__(self) -> None:
            self.images: list[list[bytes]] = []

        def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any],
                        images: list[bytes]) -> dict[str, Any]:
            self.images.append(images)
            return {'columns': ['Parameter', 'Value'], 'rows': [['gain', '3.25']],
                    'units': {}, 'footnotes': []}

        def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
            return {}

    vision = VisionFake()
    server.app.make_gateway = lambda c: ModelGateway(
        c, server.app.config, providers={'local': vision, 'paid': vision})
    status, paper = server.call('/api/papers/p?project=demo')
    assert status == 200 and paper['assets'][0]['id'] == 'asset-ui'
    assert server.call('/api/assets/asset-ui/crop') == (200, crop)
    before = len(vision.images)
    status, denied = server.call('/api/assets/asset-ui/analyze',
                                 {'mode': 'table', 'profile': 'paid'})
    assert status == 403 and denied['preview']['requires_approval']
    assert len(vision.images) == before and not server.app.jobs
    job = server.job('/api/assets/asset-ui/analyze',
                     {'mode': 'table', 'profile': 'paid', 'approved': True})
    assert job['status'] == 'done', job
    assert vision.images == [[crop]] and job['report']['evidence'][0]['kind'] == 'table_cell'
    _, paper = server.call('/api/papers/p?project=demo')
    assert paper['assets'][0]['analyses'][0]['result']['rows'] == [['gain', '3.25']]


def test_gaps_view_design_review_and_export_gate(lens_server: LensServer) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        evidence_id_row = conn.execute(
            "SELECT id FROM evidence_spans WHERE paper_id='p' ORDER BY id LIMIT 1").fetchone()
        # A real single-source claim, so the detector itself finds (and keeps finding) the gap.
        run_id = start_run(conn, 'claim_extract', 'proj', {}, {})
        conn.execute("INSERT INTO claim_clusters(id,project_id,label,created_at) VALUES ('cl','proj','lone','now')")
        conn.execute("""INSERT INTO claims(id,project_id,paper_id,text,canonical_text,canonical_hash,type,polarity,
                        status,run_id,model,prompt_version,created_at,cluster_id) VALUES ('c1','proj','p','lone',
                        'lone','c1','finding','positive','accepted',?,'fake','v1','now','cl')""", (run_id,))
        conn.execute("INSERT INTO claim_evidence(claim_id,evidence_id,relation,strength) "
                     "VALUES ('c1',?,'supports','direct')", (evidence_id_row['id'],))
        gap_id = next(g.id for g in gaps.detect_gaps(conn, 'proj') if g.kind == 'single_source_claim')
    finally:
        conn.close()

    class ProposalFake:
        def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
            cited = re.search(r'"id":"(ev_[^"]+)"', messages[-1]['content'])
            assert cited
            evidence = cited.group(1)
            return {
                'why_it_matters': [{'text': 'A study supports this candidate.', 'evidence_ids': [evidence],
                                    'label': 'evidence'}],
                'minimal_experiment': [{'text': 'Test the proposed alternative.', 'evidence_ids': [],
                                        'label': 'proposal'}],
                'data': [{'text': 'Collect outcome data.', 'evidence_ids': [], 'label': 'proposal'}],
                'controls': [{'text': 'Use a matched control.', 'evidence_ids': [], 'label': 'proposal'}],
                'outcome': [{'text': 'Compare the measured outcomes.', 'evidence_ids': [], 'label': 'proposal'}],
                'failure_modes': [{'text': 'Recruitment may be incomplete.', 'evidence_ids': [], 'label': 'proposal'}],
                'what_model_does_not_know': [{'text': 'The effect size is unknown.', 'evidence_ids': [],
                                              'label': 'unknown'}],
            }

    fake = ProposalFake()
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'local': fake})
    status, listing = server.call('/api/gaps?project=demo')
    assert status == 200 and listing[0]['id'] == gap_id and listing[0]['evidence']
    job = server.job('/api/gaps/design', {'gap_id': gap_id})
    assert job['status'] == 'done' and job['report']['title'] == 'Research proposal — not a literature finding'
    card_id = job['report']['id']
    assert server.call(f'/api/gaps/cards/{card_id}/export')[0] == 400
    status, reviewed = server.call('/api/gaps/review', {
        'card_id': card_id, 'action': 'accept', 'reason': 'reviewed in the research console',
    })
    assert status == 200 and reviewed['review_status'] == 'accepted'
    conn = server.app.db()
    try:
        review = conn.execute(
            "SELECT reason FROM human_reviews WHERE target_kind='gap_card' AND target_id=?",
            (card_id,),
        ).fetchone()
        assert review['reason'] == 'reviewed in the research console'
    finally:
        conn.close()
    assert server.call('/api/gaps?project=demo')[1][0]['review_status'] == 'accepted'
    status, exported = server.call(f'/api/gaps/cards/{card_id}/export')
    assert status == 200 and 'not a literature finding' in exported['markdown']
    _, html = server.call('/')
    _, js = server.call('/static/app.js')
    assert b'id="view-gaps"' in html and b'id="gaps-refresh"' in html
    gaps_js = js.split(b'function loadGaps()')[1].split(b'/* ---------- running searches ---------- */')[0]
    assert b'innerHTML' not in gaps_js and b'textContent' in gaps_js


@pytest.mark.parametrize('path', ['/api/claims/extract', '/api/claims/cluster', '/api/claims/relate',
                                 '/api/claims/review', '/api/screen-fulltext'])
def test_phase2_posts_require_same_origin(lens_server: LensServer, path: str) -> None:
    assert lens_server.call(path, {}, origin='https://attacker.example')[0] == 403


@pytest.mark.parametrize(('task', 'path'), [('claim_extract', '/api/claims/extract'),
                                          ('screen_fulltext', '/api/screen-fulltext')])
def test_phase2_remote_jobs_and_invalid_inputs(lens_server: LensServer, task: str, path: str) -> None:
    server = lens_server
    conn = server.app.db()
    try:
        from litforge.screening.criteria import Criteria, save_criteria
        save_criteria(conn, 'proj', Criteria(question='gain'))
    finally:
        conn.close()
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        if task == 'claim_extract':
            return {'claims': [{'text': UNTRUSTED, 'type': 'finding', 'polarity': 'positive',
                                'evidence_ids': [evidence_id(m)]}]}
        return {'relevance': 3, 'reason': UNTRUSTED}
    fake = FakeLlm(reply)
    server.app.make_gateway = lambda c: ModelGateway(c, server.app.config, providers={'paid': fake})
    body = {'project': 'demo', 'profile': 'paid'}
    status, plan = server.call('/api/preview', {**body, 'task': task})
    assert status == 200 and plan['number_of_calls'] == 1 and fake.calls == 0
    assert server.call(path, body)[0] == 403
    assert server.call(path, {**body, 'approved': 'true'})[0] == 400
    assert server.call(path, {**body, 'profile': 'missing'})[0] == 400
    assert server.job(path, {**body, 'approved': True})['status'] == 'done'
    assert fake.calls == 1
    assert server.call('/api/claims?project=unknown')[0] in (400, 404)
    assert server.call('/api/queue?project=demo&limit=0')[0] == 400
    assert server.call('/api/claims/review', {'id': 'missing', 'action': 'accept'})[0] == 404
    assert server.call('/api/claims/cluster', {'project': 'demo', 'threshold': 2})[0] == 400


def test_console_job_passes_the_separate_cloud_approval(lens_server: LensServer,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.console import planner as console_planner
    seen: list[bool] = []
    real = console_planner.execute_run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("approved_cloud", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(console_planner, "execute_run", spy)
    plan = {"request": "snap", "steps": [{"action": "snapshot", "args": {}}]}
    status, started = lens_server.call('/api/console/execute', {'plan': plan, 'project': 'demo', 'approved': True,
                                                                'approved_cloud': True})
    assert status == 200
    for _ in range(200):
        if lens_server.call('/api/console/runs/' + started['run_id'])[1]['status'] not in ('pending', 'running'):
            break
        import time
        time.sleep(.01)
    assert seen == [True]
