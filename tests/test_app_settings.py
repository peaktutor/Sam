"""
Phase 3 checks: settings are saved and restored, the Python-environment check tells real/demo/unusable apart,
disk space is reported, and the Settings page drives what the window runs.

    .venv-test\\Scripts\\python -m pytest tests/test_app_settings.py -v      (about 20 s)
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from p4_app import settings as cfg   # noqa: E402
from p4_app.runner import FAKE_SDK   # noqa: E402

# some checks assume the Python running the tests has no AllenSDK (true for .venv-test, not for .venv-vbn)
HAS_ALLENSDK = cfg.check_python(sys.executable)['real_ok']
no_sdk_only = pytest.mark.skipif(HAS_ALLENSDK, reason='this Python has AllenSDK; the check needs one without it')


def test_defaults_when_no_file(tmp_path):
    s = cfg.load(str(tmp_path))
    assert s['max_probes'] == 1 and s['demo'] is True and s['python']


def test_save_and_load_round_trip(tmp_path):
    s = cfg.load(str(tmp_path))
    s.update(out='D:/p4_out', cache='D:/vbn_cache', max_probes=2, demo=False, scenario='null', python='C:/py.exe')
    cfg.save(s, str(tmp_path))
    assert cfg.load(str(tmp_path)) == s
    assert not os.path.exists(tmp_path / (cfg.FILENAME + '.tmp'))


def test_damaged_or_foreign_settings_fall_back_to_defaults(tmp_path):
    (tmp_path / cfg.FILENAME).write_text('{not json', encoding='utf-8')
    assert cfg.load(str(tmp_path))['max_probes'] == 1
    (tmp_path / cfg.FILENAME).write_text(json.dumps({'max_probes': 2, 'evil': 1}), encoding='utf-8')
    s = cfg.load(str(tmp_path))
    assert s['max_probes'] == 2 and 'evil' not in s


@no_sdk_only
def test_check_python_without_allensdk_is_demo_only():
    r = cfg.check_python(sys.executable)
    assert r['demo_ok'] and not r['real_ok'] and r['modules']['allensdk'] is None
    assert 'demo data only' in r['message']


@no_sdk_only
def test_check_python_ignores_the_fake_allensdk(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', FAKE_SDK)          # even if the fake is on the path, it must not count
    r = cfg.check_python(sys.executable)
    assert not r['real_ok']


def test_check_python_with_allensdk_is_ready_for_real_data():
    vbn = cfg.detect_python()
    if not vbn:
        pytest.skip('no .venv-vbn or conda vbn environment on this machine')
    r = cfg.check_python(vbn)
    assert r['real_ok'] and r['modules']['allensdk'].startswith('2.16') and 'Ready for real data' in r['message']


def test_nwb_reader_problem_catches_a_too_new_hdmf():
    """The combination that failed 10 minutes into the first real step 2 (2026-10-31)."""
    assert 'too new' in cfg.nwb_reader_problem({'hdmf': '6.2.0', 'pynwb': '2.8.3'})
    assert cfg.nwb_reader_problem({'hdmf': '3.14.6', 'pynwb': '2.8.3'}) is None
    assert cfg.nwb_reader_problem({'hdmf': None, 'pynwb': None}) is None


def test_check_python_missing_interpreter():
    r = cfg.check_python('C:/no/such/python.exe')
    assert not r['exists'] and not r['demo_ok'] and 'No Python' in r['message']


def test_free_space_for_a_folder_that_does_not_exist_yet(tmp_path):
    gb = cfg.free_gb(str(tmp_path / 'not' / 'yet'))
    assert gb is not None and gb > 0


def test_space_needed_follows_the_guide():
    assert cfg.space_needed_gb('band') == 50 and cfg.space_needed_gb('group') == 0
    assert cfg.space_needed_gb('analyze', 2) > cfg.space_needed_gb('analyze', 1)


# ---------------------------------------------------------------------------------------------- window
def make_root():
    """A Tk root, retried once (creating a window can fail transiently on Windows); skip with the real reason."""
    tk = pytest.importorskip('tkinter')
    for attempt in range(2):
        try:
            root = tk.Tk()
            root.withdraw()              # tests never show a window on the user's screen
            return root
        except tk.TclError as e:
            err = e
            time.sleep(0.5)
    pytest.skip(f'cannot open a window: {err}')


@pytest.fixture
def app(tmp_path):
    root = make_root()
    root.withdraw()
    from p4_app.gui import App
    a = App(root, base=str(tmp_path), python=sys.executable)
    yield a
    root.destroy()


def pump(app, until, timeout=60):
    t0 = time.time()
    while not until() and time.time() - t0 < timeout:
        app.root.update()
        time.sleep(0.02)
    app.root.update()
    assert until(), 'timed out'


@no_sdk_only
def test_environment_check_finishes_and_shows_demo_only(app):
    pump(app, lambda: app.env is not None)
    assert app.env['demo_ok'] and not app.env['real_ok']
    assert 'demo data only' in app.env_side.cget('text')


@no_sdk_only
def test_real_mode_blocks_run_without_allensdk_and_demo_does_not(app):
    pump(app, lambda: app.env is not None)
    app.demo.set(False)
    app._mode_changed()
    app.select('list')
    assert str(app.run_btn['state']) == 'disabled'
    app.demo.set(True)
    app._mode_changed()
    assert str(app.run_btn['state']) == 'normal'


def test_settings_page_saves_folders_and_probes(app, tmp_path):
    pump(app, lambda: app.env is not None)
    app.select('settings')
    out = str(tmp_path / 'my_results')
    app.out_var.set(out)
    app._settings_changed(app.out_var)
    app.max_probes.set(2)
    saved = json.loads((tmp_path / cfg.FILENAME).read_text(encoding='utf-8'))
    assert saved['out'] == out and saved['max_probes'] == 2
    app.demo.set(False)
    assert app.out_dir == os.path.normpath(out)
    assert 'GB free' in app._free_lbl.cget('text')


def test_changing_the_interpreter_rechecks_it(app):
    pump(app, lambda: app.env is not None)
    app.select('settings')
    app._set_python('C:/no/such/python.exe')
    pump(app, lambda: app.env is not None)
    assert not app.env['exists'] and app.env_ready() is False
    assert 'not usable' in app.env_side.cget('text')
