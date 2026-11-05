"""
Phase 4 checks: step 4 runs one session at a time, deletes only the right LFP files, pauses when the disk is
full, puts a crashed session back, stops after the current session when asked, and works end to end on demo data.

    .venv-test\\Scripts\\python -m pytest tests/test_app_queue.py -v      (about 1 minute)
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p4_app import session_queue, storage                   # noqa: E402
from p4_app.session_queue import SessionQueue               # noqa: E402

SID = 1064644573


def make_cache(tmp_path, sid=SID):
    d = tmp_path / 'cache' / 'visual-behavior-neuropixels-0.5.0' / 'behavior_ecephys_sessions' / str(sid)
    d.mkdir(parents=True)
    for name in ('ecephys_session_%d.nwb' % sid, 'probe_probeA_lfp.nwb', 'probe_probeC_lfp.nwb', 'notes.txt'):
        (d / name).write_bytes(b'x' * 1000)
    other = d.parent / (str(sid) + '1')                          # a different session whose id starts the same
    other.mkdir()
    (other / 'probe_probeA_lfp.nwb').write_bytes(b'x')
    return str(tmp_path / 'cache'), d


def write_result(tmp_path, sid=SID, error=None):
    s = tmp_path / 'out' / 'sessions'
    s.mkdir(parents=True, exist_ok=True)
    (s / f'{sid}.json').write_text(json.dumps({'session': sid, **({'error': error} if error else {})}))
    return str(tmp_path / 'out')


# ---------------------------------------------------------------------------------------------- storage
def test_lfp_files_are_found_only_in_that_sessions_folder(tmp_path):
    cache, d = make_cache(tmp_path)
    names = sorted(os.path.basename(f) for f in storage.lfp_files(cache, SID))
    assert names == ['probe_probeA_lfp.nwb', 'probe_probeC_lfp.nwb']


def test_nothing_is_deleted_without_a_good_result(tmp_path):
    cache, d = make_cache(tmp_path)
    out = str(tmp_path / 'out')
    assert storage.delete_lfp(cache, out, SID) == (0, 0.0)                 # no result yet
    write_result(tmp_path, error='no driven visual-cortex channels')
    assert storage.delete_lfp(cache, out, SID)[0] == 0                     # result with an error
    assert len(storage.lfp_files(cache, SID)) == 2


def test_delete_keeps_the_session_file_and_other_sessions(tmp_path):
    cache, d = make_cache(tmp_path)
    out = write_result(tmp_path)
    n, gb = storage.delete_lfp(cache, out, SID)
    assert n == 2 and gb > 0
    assert sorted(os.listdir(d)) == ['ecephys_session_%d.nwb' % SID, 'notes.txt']
    assert os.path.exists(d.parent / (str(SID) + '1') / 'probe_probeA_lfp.nwb')


# ---------------------------------------------------------------------------------------------- queue logic
def test_sessions_with_results_are_skipped(tmp_path):
    out = write_result(tmp_path, sid=1)
    q = SessionQueue([1, 2, 3], out, str(tmp_path / 'cache'), check_space=False)
    assert q.pending == [2, 3] and q.total == 2


def test_success_cleans_up_and_moves_on(tmp_path):
    cache, d = make_cache(tmp_path)
    out = str(tmp_path / 'out')
    q = SessionQueue([SID, 2], out, cache, check_space=False)
    assert q.next() == SID
    write_result(tmp_path)
    msg = q.finished(0)
    assert 'deleted 2 LFP files' in msg and q.done_count == 1 and q.eta_seconds() is not None
    assert q.next() == 2


def test_cleanup_off_keeps_files(tmp_path):
    cache, d = make_cache(tmp_path)
    q = SessionQueue([SID], str(tmp_path / 'out'), cache, cleanup=False, check_space=False)
    q.next()
    write_result(tmp_path)
    q.finished(0)
    assert len(storage.lfp_files(cache, SID)) == 2


def test_a_crash_puts_the_session_back_and_pauses(tmp_path):
    q = SessionQueue([1, 2], str(tmp_path / 'out'), str(tmp_path), check_space=False)
    q.next()
    q.finished(1)
    assert q.pending == [1, 2] and q.paused_reason and q.next() is None


def test_stop_after_current(tmp_path):
    q = SessionQueue([1, 2, 3], str(tmp_path / 'out'), str(tmp_path), check_space=False)
    q.next()
    q.stop_after_current()
    q.finished(0)
    assert q.next() is None and q.pending == [2, 3]


def test_pauses_when_the_disk_is_too_full(tmp_path, monkeypatch):
    monkeypatch.setattr(session_queue.cfg, 'free_gb', lambda path: 3.0)
    q = SessionQueue([1], str(tmp_path / 'out'), str(tmp_path))
    assert q.next() is None and 'GB free' in q.paused_reason and q.pending == [1]


# ---------------------------------------------------------------------------------------------- window, end to end
@pytest.fixture
def small_demo(monkeypatch):
    monkeypatch.setenv('FAKE_VBN_MICE', '4')          # 2 pilot + 2 test mice = 4 test sessions
    monkeypatch.setenv('FAKE_VBN_FLASHES', '300')


@pytest.fixture
def app(tmp_path, small_demo):
    from test_app_settings import make_root
    root = make_root()
    root.withdraw()
    from p4_app.gui import App
    a = App(root, demo=True, base=str(tmp_path), python=sys.executable)
    yield a
    if a.runner.is_running:
        a.runner.stop()
        a.runner.wait(5)
    root.destroy()


def pump(app, until, timeout=180):
    t0 = time.time()
    while not until() and time.time() - t0 < timeout:
        app.root.update()
        time.sleep(0.02)
    app.root.update()
    assert until(), 'timed out'


def idle(app):
    return not app.busy and app.runner.messages.empty()


def prepare(app):
    pump(app, lambda: app.env is not None)
    for step in ('list', 'band'):
        app.run_step(step)
        pump(app, lambda: idle(app))
    assert app.states['band'] == 'done'


def test_step4_runs_every_session_and_deletes_their_lfp_files(app, tmp_path):
    prepare(app)
    app.run_step('analyze')
    assert app.queue is not None and app.queue.total == 4
    pump(app, lambda: idle(app))
    a = app.out_dir
    done = [f for f in os.listdir(os.path.join(a, 'sessions')) if f.endswith('.json')]
    assert len(done) == 4 and app.states['analyze'] == 'done'
    for f in done:
        assert storage.lfp_files(app.cache_dir, int(f[:-5])) == []      # cleaned up
    log = app.log.get('1.0', 'end')
    assert 'deleted 1 LFP file' in log and 'Finished: 4 session(s) analysed' in log
    assert len([f for f in os.listdir(os.path.join(a, 'logs')) if f.startswith('analyze_')]) == 4


def test_stop_after_this_session(app):
    prepare(app)
    app.run_step('analyze')
    app.stop_after_session()
    pump(app, lambda: idle(app))
    a = app.out_dir
    assert len(os.listdir(os.path.join(a, 'sessions'))) == 1 and app.states['analyze'] == 'partial'
    assert 'Stopped after 1 session(s), as asked. 3 still to do.' in app.log.get('1.0', 'end')
    app.run_step('analyze')                                   # resuming picks up the other three
    assert app.queue.total == 3
    pump(app, lambda: idle(app))
    assert app.states['analyze'] == 'done'


def test_stop_now_keeps_finished_sessions(app):
    prepare(app)
    app.run_step('analyze')
    pump(app, lambda: app.queue is not None and app.queue.done_count >= 1)
    app.stop()
    pump(app, lambda: idle(app))
    assert app.states['analyze'] == 'partial'
    assert 'Stopped. Finished sessions are kept' in app.log.get('1.0', 'end')
