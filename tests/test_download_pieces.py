"""
The downloader's many-connection path, against a small local web server that sends parts of a file.

    .venv-test\\Scripts\\python -m pytest tests/test_download_pieces.py -v      (a few seconds)
"""
import hashlib
import http.server
import importlib.util
import os
import pathlib
import sys
import threading
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from p4_app import storage                                   # noqa: E402

spec = importlib.util.spec_from_file_location('download_sessions', os.path.join(ROOT, 'tools', 'download_sessions.py'))
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

BODY = os.urandom(2_500_000)
HASH = hashlib.blake2b(BODY).hexdigest()
PIECE = 300_000


class Server:
    """Sends parts of BODY. drop_next requests are cut off halfway; ranges=False ignores Range, like a plain server."""

    def __init__(self):
        outer = self
        self.drop_next = 0
        self.ranges = True
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get('Range')
                outer.requests.append(rng)
                if not (rng and outer.ranges):
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(BODY)))
                    self.end_headers()
                    self.wfile.write(BODY)
                    return
                start, end = (int(x) for x in rng.split('=')[1].split('-'))
                data = BODY[start:end + 1]
                self.send_response(206)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(BODY)}')
                self.end_headers()
                if outer.drop_next > 0 and len(data) > 1:
                    outer.drop_next -= 1
                    self.wfile.write(data[:len(data) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/file'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(dl, 'PIECE_BYTES', PIECE)
    s = Server()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def in_pieces(server, path, expected=HASH, retries=3):
    dl.download_in_pieces(server.url, path, expected, len(BODY), retries=retries, wait=0, connections=4)


def test_remote_size_asks_the_server(server):
    assert dl.remote_size(server.url) == len(BODY)
    server.ranges = False
    assert dl.remote_size(server.url) is None
    assert dl.remote_size('http://127.0.0.1:9/nothing-listens-here') is None


def test_pieces_make_the_whole_file_and_leave_nothing_behind(server, tmp_path):
    path = tmp_path / 'sub' / 'probe_probeA_lfp.nwb'
    in_pieces(server, path)
    assert path.read_bytes() == BODY
    assert sorted(f.name for f in path.parent.iterdir()) == [path.name]


def test_a_dropped_connection_only_repeats_that_piece(server, tmp_path):
    path = tmp_path / 'f.nwb'
    server.drop_next = 3
    in_pieces(server, path)
    assert path.read_bytes() == BODY
    assert len(server.requests) == 9 + 3            # 9 pieces, and 3 of them twice


def test_a_stopped_download_carries_on_from_the_pieces_it_has(server, tmp_path):
    path = tmp_path / 'f.nwb'
    part = tmp_path / 'f.nwb.part'
    part.write_bytes(BODY[:PIECE * 5] + b'\0' * (len(BODY) - PIECE * 5))
    (tmp_path / 'f.nwb.part.done').write_text('0\n1\n2\n3\n4\n')
    in_pieces(server, path)
    assert path.read_bytes() == BODY
    assert len(server.requests) == 4                # only the 4 missing pieces


def test_a_part_file_of_the_wrong_size_is_not_trusted(server, tmp_path):
    path = tmp_path / 'f.nwb'
    (tmp_path / 'f.nwb.part').write_bytes(b'junk')
    (tmp_path / 'f.nwb.part.done').write_text('0\n1\n2\n')
    in_pieces(server, path)
    assert path.read_bytes() == BODY and len(server.requests) == 9


def test_a_bad_hash_fails_and_starts_clean(server, tmp_path):
    path = tmp_path / 'f.nwb'
    with pytest.raises(IOError, match='hash'):
        in_pieces(server, path, expected='not-the-hash')
    assert list(tmp_path.iterdir()) == []


def test_giving_up_keeps_the_pieces_but_no_final_file(server, tmp_path):
    path = tmp_path / 'f.nwb'
    server.drop_next = 10_000
    with pytest.raises(OSError):
        in_pieces(server, path, retries=2)
    assert not path.exists() and (tmp_path / 'f.nwb.part').exists()


def file_cache(tmp_path, url):
    """Stand-in for AllenSDK's cache. download_data records that AllenSDK itself was used."""
    path = pathlib.Path(tmp_path / 'probe_probeA_lfp.nwb')
    used = []

    def download_data(file_id):
        used.append(file_id)
        path.write_bytes(BODY)
    return types.SimpleNamespace(
        get_file_attributes=lambda f: types.SimpleNamespace(local_path=path, file_hash=HASH, url=url, version_id='v1'),
        download_data=download_data), path, used


def test_fetch_uses_pieces_and_asks_for_the_release_version(server, tmp_path):
    cache, path, used = file_cache(tmp_path, server.url)
    seen = []
    real = dl.urllib.request.urlopen
    dl.urllib.request.urlopen = lambda req, **k: (seen.append(req.full_url), real(req, **k))[1]
    try:
        dl.fetch(cache, '1', retries=2, wait=0)
    finally:
        dl.urllib.request.urlopen = real
    assert path.read_bytes() == BODY and not used
    assert seen and all(u.endswith('?versionId=v1') for u in seen)


def test_fetch_falls_back_to_allensdk_if_the_server_will_not_send_parts(server, tmp_path):
    server.ranges = False
    cache, path, used = file_cache(tmp_path, server.url)
    dl.fetch(cache, '1', retries=2, wait=0)
    assert path.read_bytes() == BODY and used == ['1']


def test_unfinished_files_are_counted_and_deleted(tmp_path):
    d = tmp_path / 'cache' / 'visual-behavior-neuropixels-0.5.0' / 'behavior_ecephys_sessions' / '111'
    d.mkdir(parents=True)
    (d / 'ecephys_session_111.nwb.part').write_bytes(b'x' * 1000)
    (d / 'ecephys_session_111.nwb.part.done').write_text('0\n')
    (d / 'probe_probeA_lfp.nwb.part').write_bytes(b'x' * 1000)
    keep = d / 'notes.txt'
    keep.write_text('mine')
    cache = str(tmp_path / 'cache')
    rec = storage.inventory(cache)[111]
    assert rec['session_file'] is None and rec['lfp'] == [] and rec['gb'] > 0     # unfinished: not usable yet
    n, _ = storage.delete_session_files(cache, 111)
    assert n == 3 and sorted(f.name for f in d.iterdir()) == ['notes.txt']
