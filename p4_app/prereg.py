"""
Preregistration (guide Section 6): export a bundle to upload to OSF, then lock it.

The lock (prereg_lock.json in the output folder) records the SHA-256 of vbn_p4.py, band.json and split.json.
If any of them changes afterwards, status() reports a mismatch and the window blocks steps 4 and 5 for real data.
"""
import datetime
import json
import os
import zipfile

from .runner import SCRIPT, sha256_file

LOCK = 'prereg_lock.json'
SCRIPT_CHANGES = (
    'The channel table column is `ecephys_probe_id` in AllenSDK 2.16.2 (the script crashed on the real files).',
    'The Welch window is rounded rather than truncated: probe clocks differ slightly from 1250 Hz, which gave spectra '
    'of unequal length (the script crashed on the real files).',
    'The test set is the first 16 mice after the pilot mice in the seeded shuffle, as the sample size above requires; '
    'the script had taken all 47.',
    'Every second channel is taken among the channels the LFP file holds. Real LFP files keep every 4th channel, and '
    'subsampling first kept as few as 3 of 19 recorded visual channels on a probe.',
    'A clear peak must stand 0.05 (log10) above the 1/f fit and above its surroundings after 4 Hz smoothing. The old '
    'rule took the highest point in 12–40 Hz, which on the pilot data was the 40 Hz edge of a spectrum rising into '
    'gamma, not a peak.',
    'In the hazard estimate, a change flash counts as one more than the flash before it. AllenSDK numbers every change '
    'flash 0, which gave every non-change flash but the first a hazard of 0.',
    'Added: the novelty contrast on the two shared images, as a control, and a report of reversals of a predicted sign. '
    'Neither changes the decision rule.',
)
GUARDED = ('band.json', 'split.json')          # outputs of steps 1-2 that the preregistration fixes


def _now():
    return datetime.datetime.now().astimezone()


def fingerprints(out, script=SCRIPT):
    """SHA-256 of the script and of each guarded output that exists."""
    fp = {'vbn_p4.py': sha256_file(script) if os.path.exists(script) else None}
    for name in GUARDED:
        p = os.path.join(out, name)
        fp[name] = sha256_file(p) if os.path.exists(p) else None
    return fp


def prereg_text(out, script=SCRIPT, osf_url=''):
    """The guide's Section 6 table, filled in with this run's values (Markdown)."""
    with open(os.path.join(out, 'band.json'), encoding='utf-8') as fh:
        band = json.load(fh)
    with open(os.path.join(out, 'split.json'), encoding='utf-8') as fh:
        split = json.load(fh)
    fp = fingerprints(out, script)
    lo, hi = band['band']
    how = "no clear peak in 12–40 Hz, so the guide's fallback" if band.get('peak') is None else \
        f'peak {band["peak"]:.2f} Hz, ± 5 Hz'
    rows = [
        ('Beta band', f'{lo:.2f}–{hi:.2f} Hz ({how}; {band.get("n_channels", "?")} channel spectra; pilot mice only; '
                      'band.json)'),
        ('Pilot mice', ', '.join(map(str, split['pilot']))),
        ('Test mice', f'{len(split["test"])}, the first after the pilot mice in the seeded shuffle: '
                      + ', '.join(map(str, split['test']))),
        ('Channels', 'visual cortex (VISp, VISl, VISal, VISrl, VISam, VISpm), every second channel of those in the LFP file (1 probe per session); driven if the '
                     'evoked potential at 30–100 or 100–200 ms differs from baseline (p < 0.0005 each, paired t test '
                     'across flashes)'),
        ('Flashes', 'task block, omitted flashes excluded; windows during 50–250 ms, after 300–550 ms, before '
                    '−250–0 ms'),
        ('Beta measure', "induced envelope: each condition's mean evoked potential removed, 4th-order Butterworth "
                         "band-pass, Hilbert amplitude, normalized to the channel's median"),
        ('Novelty contrast (primary)', 'novel sessions: flashes from the 2nd to the first 20% minus the last 40%'),
        ('Units and tests', 'mice; one-sample two-sided t tests, α = 0.05'),
        ('Decision rule', 'reset-driven if the timing index and the novelty contrast are both significantly '
                          'positive; rebound-driven if the timing index is significantly negative and the novelty '
                          'contrast is not significantly positive; otherwise inconclusive. A reversal of a predicted '
                          'sign counts as disconfirmation.'),
        ('Sample size', f'{len(split["test"])} test mice. The model recovery needed 8 sessions to recover the '
                        'reset-driven reading at full signal and 16 at half signal; the rebound-driven reading needed 3.'),
        ('Code', f'vbn_p4.py exactly as run, SHA-256 {fp["vbn_p4.py"]}'),
    ]
    lines = ['# Preregistration: prediction P4 on the Allen Visual Behavior Neuropixels data', '',
             f'Prepared {_now():%d %B %Y, %H:%M %Z} from the output folder of steps 1–2.', '']
    if osf_url:
        lines += [f'OSF: {osf_url}', '']
    lines += ['| Item | Value fixed in advance |', '|---|---|'] + [f'| {k} | {v} |' for k, v in rows]
    lines += ['', '## Changes from the script in the guide (Appendix A)', '',
              'All made on the pilot mice, before any test mouse was analysed:', ''] + [f'- {c}' for c in SCRIPT_CHANGES]
    lines += ['', '## Checksums (SHA-256)', ''] + [f'- `{k}`: `{v}`' for k, v in fp.items() if v]
    return '\n'.join(lines) + '\n'


def export_bundle(out, script=SCRIPT):
    """Write out/prereg/prereg_bundle_<timestamp>.zip with everything to upload. Returns its path."""
    for name in GUARDED:
        if not os.path.exists(os.path.join(out, name)):
            raise FileNotFoundError(f'{name} is missing: run steps 1 and 2 first')
    folder = os.path.join(out, 'prereg')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'prereg_bundle_{_now():%Y%m%d-%H%M%S}.zip')
    fp = fingerprints(out, script)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(script, 'vbn_p4.py')
        for name in GUARDED + ('day_order.csv', 'sessions.csv'):
            p = os.path.join(out, name)
            if os.path.exists(p):
                z.write(p, name)
        z.writestr('prereg.md', prereg_text(out, script))
        z.writestr('SHA256SUMS.txt', ''.join(f'{v}  {k}\n' for k, v in fp.items() if v))
    return path


def lock(out, osf_url, script=SCRIPT):
    """Record that the preregistration was uploaded. Returns the lock record."""
    rec = dict(locked_at=_now().isoformat(timespec='seconds'), osf_url=osf_url.strip(),
               fingerprints=fingerprints(out, script))
    with open(os.path.join(out, LOCK), 'w', encoding='utf-8') as fh:
        json.dump(rec, fh, indent=1)
    return rec


def withdraw(out):
    """Keep the old lock as a record (renamed), rather than deleting it."""
    p = os.path.join(out, LOCK)
    if os.path.exists(p):
        os.replace(p, os.path.join(out, f'prereg_lock.withdrawn_{_now():%Y%m%d-%H%M%S}.json'))


def status(out, script=SCRIPT):
    """('none' | 'ok' | 'mismatch', lock record or None, [names that changed])."""
    try:
        with open(os.path.join(out, LOCK), encoding='utf-8') as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return 'none', None, []
    now = fingerprints(out, script)
    changed = [k for k, v in rec.get('fingerprints', {}).items() if now.get(k) != v]
    return ('mismatch' if changed else 'ok'), rec, changed


def nice_date(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime('%d %B %Y, %H:%M')
    except (TypeError, ValueError):
        return iso or '?'
