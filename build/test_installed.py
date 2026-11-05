"""
Test the real installer from start to finish:   python build/test_installed.py [--real]

Installs silently into a temporary folder, with a temporary home folder, so nothing touches the real install or
the real data folder. Then: checks the launcher opens a window (the one window this test shows, for a few seconds), runs the whole demo flow with the installed
Python with its window hidden, optionally (--real) does one real download and analysis (about 8 GB, deleted afterwards), and uninstalls.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

BUILD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BUILD)
SMOKE = os.path.join(BUILD, 'installed_smoke.py')
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0     # no console pop-ups


def powershell(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True,
                          creationflags=NO_WINDOW).stdout.strip()


def window_title(install):
    pattern = install.replace("'", "''")
    return powershell(f"Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{pattern}*' }}"
                      " | ForEach-Object { $_.MainWindowTitle }")


def stop_window(install):
    pattern = install.replace("'", "''")
    powershell(f"Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{pattern}*' }}"
               " | ForEach-Object { [void]$_.CloseMainWindow() }")


def run_smoke(args, env, cwd):
    """Run the hidden test driver, passing on its output line by line; a failure stops the test."""
    p = subprocess.Popen(args, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors='replace', creationflags=NO_WINDOW)
    for line in p.stdout:
        print(line, end='', flush=True)
    if p.wait() != 0:
        raise SystemExit(f'the test driver failed (exit {p.returncode})')


def screenshot(path, title='P4 Analysis', front=True):
    """Save a picture of just that window, bringing it to the front first if asked."""
    import ctypes
    from ctypes import wintypes

    from PIL import ImageGrab
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    hwnd = user32.FindWindowW(None, title)
    if front:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(1)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ImageGrab.grab((rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--real', action='store_true', help='also do one real download and analysis (about 8 GB)')
    ap.add_argument('--keep-after-failure', action='store_true')
    ap.add_argument('--screenshot', help='save a picture of the installed window here (needs Pillow)')
    a = ap.parse_args()
    setups = sorted(glob.glob(os.path.join(ROOT, 'dist', 'P4-Analysis-Setup-*.exe')), key=os.path.getmtime)
    if not setups:
        sys.exit('No installer in dist/. Run build/build_installer.py first.')
    setup = setups[-1]
    work = tempfile.mkdtemp(prefix='p4_install_test_')
    install, home = os.path.join(work, 'P4 Analysis'), os.path.join(work, 'home')
    os.makedirs(home)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(USERPROFILE=home, HOME=home)
    ok = False
    try:
        print(f'Installing {os.path.basename(setup)} into {install}', flush=True)
        t0 = time.time()
        subprocess.run([setup, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/DIR={install}', '/TASKS=',
                        f'/LOG={os.path.join(work, "install.log")}'], check=True)
        print(f'PASS  installed silently, without administrator rights, in {time.time() - t0:.0f} s', flush=True)
        exe = os.path.join(install, 'P4 Analysis.exe')
        for must in (exe, os.path.join(install, 'runtime', 'pythonw.exe'), os.path.join(install, 'vbn_p4.py'),
                     os.path.join(install, 'tests', 'fake_allensdk', 'fake_vbn', 'world.py')):
            assert os.path.exists(must), must
        assert not os.path.exists(os.path.join(install, 'p4_out')) and not os.path.exists(os.path.join(install, 'vbn_cache'))
        print('PASS  program files are in place, with no downloads or results bundled', flush=True)

        t0 = time.time()
        subprocess.Popen([exe], env=env, cwd=home)
        title, splash_at = '', None
        while time.time() - t0 < 120 and title != 'P4 Analysis':
            time.sleep(0.2)
            title = window_title(install)
            if title == 'P4 Analysis starting' and splash_at is None:
                splash_at = time.time() - t0
                if a.screenshot:
                    screenshot(a.screenshot + '.splash.png', title, front=False)
        assert title == 'P4 Analysis', f'no window after 120 s; title={title!r}'
        assert splash_at is not None, 'the main window appeared without a starting splash'
        print(f'PASS  the "Starting" splash appeared in {splash_at:.1f} s, '
              f'the window in {time.time() - t0:.1f} s', flush=True)
        time.sleep(3)
        if a.screenshot:
            screenshot(a.screenshot)
        stop_window(install)
        time.sleep(3)
        assert not window_title(install), 'window did not close'
        print('PASS  the window closed cleanly', flush=True)

        python = os.path.join(install, 'runtime', 'python.exe')
        demo_env = dict(env, FAKE_VBN_MICE='5', FAKE_VBN_FLASHES='300')
        run_smoke([python, '-W', 'ignore', SMOKE, 'demo'], demo_env, install)
        if a.real:
            shutil.rmtree(os.path.join(home, 'P4 Analysis'), ignore_errors=True)
            band = os.path.join(ROOT, 'p4_out', 'band.json')
            run_smoke([python, '-W', 'ignore', SMOKE, 'real', band], env, install)

        keep = os.path.join(home, 'P4 Analysis')
        had_data = os.path.isdir(keep)
        uninstaller = os.path.join(install, 'unins000.exe')
        subprocess.run([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'], check=True)
        t0 = time.time()
        while os.path.exists(uninstaller) and time.time() - t0 < 120:
            time.sleep(1)
        leftover = [os.path.join(d, f) for d, _, fs in os.walk(install) for f in fs] if os.path.exists(install) else []
        assert not leftover, f'{len(leftover)} files left after uninstalling, e.g. {leftover[:3]}'
        assert not had_data or os.path.isdir(keep), 'uninstalling removed the data folder'
        print('PASS  uninstalled completely; the data folder was left alone', flush=True)
        ok = True
        print('\nALL INSTALLER CHECKS PASSED', flush=True)
    finally:
        stop_window(install)
        if ok or not a.keep_after_failure:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print('Kept for inspection:', work)


if __name__ == '__main__':
    main()
