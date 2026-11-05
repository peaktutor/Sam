"""
Read what vbn_p4.py has written to the output folder and summarise it for the window. Read-only.

Every function returns None when the step's files are not there yet, so the window can show "not run yet".
"""
import json
import os
import time

import pandas as pd

from . import prereg, storage

STEP_KEYS = ('list', 'band', 'prereg', 'analyze', 'group')
GROUPS = ('Sample', 'Test', 'Other')   # Sample = the pilot mice; Other = mice without both a familiar and a novel session
LFP_GB, SESSION_GB = 4.7, 3.0          # the guide's file sizes


def sample_dir(out):
    """Results of sessions run from the Data page that are not test sessions. vbn_p4.py's group step counts every
    file in out/sessions, so these must never go there: a pilot mouse in the results would break the protocol."""
    return os.path.join(out, 'sample_runs')


def session_groups(out):
    """{session id: 'Sample' | 'Test' | 'Other'}, or {} before step 1 has run."""
    split = _json(os.path.join(out, 'split.json'))
    try:
        st = pd.read_csv(os.path.join(out, 'sessions.csv'), index_col=0)
    except (OSError, ValueError):
        return {}
    if split is None:
        return {}
    return {int(sid): 'Sample' if m in split['pilot'] else 'Test' if m in split['test'] else 'Other'
            for sid, m in st['mouse_id'].items()}


def data_rows(out, cache):
    """One row per session in the dataset for the Data page: its group, what is downloaded, and any result.
    Sample sessions first, then test sessions by mouse, then the rest. [] before step 1 has run."""
    groups = session_groups(out)
    if not groups:
        return []
    st = pd.read_csv(os.path.join(out, 'sessions.csv'), index_col=0)
    inv = storage.inventory(cache)
    rows = []
    for sid, s in st.iterrows():
        sid, group = int(sid), groups[int(sid)]
        rec = inv.get(sid, dict(session_file=None, lfp=[], gb=0.0))
        where = out if group == 'Test' else sample_dir(out)
        path = os.path.join(where, 'sessions', f'{sid}.json')
        res = _json(path) if os.path.exists(path) else None
        rows.append(dict(session=sid, mouse=int(s['mouse_id']), level=s['experience_level'],
                         day=int(s['session_number']), group=group, has_session=bool(rec['session_file']),
                         n_lfp=len(rec['lfp']), gb=rec['gb'], result=res, result_path=path if res else None))
    rows.sort(key=lambda r: (GROUPS.index(r['group']), r['mouse'], r['day']))
    return rows


def missing_gb(row, max_probes):
    """Rough size still to download for this session, for `max_probes` LFP files."""
    return (0.0 if row['has_session'] else SESSION_GB) + max(0, max_probes - row['n_lfp']) * LFP_GB


def result_text(row):
    """A short description of a session's result for the table."""
    r = row['result']
    if r is None:
        return '—'
    if 'error' in r:
        return f'✗  {r["error"]}'
    return f'✓  timing {r.get("timing_index", float("nan")):+.3f}, change {r.get("change_during", float("nan")):+.3f}'


def _json(path):
    """json.load (which accepts the NaN values vbn_p4.py writes), or None if the file is missing or unreadable."""
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def ago(t):
    """'just now', '5 min ago', '3 h ago', '2 days ago'."""
    if t is None:
        return ''
    s = time.time() - t
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{s / 60:.0f} min ago'
    if s < 86400:
        return f'{s / 3600:.0f} h ago'
    return f'{s / 86400:.0f} days ago'


def list_summary(out):
    split = _json(os.path.join(out, 'split.json'))
    try:
        st = pd.read_csv(os.path.join(out, 'sessions.csv'), index_col=0)
        order = pd.read_csv(os.path.join(out, 'day_order.csv'), index_col=0)
    except (OSError, ValueError):
        return None
    if split is None:
        return None
    test = st[st['mouse_id'].isin(split['test'])]
    return dict(n_sessions=len(st), n_mice=int(st['mouse_id'].nunique()), pilot=split['pilot'],
                n_paired=len(split['pilot']) + len(split['test']), n_test=len(split['test']),
                n_test_sessions=len(test), order=order, when=_mtime(os.path.join(out, 'split.json')))


def band_summary(out):
    b = _json(os.path.join(out, 'band.json'))
    if b is None:
        return None
    lo, hi = b['band']
    return dict(low=lo, high=hi, peak=b.get('peak'), n_channels=b.get('n_channels'),
                slope=b.get('aperiodic_slope'), fallback=b.get('fallback', (lo, hi) == (15.0, 30.0)),
                pilot_plot=os.path.join(out, 'pilot_check_spectrum.png'),
                when=_mtime(os.path.join(out, 'band.json')))


def analyze_summary(out):
    """One row per test session: done, error (with the message) or waiting."""
    ls = list_summary(out)
    if ls is None:
        return None
    st = pd.read_csv(os.path.join(out, 'sessions.csv'), index_col=0)
    split = _json(os.path.join(out, 'split.json'))
    rows = []
    for sid, s in st[st['mouse_id'].isin(split['test'])].sort_values(['mouse_id', 'session_number']).iterrows():
        r = _json(os.path.join(out, 'sessions', f'{int(sid)}.json'))
        status = 'waiting' if r is None else ('error' if 'error' in r else 'done')
        rows.append(dict(session=int(sid), mouse=int(s['mouse_id']), level=s['experience_level'],
                         day=int(s['session_number']), status=status,
                         detail=(r or {}).get('error', f"{(r or {}).get('n_driven', '')} driven channels" if r else '')))
    n = len(rows)
    done = sum(r['status'] == 'done' for r in rows)
    errors = sum(r['status'] == 'error' for r in rows)
    return dict(rows=rows, total=n, done=done, errors=errors, waiting=n - done - errors)


def group_summary(out):
    g = _json(os.path.join(out, 'group_results.json'))
    if g is not None:
        g['when'] = _mtime(os.path.join(out, 'group_results.json'))
    return g


def step_states(out, require_prereg=False):
    """step -> 'done' | 'partial' | 'ready' | 'locked' (prereg can also be 'mismatch').
    With require_prereg (real data), steps 4 and 5 stay locked until the preregistration lock exists and
    vbn_p4.py, band.json and split.json still match it."""
    ls, bs, an = list_summary(out), band_summary(out), analyze_summary(out)
    s = dict(list='done' if ls else 'ready')
    s['band'] = 'done' if bs else ('ready' if ls else 'locked')
    pr = prereg.status(out)[0]
    s['prereg'] = 'locked' if not bs else {'ok': 'done', 'mismatch': 'mismatch'}.get(pr, 'ready')
    gate = require_prereg and s['prereg'] != 'done'
    if not bs or gate:
        s['analyze'] = 'locked'
    else:
        s['analyze'] = 'done' if an and an['done'] + an['errors'] == an['total'] else \
            ('partial' if an and an['done'] + an['errors'] else 'ready')
    s['group'] = 'locked' if gate else ('done' if group_summary(out) else ('ready' if an and an['done'] else 'locked'))
    return s
