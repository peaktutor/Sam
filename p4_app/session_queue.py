"""
Runs step 4 one session at a time (`vbn_p4.py analyze --session ID`), so that between sessions the app can
delete the finished session's LFP files, check disk space, show progress and stop cleanly.

GUI-agnostic: the window asks next() for the command to run, and calls finished() with the exit code.
"""
import os
import time

from . import settings as cfg
from . import storage


class SessionQueue:
    def __init__(self, session_ids, out, cache, max_probes=1, cleanup=True, check_space=True, margin_gb=5.0,
                 out_for=None):
        """`out_for(session)` gives a session's output folder when they differ (sample runs are kept apart from
        test results); otherwise every session uses `out`."""
        self.out_for = out_for or (lambda sid: out)
        self.pending = [int(s) for s in session_ids if not storage_has_result(self.out_for(int(s)), s)]
        self.total = len(self.pending)
        self.out, self.cache = out, cache
        self.max_probes, self.cleanup, self.check_space, self.margin_gb = max_probes, cleanup, check_space, margin_gb
        self.current = None
        self.started_at = None
        self.durations = []             # seconds per finished session, this run
        self.results = []               # (session, 'ok' | 'error' | 'failed', detail)
        self.freed_gb = 0.0
        self.stop_requested = False     # "stop after this session"
        self.paused_reason = None       # set when the queue stops on its own (disk full, crash)

    # ---------------------------------------------------------------- driving
    def next(self):
        """The next session to run, or None (queue finished, stop requested, or paused)."""
        if self.stop_requested or self.paused_reason or not self.pending:
            return None
        if self.check_space:
            need = cfg.space_needed_gb('analyze', self.max_probes) + self.margin_gb
            free = cfg.free_gb(self.cache)
            if free is not None and free < need:
                self.paused_reason = (f'Paused: the cache drive has {free:.0f} GB free and a session needs about '
                                      f'{need:.0f} GB. Free some space, then start it again to continue.')
                return None
        self.current = self.pending.pop(0)
        self.started_at = time.time()
        return self.current

    def finished(self, exit_code):
        """Record the session that just ended. Returns a one-line summary for the log."""
        sid, secs = self.current, time.time() - (self.started_at or time.time())
        self.current = None
        if exit_code != 0:
            # a crash or a kill: put it back so a later run retries it, and stop the queue
            self.pending.insert(0, sid)
            self.paused_reason = self.paused_reason or (
                f'Stopped: session {sid} ended with exit code {exit_code}. The log has the details.')
            self.results.append((sid, 'failed', f'exit {exit_code}'))
            return f'session {sid} did not finish (exit {exit_code})'
        self.durations.append(secs)
        out = self.out_for(sid)
        if storage.result_ok(out, sid):
            msg = f'session {sid} done in {secs / 60:.1f} min'
            self.results.append((sid, 'ok', ''))
            if self.cleanup:
                n, gb = storage.delete_lfp(self.cache, out, sid)
                self.freed_gb += gb
                if n:
                    msg += f'; deleted {n} LFP file{"s" if n > 1 else ""} ({gb:.1f} GB)'
        else:
            msg = f'session {sid}: no usable result (kept its files for checking)'
            self.results.append((sid, 'error', ''))
        return msg

    def stop_after_current(self):
        self.stop_requested = True

    # ---------------------------------------------------------------- progress
    @property
    def done_count(self):
        return len([r for r in self.results if r[1] != 'failed'])

    @property
    def active(self):
        return self.current is not None

    def eta_seconds(self):
        """Remaining time from the mean duration of the sessions finished in this run (None until one finishes)."""
        if not self.durations:
            return None
        left = len(self.pending) + (1 if self.current else 0)
        mean = sum(self.durations) / len(self.durations)
        elapsed = time.time() - self.started_at if self.current and self.started_at else 0
        return max(left * mean - min(elapsed, mean), 0)


def storage_has_result(out, session_id):
    """Any result file, with or without an error: vbn_p4.py skips those sessions itself."""
    return os.path.exists(os.path.join(out, 'sessions', f'{int(session_id)}.json'))
