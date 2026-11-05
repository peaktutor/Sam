"""
Checks on the PILOT sessions, run after step 2 (guide Section 5: "fix the problem on the pilot sessions before
preregistering"). Uses the files step 2 already downloaded and vbn_p4.py's own functions.
Pilot mice are never part of the test, so this looks at no test data.

    python tools/pilot_check.py --cache vbn_cache --out p4_out
    python tools/pilot_check.py --out p4_out --replot     (redraw the plot from pilot_check.json; needs no data files)

For each pilot session it reports how AllenSDK numbers flashes_since_change, the change hazards the script computes,
how many flashes show the shared images, how many channels are recorded and driven, and the per-session contrasts.
It also draws the periodic part of the spectrum. Writes OUT/pilot_check.json and OUT/pilot_check_spectrum.png.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import vbn_p4  # noqa: E402  (imported, never modified)


def script_hazards(fl):
    """The hazard values vbn_p4.analyze_session computes for non-change flashes, from the same counting."""
    k = fl['flashes_since_change'].values.astype(float)
    change = fl['is_change'].values
    k = np.where(change, np.r_[0, k[:-1]] + 1, k)
    haz = np.full(len(fl), np.nan)
    for v in np.unique(k[np.isfinite(k)]).astype(int):
        at = k == v
        haz[at] = (at & change).sum() / max(at.sum(), 1)
    return haz[~change]


def periodic_part(f, P):
    """log10 power above vbn_p4.pick_band's 1/f fit, for the plot."""
    fit = (f >= 2) & (f <= 100) & ~((f > 55) & (f < 65))
    slope, icpt = np.polyfit(np.log10(f[fit]), np.log10(P[fit]), 1)
    return np.log10(P) - (icpt + slope * np.log10(np.maximum(f, 1e-9)))


def band_variants(cache, pilot, max_probes=2):
    """The band from the pilot sessions: with step 2's channels, and with every recorded visual channel."""
    from scipy import signal
    sets = {'every 4th recorded visual channel (step 2)': [], 'all recorded visual channels': []}
    f = None
    for sid in pilot.index:
        session = cache.get_ecephys_session(ecephys_session_id=int(sid))
        fl = vbn_p4.stimulus_table(session)
        t0, t1 = fl['start_time'].min(), fl['start_time'].max()
        for pid in vbn_p4.visual_probes(cache, sid).index[:max_probes]:
            lfp = session.get_lfp(int(pid))
            fs = 1 / np.median(np.diff(lfp['time'].values[:1000]))
            lfp = lfp.sel(time=slice(t0, t1))
            chans = vbn_p4.visual_channels(cache, pid, lfp['channel'].values, every=1)
            psd = {}
            for ch in chans:
                f, psd[ch] = signal.welch(lfp.sel(channel=ch).values, fs=fs, nperseg=int(round(2 * fs)))
            sets['every 4th recorded visual channel (step 2)'] += [psd[ch] for ch in chans[::4]]
            sets['all recorded visual channels'] += list(psd.values())
            del lfp
    out, curves = {}, {}
    for name, psds in sets.items():
        P = np.median(np.array(psds), 0)
        out[name] = dict(vbn_p4.pick_band(f, P), n_channels=len(psds))
        keep = (f >= 1) & (f <= 100)
        curves[name] = (f[keep].tolist(), periodic_part(f, P)[keep].tolist())
    return out, curves


def check_session(cache, sid, band):
    t0 = time.time()
    session = cache.get_ecephys_session(ecephys_session_id=int(sid))
    fl = vbn_p4.stimulus_table(session)
    change = fl['is_change'].values
    shared = fl['image_name'].isin(vbn_p4.SHARED).values
    h = script_hazards(fl)
    out = dict(session=int(sid), n_task_flashes=int(len(fl)), n_change=int(change.sum()),
               flashes_since_change_on_change_flashes=sorted({int(v) for v in fl['flashes_since_change'].values[change]}),
               n_shared_image_flashes=int(shared.sum()), shared_images_seen=sorted(set(fl['image_name'][shared])),
               hazard_nonchange=dict(distinct_values=int(len(np.unique(np.round(h, 3)))), max=float(np.nanmax(h))))
    probes = vbn_p4.visual_probes(cache, sid)
    out['visual_probes'] = [int(p) for p in probes.index]
    if len(probes):
        pid = probes.index[0]
        lfp = session.get_lfp(int(pid))
        fs = 1 / np.median(np.diff(lfp['time'].values[:1000]))
        everything = vbn_p4.visual_channels(cache, pid, lfp['channel'].values, every=1)
        used = vbn_p4.visual_channels(cache, pid, lfp['channel'].values, every=2)
        driven = 0
        for ch in used:
            ep, ok, tax = vbn_p4.raw_epochs(lfp, ch, fl['start_time'].values, fs)
            driven += bool(vbn_p4.driven(ep, tax))
        out.update(first_probe=int(pid), lfp_fs_hz=float(fs), lfp_channels=int(lfp.shape[1]),
                   visual_channels_in_lfp=len(everything), channels_used=len(used), channels_driven=driven)
        del lfp
    try:
        res = vbn_p4.analyze_session(cache, int(sid), band, 1)
        out['analyze_session'] = {k: res.get(k) for k in ('error', 'n_driven', 'timing_index', 'novelty_within',
                                                          'novelty_shared', 'change_during', 'hazard_slope') if k in res}
    except Exception as e:                                       # report, don't hide, a crash
        out['analyze_session'] = {'crash': f'{type(e).__name__}: {e}'}
    out['seconds'] = round(time.time() - t0)
    return out


def plot_curves(curves, variants, path):
    """The periodic part of the pilot spectrum, with the search window and the clear-peak threshold.
    Series colours: dataviz reference palette slots 1-2."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=120)
    ax.axvspan(12, 40, color='#eef1f5', lw=0, zorder=0)
    ax.text(26, 0.02, "the script's search window, 12–40 Hz", transform=ax.get_xaxis_transform(), ha='center',
            va='bottom', color='#5f6b7d', fontsize=8.5)
    ax.axhline(0.05, color='#97a1b0', lw=1, ls=(0, (3, 3)))
    ax.axhline(0, color='#d8dee8', lw=1)
    for (name, (f, r)), col in zip(curves.items(), ('#2a78d6', '#eb6834')):
        v = variants[name]
        found = 'no clear peak' if v['peak'] is None else f'peak {v["peak"]:.1f} Hz'
        ax.plot(f, r, color=col, lw=2, label=f'{name}  ({v["n_channels"]} spectra; {found})')
    ax.set_xlim(1, 100)
    ax.set_xlabel('Frequency (Hz)', color='#5f6b7d')
    ax.set_ylabel('log10 power above the 1/f fit', color='#5f6b7d')
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, fontsize=8.5, loc='upper right')
    ax.set_title("Pilot spectra: periodic part (dashed line: the 0.05 clear-peak threshold)", loc='left',
                 fontsize=10, color='#1d2433')
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print('written', path)


def replot(out):
    with open(os.path.join(out, 'pilot_check.json'), encoding='utf-8') as fh:
        d = json.load(fh)
    curves = {k: (c['freq_hz'], c['log10_power_above_fit']) for k, c in d['spectrum_curves'].items()}
    plot_curves(curves, d['band_by_channel_choice'], os.path.join(out, 'pilot_check_spectrum.png'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', default=os.path.join(ROOT, 'vbn_cache'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'p4_out'))
    ap.add_argument('--replot', action='store_true')
    a = ap.parse_args()
    if a.replot:
        return replot(a.out)
    with open(os.path.join(a.out, 'band.json'), encoding='utf-8') as fh:
        band = json.load(fh)['band']
    cache = vbn_p4.get_cache(a.cache)
    pilot = vbn_p4.sessions_for(a, 'pilot')
    results = []
    for sid in pilot.index:
        print(f'--- pilot session {sid} ({pilot.loc[sid, "experience_level"]})', flush=True)
        r = check_session(cache, sid, band)
        print(json.dumps(r, indent=1), flush=True)
        results.append(r)
    print('--- beta band from the pilot sessions', flush=True)
    variants, curves = band_variants(cache, pilot)
    print(json.dumps(variants, indent=1), flush=True)
    plot_curves(curves, variants, os.path.join(a.out, 'pilot_check_spectrum.png'))
    summary = dict(band_json=band, band_by_channel_choice=variants, sessions=results,
                   spectrum_curves={k: dict(freq_hz=f, log10_power_above_fit=r) for k, (f, r) in curves.items()})
    with open(os.path.join(a.out, 'pilot_check.json'), 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, indent=1)
    print('written', os.path.join(a.out, 'pilot_check.json'))


if __name__ == '__main__':
    main()
