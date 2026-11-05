"""
App settings (saved to app_settings.json next to the app), the Python-environment check, and disk space.

The interpreter that runs vbn_p4.py is a separate Python (the conda env with AllenSDK 2.16), so it is checked
by running it, never by importing anything into the app.
"""
import json
import os
import shutil
import subprocess
import sys

from .runner import DATA_ROOT, INSTALLED, ROOT, RUNTIME_PYTHON

DEFAULTS = dict(python='', cache=os.path.join(DATA_ROOT, 'vbn_cache'), out=os.path.join(DATA_ROOT, 'p4_out'),
                max_probes=1, demo=True, scenario='reset', cleanup=True)
FILENAME = 'app_settings.json'
ALLENSDK_WANTED = '2.16'
DEMO_MODULES = ('numpy', 'scipy', 'pandas', 'xarray')     # what vbn_p4.py needs besides allensdk


def load(folder=DATA_ROOT):
    """Saved settings merged over the defaults; a missing or damaged file gives the defaults."""
    s = dict(DEFAULTS)
    try:
        with open(os.path.join(folder, FILENAME), encoding='utf-8') as fh:
            saved = json.load(fh)
        s.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    # an installed app may have moved or been reinstalled: a saved interpreter that has gone falls back to its own
    if not s['python'] or (INSTALLED and not os.path.isfile(s['python'])):
        s['python'] = detect_python() or sys.executable
    return s


def save(settings, folder=DATA_ROOT):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, FILENAME)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({k: settings[k] for k in DEFAULTS}, fh, indent=1)
    os.replace(tmp, path)                   # never leaves a half-written settings file


def python_candidates(env_name='vbn'):
    """The Python bundled by the installer, the project's own .venv-vbn (made with uv), then where
    `conda create -n vbn` usually puts it."""
    local = [RUNTIME_PYTHON, os.path.join(ROOT, '.venv-vbn', 'Scripts', 'python.exe'),
             os.path.join(ROOT, '.venv-vbn', 'bin', 'python')]
    home = os.path.expanduser('~')
    roots = [os.path.join(home, d) for d in ('miniconda3', 'anaconda3', 'miniforge3', 'mambaforge', '.conda')]
    if sys.platform == 'win32':
        roots += [os.path.join(os.environ.get('LOCALAPPDATA', ''), 'miniconda3'),
                  r'C:\ProgramData\miniconda3', r'C:\ProgramData\anaconda3']
        exe = 'python.exe'
    else:
        roots += ['/opt/miniconda3', '/opt/anaconda3', '/opt/homebrew/Caskroom/miniconda/base']
        exe = os.path.join('bin', 'python')
    return local + [os.path.join(r, 'envs', env_name, exe) for r in roots]


def detect_python():
    return next((p for p in python_candidates() if os.path.isfile(p)), None)


_CHECK = (
    "import sys, json, importlib\n"
    "out = dict(python=sys.version.split()[0], modules={})\n"
    "for m in ('allensdk', 'pynwb', 'hdmf') + %r:\n"
    "    try:\n"
    "        out['modules'][m] = getattr(importlib.import_module(m), '__version__', '?')\n"
    "    except Exception:\n"
    "        out['modules'][m] = None\n"
    "print(json.dumps(out))\n" % (DEMO_MODULES,))


def check_python(python, timeout=180):
    """Run `python` and report what it has. Returns a dict:
        exists, python (version), modules {name: version or None},
        real_ok (AllenSDK present and not the test stand-in), demo_ok (numpy, scipy, pandas, xarray present),
        message (one plain sentence for the window), error (if it could not run)."""
    r = dict(path=python, exists=bool(python) and os.path.isfile(python), python=None, modules={},
             real_ok=False, demo_ok=False, error=None)
    if not r['exists']:
        r['error'] = 'not found'
        r['message'] = 'No Python interpreter at this path.'
        return r
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}      # never pick up the fake AllenSDK
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        p = subprocess.run([python, '-c', _CHECK], capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL, creationflags=flags)
        info = json.loads(p.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        r['error'] = f'no answer after {timeout} s'
    except (OSError, ValueError, IndexError) as e:
        r['error'] = str(e) or type(e).__name__
    if r['error']:
        r['message'] = f'Could not run this interpreter ({r["error"]}).'
        return r
    r['python'], r['modules'] = info['python'], info['modules']
    sdk = r['modules'].get('allensdk')
    r['demo_ok'] = all(r['modules'].get(m) for m in DEMO_MODULES)
    nwb_problem = nwb_reader_problem(r['modules'])
    r['real_ok'] = bool(sdk) and 'fake' not in sdk and r['demo_ok'] and not nwb_problem
    missing = [m for m in DEMO_MODULES if not r['modules'].get(m)]
    if sdk and 'fake' not in sdk and nwb_problem:
        r['message'] = f'Python {r["python"]} with AllenSDK {sdk}, but {nwb_problem}'
    elif r['real_ok']:
        r['message'] = f'Python {r["python"]} with AllenSDK {sdk}. Ready for real data.'
        if not sdk.startswith(ALLENSDK_WANTED):
            r['message'] += f' (The script was checked against AllenSDK {ALLENSDK_WANTED}.)'
    elif r['demo_ok']:
        r['message'] = f'Python {r["python"]} without AllenSDK: demo data only.'
    else:
        r['message'] = f'Python {r["python"]} is missing {", ".join(missing)}.'
    return r


def nwb_reader_problem(modules):
    """AllenSDK 2.16 pins neither pynwb nor (much of) hdmf, so an installer can pick an hdmf that pynwb 2.x
    cannot use; the NWB files then fail to open ("Can't instantiate abstract class NWBFile"). Returns a
    sentence describing the problem, or None."""
    hdmf, pynwb = modules.get('hdmf'), modules.get('pynwb')
    if not hdmf or not pynwb:
        return None
    try:
        h_major, p_major = int(hdmf.split('.')[0]), int(pynwb.split('.')[0])
    except ValueError:
        return None
    if p_major < 3 and h_major >= 4:
        return (f'hdmf {hdmf} is too new for pynwb {pynwb}: the session files will not open. '
                f'Fix: {HDMF_FIX}')
    return None


RAM_MINIMUM, RAM_RECOMMENDED = 16, 32       # the guide, Section 2


def ram_gb():
    """Installed memory in GB (Windows), or None if it cannot be read."""
    if sys.platform != 'win32':
        return None
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + \
                   [(n, ctypes.c_ulonglong) for n in ('total', 'avail', 'pfTotal', 'pfAvail', 'vTotal', 'vAvail', 'x')]
    s = Status(dwLength=ctypes.sizeof(Status))
    return s.total / 2 ** 30 if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)) else None


def free_gb(path):
    """Free space (GB) on the drive that holds `path`, even if the folder does not exist yet."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    return shutil.disk_usage(p).free / 1e9


def space_needed_gb(step, max_probes=1):
    """Rough download size to start a step (from the guide's table). Analyze is per session."""
    return {'list': 0.1, 'band': 50.0, 'analyze': 3.0 + 4.7 * max_probes}.get(step, 0.0)


INSTALL_COMMANDS = ('uv python install 3.10',
                    'uv venv .venv-vbn --python 3.10',
                    'uv pip install --python .venv-vbn\\Scripts\\python.exe allensdk==2.16.2 hdmf==3.14.6 scipy xarray')
HDMF_FIX = 'uv pip install --python <this python> hdmf==3.14.6 (or pip install hdmf==3.14.6 inside the environment)'
