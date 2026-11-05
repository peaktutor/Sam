"""
Finding and deleting a session's LFP files in the AllenSDK download cache.

The guide (Section 7): "delete a session's probe LFP files (named like probe_probeC_lfp.nwb, inside that
session's folder in the cache) once its result file exists". Only files whose name contains 'lfp' and ends in
.nwb, inside a folder named exactly after the session id, are ever touched. The ~3 GB session file is kept.
"""
import glob
import json
import os


def session_dirs(cache, session_id):
    """Folders in the cache named after the session (any release, e.g. visual-behavior-neuropixels-0.5.0)."""
    pattern = os.path.join(glob.escape(cache), '**', str(int(session_id)))
    return [d for d in glob.glob(pattern, recursive=True) if os.path.isdir(d)]


def lfp_files(cache, session_id):
    out = []
    for d in session_dirs(cache, session_id):
        out += [f for f in glob.glob(os.path.join(glob.escape(d), '*.nwb')) if 'lfp' in os.path.basename(f).lower()]
    return sorted(out)


def unfinished_files(folder, lfp_only=False):
    """What a stopped download keeps beside the final name (NAME.nwb.part and NAME.nwb.part.done)."""
    found = glob.glob(os.path.join(glob.escape(folder), '*.nwb.part*'))
    return [f for f in found if 'lfp' in os.path.basename(f).lower()] if lfp_only else found


def result_ok(out, session_id):
    """True if vbn_p4.py wrote a result for this session without an error."""
    try:
        with open(os.path.join(out, 'sessions', f'{int(session_id)}.json'), encoding='utf-8') as fh:
            return 'error' not in json.load(fh)
    except (OSError, ValueError):
        return False


def delete_lfp(cache, out, session_id):
    """Delete the session's LFP files, but only if its result file exists and has no error.
    Returns (number of files deleted, GB freed)."""
    if not result_ok(out, session_id):
        return 0, 0.0
    n, freed = 0, 0
    files = lfp_files(cache, session_id)
    for d in session_dirs(cache, session_id):
        files += unfinished_files(d, lfp_only=True)
    for f in files:
        try:
            size = os.path.getsize(f)
            os.remove(f)
            n, freed = n + 1, freed + size
        except OSError:
            pass
    return n, freed / 1e9


def old_lfp_releases(cache):
    """Dataset releases older than 0.5.0 with LFP files in this cache. Release 0.5.0 corrected an LFP amplitude
    error, so the guide says not to reuse those files."""
    old = set()
    for f in glob.glob(os.path.join(glob.escape(cache), 'visual-behavior-neuropixels-*', 'behavior_ecephys_sessions',
                                    '*', '*lfp*.nwb')):
        name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f))))
        version = name.rsplit('-', 1)[-1]
        try:
            if tuple(int(x) for x in version.split('.')) < (0, 5, 0):
                old.add(version)
        except ValueError:
            pass
    return sorted(old)


def inventory(cache):
    """{session id: dict(session_file=path or None, lfp=[paths], gb=size)} for every session folder in the cache.
    One walk of the cache, so the whole table can be filled without searching once per session."""
    out = {}
    pattern = os.path.join(glob.escape(cache), '*', 'behavior_ecephys_sessions', '*')
    for d in glob.glob(pattern):
        name = os.path.basename(d)
        if not (name.isdigit() and os.path.isdir(d)):
            continue
        rec = dict(session_file=None, lfp=[], gb=0.0)
        for f in glob.glob(os.path.join(glob.escape(d), '*.nwb')) + unfinished_files(d):
            base = os.path.basename(f).lower()
            if base.endswith('.nwb'):
                if 'lfp' in base:
                    rec['lfp'].append(f)
                elif base.startswith('ecephys_session_'):
                    rec['session_file'] = f
            try:
                rec['gb'] += os.path.getsize(f) / 1e9
            except OSError:
                pass
        out[int(name)] = rec
    return out


def delete_session_files(cache, session_id):
    """Delete a session's downloaded .nwb files (session file and LFP), and any unfinished ones. Returns (files
    deleted, GB freed). Only those files, directly inside the folder named exactly after the session, are touched."""
    n, freed = 0, 0
    for d in session_dirs(cache, session_id):
        for f in glob.glob(os.path.join(glob.escape(d), '*.nwb')) + unfinished_files(d):
            try:
                size = os.path.getsize(f)
                os.remove(f)
                n, freed = n + 1, freed + size
            except OSError:
                pass
    return n, freed / 1e9


def folder_gb(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / 1e9
