"""
End-to-end checks of the unchanged vbn_p4.py on synthetic data (see tests/fake_pipeline.py).

    .venv-test\\Scripts\\python -m pytest tests -v        (about 3 minutes; the scenarios run in parallel)
"""
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'fake_allensdk'))
from fake_pipeline import run_pipeline          # noqa: E402
from fake_vbn import signals                    # noqa: E402
import vbn_p4                                     # noqa: E402

PLANTED_BETA_HZ = 22.0


def rebound_written():
    try:
        signals.rebound_gain(np.zeros(3), signals.Flash(False, False, 0.5, 0.0))
        return True
    except NotImplementedError:
        return False


# Noise seed per scenario. With no signal planted, the decision rule calls "rebound-driven" whenever the timing
# index happens to be significantly negative: about 2.5% of runs. Seed 0 is one of those (timing p = 0.014);
# seeds 1-3 are inconclusive, as most runs are (checked 2026-10-31). A fixed seed keeps the test reproducible.
SEEDS = {'null': 1}


@pytest.fixture(scope='session')
def runs(tmp_path_factory):
    """Run every available scenario once, in parallel. Maps scenario -> (group, band, workdir) or Exception."""
    names = ['reset', 'null'] + (['rebound'] if rebound_written() else [])
    dirs = {name: str(tmp_path_factory.mktemp(name)) for name in names}   # mktemp is not thread-safe

    def one(name):
        wd = dirs[name]
        try:
            group, band, _ = run_pipeline(wd, name, echo=False, seed=SEEDS.get(name, 0))
            return name, (group, band, wd)
        except Exception as e:
            return name, e

    with ThreadPoolExecutor(len(names)) as ex:
        return dict(ex.map(one, names))


def result(runs, name):
    r = runs[name]
    if isinstance(r, Exception):
        pytest.fail(str(r))
    return r


def test_band_recovers_planted_peak(runs):
    _, band, _ = result(runs, 'reset')
    lo, hi = band['band']
    assert lo < PLANTED_BETA_HZ < hi, band
    assert abs(band['peak'] - PLANTED_BETA_HZ) < 2, band


def test_reset_scenario_gives_reset_decision(runs):
    group, _, _ = result(runs, 'reset')
    assert group['decision'].startswith('reset-driven'), group
    assert group['timing_index']['mean'] > 0 and group['change_during']['mean'] > 0


def test_null_scenario_is_inconclusive(runs):
    group, _, _ = result(runs, 'null')
    assert group['decision'].startswith('inconclusive'), group


@pytest.mark.skipif(not rebound_written(), reason='rebound_gain in fake_vbn/signals.py is not written yet')
def test_rebound_scenario_gives_rebound_decision(runs):
    group, _, _ = result(runs, 'rebound')
    assert group['decision'].startswith('rebound-driven'), group
    assert group['timing_index']['mean'] < 0


def test_precision_clause_recovered(runs):
    """AllenSDK numbers every change flash 0; the script must still recover a planted effect of the change hazard."""
    group, _, _ = result(runs, 'reset')
    assert group['precision_clause'] == 'supported', group['hazard_slope']


def test_precision_clause_not_claimed_without_an_effect(runs):
    group, _, _ = result(runs, 'null')
    assert group['precision_clause'] == 'not supported', group['hazard_slope']


def test_shared_images_show_less_novelty_than_novel_images(runs):
    """The shared images are familiar in both sessions: the guide's further within-session control."""
    group, _, _ = result(runs, 'reset')
    assert group['novelty_shared_images']['n'] == group['novelty_within']['n']
    assert abs(group['novelty_shared_images']['mean']) < group['novelty_within']['mean']


def test_a_planted_reset_signal_reverses_nothing(runs):
    group, _, _ = result(runs, 'reset')
    assert group['reversals']['reset'] == [] and 'timing_index' in group['reversals']['rebound']


def test_test_set_is_16_mice_and_the_rule_is_fixed_by_the_seed():
    """The guide's sample size, chosen with no reference to the data."""
    assert vbn_p4.N_TEST == 16 and vbn_p4.SEED == 20261018


def spectrum(resid_fn):
    f = np.arange(0, 625.5, 0.5)
    return f, 10 ** (resid_fn(f) - 1.7 * np.log10(np.maximum(f, 1e-9)))


def test_band_rule_finds_a_real_peak():
    f, P = spectrum(lambda f: 0.3 * np.exp(-0.5 * ((f - 22) / 2.5) ** 2))
    r = vbn_p4.pick_band(f, P)
    assert abs(r['peak'] - 22) <= 1 and not r['fallback'] and r['band'][0] < 22 < r['band'][1]


def test_band_rule_falls_back_on_the_real_pilot_spectrum():
    """Recorded from the real pilot sessions: no beta peak, just a rise into gamma. The old rule answered 40 Hz."""
    with open(os.path.join(os.path.dirname(__file__), 'data', 'real_pilot_spectrum.json')) as fh:
        d = json.load(fh)
    r = vbn_p4.pick_band(np.array(d['freq_hz']), 10 ** np.array(d['log10_power_above_fit']))
    assert r['fallback'] and r['band'] == [15.0, 30.0] and r['peak'] is None


def test_band_rule_does_not_count_narrow_harmonics():
    f, P = spectrum(lambda f: 0.1 * np.isin(f, [20, 24, 28, 32, 36]))
    assert vbn_p4.pick_band(f, P)['fallback']


def test_placeholder_lfp_files_written_for_cleanup(runs):
    """Phase 4 deletes probe LFP files after a session's result exists; the fake cache writes them where real ones go."""
    _, _, wd = result(runs, 'reset')
    lfp = glob.glob(os.path.join(wd, 'cache', '*', 'behavior_ecephys_sessions', '*', 'probe_*_lfp.nwb'))
    results = glob.glob(os.path.join(wd, 'p4_out', 'sessions', '*.json'))
    assert results and lfp
    assert {os.path.basename(os.path.dirname(f)) for f in lfp} >= {os.path.basename(f)[:-5] for f in results}


# ---------------------------------------------------------------------------------------------- real-data layout
ORIGINAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vbn_p4_original.py')


@pytest.mark.skipif(not os.path.exists(ORIGINAL), reason='vbn_p4_original.py not present')
def test_original_script_used_the_wrong_channel_column(tmp_path):
    """Found on the real data (2026-10-31): AllenSDK 2.16.2's channel table has ecephys_probe_id, not probe_id.
    The fake now uses the real column names, so the docx version fails here exactly as it did on the real files."""
    with pytest.raises(RuntimeError, match="KeyError: 'probe_id'"):
        run_pipeline(str(tmp_path), 'reset', mice=4, flashes=300, echo=False, script=ORIGINAL,
                     steps=('list', 'band'))


def test_every_second_channel_means_of_those_recorded(tmp_path):
    """LFP files hold every 4th channel, so subsampling must come after checking what the file holds."""
    group, band, _ = run_pipeline(str(tmp_path), 'reset', mice=4, flashes=300, echo=False, steps=('list', 'band'))
    assert band is not None and band['n_channels'] > 0
    out = os.path.join(str(tmp_path), 'p4_out')
    run_pipeline(str(tmp_path), 'reset', mice=4, flashes=300, echo=False, steps=('analyze',))
    with open(os.path.join(out, 'sessions', sorted(os.listdir(os.path.join(out, 'sessions')))[0])) as fh:
        assert json.load(fh)['n_driven'] == 6                      # 12 recorded visual channels, every second
