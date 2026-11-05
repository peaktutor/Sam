"""
Run all four steps of the UNCHANGED vbn_p4.py on synthetic data.

    python tests/fake_pipeline.py --scenario reset      # or rebound, null
    python tests/fake_pipeline.py --scenario reset --workdir fake_run --keep

The fake AllenSDK in tests/fake_allensdk is put first on PYTHONPATH, so `vbn_p4.py` imports it instead of
the real package. Nothing is downloaded.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), 'vbn_p4.py')
FAKE_SDK = os.path.join(HERE, 'fake_allensdk')
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0     # no console pop-ups
STEPS = (('list', []), ('band', []), ('analyze', ['--max-probes', '1']), ('group', []))


def sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run_pipeline(workdir, scenario, python=sys.executable, mice=None, flashes=None, seed=0, echo=True,
                 script=SCRIPT, steps=None):
    """Run list -> band -> analyze -> group in workdir. Returns (group_results, band, logs by step).
    Raises RuntimeError with the step's output if a step exits non-zero."""
    # FutureWarning: pandas >= 2.2 warns about vbn_p4.py's fillna(False) on object columns (harmless; the
    # AllenSDK 2.16 environment has an older pandas that does not warn)
    env = dict(os.environ, FAKE_VBN_SCENARIO=scenario, PYTHONPATH=FAKE_SDK, PYTHONIOENCODING='utf-8',
               PYTHONWARNINGS='ignore::FutureWarning', FAKE_VBN_SEED=str(seed))
    if mice:
        env['FAKE_VBN_MICE'] = str(mice)
    if flashes:
        env['FAKE_VBN_FLASHES'] = str(flashes)
    cache, out = os.path.join(workdir, 'cache'), os.path.join(workdir, 'p4_out')
    logs = {}
    for step, extra in STEPS:
        if steps and step not in steps:
            continue
        args = [python, '-u', script, step, '--cache', cache, '--out', out] + extra
        t = time.time()
        p = subprocess.run(args, env=env, capture_output=True, text=True, encoding='utf-8', creationflags=NO_WINDOW)
        logs[step] = p.stdout + p.stderr
        if echo:
            print(f'--- {step} ({time.time() - t:.1f} s, exit {p.returncode})\n{logs[step]}')
        if p.returncode != 0:
            raise RuntimeError(f'step {step!r} failed (exit {p.returncode}):\n{logs[step]}')

    def read(name):
        p = os.path.join(out, name)
        if not os.path.exists(p):
            return None
        with open(p) as fh:
            return json.load(fh)
    return read('group_results.json'), read('band.json'), logs


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scenario', default='reset', choices=['reset', 'rebound', 'null'])
    ap.add_argument('--workdir', help='folder for cache and p4_out (default: a temporary folder)')
    ap.add_argument('--keep', action='store_true', help='keep the temporary folder')
    ap.add_argument('--mice', type=int, help='mice with both sessions (default 10)')
    ap.add_argument('--flashes', type=int, help='task flashes per session (default 600)')
    ap.add_argument('--seed', type=int, default=0, help='noise seed (try several to check robustness)')
    a = ap.parse_args()
    print('vbn_p4.py sha256:', sha256(SCRIPT))
    wd = a.workdir or tempfile.mkdtemp(prefix=f'p4_fake_{a.scenario}_')
    t0 = time.time()
    try:
        group, band, _ = run_pipeline(wd, a.scenario, mice=a.mice, flashes=a.flashes, seed=a.seed)
        print(f'scenario={a.scenario} seed={a.seed}  band={band["band"]}  decision: {group["decision"]}  '
              f'precision clause: {group["precision_clause"]}  ({time.time() - t0:.0f} s)')
    finally:
        if not (a.workdir or a.keep):
            shutil.rmtree(wd, ignore_errors=True)
        else:
            print('output kept in', wd)
