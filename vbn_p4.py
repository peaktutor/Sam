"""
vbn_p4.py - test of prediction P4 (two readings of beta) on the Allen Visual Behavior Neuropixels data.
 
Leven, "Adaptive Resonance and the Stencils of Spatial Computing", Section 7 and Section 6.3 (model recovery).
 
Steps (run in order; each writes to OUT):
  python vbn_p4.py list     --cache CACHE --out OUT                  # session table, day-order check, size estimate, mouse split
  python vbn_p4.py band     --cache CACHE --out OUT                  # beta band from the PILOT mice only
  python vbn_p4.py analyze  --cache CACHE --out OUT [--session ID]   # per-session contrasts for the TEST mice
  python vbn_p4.py group    --out OUT                                # group statistics and the preregistered decision
 
Requires: allensdk (2.16.x), numpy, scipy, pandas, xarray.  Python 3.10 recommended.

Changes from the guide's Appendix A, all made on real pilot data before preregistration (the original is kept as
vbn_p4_original.py):
  Crashes on the real files:  channel table column is ecephys_probe_id;  rounded Welch window (probe clocks differ).
  To match the guide:  16 test mice;  every second channel is of those the LFP file holds;  a spectrum still rising at
  the edge of the 12-40 Hz window is not a peak, so the 15-30 Hz fallback applies;  reversals of a predicted sign are
  reported;  the shared images give a within-session control.
  To make the hazard measurable:  a change flash is counted as one more than the flash before it.
"""
import argparse, json, os, sys, time
import numpy as np
import pandas as pd
from scipy import signal, stats
 
VIS = ('VISp', 'VISl', 'VISal', 'VISrl', 'VISam', 'VISpm')
EPOCH = (-0.25, 0.75)          # analysis window around flash onset (s); flashes are 250 ms on, 500 ms gray
PAD = 0.5                      # filter padding on each side of the epoch (s)
DT_OUT = 0.002                 # output resolution of envelopes (s)
WIN = dict(during=(0.05, 0.25), after=(0.30, 0.55), cycle=(0.0, 0.75), pre=(-0.25, 0.0))
SEED = 20261018                # fixed seed for the pilot/test split
N_TEST = 16                    # test mice: the first 16 after the pilot mice in the seeded shuffle
SHARED = ('im083_r', 'im111_r')   # images in both the familiar and the novel set
PEAK_MIN = 0.05                # a clear beta peak: this far (log10) above the fit and its surroundings
 
 
# ----------------------------------------------------------------------------- cache helpers
def get_cache(cache_dir):
    from allensdk.brain_observatory.behavior.behavior_project_cache.behavior_neuropixels_project_cache \
        import VisualBehaviorNeuropixelsProjectCache
    cache = VisualBehaviorNeuropixelsProjectCache.from_s3_cache(cache_dir=cache_dir)
    cache.load_latest_manifest()          # v0.5.0 and later fix an LFP amplitude-scaling error
    print('manifest:', cache.current_manifest())
    return cache
 
 
def stimulus_table(session):
    sp = session.stimulus_presentations.copy()
    if 'end_time' not in sp.columns and 'stop_time' in sp.columns:   # renamed in release 0.4.0
        sp['end_time'] = sp['stop_time']
    for c in ('active', 'omitted', 'is_change'):
        sp[c] = sp[c].fillna(False).astype(bool)
    task = sp[sp['active']].copy()
    flashes = task[~task['omitted'] & (task['image_name'] != 'omitted')].copy()
    flashes = flashes.sort_values('start_time')
    flashes['order'] = np.arange(len(flashes))
    # the flash immediately before each change (same stimulus history up to the change)
    idx = np.flatnonzero(flashes['is_change'].values)
    pre = np.zeros(len(flashes), bool); pre[idx[idx > 0] - 1] = True
    flashes['pre_change'] = pre
    return flashes
 
 
def visual_probes(cache, session_id):
    probes = cache.get_probe_table()
    probes = probes[(probes['ecephys_session_id'] == session_id) & probes['has_lfp_data'].astype(bool)]
    keep = [pid for pid, r in probes.iterrows() if any(f"'{a}'" in str(r['structure_acronyms']) for a in VIS)]
    return probes.loc[keep]
 
 
def visual_channels(cache, probe_id, available, every=2):
    """Every nth visual-cortex channel among those the LFP file holds. Real LFP files keep every 4th channel, so
    this must subsample after checking `available`; the sort is stable because channels share positions in pairs."""
    ch = cache.get_channel_table()
    ch = ch[(ch['ecephys_probe_id'] == probe_id) & ch['structure_acronym'].isin(VIS) & ch.index.isin(available)]
    if 'valid_data' in ch.columns:
        ch = ch[ch['valid_data'].astype(bool)]
    return ch.sort_values('probe_vertical_position', kind='mergesort').index.values[::every]
 
 
# ----------------------------------------------------------------------------- signal processing
def raw_epochs(lfp, chan, onsets, fs):
    """LFP epochs (n_flash x n_samples) for one channel, with filter padding."""
    x = lfp.sel(channel=chan).values.astype(np.float64)
    t = lfp['time'].values
    n0 = int(round((EPOCH[0] - PAD) * fs)); n1 = int(round((EPOCH[1] + PAD) * fs))
    i0 = np.searchsorted(t, onsets)
    ok = (i0 + n0 >= 0) & (i0 + n1 < len(x))
    idx = i0[ok, None] + np.arange(n0, n1)[None, :]
    return x[idx], ok, np.arange(n0, n1) / fs
 
 
def induced_envelope(ep, labels, fs, band, tax):
    """Subtract each condition's mean evoked potential, band-pass, Hilbert amplitude; resample to DT_OUT."""
    ep = ep.copy()
    for lab in np.unique(labels):
        m = labels == lab
        ep[m] -= ep[m].mean(0, keepdims=True)
    sos = signal.butter(4, band, btype='bandpass', fs=fs, output='sos')
    env = np.abs(signal.hilbert(signal.sosfiltfilt(sos, ep, axis=1), axis=1))
    out_t = np.arange(EPOCH[0], EPOCH[1], DT_OUT)
    return np.stack([np.interp(out_t, tax, e) for e in env]), out_t
 
 
def driven(ep, tax, alpha=1e-3):
    """A channel is driven if the evoked potential in 30-100 ms or 100-200 ms after onset differs from the
    pre-flash baseline (-100-0 ms) across flashes (paired t tests, Bonferroni over the two windows)."""
    base = ep[:, (tax >= -0.10) & (tax < 0.0)].mean(1)
    ps = [stats.ttest_rel(ep[:, (tax >= w0) & (tax < w1)].mean(1), base).pvalue for w0, w1 in ((0.03, 0.10), (0.10, 0.20))]
    return min(ps) < alpha / 2
 
 
def wmean(env, out_t, w, rows=None):
    m = (out_t >= w[0]) & (out_t < w[1])
    e = env if rows is None else env[rows]
    return float(e[:, m].mean()) if len(e) else np.nan
 
 
# ----------------------------------------------------------------------------- steps
def cmd_list(a):
    cache = get_cache(a.cache)
    st = cache.get_ecephys_session_table()                      # abnormal sessions excluded by default
    print(len(st), 'sessions,', st['mouse_id'].nunique(), 'mice')
    order = pd.crosstab(st['experience_level'], st['session_number'])
    print('\nexperience level by recording day (session_number):\n', order)
    print('(The Allen technical white paper lists three training and recording paths; two show the familiar set first.)')
    if (order.values > 0).sum() == 2:
        print('\nWARNING: novel and familiar sessions occur in a fixed order. The novel-versus-familiar contrast is then\n'
              'confounded with recording day; treat it as secondary and use the within-session contrast as primary.')
    pairs = st.groupby('mouse_id')['experience_level'].agg(lambda s: set(s) >= {'Familiar', 'Novel'})
    mice = np.array(sorted(pairs[pairs].index))
    rng = np.random.default_rng(SEED); rng.shuffle(mice)
    split = dict(pilot=[int(m) for m in mice[:2]], test=[int(m) for m in mice[2:2 + N_TEST]])
    print(f'\n{len(mice)} mice with both a familiar and a novel session; pilot (band definition only): {split["pilot"]}')
    probes = cache.get_probe_table()
    vis = probes[probes['has_lfp_data'].astype(bool) & probes['structure_acronyms'].astype(str).str.contains("'VISp'")]
    print(f'Probes with LFP in VISp: {len(vis)}. Download per session: about 3 GB (session file) plus about 4.5 GB per LFP probe.')
    os.makedirs(a.out, exist_ok=True)
    st.to_csv(os.path.join(a.out, 'sessions.csv'))
    json.dump(split, open(os.path.join(a.out, 'split.json'), 'w'), indent=1)
    order.to_csv(os.path.join(a.out, 'day_order.csv'))
 
 
def sessions_for(a, group):
    st = pd.read_csv(os.path.join(a.out, 'sessions.csv'), index_col=0)
    split = json.load(open(os.path.join(a.out, 'split.json')))
    return st[st['mouse_id'].isin(split[group])]
 
 
def cmd_band(a):
    """Beta band from the pilot mice: peak of the periodic component in 12-40 Hz, +/- 5 Hz (fallback 15-30 Hz)."""
    cache = get_cache(a.cache)
    psds = []
    for sid in sessions_for(a, 'pilot').index:
        session = cache.get_ecephys_session(ecephys_session_id=int(sid))
        fl = stimulus_table(session); t0, t1 = fl['start_time'].min(), fl['start_time'].max()
        for pid in visual_probes(cache, sid).index[:a.max_probes]:
            lfp = session.get_lfp(int(pid)); fs = 1 / np.median(np.diff(lfp['time'].values[:1000]))
            lfp = lfp.sel(time=slice(t0, t1))
            for ch in visual_channels(cache, pid, lfp['channel'].values, every=4):
                f, p = signal.welch(lfp.sel(channel=ch).values, fs=fs, nperseg=int(round(2 * fs)))   # probe clocks differ slightly
                psds.append(p)
            del lfp
    if not psds:
        sys.exit('no visual-cortex channels found in the pilot LFP files')
    res = pick_band(f, np.median(np.array(psds), 0))
    if res['fallback']:
        print('no clear beta peak above the aperiodic fit; using the fallback band 15-30 Hz')
    json.dump(dict(n_channels=len(psds), **res), open(os.path.join(a.out, 'band.json'), 'w'), indent=1)
    print('beta band:', res['band'])


def pick_band(f, P):
    """The band from a median spectrum: fit the aperiodic component over 2-100 Hz (without 55-65 Hz), then take the
    largest clear peak above the fit in 12-40 Hz, +/- 5 Hz; with no clear peak, 15-30 Hz. A clear peak stands 0.05
    (log10) above both the fit and its surroundings after 4 Hz smoothing, so a spectrum still rising at the edge of
    the window and the narrow harmonics of the 1.33 Hz flash rate do not count."""
    fit = (f >= 2) & (f <= 100) & ~((f > 55) & (f < 65))
    slope, icpt = np.polyfit(np.log10(f[fit]), np.log10(P[fit]), 1)
    resid = np.log10(P) - (icpt + slope * np.log10(np.maximum(f, 1e-9)))
    width = int(round(4 / (f[1] - f[0]))) | 1
    smooth = np.convolve(resid, np.ones(width) / width, mode='same')
    win = (f >= 12) & (f <= 40)
    idx, prop = signal.find_peaks(smooth[win], height=PEAK_MIN, prominence=PEAK_MIN)
    res = dict(aperiodic_slope=float(slope), fallback=len(idx) == 0)
    if len(idx) == 0:
        return dict(band=[15.0, 30.0], peak=None, **res)
    peak = float(f[win][idx[np.argmax(prop['peak_heights'])]])
    return dict(band=[peak - 5, peak + 5], peak=peak, **res)
 
 
def analyze_session(cache, sid, band, max_probes):
    session = cache.get_ecephys_session(ecephys_session_id=int(sid))
    fl = stimulus_table(session)
    onsets = fl['start_time'].values
    n = len(fl)
    early = (fl['order'].values >= 1) & (fl['order'].values < int(0.2 * n))        # same fractions as the model recovery
    late = fl['order'].values >= int(0.6 * n)
    change = fl['is_change'].values; prech = fl['pre_change'].values
    labels = np.where(change, 2, np.where(prech, 1, 0))                            # ERP removed separately per condition
    shared = fl['image_name'].isin(SHARED).values                                  # familiar in both sessions: a further control
    # empirical change hazard by flashes since the last change. AllenSDK numbers every change flash 0, so a change
    # flash is counted as one more than the flash before it, the position it arrived at
    k = fl['flashes_since_change'].values.astype(float) if 'flashes_since_change' in fl.columns else np.full(n, np.nan)
    k = np.where(change, np.r_[0, k[:-1]] + 1, k)
    haz = np.full(n, np.nan)
    if np.isfinite(k).any():
        kk = k[np.isfinite(k)].astype(int)
        for v in np.unique(kk):
            at = (k == v); haz[at] = (at & change).sum() / max(at.sum(), 1)
    res = dict(session=int(sid), n_flashes=int(n), n_change=int(change.sum()), channels=[])
    envs = []
    for pid in visual_probes(cache, sid).index[:max_probes]:
        lfp = session.get_lfp(int(pid)); fs = 1 / np.median(np.diff(lfp['time'].values[:1000]))
        for ch in visual_channels(cache, pid, lfp['channel'].values, every=2):
            ep, ok, tax = raw_epochs(lfp, ch, onsets, fs)
            if not driven(ep, tax): continue
            env, out_t = induced_envelope(ep, labels[ok], fs, band, tax)
            full = np.full((n, env.shape[1]), np.nan); full[ok] = env
            full /= np.nanmedian(full)                                               # units: fold of the channel's median envelope
            envs.append(full); res['channels'].append(int(ch))
        del lfp
    if not envs:
        res['error'] = 'no driven visual-cortex channels'; return res
    E = np.nanmean(np.array(envs), 0)                                                # flashes x time, mean over driven channels
    W = lambda w, rows=None: wmean(E[np.isfinite(E[:, 0])] if rows is None else E[rows & np.isfinite(E[:, 0])], out_t, WIN[w])
    res.update(
        timing_index=W('during') - W('after'),                                     # reset > 0; rebound < 0
        novelty_within=W('cycle', early) - W('cycle', late),                       # reset > 0; rebound not > 0
        novelty_shared=W('cycle', early & shared) - W('cycle', late & shared),     # control: the shared images are not novel
        session_level=W('cycle'),                                                  # for the novel-minus-familiar contrast
        change_during=W('during', change) - W('during', prech),                    # reset > 0
        change_after=W('after', change) - W('after', prech),
        n_driven=len(envs))
    pre = np.array([np.nanmean(r[(out_t >= WIN['pre'][0]) & (out_t < WIN['pre'][1])]) for r in E])
    good = np.isfinite(haz) & np.isfinite(pre) & ~change
    res['hazard_slope'] = float(stats.linregress(haz[good], pre[good]).slope) if good.sum() > 50 else np.nan   # precision: < 0
    res['mean_envelope_change'] = np.nanmean(E[change], 0).tolist(); res['mean_envelope_prechange'] = np.nanmean(E[prech], 0).tolist()
    res['time'] = out_t.tolist()
    return res
 
 
def cmd_analyze(a):
    band = json.load(open(os.path.join(a.out, 'band.json')))['band']
    cache = get_cache(a.cache)
    ids = [a.session] if a.session else list(sessions_for(a, 'test').index)
    os.makedirs(os.path.join(a.out, 'sessions'), exist_ok=True)
    for sid in ids:
        f = os.path.join(a.out, 'sessions', f'{int(sid)}.json')
        if os.path.exists(f): print('done already', sid); continue
        t = time.time(); r = analyze_session(cache, int(sid), band, a.max_probes)
        json.dump(r, open(f, 'w')); print(sid, r.get('error', 'ok'), f'{time.time() - t:.0f} s')
 
 
def cmd_group(a):
    st = pd.read_csv(os.path.join(a.out, 'sessions.csv'), index_col=0)
    R = []
    for fn in os.listdir(os.path.join(a.out, 'sessions')):
        r = json.load(open(os.path.join(a.out, 'sessions', fn)))
        if 'error' in r: continue
        s = st.loc[r['session']]; R.append(dict(session=r['session'], mouse=s['mouse_id'], level=s['experience_level'],
            **{k: r[k] for k in ('timing_index', 'novelty_within', 'novelty_shared', 'session_level', 'change_during', 'change_after', 'hazard_slope')}))
    D = pd.DataFrame(R)
    mice = D.groupby('mouse')
    T = mice['timing_index'].mean()                                                  # per mouse, both sessions
    Nw = D[D['level'] == 'Novel'].set_index('mouse')['novelty_within']               # primary novelty contrast
    Ns = D[D['level'] == 'Novel'].set_index('mouse')['novelty_shared']               # its control: should show no novelty
    piv = D.pivot_table(index='mouse', columns='level', values='session_level')
    Nb = (piv['Novel'] - piv['Familiar']).dropna() if {'Novel', 'Familiar'} <= set(piv.columns) else pd.Series(dtype=float)
    Cd = mice['change_during'].mean(); H = mice['hazard_slope'].mean()
    def t1(x):
        x = np.asarray(x, float); x = x[np.isfinite(x)]
        return dict(n=int(len(x)), mean=float(x.mean()), t=float(stats.ttest_1samp(x, 0).statistic), p=float(stats.ttest_1samp(x, 0).pvalue)) if len(x) > 2 else dict(n=int(len(x)))
    out = dict(timing_index=t1(T), novelty_within=t1(Nw), novelty_shared_images=t1(Ns), novelty_between=t1(Nb),
               change_during=t1(Cd), hazard_slope=t1(H))
    # order-adjusted novel-minus-familiar contrast: the recording order varies across mice but is not balanced
    nf = st[st['experience_level'] == 'Novel'].groupby('mouse_id')['session_number'].min().reindex(Nb.index) == 1
    if len(Nb) > 3 and nf.nunique() == 2:
        x = nf.astype(float).values - 0.5          # effect coding: intercept = mean of the two order groups, weighted equally
        fit = stats.linregress(x, Nb.values)
        out['novelty_between_order_adjusted'] = dict(n=int(len(Nb)), mean=float(fit.intercept), se=float(fit.intercept_stderr),
            p=float(2 * stats.t.sf(abs(fit.intercept / fit.intercept_stderr), len(Nb) - 2)), novel_first_mice=int(nf.sum()))
    sig = lambda d, sgn: d.get('p', 1) < 0.05 and np.sign(d.get('mean', 0)) == sgn
    if sig(out['timing_index'], 1) and sig(out['novelty_within'], 1): decision = 'reset-driven reading supported'
    elif sig(out['timing_index'], -1) and not sig(out['novelty_within'], 1): decision = 'rebound-driven reading supported'
    else: decision = 'inconclusive (see Section 6.3: a null result says little about the reset-driven reading unless the signal is strong)'
    out['decision'] = decision
    out['precision_clause'] = 'supported' if sig(out['hazard_slope'], -1) else 'not supported'
    # "A reversal of a predicted sign counts as disconfirmation": contrasts significantly against each reading
    names = ('timing_index', 'novelty_within', 'novelty_between', 'change_during')
    out['reversals'] = dict(reset=[k for k in names if sig(out[k], -1)],
                            rebound=[k for k in names[:3] if sig(out[k], 1)],
                            precision=['hazard_slope'] if sig(out['hazard_slope'], 1) else [])
    json.dump(out, open(os.path.join(a.out, 'group_results.json'), 'w'), indent=1)
    D.to_csv(os.path.join(a.out, 'per_session.csv'), index=False)
    print(json.dumps(out, indent=1))
 
 
if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step', choices=['list', 'band', 'analyze', 'group'])
    ap.add_argument('--cache', default='vbn_cache'); ap.add_argument('--out', default='p4_out')
    ap.add_argument('--session', type=int); ap.add_argument('--max-probes', dest='max_probes', type=int, default=2)
    a = ap.parse_args()
    dict(list=cmd_list, band=cmd_band, analyze=cmd_analyze, group=cmd_group)[a.step](a)
 
