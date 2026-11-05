"""
Drives the window inside an installed copy, using the installed copy's own Python. Run by test_installed.py.

    <install>\\runtime\\python.exe <this file> demo         the whole flow on synthetic data
    <install>\\runtime\\python.exe <this file> real BAND    real step 1, then download and run one real pilot session
"""
import ctypes
import json
import os
import shutil
import sys
import time
from ctypes import wintypes

INSTALL = os.getcwd()
sys.path.insert(0, INSTALL)
import tkinter as tk                           # noqa: E402

from p4_app import gui, state, storage         # noqa: E402
from p4_app.runner import DATA_ROOT, INSTALLED, ROOT, RUNTIME_PYTHON   # noqa: E402

gui.messagebox.askyesno = lambda *a, **k: True


def pump(app, until, timeout):
    t0 = time.time()
    while not until():
        app.root.update()
        time.sleep(0.05)
        if time.time() - t0 > timeout:
            print(app.log.get('1.0', 'end')[-3000:])
            raise SystemExit(f'timed out after {timeout} s')
    app.root.update()


def idle(app):
    return not app.busy and app.runner.messages.empty() and app._after_ok is None


def check(ok, what):
    print(('PASS  ' if ok else 'FAIL  ') + what, flush=True)
    if not ok:
        raise SystemExit(1)


def loaded_dlls():
    """Full paths of every DLL loaded in this process."""
    kernel, psapi = ctypes.WinDLL('kernel32'), ctypes.WinDLL('psapi')
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    process = kernel.GetCurrentProcess()
    handles, needed = (wintypes.HMODULE * 4096)(), wintypes.DWORD()
    psapi.EnumProcessModules(process, handles, ctypes.sizeof(handles), ctypes.byref(needed))
    paths = []
    for handle in handles[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        name = ctypes.create_unicode_buffer(1024)
        psapi.GetModuleFileNameExW(process, handle, name, 1024)
        paths.append(name.value)
    return paths


def check_cpp_runtime():
    """A PC without Microsoft's C++ runtime must still work, so none of it may come from Windows. numexpr, loaded with
    pandas by the analysis, is the package that needs msvcp140.dll."""
    import numexpr, pandas, scipy.signal  # noqa: E401,F401
    runtime = [p for p in loaded_dlls() if os.path.basename(p).lower().startswith(('msvcp140', 'vcruntime140', 'concrt140'))]
    outside = [p for p in runtime if not os.path.normcase(p).startswith(os.path.normcase(INSTALL))]
    check(any(os.path.basename(p).lower() == 'msvcp140.dll' for p in runtime) and not outside,
          f'C++ runtime loads from the bundle: {sorted(os.path.basename(p) for p in runtime)}'
          + (f'; from outside it: {outside}' if outside else ''))


def common(app):
    check(INSTALLED and os.path.normcase(ROOT) == os.path.normcase(INSTALL), f'runs as an installed copy from {ROOT}')
    check_cpp_runtime()
    check(not DATA_ROOT.lower().startswith(INSTALL.lower()), f'data folder is outside the program folder: {DATA_ROOT}')
    check(os.path.normcase(app.python) == os.path.normcase(RUNTIME_PYTHON), 'uses the bundled Python')
    pump(app, lambda: app.env is not None, 240)
    check(app.env['real_ok'], f'environment check: {app.env["message"]}')


def demo(app):
    common(app)
    app.select('data')
    app.download_sample()
    pump(app, lambda: idle(app), 300)
    rows = state.data_rows(app.out_dir, app.cache_dir)
    sample = [r for r in rows if r['group'] == 'Sample']
    check(len(sample) == 4 and all(r['has_session'] and r['n_lfp'] == 2 for r in sample), 'demo: sample set downloaded')
    app.run_step('band')
    pump(app, lambda: idle(app), 600)
    check(state.band_summary(app.out_dir) is not None, 'demo: step 2 found a band')
    app.select('data')
    app.selected_sessions = {sample[0]['session']} | {r['session'] for r in rows if r['group'] == 'Test'}
    app._refresh_ticks()
    app.run_selected()
    pump(app, lambda: idle(app), 1200)
    results = os.listdir(os.path.join(app.out_dir, 'sessions'))
    extra = os.listdir(os.path.join(state.sample_dir(app.out_dir), 'sessions'))
    check(len(extra) == 1 and f'{sample[0]["session"]}.json' not in results, 'demo: the sample run stayed out of the results')
    app.run_step('group')
    pump(app, lambda: idle(app), 120)
    g = state.group_summary(app.out_dir)
    check(g is not None and g['timing_index']['n'] >= 3, f'demo: results: {g["decision"][:60]}')
    app.select('group')
    app.root.update()


def real(app, band_json):
    common(app)
    app.demo.set(False)
    app._mode_changed()
    app.select('data')
    app.run_step('list')
    pump(app, lambda: idle(app), 900)
    rows = state.data_rows(app.out_dir, app.cache_dir)
    check(len(rows) == 103, f'real: step 1 through the bundled Python lists {len(rows)} sessions')
    # step 2 downloads ~45 GB, so reuse its result from the earlier real run
    shutil.copy2(band_json, os.path.join(app.out_dir, 'band.json'))
    sid = next(r['session'] for r in rows if r['group'] == 'Sample')
    app.select('data')
    app.selected_sessions = {sid}
    app.max_probes.set(1)
    app._refresh_ticks()
    app.download_selected()
    pump(app, lambda: idle(app), 6000)
    rec = storage.inventory(app.cache_dir).get(sid)
    check(bool(rec and rec['session_file'] and len(rec['lfp']) == 1 and rec['gb'] > 5),
          f'real: session {sid} downloaded ({rec["gb"] if rec else 0:.1f} GB) without loading it')
    app.run_selected()
    pump(app, lambda: idle(app), 1800)
    path = os.path.join(state.sample_dir(app.out_dir), 'sessions', f'{sid}.json')
    with open(path, encoding='utf-8') as fh:
        r = json.load(fh)
    check('error' not in r and r.get('n_driven', 0) > 0,
          f'real: analysed from the Data page: {r.get("n_driven")} driven channels, timing {r.get("timing_index"):+.3f}')
    rec = storage.inventory(app.cache_dir)[sid]
    check(rec['session_file'] and not rec['lfp'], 'real: its LFP file was cleaned up, the session file kept')
    app.selected = 'data'
    app.selected_sessions = {sid}
    app.refresh()
    app.delete_selected()
    check(not storage.inventory(app.cache_dir)[sid]['session_file'], 'real: Delete their files removed the session file')


def main():
    root = tk.Tk()
    root.withdraw()                              # this test drives the window without showing it
    app = gui.App(root, demo=True)
    root.update()
    if sys.argv[1] == 'demo':
        demo(app)
    else:
        real(app, sys.argv[2])
    root.destroy()
    print('ALL PASSED', flush=True)


if __name__ == '__main__':
    main()
