"""
Phase 2 checks: the runner streams output live, handles progress-bar redraws, records exit codes and logs,
can be stopped, and the window keeps processing events while a real step runs.

    .venv-test\\Scripts\\python -m pytest tests/test_app_runner.py -v      (about 15 s)
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from p4_app.runner import SCRIPT, StepRunner, demo_env, step_command   # noqa: E402


def collect(runner, timeout=20):
    """Drain messages until the exit message; returns (messages, seconds until the first message)."""
    msgs, first, t0 = [], None, time.time()
    while time.time() - t0 < timeout:
        try:
            m = runner.messages.get(timeout=0.1)
        except Exception:
            continue
        if first is None:
            first = time.time() - t0
        msgs.append(m)
        if m[0] == 'exit':
            return msgs, first
    raise AssertionError(f'no exit message after {timeout} s: {msgs}')


def child(code):
    return [sys.executable, '-u', '-c', code]


def test_lines_progress_exit_code_and_log(tmp_path):
    log = tmp_path / 'logs' / 'x.log'
    r = StepRunner()
    r.start(child("import sys\nprint('first')\nsys.stdout.write('10%\\r50%\\r100%\\n')\nprint('last')\nsys.exit(3)"),
            log_path=str(log))
    msgs, _ = collect(r)
    assert [m for m in msgs if m[0] == 'progress'] == [('progress', '10%'), ('progress', '50%')]
    assert [m[1] for m in msgs if m[0] == 'line'] == ['first', '100%', 'last']
    assert msgs[-1][0] == 'exit' and msgs[-1][1] == 3
    text = log.read_text(encoding='utf-8')
    body = [ln for ln in text.splitlines() if not ln.startswith('#') and not ln.startswith(' ')]
    assert body[-3:] == ['first', '100%', 'last'] and '# exit code 3' in text
    assert not any('50%' == ln for ln in text.splitlines())       # progress redraws are not logged


def test_output_arrives_while_process_is_still_running():
    r = StepRunner()
    r.start(child("import time\nprint('early')\ntime.sleep(3)\nprint('late')"))
    t0 = time.time()
    m = r.messages.get(timeout=10)
    assert m == ('line', 'early') and r.is_running and time.time() - t0 < 2.5
    collect(r)


def test_stop_kills_a_running_step():
    r = StepRunner()
    r.start(child("import time\nprint('sleeping', flush=True)\ntime.sleep(60)"))
    assert r.messages.get(timeout=10) == ('line', 'sleeping')
    t0 = time.time()
    r.stop()
    msgs, _ = collect(r, timeout=10)
    assert msgs[-1][1] != 0 and time.time() - t0 < 5 and not r.is_running


def test_missing_interpreter_reports_instead_of_crashing():
    r = StepRunner()
    r.start(['C:/no/such/python.exe', '-u', SCRIPT, 'list'])
    msgs, _ = collect(r)
    assert msgs[-1] == ('exit', None, msgs[-1][2]) and 'could not start' in msgs[0][1]


def test_step_commands_match_the_guide():
    assert step_command('py', 'list', 'C', 'O')[2:] == [SCRIPT, 'list', '--out', 'O', '--cache', 'C']
    assert step_command('py', 'analyze', 'C', 'O', session=1064644573, max_probes=1)[-4:] == \
        ['--session', '1064644573', '--max-probes', '1']
    assert '--cache' not in step_command('py', 'group', 'C', 'O')
    with pytest.raises(ValueError):
        step_command('py', 'delete-everything', 'C', 'O')


def test_window_stays_responsive_during_a_real_step(tmp_path):
    from test_app_settings import make_root
    root = make_root()
    from p4_app.gui import App
    root.withdraw()
    app = App(root, demo=True, base=str(tmp_path), python=sys.executable)
    try:
        app.run_step('list')
        t0 = time.time()
        while (app.runner.is_running or not app.runner.messages.empty()) and time.time() - t0 < 60:
            root.update()
            time.sleep(0.01)
        root.update()
        log = app.log.get('1.0', 'end')
        assert 'sessions,' in log and 'list finished (exit 0)' in log, log
        assert app.polls_while_running >= 5         # the Tk event loop kept ticking during the run
        assert os.path.exists(tmp_path / 'demo_run' / 'p4_out' / 'split.json')
        logs = os.listdir(tmp_path / 'demo_run' / 'p4_out' / 'logs')
        assert len(logs) == 1 and logs[0].startswith('list_')
        assert str(app.run_btn['state']) == 'normal' and str(app.stop_btn['state']) == 'disabled'
    finally:
        root.destroy()
