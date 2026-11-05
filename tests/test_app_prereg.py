"""
Phase 5 checks: the preregistration bundle, the lock and its checksum guard, the gate on steps 4-5 for real
data, and the charts.

    .venv-test\\Scripts\\python -m pytest tests/test_app_prereg.py -v      (about 40 s)
"""
import hashlib
import json
import os
import shutil
import sys
import time
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_pipeline import run_pipeline                    # noqa: E402
from p4_app import plots, prereg, state                   # noqa: E402
from p4_app.runner import SCRIPT, sha256_file             # noqa: E402


def load_json(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


@pytest.fixture(scope='module')
def demo_out(tmp_path_factory):
    """A complete small demo run (all four steps); each test gets its own copy of the output folder."""
    wd = str(tmp_path_factory.mktemp('demo'))
    run_pipeline(wd, 'reset', mice=5, flashes=300, echo=False)     # 2 pilot + 3 test mice (t tests need > 2)
    return os.path.join(wd, 'p4_out')


@pytest.fixture
def out(demo_out, tmp_path):
    dst = str(tmp_path / 'p4_out')
    shutil.copytree(demo_out, dst)
    return dst


# ---------------------------------------------------------------------------------------------- bundle and lock
def test_bundle_contains_everything_with_matching_checksums(out):
    path = prereg.export_bundle(out)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        assert {'vbn_p4.py', 'band.json', 'split.json', 'day_order.csv', 'sessions.csv', 'prereg.md',
                'SHA256SUMS.txt'} <= names
        sums = dict(line.split('  ')[::-1] for line in z.read('SHA256SUMS.txt').decode().splitlines())
        for name, h in sums.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == h
        md = z.read('prereg.md').decode()
    band = load_json(os.path.join(out, 'band.json'))
    assert f'{band["band"][0]:.2f}' in md and sha256_file(SCRIPT) in md and 'Decision rule' in md


def test_bundle_needs_steps_1_and_2(tmp_path):
    with pytest.raises(FileNotFoundError):
        prereg.export_bundle(str(tmp_path))


def test_lock_then_change_then_withdraw(out):
    assert prereg.status(out)[0] == 'none'
    prereg.lock(out, 'https://osf.io/abc12/')
    st, rec, changed = prereg.status(out)
    assert st == 'ok' and rec['osf_url'] == 'https://osf.io/abc12/' and changed == []
    with open(os.path.join(out, 'split.json'), 'a') as fh:
        fh.write('\n')
    st, _, changed = prereg.status(out)
    assert st == 'mismatch' and changed == ['split.json']
    prereg.withdraw(out)
    assert prereg.status(out)[0] == 'none'
    assert any(f.startswith('prereg_lock.withdrawn_') for f in os.listdir(out))


def test_real_data_gates_steps_4_and_5_on_the_lock(out):
    assert state.step_states(out, require_prereg=False)['analyze'] == 'done'      # demo: not gated
    s = state.step_states(out, require_prereg=True)
    assert s['prereg'] == 'ready' and s['analyze'] == 'locked' and s['group'] == 'locked'
    prereg.lock(out, '')
    s = state.step_states(out, require_prereg=True)
    assert s['prereg'] == 'done' and s['analyze'] == 'done' and s['group'] == 'done'
    with open(os.path.join(out, 'band.json'), 'a') as fh:
        fh.write(' ')
    s = state.step_states(out, require_prereg=True)
    assert s['prereg'] == 'mismatch' and s['analyze'] == 'locked' and s['group'] == 'locked'


# ---------------------------------------------------------------------------------------------- charts
def test_per_mouse_values_match_the_group_step(out):
    D = pd.read_csv(os.path.join(out, 'per_session.csv'))
    v = plots.per_mouse_values(D)
    g = load_json(os.path.join(out, 'group_results.json'))
    assert abs(v['timing_index'].mean() - g['timing_index']['mean']) < 1e-9
    assert abs(v['novelty_within'].mean() - g['novelty_within']['mean']) < 1e-9
    assert len(v['timing_index']) == g['timing_index']['n']


@pytest.fixture
def root():
    from test_app_settings import make_root
    r = make_root()
    r.withdraw()
    yield r
    r.destroy()


def test_charts_draw_including_missing_values(out, root):
    import tkinter as tk
    sid = sorted(os.listdir(os.path.join(out, 'sessions')))[0]
    res = load_json(os.path.join(out, 'sessions', sid))
    f = tk.Frame(root)
    plots.envelope(f, res, 900, 1.0)
    res['mean_envelope_change'] = [float('nan')] * len(res['time'])      # a session without change flashes
    plots.envelope(f, res, 900, 1.0)
    D = pd.read_csv(os.path.join(out, 'per_session.csv'))
    plots.per_mouse(f, D, 900, 1.0)
    plots.per_mouse(f, D[D['mouse'].isin(D['mouse'].unique()[:2])], 900, 1.0)   # too few mice for an interval


def test_results_page_copes_with_too_few_mice(out, tmp_path, root):
    """With 2 test mice the group step reports only n (no mean, no p): the page must still draw."""
    from p4_app import gui
    g = load_json(os.path.join(out, 'group_results.json'))
    for k in ('timing_index', 'novelty_within', 'novelty_between', 'change_during', 'hazard_slope'):
        g[k] = {'n': 2}
    g['decision'] = 'inconclusive (see Section 6.3: a null result says little)'
    with open(os.path.join(out, 'group_results.json'), 'w') as fh:
        json.dump(g, fh)
    app = gui.App(root, demo=False, base=str(tmp_path), python=sys.executable)
    app.out_var.set(out)
    prereg.lock(out, '')
    app._settings_changed(app.out_var)
    app.select('group')
    assert app.states['group'] == 'done'


# ---------------------------------------------------------------------------------------------- window
def test_window_locks_and_withdraws(out, tmp_path, root, monkeypatch):
    from p4_app import gui
    monkeypatch.setattr(gui.messagebox, 'askyesno', lambda *a, **k: True)
    app = gui.App(root, demo=False, base=str(tmp_path), python=sys.executable)
    monkeypatch.setattr(app, '_open', lambda path: None)          # never open Explorer from a test
    app.out_var.set(out)
    app._settings_changed(app.out_var)
    assert app.states['analyze'] == 'locked'
    app.select('prereg')
    app.export_bundle()
    assert os.listdir(os.path.join(out, 'prereg'))
    app.osf_var.set('https://osf.io/abc12/')
    app.lock_prereg()
    assert app.states['prereg'] == 'done' and app.states['analyze'] == 'done'
    assert 'checksums match' in app.step_items['prereg']['sub'].cget('text')
    app.withdraw_prereg()
    assert app.states['prereg'] == 'ready' and app.states['analyze'] == 'locked'
    assert app.step_items['analyze']['sub'].cget('text') == 'Preregister first (step 3)'


def test_window_shows_the_mismatch_banner(out, tmp_path, root):
    from p4_app import gui
    prereg.lock(out, '')
    with open(os.path.join(out, 'band.json'), 'a') as fh:
        fh.write(' ')
    app = gui.App(root, demo=False, base=str(tmp_path), python=sys.executable)
    app.out_var.set(out)
    app._settings_changed(app.out_var)
    app.select('list')
    texts = [w.cget('text') for w in app.page_host.winfo_children()[0].winfo_children()]
    assert 'Changed since preregistration' in texts and app.states['analyze'] == 'locked'


def test_fallback_band_is_recorded_as_such(out):
    """The real pilot outcome: no clear peak, so the guide's 15-30 Hz, and the preregistration must say so."""
    with open(os.path.join(out, 'band.json'), 'w') as fh:
        json.dump({'band': [15.0, 30.0], 'peak': None, 'fallback': True, 'n_channels': 42}, fh)
    assert state.band_summary(out)['fallback'] is True
    text = prereg.prereg_text(out)
    assert "guide's fallback" in text and 'Changes from the script in the guide' in text and 'seeded shuffle' in text
