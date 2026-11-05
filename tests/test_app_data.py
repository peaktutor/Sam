"""
Data page checks: the downloader, the sample set, choosing sessions, keeping sample runs out of the results,
the preregistration gate, and deleting downloaded files.

    .venv-test\\Scripts\\python -m pytest tests/test_app_data.py -v      (about 1 minute)
"""
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p4_app import prereg, state, storage                    # noqa: E402
from p4_app.runner import DOWNLOADER, FAKE_SDK                # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0     # no console pop-ups
FEW = {'FAKE_VBN_MICE': '5', 'FAKE_VBN_FLASHES': '300'}      # 2 pilot + 3 test mice


@pytest.fixture
def few(monkeypatch):
    for k, v in FEW.items():
        monkeypatch.setenv(k, v)


def test_downloader_fetches_session_and_lfp_files_without_analysing(tmp_path, few):
    env = dict(os.environ, PYTHONPATH=FAKE_SDK, PYTHONWARNINGS='ignore')
    cache = str(tmp_path / 'cache')
    p = subprocess.run([sys.executable, DOWNLOADER, '--cache', cache, '--session', '1050000001', '--session',
                        '1050010002', '--max-probes', '2'], env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    assert p.returncode == 0, p.stdout + p.stderr
    inv = storage.inventory(cache)
    assert set(inv) == {1050000001, 1050010002}
    assert all(r['session_file'] and len(r['lfp']) == 2 for r in inv.values())


def test_downloader_reports_a_bad_session_and_keeps_going(tmp_path, few):
    env = dict(os.environ, PYTHONPATH=FAKE_SDK, PYTHONWARNINGS='ignore')
    cache = str(tmp_path / 'cache')
    p = subprocess.run([sys.executable, DOWNLOADER, '--cache', cache, '--session', '42', '--session', '1050000001'],
                       env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    assert p.returncode == 1 and 'could not download session 42' in p.stdout
    assert 1050000001 in storage.inventory(cache)


def test_downloader_replaces_a_file_left_by_an_interrupted_download(tmp_path, few):
    """AllenSDK writes to the final filename and treats any existing file as complete, so a stopped download
    leaves a truncated file that looks finished. The manifest's hash has to catch it."""
    env = dict(os.environ, PYTHONPATH=FAKE_SDK, PYTHONWARNINGS='ignore')
    cache = str(tmp_path / 'cache')
    args = [sys.executable, DOWNLOADER, '--cache', cache, '--session', '1050000001', '--max-probes', '1']
    run = lambda: subprocess.run(args, env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    assert run().returncode == 0
    rec = storage.inventory(cache)[1050000001]
    whole = open(rec['lfp'][0], 'rb').read()
    with open(rec['lfp'][0], 'wb') as fh:
        fh.write(whole[:5])                                       # what an interrupted download leaves behind
    p = run()
    assert p.returncode == 0 and 'incomplete' in p.stdout
    assert open(rec['lfp'][0], 'rb').read() == whole
    p = run()
    assert 'incomplete' not in p.stdout and 'already here and complete' in p.stdout


def test_downloader_retries_a_dropped_connection(tmp_path, few):
    """Found on real data: a PC that slept dropped the connection, and the file was simply given up on."""
    env = dict(os.environ, PYTHONPATH=FAKE_SDK, PYTHONWARNINGS='ignore', P4_RETRY_WAIT_S='0',
               FAKE_VBN_DOWNLOAD_FAILURES='2')
    cache = str(tmp_path / 'cache')
    p = subprocess.run([sys.executable, DOWNLOADER, '--cache', cache, '--session', '1050000001'], env=env,
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    assert p.returncode == 0 and p.stdout.count('retrying') == 2, p.stdout + p.stderr
    rec = storage.inventory(cache)[1050000001]
    assert b'partial' not in open(rec['session_file'], 'rb').read()


def test_downloader_gives_up_after_its_retries_and_leaves_no_partial_file(tmp_path, few):
    env = dict(os.environ, PYTHONPATH=FAKE_SDK, PYTHONWARNINGS='ignore', P4_RETRY_WAIT_S='0',
               FAKE_VBN_DOWNLOAD_FAILURES='50')
    cache = str(tmp_path / 'cache')
    p = subprocess.run([sys.executable, DOWNLOADER, '--cache', cache, '--session', '1050000001'], env=env,
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    assert p.returncode == 1 and 'could not download session 1050000001' in p.stdout
    assert not storage.inventory(cache)[1050000001]['session_file']


def test_inventory_and_delete_touch_only_that_session(tmp_path):
    root = tmp_path / 'cache' / 'visual-behavior-neuropixels-0.5.0' / 'behavior_ecephys_sessions'
    for sid in ('111', '1111'):
        (root / sid).mkdir(parents=True)
        (root / sid / f'ecephys_session_{sid}.nwb').write_bytes(b'x' * 1000)
        (root / sid / 'probe_probeA_lfp.nwb').write_bytes(b'x' * 1000)
        (root / sid / 'notes.txt').write_text('keep')
    cache = str(tmp_path / 'cache')
    inv = storage.inventory(cache)
    assert inv[111]['session_file'] and len(inv[111]['lfp']) == 1
    n, _ = storage.delete_session_files(cache, 111)
    assert n == 2 and sorted(os.listdir(root / '111')) == ['notes.txt']
    assert len(os.listdir(root / '1111')) == 3


def test_old_lfp_files_are_flagged_as_the_guide_warns(tmp_path):
    for release, name in (('0.4.0', 'probe_probeA_lfp.nwb'), ('0.5.0', 'probe_probeA_lfp.nwb'), ('0.3.0', 'notes.nwb')):
        d = tmp_path / f'visual-behavior-neuropixels-{release}' / 'behavior_ecephys_sessions' / '111'
        d.mkdir(parents=True)
        (d / name).write_bytes(b'x')
    assert storage.old_lfp_releases(str(tmp_path)) == ['0.4.0']


# ---------------------------------------------------------------------------------------------- window
@pytest.fixture
def app(tmp_path, few, monkeypatch):
    from test_app_settings import make_root
    root = make_root()
    from p4_app import gui
    monkeypatch.setattr(gui.messagebox, 'askyesno', lambda *a, **k: True)
    a = gui.App(root, demo=True, base=str(tmp_path), python=sys.executable)
    monkeypatch.setattr(a, '_open', lambda path: None)
    root.update()
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
    return not app.busy and app.runner.messages.empty() and app._after_ok is None


def prepare(app, steps=('list', 'band')):
    pump(app, lambda: app.env is not None)
    for step in steps:
        app.run_step(step)
        pump(app, lambda: idle(app))


def test_download_sample_gets_the_session_list_first(app):
    pump(app, lambda: app.env is not None)
    app.select('data')
    assert not state.data_rows(app.out_dir, app.cache_dir)
    app.download_sample()                                          # no session list yet: runs step 1, then downloads
    pump(app, lambda: idle(app))
    rows = state.data_rows(app.out_dir, app.cache_dir)
    sample = [r for r in rows if r['group'] == 'Sample']
    assert len(sample) == 4 and all(r['has_session'] and r['n_lfp'] == 2 for r in sample)
    assert not any(r['has_session'] for r in rows if r['group'] != 'Sample')
    assert app.selected == 'data' and str(app.sample_btn['state']) == 'disabled'    # nothing left to download
    assert 'All 4 session(s) downloaded' in app.log.get('1.0', 'end')


def test_clicking_a_row_ticks_it(app):
    prepare(app, ('list',))
    app.select('data')
    app.root.update()
    tv = app.data_table
    iid = tv.get_children()[1]
    # the window stays hidden in tests, so where the click landed is supplied rather than measured on screen
    tv.identify_row = lambda y: iid
    tv.identify_region = lambda x, y: 'cell'

    class Click:
        x, y = 5, 5
    app._on_data_click(Click)
    sid = app._data_iids[iid]
    assert sid in app.selected_sessions and tv.set(iid, '✓') == '☑'
    assert '1 selected' in app.data_summary.cget('text')
    assert str(app.dl_btn['state']) == 'normal'
    app._on_data_click(Click)
    assert sid not in app.selected_sessions and tv.set(iid, '✓') == '☐'
    assert str(app.dl_btn['state']) == 'disabled'


def test_filter_shows_one_group_and_keeps_ticks(app):
    prepare(app, ('list',))
    app.select('data')
    app.data_filter.set('Sample')
    app._apply_data_filter()
    assert len(app.data_table.get_children()) == 4
    app._select_shown()
    app.data_filter.set('All')
    app._apply_data_filter()
    assert len(app.data_table.get_children()) == len(app._data_rows) and len(app.selected_sessions) == 4


def test_sample_runs_stay_out_of_the_results(app):
    prepare(app)
    app.select('data')
    sample = [s for s, r in app._data_rows.items() if r['group'] == 'Sample'][:2]
    app.selected_sessions |= set(sample)
    app.run_selected()
    assert app.queue.total == 2
    pump(app, lambda: idle(app))
    where = os.path.join(state.sample_dir(app.out_dir), 'sessions')
    assert sorted(os.listdir(where)) == sorted(f'{s}.json' for s in sample)
    assert not os.path.exists(os.path.join(app.out_dir, 'sessions'))       # the group step never sees them
    assert app.states['analyze'] == 'ready'
    rows = {r['session']: r for r in state.data_rows(app.out_dir, app.cache_dir)}
    assert all(rows[s]['result'] and rows[s]['n_lfp'] == 0 for s in sample)   # clean-up ran, in the sample folder
    assert app.data_detail in sample


def test_selected_test_sessions_count_in_the_results(app):
    prepare(app)
    app.select('data')
    test = [s for s, r in app._data_rows.items() if r['group'] == 'Test'][:6]
    app.selected_sessions |= set(test)
    app.run_selected()
    pump(app, lambda: idle(app))
    assert sorted(os.listdir(os.path.join(app.out_dir, 'sessions'))) == sorted(f'{s}.json' for s in test)
    app.run_step('group')
    pump(app, lambda: idle(app))
    assert state.group_summary(app.out_dir)['timing_index']['n'] == 3


def test_real_data_keeps_test_sessions_locked_until_preregistered(app):
    prepare(app)
    out, cache = app.out_dir, app.cache_dir
    app.demo.set(False)
    app.out_var.set(out)
    app.cache_var.set(cache)
    app._mode_changed()
    app.env = dict(real_ok=True, demo_ok=True, modules={'allensdk': '2.16.2'}, python='3.10', message='', path='')
    app.select('data')
    test = next(s for s, r in app._data_rows.items() if r['group'] == 'Test')
    sample = next(s for s, r in app._data_rows.items() if r['group'] == 'Sample')
    app.selected_sessions.add(test)
    app._refresh_ticks()
    assert 'stay locked' in app._run_block_reason() and str(app.run_sel_btn['state']) == 'disabled'
    assert str(app.dl_btn['state']) == 'normal'                            # downloading is not analysing
    app.selected_sessions = {sample}
    app._refresh_ticks()
    assert app._run_block_reason() is None and str(app.run_sel_btn['state']) == 'normal'
    prereg.lock(out, '')
    app.refresh()
    app.selected_sessions = {test}
    app._refresh_ticks()
    assert app._run_block_reason() is None


def test_running_needs_the_band(app):
    prepare(app, ('list',))
    app.select('data')
    app.selected_sessions.add(next(iter(app._data_rows)))
    app._refresh_ticks()
    assert 'step 2' in app._run_block_reason()


def test_delete_selected_keeps_results(app):
    prepare(app)
    app.select('data')
    sid = next(s for s, r in app._data_rows.items() if r['group'] == 'Sample')
    app.selected_sessions = {sid}
    app.cleanup.set(False)
    app.run_selected()
    pump(app, lambda: idle(app))
    assert storage.inventory(app.cache_dir)[sid]['gb'] > 0
    app.delete_selected()
    rows ={r['session']: r for r in state.data_rows(app.out_dir, app.cache_dir)}
    assert not rows[sid]['has_session'] and rows[sid]['n_lfp'] == 0 and rows[sid]['result']


def test_step4_repairs_a_truncated_file_before_analysing(app):
    prepare(app)
    sid = next(s for s, g in state.session_groups(app.out_dir).items() if g == 'Test')
    app.select('data')
    app.selected_sessions = {sid}
    app.download_selected()
    pump(app, lambda: idle(app))
    lfp = storage.inventory(app.cache_dir)[sid]['lfp'][0]
    with open(lfp, 'wb') as fh:
        fh.write(b'x')                                            # as if the download had been interrupted
    app.selected_sessions = {sid}
    app.run_selected()
    pump(app, lambda: idle(app))
    log = app.log.get('1.0', 'end')
    assert 'incomplete' in log and f'Check or download session {sid}' in log
    assert storage.result_ok(app.out_dir, sid)


def test_pc_is_kept_awake_only_while_something_runs(app, monkeypatch):
    from p4_app import gui
    calls = []
    monkeypatch.setattr(gui, 'keep_awake', calls.append)
    pump(app, lambda: app.env is not None)
    app.root.update()
    assert not app.awake
    app.run_step('list')
    pump(app, lambda: app.awake, timeout=10)
    pump(app, lambda: idle(app) and not app.awake, timeout=120)
    assert calls == [True, False]


def test_stopping_a_download(app, monkeypatch):
    prepare(app, ('list',))
    monkeypatch.setenv('FAKE_VBN_DOWNLOAD_SECS', '3')
    app.select('data')
    app.selected_sessions = {next(iter(app._data_rows))}
    app.download_selected()
    assert app.busy and app.current == 'data' and str(app.stop_btn['state']) == 'normal'
    pump(app, lambda: 'Session 1 of 1' in app.log.get('1.0', 'end'), timeout=30)
    app.stop()
    pump(app, lambda: idle(app), timeout=30)
    assert 'FAILED' in app.log.get('1.0', 'end') or 'stopped' in app.status.cget('text')
