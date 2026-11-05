"""
Download Visual Behavior Neuropixels session files without loading them, so a large selection needs little memory.

    python tools/download_sessions.py --cache CACHE --session ID [--session ID ...] [--max-probes N]

For each session it fetches the session file and the LFP files of the first N visual-cortex probes: the same
probes, in the same order, that vbn_p4.py analyses. Complete files already in the cache are not fetched again;
incomplete ones, left by an interrupted download, are.

Files come over several connections at once, in small pieces. Amazon limits each connection, so that is 2-3 times
faster on a slow line, and a stopped download carries on from the pieces it already has (kept beside the file, as
NAME.part and NAME.part.done, until the whole file has passed its hash check and takes its final name).
Exit code: 0 if every file arrived, 1 otherwise.
"""
import argparse
import hashlib
import http.client
import math
import os
import queue
import sys
import threading
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import vbn_p4  # noqa: E402  (imported, never modified)

RETRIES = 5
RETRY_WAIT_S = float(os.environ.get('P4_RETRY_WAIT_S', 30))
CONNECTIONS = int(os.environ.get('P4_DOWNLOAD_CONNECTIONS', 8))
PIECE_BYTES = 8_000_000
TIMEOUT_S = 60


def session_file_ids(cache, session_id, max_probes):
    """[(description, file id)] for one session, session file first."""
    row = cache.get_ecephys_session_table().loc[int(session_id)]
    files = [('session file', str(int(row['file_id'])))]
    probes = cache.get_probe_table()
    for pid in vbn_p4.visual_probes(cache, int(session_id)).index[:max_probes]:
        files.append((f"LFP of {probes.loc[pid, 'name']}", str(int(probes.loc[pid, 'file_id']))))
    return files


def file_hash(path):
    """Blake2b, the hash in the dataset's manifest."""
    h = hashlib.blake2b()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 24), b''):
            h.update(block)
    return h.hexdigest()


def remote_size(url):
    """The file's size, or None if the server will not send parts of it."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=0-0'}), timeout=TIMEOUT_S) as r:
            return int(r.headers['Content-Range'].rsplit('/', 1)[1]) if r.status == 206 else None
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        return None


def _time_left(seconds):
    return f'{seconds / 3600:.1f} h' if seconds >= 3600 else f'{seconds / 60:.0f} min'


def download_in_pieces(url, path, expected_hash, size, retries=RETRIES, wait=RETRY_WAIT_S, connections=None):
    """Fetch url to path over several connections. Raises if a piece cannot be had after its retries or the finished
    file fails its hash check; the pieces that arrived are kept for next time unless the hash failed."""
    path, part = str(path), str(path) + '.part'
    done_path = part + '.done'
    pieces = math.ceil(size / PIECE_BYTES)
    done = set()
    if os.path.exists(part) and os.path.getsize(part) == size and os.path.exists(done_path):
        with open(done_path) as fh:
            done = {int(x) for x in fh.read().split() if int(x) < pieces}
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(part, 'wb') as fh:
            fh.truncate(size)
        open(done_path, 'w').close()
    todo = queue.Queue()
    for i in range(pieces):
        if i not in done:
            todo.put(i)
    lock = threading.Lock()
    stop = threading.Event()
    errors = []
    new_bytes = [0]

    def get_piece(i, fh):
        start = i * PIECE_BYTES
        end = min(start + PIECE_BYTES, size) - 1
        got = 0
        fh.seek(start)
        try:
            req = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                if r.status != 206:
                    raise IOError(f'the server sent status {r.status}, not the part asked for')
                while True:
                    block = r.read(1 << 16)
                    if not block:
                        break
                    fh.write(block)
                    got += len(block)
                    with lock:
                        new_bytes[0] += len(block)
            if got != end - start + 1:
                raise IOError(f'{got} of {end - start + 1} bytes arrived')
        except Exception:
            with lock:
                new_bytes[0] -= got           # this piece starts again, so its bytes do not count
            raise
        fh.flush()
        with lock, open(done_path, 'a') as log:
            log.write(f'{i}\n')

    def worker():
        with open(part, 'r+b') as fh:
            while not stop.is_set():
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                for attempt in range(1, retries + 1):
                    try:
                        get_piece(i, fh)
                        break
                    except (OSError, http.client.HTTPException) as e:
                        if attempt == retries:
                            errors.append(e)
                            stop.set()
                            return
                        time.sleep(wait * attempt)
                        if stop.is_set():
                            return

    name = os.path.basename(path)
    already = len(done) * PIECE_BYTES
    if done:
        print(f'    carrying on from {min(already, size) / size:.0%}', flush=True)
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(connections or CONNECTIONS)]
    t0 = last = time.time()
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(0.1)
        if time.time() - last >= 2:
            last = time.time()
            with lock:
                new = new_bytes[0]
            rate = new / (last - t0)
            have = min(already + new, size)
            left = f', about {_time_left((size - have) / rate)} left' if rate > 0 else ''
            print(f'    {name}: {have / size:.0%} of {size / 1e9:.2f} GB, {rate / 1e6:.2f} MB/s{left}', end='\r', flush=True)
    if errors:
        raise errors[0]
    print(f'    {name}: all {size / 1e9:.2f} GB here, checking it', flush=True)
    if file_hash(part) != expected_hash:
        os.remove(part)
        os.remove(done_path)
        raise IOError('the finished file failed its hash check')
    os.replace(part, path)
    os.remove(done_path)


def fetch(file_cache, file_id, retries=RETRIES, wait=RETRY_WAIT_S):
    """Download one file, unless a complete copy is here. AllenSDK writes straight to the final filename and treats
    any file that exists as complete, so an interrupted download must be caught here, by its hash. A dropped
    connection (a long download will meet one) is retried, waiting longer each time. If the server will not send
    parts of the file, AllenSDK fetches it whole."""
    attrs = file_cache.get_file_attributes(file_id)
    if attrs.local_path.exists():
        if file_hash(attrs.local_path) == attrs.file_hash:
            print('    already here and complete', flush=True)
            return
        print('    incomplete (an interrupted download): fetching it again', flush=True)
        attrs.local_path.unlink()
    url = getattr(attrs, 'url', None)
    if url and getattr(attrs, 'version_id', None):
        url += f'?versionId={attrs.version_id}'      # the release's own copy of the file, as AllenSDK asks for
    for attempt in range(1, retries + 1):
        try:
            size = remote_size(url) if url else None
            if size:
                download_in_pieces(url, attrs.local_path, attrs.file_hash, size, retries, wait)
            else:
                file_cache.download_data(file_id)
            return
        except Exception as e:
            if attrs.local_path.exists():
                attrs.local_path.unlink()
            if attempt == retries:
                raise
            print(f'    connection problem ({e}): retrying in {wait * attempt} s '
                  f'(attempt {attempt + 1} of {retries})', flush=True)
            time.sleep(wait * attempt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', required=True)
    ap.add_argument('--session', type=int, action='append', required=True)
    ap.add_argument('--max-probes', dest='max_probes', type=int, default=1)
    a = ap.parse_args()
    cache = vbn_p4.get_cache(a.cache)
    failed = []
    for i, sid in enumerate(a.session, 1):
        print(f'Session {i} of {len(a.session)}: {sid}', flush=True)
        try:
            for what, file_id in session_file_ids(cache, sid, a.max_probes):
                print(f'  {what}', flush=True)
                fetch(cache.fetch_api.cache, file_id)
        except Exception as e:                       # report it, carry on with the other sessions
            traceback.print_exc()
            print(f'  Error: could not download session {sid}: {e}', flush=True)
            failed.append(sid)
    if failed:
        print(f'Finished with {len(failed)} failed session(s): {failed}', flush=True)
        return 1
    print(f'All {len(a.session)} session(s) downloaded.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
