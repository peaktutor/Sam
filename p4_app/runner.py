"""
Run one vbn_p4.py step as a subprocess and stream its output without blocking the GUI.

vbn_p4.py is never imported or modified: the preregistration fixes it by SHA-256, so the app only runs it,
exactly as the guide's command lines do, and reads the files it writes.

Messages arrive on StepRunner.messages (a queue.Queue) as tuples:
    ('line', text)              a complete output line
    ('progress', text)          a line ended by a bare carriage return (download progress bars); it replaces
                                the previous progress line rather than adding a new one
    ('exit', returncode, secs)  the process has finished (returncode is None if it could not start)
"""
import datetime
import hashlib
import io
import os
import queue
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
SCRIPT = os.path.join(ROOT, 'vbn_p4.py')
FAKE_SDK = os.path.join(ROOT, 'tests', 'fake_allensdk')
DOWNLOADER = os.path.join(ROOT, 'tools', 'download_sessions.py')
RUNTIME_PYTHON = os.path.join(ROOT, 'runtime', 'python.exe')     # the Python bundled by the Windows installer
INSTALLED = os.path.exists(RUNTIME_PYTHON)
# An installed app keeps data out of its program folder. The home folder, not Documents: Documents is often synced
# by OneDrive, and the downloads run to many GB.
DATA_ROOT = os.path.join(os.path.expanduser('~'), 'P4 Analysis') if INSTALLED else ROOT
STEPS = ('list', 'band', 'analyze', 'group')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def step_command(python, step, cache, out, session=None, max_probes=None, script=SCRIPT):
    """The guide's command line for one step. `-u` makes print() output arrive line by line."""
    if step not in STEPS:
        raise ValueError(f'unknown step {step!r}')
    args = [python, '-u', script, step, '--out', out]
    if step != 'group':
        args += ['--cache', cache]
    if session is not None:
        args += ['--session', str(int(session))]
    if max_probes is not None:
        args += ['--max-probes', str(int(max_probes))]
    return args


def demo_env(scenario='reset', base=None):
    """Environment that makes vbn_p4.py import the synthetic AllenSDK in tests/fake_allensdk instead of the real one."""
    env = dict(os.environ if base is None else base)
    env.update(PYTHONPATH=FAKE_SDK, FAKE_VBN_SCENARIO=scenario, PYTHONWARNINGS='ignore::FutureWarning')
    return env


class StepRunner:
    def __init__(self):
        self.messages = queue.Queue()
        self._proc = None
        self._thread = None
        self._running = False

    @property
    def is_running(self):
        """False as soon as the 'exit' message is queued, so a new step can start straight away."""
        return self._running

    def start(self, args, env=None, log_path=None):
        """Start `args` in the background. Output goes to self.messages and, if given, to log_path."""
        if self.is_running:
            raise RuntimeError('a step is already running')
        env = dict(os.environ if env is None else env, PYTHONIOENCODING='utf-8')
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(list(args), env, log_path), daemon=True)
        self._thread.start()

    def stop(self):
        """Kill the running step. vbn_p4.py writes each result file only after a session is complete,
        so killing it mid-session loses that session's work but never leaves a half-written result."""
        p = self._proc
        if p is not None and p.poll() is None:
            p.kill()

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, args, env, log_path):
        t0 = time.time()
        code = None
        try:
            code = self._execute(args, env, log_path, t0)
        except Exception as e:                      # e.g. the log folder cannot be created
            self.messages.put(('line', f'runner error: {e!r}'))
        finally:
            self._proc = None
            self._running = False
            self.messages.put(('exit', code, time.time() - t0))

    def _execute(self, args, env, log_path, t0):
        """Run the process to completion; returns its exit code, or None if it could not start."""
        log = None
        try:
            if log_path:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                log = open(log_path, 'w', encoding='utf-8')
                log.write(f'# started {datetime.datetime.now().isoformat(timespec="seconds")}\n'
                          f'# command: {subprocess.list2cmdline(args)}\n')
                if os.path.exists(args[2] if len(args) > 2 else ''):
                    log.write(f'# script sha256: {sha256_file(args[2])}\n')
                log.flush()
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            try:
                self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, env=env, creationflags=flags)
            except OSError as e:
                self.messages.put(('line', f'could not start: {e}'))
                return None
            # newline='' keeps the line endings, so a bare '\r' (progress bar redraw) can be told apart from '\n'
            pending = None
            for raw in io.TextIOWrapper(self._proc.stdout, encoding='utf-8', errors='replace', newline=''):
                if raw.endswith('\r'):
                    pending = raw.rstrip('\r')
                    self.messages.put(('progress', pending))
                    continue
                line = raw.rstrip('\r\n')
                self.messages.put(('line', line))
                if log:                       # progress redraws are not logged: tqdm ends each bar with a full line
                    log.write(line + '\n')
                    log.flush()
                pending = None
            code = self._proc.wait()
            if log:
                if pending is not None:
                    log.write(pending + '\n')
                log.write(f'# exit code {code} after {time.time() - t0:.1f} s\n')
            return code
        finally:
            if log:
                log.close()


def log_path_for(out, step):
    return os.path.join(out, 'logs', f'{step}_{datetime.datetime.now():%Y%m%d-%H%M%S}.log')
