import sqlite3
import threading

import pytest

from web_service import APIError, Store, Worker, create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / 'sessions.sqlite3')


def enqueue(store, sid, prompt='test', key='one'):
    return store.submit(sid, prompt, key)


def test_sessions_survive_restart_and_delete(app):
    client = app.test_client()
    sid = client.post('/api/sessions', json={}).json['id']
    response = client.post(f'/api/sessions/{sid}/tasks', json={'prompt': 'hello', 'request_id': 'abc'})
    assert response.status_code == 202
    reopened = create_app(app.extensions['store'].path).test_client()
    assert reopened.get(f'/api/sessions/{sid}').json['tasks'][0]['prompt'] == 'hello'
    assert reopened.delete(f'/api/sessions/{sid}', json={}).status_code == 204
    assert client.get(f'/api/sessions/{sid}').status_code == 404
    with app.extensions['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_idempotent_submission(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    a = enqueue(store, sid)
    assert enqueue(store, sid)['id'] == a['id']
    with pytest.raises(APIError):
        enqueue(store, sid, 'other')
    assert len(store.session_detail(sid)['tasks']) == 1


def test_one_worker_and_history(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    enqueue(store, sid, 'first')
    enqueue(store, sid, 'followup', 'two')
    seen = []
    def runner(task, history, on_progress, on_message):
        seen.append((task, history))
        on_progress('working')
        on_message({'attempt': 1, 'sender': 'coordinator', 'recipient': 'executor',
                    'kind': 'request', 'content': task, 'created': 10.0,
                    'response_seconds': None})
        on_message({'attempt': 1, 'sender': 'executor', 'recipient': 'coordinator',
                    'kind': 'response', 'content': '{"status":"success"}', 'created': 11.5,
                    'response_seconds': 1.5})
        assert store.claim() is None
        with pytest.raises(APIError) as error:
            store.delete_session(sid)
        assert error.value.status == 409
        return {'type': 'final', 'status': 'success', 'summary': 'done: ' + task}
    worker = Worker(store, runner)
    assert worker.once() and worker.once() and not worker.once()
    assert seen[0][1] == []
    assert seen[1][1][0]['content'] == 'first'
    assert 'done: first' in seen[1][1][1]['content']
    tasks = store.session_detail(sid)['tasks']
    assert [t['status'] for t in tasks] == ['success', 'success']
    assert len(tasks[0]['agent_messages']) == 2
    assert tasks[0]['agent_messages'][1]['response_seconds'] == 1.5


def test_agent_messages_require_running_task(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    task = enqueue(store, sid)
    message = {'attempt': 1, 'sender': 'coordinator', 'recipient': 'developer',
               'kind': 'request', 'content': 'inspect', 'created': 10.0,
               'response_seconds': None}
    with pytest.raises(ValueError):
        store.append_agent_message(task['id'], message)
    store.claim()
    store.append_agent_message(task['id'], message)
    store.append_agent_message(task['id'], {
        'attempt': 1, 'step': 1, 'sender': 'developer', 'recipient': 'llm',
        'kind': 'request', 'content': '[{"role":"user","content":"inspect"}]',
        'created': 10.5, 'response_seconds': None,
    })
    messages = store.session_detail(sid)['tasks'][0]['agent_messages']
    assert messages[0]['content'] == 'inspect'
    assert messages[1]['recipient'] == 'llm' and messages[1]['step'] == 1


def test_agent_message_schema_migrates_existing_database(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as database:
        database.execute('''
            CREATE TABLE agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created REAL NOT NULL,
                response_seconds REAL)
        ''')
    Store(path)
    with sqlite3.connect(path) as database:
        columns = {row[1] for row in database.execute('PRAGMA table_info(agent_messages)')}
    assert 'step' in columns


def test_recovery_does_not_reexecute_running(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    a = enqueue(store, sid)
    b = enqueue(store, sid, 'second', 'two')
    assert store.claim()['id'] == a['id']
    reopened = Store(store.path)
    reopened.recover_interrupted()
    assert [t['status'] for t in reopened.session_detail(sid)['tasks']] == ['interrupted', 'queued']
    assert reopened.claim()['id'] == b['id']


def test_worker_continues_after_exception(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    enqueue(store, sid)
    def bad(*args, **kwargs):
        raise RuntimeError('private traceback')
    assert Worker(store, bad).once()
    task = store.session_detail(sid)['tasks'][0]
    assert task['status'] == 'failed'
    assert 'private traceback' not in str(task['result'])


def test_background_independent_of_client(app):
    client = app.test_client()
    sid = client.post('/api/sessions', json={}).json['id']
    client.post(f'/api/sessions/{sid}/tasks', json={'prompt':'run', 'request_id':'x'})
    del client
    started, release = threading.Event(), threading.Event()
    def runner(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return {'status':'success','summary':'background done'}
    worker = Worker(app.extensions['store'], runner)
    thread = threading.Thread(target=worker.once)
    thread.start()
    assert started.wait(3)
    assert app.test_client().get(f'/api/sessions/{sid}').json['tasks'][0]['status'] == 'running'
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert app.test_client().get(f'/api/sessions/{sid}').json['tasks'][0]['result']['summary'] == 'background done'


def test_history_pagination(app):
    store = app.extensions['store']
    sid = store.create_session()['id']
    for i in range(56):
        enqueue(store, sid, str(i), str(i))
    newest = store.session_detail(sid)
    assert newest['has_more'] and len(newest['tasks']) == 50
    older = store.session_detail(sid, newest['tasks'][0]['id'])
    assert not older['has_more'] and len(older['tasks']) == 6


def test_validation_and_same_origin(app):
    c = app.test_client()
    assert c.post('/api/sessions', json={}, headers={'Origin':'https://elsewhere.test'}).status_code == 403
    assert c.post('/api/sessions', data='{}').status_code == 415
    sid = c.post('/api/sessions', json={}).json['id']
    for prompt in ['', ' ', 'x'*1801, None, 1, []]:
        assert c.post(f'/api/sessions/{sid}/tasks', json={'prompt':prompt,'request_id':'x'}).status_code == 400
    assert c.post(f'/api/sessions/{sid}/tasks', json=[]).status_code == 400
    assert c.post(f'/api/sessions/{sid}/tasks', data='{', content_type='application/json').status_code == 400
    assert c.post('/api/sessions', data='x'*20000, content_type='application/json').status_code == 413


def test_static_ui(app):
    c = app.test_client()
    assert c.get('/').status_code == 200
    assert c.get('/static/app.js').status_code == 200
    assert c.get('/api/health').json == {'status':'ok'}
    assert "frame-ancestors 'none'" in c.get('/').headers['Content-Security-Policy']
