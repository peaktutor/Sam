"""
Stand-in for allensdk's VisualBehaviorNeuropixelsProjectCache, backed by synthetic data (fake_vbn).
Implements only what vbn_p4.py calls. Nothing is downloaded; small placeholder files are written to the
cache folder in the same places the real files would go, so cleanup code can be tested against them.
"""
import hashlib
import os
import pathlib
import time
import types

from fake_vbn import lfp, world

PLACEHOLDER = 'placeholder written by fake_allensdk'

RELEASE = 'visual-behavior-neuropixels-0.5.0'
FAKE_MANIFEST = f'{RELEASE[:-6]}_project_manifest_v0.5.0.json'


class _Session:
    def __init__(self, cache, session_id):
        self._cache, self._id = cache, int(session_id)
        self.stimulus_presentations, _ = world.stimulus_presentations(self._id)
        self._cache._touch(self._id, f'ecephys_session_{self._id}.nwb')

    def get_lfp(self, probe_id):
        probes = world.probe_table()
        p = probes.loc[int(probe_id)]
        if int(p['ecephys_session_id']) != self._id or not p['has_lfp_data']:
            raise ValueError(f'probe {probe_id} has no LFP data in session {self._id}')
        ch = world.lfp_channel_ids(probe_id)                          # real LFP files hold every 4th channel
        self._cache._touch(self._id, f"probe_{p['name']}_lfp.nwb")
        visual = ch['structure_acronym'].str.startswith('VIS').values
        return lfp.session_lfp(self._id, int(probe_id), ch.index.values, visual)


class _FileCache:
    """Stand-in for fetch_api.cache: download_data(file_id) "downloads" one file by writing its placeholder."""

    def __init__(self, owner):
        self.owner = owner
        self.failures_left = int(os.environ.get('FAKE_VBN_DOWNLOAD_FAILURES', 0))

    def _where(self, file_id):
        fid = int(file_id)
        if fid in world.session_table().index:
            return fid, f'ecephys_session_{fid}.nwb'
        p = world.probe_table().loc[fid]
        return int(p['ecephys_session_id']), f"probe_{p['name']}_lfp.nwb"

    def get_file_attributes(self, file_id):
        return types.SimpleNamespace(local_path=pathlib.Path(self.owner._path(*self._where(file_id))),
                                     file_hash=hashlib.blake2b(PLACEHOLDER.encode()).hexdigest())

    def download_data(self, file_id):
        time.sleep(float(os.environ.get('FAKE_VBN_DOWNLOAD_SECS', 0)))
        if self.failures_left > 0:               # like a dropped connection: a partial file, then an error
            self.failures_left -= 1
            path = self.get_file_attributes(file_id).local_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'partial')
            raise IOError('0 read, but total bytes expected is 1000 (simulated)')
        return self.owner._touch(*self._where(file_id))


class _FetchApi:
    def __init__(self, owner):
        self.cache = _FileCache(owner)


class VisualBehaviorNeuropixelsProjectCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.fetch_api = _FetchApi(self)

    @classmethod
    def from_s3_cache(cls, cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
        return cls(cache_dir)

    def load_latest_manifest(self):
        pass

    def current_manifest(self):
        return f'{FAKE_MANIFEST} (FAKE DATA, scenario={lfp.scenario()})'

    def get_ecephys_session_table(self):
        return world.session_table()

    def get_probe_table(self):
        return world.probe_table()

    def get_channel_table(self):
        return world.channel_table()

    def get_ecephys_session(self, ecephys_session_id):
        return _Session(self, ecephys_session_id)

    def _path(self, session_id, filename):
        return os.path.join(self.cache_dir, RELEASE, 'behavior_ecephys_sessions', str(session_id), filename)

    def _touch(self, session_id, filename):
        path = self._path(session_id, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):             # like AllenSDK: any file that exists counts as already downloaded
            with open(path, 'wb') as fh:
                fh.write(PLACEHOLDER.encode())
        return path
