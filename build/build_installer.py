"""
Build the Windows installer:  python build/build_installer.py        (about 15 minutes)

  1. stage/runtime: a copy of a portable Python 3.10 (from uv) with the exact packages in requirements-lock.txt
  2. stage/: the app, vbn_p4.py, the tools and the demo data generator; no downloads or results
  3. stage/P4 Analysis.exe: the launcher, compiled with the C# compiler that ships with Windows
  4. dist/P4-Analysis-Setup-<version>.exe: the installer, compiled with Inno Setup

Needs: uv and Inno Setup 6 (winget install JRSoftware.InnoSetup). Output is unsigned, so Windows SmartScreen warns.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

BUILD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BUILD)
STAGE = os.path.join(BUILD, 'stage')
RUNTIME = os.path.join(STAGE, 'runtime')
PYTHON_VERSION = '3.10'
APP_PARTS = ['p4_app', 'tools', os.path.join('tests', 'fake_allensdk')]
APP_FILES = ['vbn_p4.py', 'vbn_p4_original.py', 'README.md']
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0     # no console pop-ups
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', 'pilot_check.json')


def run(*args, **kw):
    print('  $', ' '.join(str(a) for a in args), flush=True)
    return subprocess.run(args, check=True, creationflags=NO_WINDOW, **kw)


def step(title):
    print(f'\n=== {title} ({time.strftime("%H:%M:%S")})', flush=True)


def version():
    with open(os.path.join(ROOT, 'p4_app', '__init__.py'), encoding='utf-8') as fh:
        return re.search(r"__version__ = '([^']+)'", fh.read()).group(1)


def find_tool(names, hint):
    for n in names:
        if os.path.isfile(n):
            return n
        found = shutil.which(n)
        if found:
            return found
    sys.exit(f'Could not find {names[0]}. {hint}')


def build_runtime(uv):
    step('Bundled Python')
    run(uv, 'python', 'install', PYTHON_VERSION)
    found = subprocess.run([uv, 'python', 'find', PYTHON_VERSION, '--no-project'], check=True, capture_output=True,
                           text=True, creationflags=NO_WINDOW).stdout.strip()
    shutil.copytree(os.path.dirname(found), RUNTIME)
    python = os.path.join(RUNTIME, 'python.exe')
    run(uv, 'pip', 'install', '--python', python, '--break-system-packages', '-r',
        os.path.join(BUILD, 'requirements-lock.txt'))
    # entry-point scripts hold absolute paths and are never used; headers and import libs are only for compiling
    for name in ('Scripts', 'include', 'libs'):
        shutil.rmtree(os.path.join(RUNTIME, name), ignore_errors=True)
    return python


def copy_app():
    step('App files')
    for part in APP_PARTS:
        shutil.copytree(os.path.join(ROOT, part), os.path.join(STAGE, part), ignore=IGNORE)
    for f in APP_FILES:
        shutil.copy2(os.path.join(ROOT, f), STAGE)
    if os.path.isdir(os.path.join(ROOT, 'docs')):
        shutil.copytree(os.path.join(ROOT, 'docs'), os.path.join(STAGE, 'docs'))
    # AllenSDK's license: binary redistribution must carry its notice in the documentation (and no commercial use)
    licences = os.path.join(STAGE, 'third_party_licenses')
    os.makedirs(licences)
    site = os.path.join(RUNTIME, 'Lib', 'site-packages')
    shutil.copy2(os.path.join(site, 'allensdk-2.16.2.dist-info', 'LICENSE.txt'),
                 os.path.join(licences, 'AllenSDK-LICENSE.txt'))
    shutil.copy2(os.path.join(RUNTIME, 'LICENSE.txt'), os.path.join(licences, 'Python-LICENSE.txt'))


def prepare(python):
    step('Compile Python files, so the first start is not slow')
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    cmd = [python, '-m', 'compileall', '-q', '-j', '0', os.path.join(RUNTIME, 'Lib'), os.path.join(STAGE, 'p4_app')]
    # The compile's workers import modules from this same folder. Writing .pyc files for those imports races with
    # compiling the same modules (Access is denied on Windows), so imports write none; compileall itself still does.
    compile_env = dict(env, PYTHONDONTWRITEBYTECODE='1')
    for attempt in (1, 2, 3):                   # a virus scanner can briefly lock a file: only the failed ones repeat
        done = subprocess.run(cmd, env=compile_env, capture_output=True, text=True, errors='replace',
                              creationflags=NO_WINDOW)
        if done.returncode == 0:
            break
        print(f'  compile attempt {attempt} failed:\n{(done.stdout + done.stderr)[-1500:]}', flush=True)
    else:
        sys.exit('Compiling the Python files failed 3 times; see above.')
    # pynwb caches its type map on first import; build it now, with the pinned hdmf
    for stale in glob.glob(os.path.join(RUNTIME, 'Lib', 'site-packages', 'pynwb', '*typemap*')):
        os.remove(stale)
    check = 'import allensdk, pynwb, hdmf, scipy, xarray, pandas, matplotlib, tkinter; ' \
            'print("allensdk", allensdk.__version__, "hdmf", hdmf.__version__, "pynwb", pynwb.__version__)'
    run(python, '-W', 'ignore', '-c', check, env=env)


def build_launcher():
    step('Launcher')
    csc = find_tool([r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'], 'It ships with Windows.')
    run(csc, '/nologo', '/target:winexe', '/optimize+', f'/out:{os.path.join(STAGE, "P4 Analysis.exe")}',
        f'/win32icon:{os.path.join(ROOT, "p4_app", "assets", "p4.ico")}', '/r:System.Windows.Forms.dll',
        os.path.join(BUILD, 'launcher.cs'))


def build_setup(ver):
    step('Installer')
    iscc = find_tool(['ISCC', os.path.expandvars(r'%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe'),
                      r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'],
                     'Install it with: winget install JRSoftware.InnoSetup')
    run(iscc, '/Qp', f'/DAppVersion={ver}', os.path.join(BUILD, 'p4_analysis.iss'))
    return os.path.join(ROOT, 'dist', f'P4-Analysis-Setup-{ver}.exe')


def report(setup):
    longest = max((os.path.join(d, f) for d, _, fs in os.walk(STAGE) for f in fs), key=len)
    rel = os.path.relpath(longest, STAGE)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(STAGE) for f in fs)
    print(f'\nInstalled size: {total / 1e9:.2f} GB   Installer: {os.path.getsize(setup) / 1e9:.2f} GB')
    print(f'Longest path under the install folder: {len(rel)} characters')
    print('Output:', setup)


def main():
    t0 = time.time()
    uv = find_tool(['uv'], 'Install it from https://docs.astral.sh/uv/')
    shutil.rmtree(STAGE, ignore_errors=True)
    os.makedirs(STAGE)
    ver = version()
    python = build_runtime(uv)
    copy_app()
    prepare(python)
    build_launcher()
    setup = build_setup(ver)
    report(setup)
    print(f'Done in {(time.time() - t0) / 60:.1f} min')


if __name__ == '__main__':
    main()
