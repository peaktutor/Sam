"""
P4 Analysis window.

Layout: header (title and data-source badge) / sidebar with the five steps / the selected step's page /
collapsible technical log / status bar.

The window never blocks: StepRunner reads the subprocess on a worker thread and puts lines on a queue;
the Tk main loop drains that queue every POLL_MS. Only the main thread touches widgets.
"""
import glob
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from . import plots, prereg, state, storage
from . import settings as cfg
from .runner import DATA_ROOT, DOWNLOADER, ROOT, SCRIPT, StepRunner, demo_env, log_path_for, sha256_file, step_command
from .session_queue import SessionQueue
from .theme import C, enable_dpi_awareness, label, setup

POLL_MS = 100
PAGE_PAD = 36               # page margin (px at 96 dpi); cards and notes add their own padding inside it
CARD_PAD, NOTE_PAD = 22, 14
MAX_PER_TICK = 500          # lines drained per poll, so a burst of output cannot freeze the window
SCENARIOS = ('reset', 'rebound', 'null')

STEPS = [
    dict(key='list', title='List sessions', run='Run step 1',
         what="Downloads the dataset's session tables and splits the mice that have both a familiar and a "
              "novel session into 2 pilot mice and 16 test mice.",
         cost='About 160 MB of tables  ·  about a minute'),
    dict(key='band', title='Find the beta band', run='Find beta band',
         what='Uses only the 2 pilot mice. Measures the power spectrum of visual-cortex channels, finds the beta '
              'peak above the 1/f background, and fixes the band at the peak ± 5 Hz.',
         cost='About 50 GB of downloads (4 sessions)  ·  2 hours at 7 MB/s, longer on a slow line'),
    dict(key='prereg', title='Preregister', run=None,
         what="Before any test mouse is analysed, upload the beta band, the mouse split and the script's checksum "
              "to OSF with a time stamp. This step is done by hand.",
         cost='No downloads'),
    dict(key='analyze', title='Analyse test sessions', run='Analyse sessions',
         what='Measures beta around every flash in each test session and saves one result file per session. '
              'You can stop at any time: finished sessions are skipped next time.',
         cost='About 7.5 GB of downloads per session with 1 probe  ·  several hours in total'),
    dict(key='group', title='Results', run='Compute results',
         what="Averages each mouse's sessions, runs the preregistered tests and applies the decision rule.",
         cost='No downloads  ·  a few seconds'),
]
STEP = {s['key']: s for s in STEPS}
STEP['data'] = dict(key='data', title='Data files', run=None)     # not a numbered step, but runs things the same way
SAMPLE_PROBES = 2           # the sample set has what step 2 uses: 2 probes per pilot session
DATA_COLUMNS = ('✓', 'Session', 'Mouse', 'Images', 'Day', 'Group', 'Downloaded', 'Result')

CONTRASTS = [   # key in group_results.json, name, what it measures
    ('timing_index', 'Timing index', 'beta during the flash minus after it'),
    ('novelty_within', 'Novelty, within session', 'early minus late flashes (primary)'),
    ('novelty_shared_images', '   … shared images (control)', 'the same contrast on images that are not novel'),
    ('novelty_between', 'Novelty, between sessions', 'novel minus familiar session'),
    ('novelty_between_order_adjusted', '   … adjusted for order', 'familiar-first and novel-first equal'),
    ('change_during', 'Change contrast', 'change flash minus the flash before'),
    ('hazard_slope', 'Hazard slope', 'pre-flash beta vs change probability'),
]


GUIDE_TEST_MICE = 16


def disk_estimate(n_sessions, max_probes=1, cleanup=True):
    """(GB downloaded, GB kept afterwards) for step 4, from the guide's sizes (3 GB session file + 4.7 GB per
    LFP probe). With clean-up, each session's LFP files are deleted once its result is saved."""
    per = 3.0 + 4.7 * max_probes
    return n_sessions * per, n_sessions * (3.0 if cleanup else per)


def paths(demo, base=DATA_ROOT):
    """Demo runs get their own folders so synthetic results can never mix with real ones."""
    base = os.path.join(base, 'demo_run') if demo else base
    return os.path.normpath(os.path.join(base, 'vbn_cache')), os.path.normpath(os.path.join(base, 'p4_out'))


def open_path(path):
    if sys.platform == 'win32':
        os.startfile(path)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path])


def keep_awake(on):
    """Stop Windows sleeping from idleness while something runs: a sleeping PC pauses the analysis and drops downloads.
    Lasts only while this thread asks; it changes no settings, and closing a laptop lid can still put it to sleep."""
    if sys.platform == 'win32':
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def fmt_p(p):
    return '' if p is None else ('< 0.001' if p < 0.001 else f'{p:.3f}')


def fmt_elapsed(secs):
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


class App:
    def __init__(self, root, demo=None, scenario=None, python=None, base=DATA_ROOT):
        """Settings are loaded from base/app_settings.json; demo/scenario/python, when given, override them."""
        self.root, self.base = root, base
        self.F = setup(root)
        self.runner = StepRunner()
        self.current = None                  # step that is running (or last ran)
        self.started = None
        self.polls_while_running = 0         # used by the smoke test to show the window stays responsive
        self.selected = 'list'
        s = cfg.load(base)
        self.demo = tk.BooleanVar(value=s['demo'] if demo is None else demo)
        self.scenario = tk.StringVar(value=scenario or s['scenario'])
        self.max_probes = tk.IntVar(value=s['max_probes'])
        self.python_var = tk.StringVar(value=python or s['python'])
        self.cache_var = tk.StringVar(value=s['cache'])
        self.out_var = tk.StringVar(value=s['out'])
        self.cleanup = tk.BooleanVar(value=s['cleanup'])
        self.max_probes.trace_add('write', lambda *a: self.save_settings())
        self.cleanup.trace_add('write', lambda *a: self.save_settings())
        self.queue = None                    # SessionQueue while step 4 is working through sessions
        self.detail_session = None           # session shown in the analyse page's chart
        self.selected_sessions = set()       # sessions ticked on the Data page
        self.data_filter = tk.StringVar(value='All')
        self.data_detail = None              # session shown in the Data page's chart
        self._after_ok = None                # what to do next if the running step succeeds
        self._queue_phase = None             # which part of a session the queue is on: 'download' or 'analyze'
        self.awake = False                   # whether this window is holding the PC awake
        self.prereg = ('none', None, [])
        self.env = None                      # result of settings.check_python, or None while unknown
        self._env_result = None              # handed over from the checking thread
        self._checking = False
        self.latest = ''
        self.run_btn = self.stop_btn = self.progress = self.elapsed_lbl = self.latest_lbl = None
        self._wrap = []
        self._in_progress = False
        self.script_hash = sha256_file(SCRIPT) if os.path.exists(SCRIPT) else None

        root.title('P4 Analysis')
        self.k = k = root.winfo_fpixels('1i') / 96            # Tk sizes are in pixels: scale them with the DPI
        w, h = min(int(1180 * k), root.winfo_screenwidth() - 40), min(int(800 * k), root.winfo_screenheight() - 80)
        root.geometry(f'{w}x{h}')
        root.minsize(int(1040 * k), int(620 * k))

        self._build_header()
        self._build_statusbar()
        self._build_log()
        self._build_body()
        self.refresh()
        self.check_env()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(POLL_MS, self._poll)

    @property
    def python(self):
        return self.python_var.get().strip()

    @property
    def busy(self):
        """A step is running, or step 4 is between two sessions of its queue."""
        return self.runner.is_running or self.queue is not None

    # ================================================================== layout
    def _build_header(self):
        hd = tk.Frame(self.root, bg=C['header'], padx=24, pady=14)
        hd.pack(fill='x')
        left = tk.Frame(hd, bg=C['header'])
        left.pack(side='left')
        label(left, 'P4 Analysis', self.F['title'], fg='white').pack(anchor='w')
        label(left, 'Two readings of beta  ·  Allen Visual Behavior Neuropixels', self.F['small'],
              fg=C['header_muted']).pack(anchor='w')
        self.badge = tk.Label(hd, font=self.F['small_bold'], padx=12, pady=5)
        self.badge.pack(side='right')

    def _build_body(self):
        body = tk.Frame(self.root, bg=C['bg'])
        body.pack(fill='both', expand=True)

        side = tk.Frame(body, bg=C['sidebar'], width=int(280 * self.k))
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        ttk.Button(side, text='⇩  Download & choose data', style='Accent.TButton',
                   command=lambda: self.select('data')).pack(fill='x', padx=16, pady=(16, 0))
        label(side, 'STEPS', self.F['small_bold'], fg=C['muted']).pack(anchor='w', padx=20, pady=(14, 6))
        self.step_items = {}
        for i, s in enumerate(STEPS, 1):
            self.step_items[s['key']] = self._step_item(side, i, s)

        foot = tk.Frame(side, bg=C['sidebar'], padx=20, pady=16)
        foot.pack(side='bottom', fill='x')
        tk.Frame(side, bg=C['border'], height=1).pack(side='bottom', fill='x', padx=20)
        label(foot, 'DATA SOURCE', self.F['small_bold'], fg=C['muted']).pack(anchor='w', pady=(0, 6))
        self.mode_btns = [
            ttk.Radiobutton(foot, text='Demo (synthetic data)', value=True, variable=self.demo,
                            command=self._mode_changed, style='Side.TRadiobutton'),
            ttk.Radiobutton(foot, text='Real Allen data', value=False, variable=self.demo,
                            command=self._mode_changed, style='Side.TRadiobutton')]
        for b in self.mode_btns:
            b.pack(anchor='w', pady=1)
        row = self.scenario_row = tk.Frame(foot, bg=C['sidebar'])
        row.pack(anchor='w', pady=(6, 10), padx=(24, 0))
        label(row, 'Scenario', self.F['small'], fg=C['muted']).pack(side='left')
        self.scenario_box = ttk.Combobox(row, textvariable=self.scenario, values=SCENARIOS, state='readonly',
                                         width=9, font=self.F['small'])
        self.scenario_box.pack(side='left', padx=6)
        self.scenario_box.bind('<<ComboboxSelected>>', lambda e: (self.save_settings(), self.refresh()))
        self.settings_btn = ttk.Button(foot, text='⚙  Settings', command=lambda: self.select('settings'))
        self.settings_btn.pack(anchor='w', fill='x', pady=(4, 6))
        self.env_side = label(foot, '', self.F['small'], fg=C['muted'], wraplength=int(235 * self.k))
        self.env_side.pack(anchor='w')

        # the page scrolls: a frame inside a canvas. The scrollbar is always there, so the page width never
        # jumps when content grows; when the width changes, the page is redrawn once to re-fit text and tables.
        holder = tk.Frame(body, bg=C['bg'])
        holder.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(holder, bg=C['bg'], highlightthickness=0)
        self.vsb = ttk.Scrollbar(holder, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.page_host = tk.Frame(self.canvas, bg=C['bg'], padx=int(PAGE_PAD * self.k), pady=int(26 * self.k))
        self._win = self.canvas.create_window(0, 0, window=self.page_host, anchor='nw')
        self.page_host.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._on_resize)
        self.root.bind_all('<MouseWheel>', self._on_wheel)
        self._holder = holder
        self._drawn_w = None
        self._redraw_job = None

    def _step_item(self, parent, n, s):
        f = tk.Frame(parent, bg=C['sidebar'], padx=14, pady=10, cursor='hand2')
        f.pack(fill='x', padx=8, pady=1)
        self.dot = int(30 * self.k)
        dot = tk.Canvas(f, width=self.dot, height=self.dot, highlightthickness=0, bg=C['sidebar'])
        dot.pack(side='left')
        txt = tk.Frame(f, bg=C['sidebar'])
        txt.pack(side='left', padx=(12, 0), fill='x')
        title = label(txt, f'{s["title"]}', self.F['body_bold'])
        title.pack(anchor='w')
        sub = label(txt, '', self.F['small'], fg=C['muted'])
        sub.pack(anchor='w')
        for w in (f, dot, txt, title, sub):
            w.bind('<Button-1>', lambda e, k=s['key']: self.select(k))
        return dict(frame=f, dot=dot, txt=txt, title=title, sub=sub, n=n)

    def _build_log(self):
        self.log_open = False
        wrap = tk.Frame(self.root, bg=C['surface'])
        wrap.pack(side='bottom', fill='x')
        tk.Frame(wrap, bg=C['border'], height=1).pack(fill='x')
        bar = tk.Frame(wrap, bg=C['surface'], padx=16, pady=6)
        bar.pack(fill='x')
        self.log_toggle = label(bar, '', self.F['small_bold'], fg=C['muted'], cursor='hand2')
        self.log_toggle.pack(side='left')
        self.log_toggle.bind('<Button-1>', lambda e: self.toggle_log())
        ttk.Button(bar, text='Clear', style='Small.TButton', command=self.clear_log).pack(side='right')
        ttk.Button(bar, text='Open log files', style='Small.TButton',
                   command=lambda: self._open(os.path.join(self.out_dir, 'logs'))).pack(side='right', padx=6)
        self.log_body = tk.Frame(wrap, bg=C['log_bg'])
        self.log = tk.Text(self.log_body, height=9, wrap='none', state='disabled', font=self.F['mono'],
                           bg=C['log_bg'], fg=C['log_text'], relief='flat', padx=12, pady=8,
                           insertbackground=C['log_text'], selectbackground='#3b4a66')
        sb = ttk.Scrollbar(self.log_body, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log.pack(fill='both', expand=True)
        self.log.tag_configure('cmd', foreground='#8ab4ff')
        self.log.tag_configure('err', foreground='#ff8a80')
        self.log.tag_configure('ok', foreground='#7ee2a8')
        self.log.tag_configure('dim', foreground=C['log_dim'])
        self.log.mark_set('progress', 'end-1c')
        self.log.mark_gravity('progress', 'left')
        self._update_log_toggle()

    def _build_statusbar(self):
        sb = tk.Frame(self.root, bg=C['sidebar'], padx=16, pady=5)
        sb.pack(side='bottom', fill='x')
        self.status = label(sb, 'Ready.', self.F['small'], fg=C['muted'])
        self.status.pack(side='left')
        h = self.script_hash
        label(sb, f'vbn_p4.py  ·  SHA-256 {h[:12]}…' if h else 'vbn_p4.py not found', self.F['small'],
              fg=C['faint'] if h else C['err']).pack(side='right')
        self.env_status = label(sb, '', self.F['small'], fg=C['faint'])
        self.env_status.pack(side='right', padx=(0, 18))

    # ================================================================== state and navigation
    @property
    def out_dir(self):
        return paths(True, self.base)[1] if self.demo.get() else os.path.normpath(self.out_var.get().strip())

    @property
    def cache_dir(self):
        return paths(True, self.base)[0] if self.demo.get() else os.path.normpath(self.cache_var.get().strip())

    def select(self, key):
        self.selected = key
        self.refresh()

    def refresh(self):
        """Recompute step states from the output folder, then redraw the sidebar and the current page."""
        self.refresh_chrome()
        self._render_page()

    def refresh_chrome(self):
        """Everything except the page: step states, sidebar, badge, environment status."""
        self.states = state.step_states(self.out_dir, require_prereg=not self.demo.get())
        self.prereg = prereg.status(self.out_dir)              # ('none' | 'ok' | 'mismatch', record, changed)
        for key, it in self.step_items.items():
            self._paint_step(key, it)
        demo = self.demo.get()
        self.badge.configure(text=f'DEMO DATA  ·  {self.scenario.get()} scenario' if demo else 'REAL ALLEN DATA',
                             bg=C['warn_bg'] if demo else C['ok_bg'], fg=C['warn'] if demo else C['ok'])
        self.scenario_box.configure(state='readonly' if demo and not self.busy else 'disabled')
        if demo and not self.scenario_row.winfo_manager():          # the scenario only exists for demo data
            self.scenario_row.pack(anchor='w', pady=(6, 10), padx=(24, 0), before=self.settings_btn)
        elif not demo and self.scenario_row.winfo_manager():
            self.scenario_row.pack_forget()
        for b in self.mode_btns:
            b.configure(state='disabled' if self.busy else 'normal')
        text, kind = self._env_summary()
        self.env_side.configure(text=text, fg=C[kind] if kind != 'muted' else C['muted'])
        self.env_status.configure(text=text)

    # ================================================================== settings and environment
    def save_settings(self):
        s = dict(python=self.python, cache=self.cache_var.get().strip(), out=self.out_var.get().strip(),
                 max_probes=int(self.max_probes.get()), demo=bool(self.demo.get()), scenario=self.scenario.get(),
                 cleanup=bool(self.cleanup.get()))
        try:
            cfg.save(s, self.base)
        except OSError as e:
            self._set_status(f'Could not save settings: {e}')

    def check_env(self):
        """Check the chosen interpreter on a worker thread (importing AllenSDK can take a while)."""
        if self._checking:
            return
        self._checking, self.env = True, None
        python = self.python

        def work():
            self._env_result = cfg.check_python(python)
        threading.Thread(target=work, daemon=True).start()
        self.refresh_chrome()

    def env_ready(self):
        """True/False once the check is done for the current data source; None while still checking."""
        if self.env is None:
            return None
        return self.env['demo_ok'] if self.demo.get() else self.env['real_ok']

    def _env_summary(self):
        if self.env is None:
            return 'Checking the Python environment…', 'muted'
        if self.env['real_ok']:
            return f'✓  AllenSDK {self.env["modules"]["allensdk"]}  ·  Python {self.env["python"]}', 'ok'
        if self.env['demo_ok']:
            return '⚠  No AllenSDK: demo data only', 'warn'
        return '✗  Python environment not usable', 'err'

    def _paint_step(self, key, it):
        st = self.states[key]
        running = self.busy and self.current == key
        sel = key == self.selected
        bg = C['sidebar_sel'] if sel else C['sidebar']
        for w in (it['frame'], it['dot'], it['txt'], it['title'], it['sub']):
            w.configure(bg=bg)
        it['title'].configure(fg=C['faint'] if st == 'locked' else C['text'])
        d, z = it['dot'], self.dot
        d.delete('all')
        box, mid = (2, 2, z - 2, z - 2), z / 2
        if running:
            d.create_oval(*box, fill=C['accent'], outline=C['accent'])
            d.create_text(mid, mid, text='…', fill='white', font=self.F['step_num'])
        elif st == 'done':
            d.create_oval(*box, fill=C['ok'], outline=C['ok'])
            d.create_text(mid, mid, text='✓', fill='white', font=self.F['step_num'])
        elif st == 'partial':
            d.create_oval(*box, fill=C['warn_bg'], outline=C['warn'], width=2)
            d.create_text(mid, mid, text=str(it['n']), fill=C['warn'], font=self.F['step_num'])
        elif st == 'mismatch':
            d.create_oval(*box, fill=C['err'], outline=C['err'])
            d.create_text(mid, mid, text='!', fill='white', font=self.F['step_num'])
        elif st == 'ready':
            d.create_oval(*box, fill=bg, outline=C['accent'], width=2)
            d.create_text(mid, mid, text=str(it['n']), fill=C['accent'], font=self.F['step_num'])
        else:
            d.create_oval(*box, fill=C['border'], outline=C['border'])
            d.create_text(mid, mid, text=str(it['n']), fill=C['faint'], font=self.F['step_num'])
        it['sub'].configure(text=self._step_subtitle(key, st, running))

    def _step_subtitle(self, key, st, running):
        if running:
            return f'Running  ·  {fmt_elapsed(time.time() - self.started)}'
        if key == 'analyze' and st in ('partial', 'done'):
            a = state.analyze_summary(self.out_dir)
            return f'{a["done"]} of {a["total"]} sessions done' + (f'  ·  {a["errors"]} failed' if a['errors'] else '')
        if key == 'prereg':
            if st == 'ready':
                return 'Optional for demo data' if self.demo.get() else 'Do by hand before step 4'
            if st == 'done':
                return 'Locked  ·  checksums match'
            if st == 'mismatch':
                return 'Changed since it was locked!'
        if st == 'locked':
            if key in ('analyze', 'group') and self.states.get('band') == 'done' and not self.demo.get() \
                    and self.states.get('prereg') != 'done':
                return 'Preregister first (step 3)'
            n = [s['key'] for s in STEPS].index(key)
            return f'Finish step {n} first' if key != 'group' else 'Needs analysed sessions'
        return {'done': 'Done', 'ready': 'Ready to run'}.get(st, '')

    # ================================================================== pages
    def _render_page(self):
        for w in self.page_host.winfo_children():
            w.destroy()
        self._wrap = []
        self.run_btn = self.stop_btn = self.progress = self.elapsed_lbl = self.latest_lbl = None
        p = self.page_host
        if self.selected in ('settings', 'data'):
            getattr(self, f'_render_{self.selected}')(p)
            self._rewrap()
            self._drawn_w = self.canvas.winfo_width()
            self.canvas.yview_moveto(0)
            return
        key, s = self.selected, STEP[self.selected]
        n = [x['key'] for x in STEPS].index(key) + 1
        if self.prereg[0] == 'mismatch':
            rec, changed = self.prereg[1], self.prereg[2]
            f = tk.Frame(p, bg=C['err_bg'], padx=int(NOTE_PAD * self.k), pady=int(10 * self.k))
            f.pack(fill='x', anchor='w', pady=(0, 16))
            label(f, 'Changed since preregistration', self.F['body_bold'], fg=C['err']).pack(anchor='w')
            self._wrapped(f, f'{" and ".join(changed)} {"is" if len(changed) == 1 else "are"} different from the '
                             f'version locked on {prereg.nice_date(rec.get("locked_at"))}. '
                             + ('Steps 4 and 5 are blocked for real data until this is resolved (see step 3).'
                                if not self.demo.get() else 'With real data, steps 4 and 5 would be blocked.'),
                          self.F['small'], C['err'], inset=2 * NOTE_PAD).pack(anchor='w', fill='x')
        label(p, f'STEP {n} OF {len(STEPS)}', self.F['small_bold'], fg=C['accent']).pack(anchor='w')
        label(p, s['title'], self.F['title']).pack(anchor='w', pady=(2, 6))
        self._wrapped(p, s['what'], self.F['body']).pack(anchor='w', fill='x')
        label(p, s['cost'], self.F['small'], fg=C['muted']).pack(anchor='w', pady=(6, 16))
        if s['run']:
            self._action_row(p, key, s)
        getattr(self, f'_render_{key}')(p)
        self._rewrap()
        self._drawn_w = self.canvas.winfo_width()
        self.canvas.yview_moveto(0)

    def _action_row(self, p, key, s):
        row = tk.Frame(p, bg=C['bg'])
        row.pack(anchor='w', fill='x', pady=(0, 18))
        running_here = self.busy and self.current == key
        no_env = self.env_ready() is False
        blocked = self.states[key] == 'locked' or self.busy or no_env
        self.run_btn = ttk.Button(row, text='Running…' if running_here else f'▶  {s["run"]}', style='Accent.TButton',
                                  command=self.run_step, state='disabled' if blocked else 'normal')
        self.run_btn.pack(side='left')
        self._stop_buttons(row, key)
        if key == 'analyze':
            opt = tk.Frame(p, bg=C['bg'])
            opt.pack(anchor='w', fill='x', pady=(0, 16))
            r = tk.Frame(opt, bg=C['bg'])
            r.pack(anchor='w')
            label(r, 'Probes per session', self.F['small'], fg=C['muted']).pack(side='left', padx=(0, 10))
            for v, t in ((1, '1  (~7.5 GB)'), (2, '2  (~12 GB)')):
                ttk.Radiobutton(r, text=t, value=v, variable=self.max_probes,
                                state='disabled' if self.busy else 'normal').pack(side='left', padx=(0, 10))
            ttk.Checkbutton(opt, text="Delete each session's LFP files once its result is saved  (frees ~4.5 GB per "
                                      'probe; the 3 GB session file is kept)', variable=self.cleanup,
                            state='disabled' if self.busy else 'normal').pack(anchor='w', pady=(4, 0))
        if running_here:
            self._running_area(p, key)
        elif self.busy:
            label(p, f'“{STEP[self.current]["title"]}” is running. Steps run one at a time.', self.F['small'],
                  fg=C['muted']).pack(anchor='w', pady=(0, 14))
        elif no_env:
            need = 'numpy, scipy, pandas and xarray' if self.demo.get() else 'AllenSDK'
            f = tk.Frame(p, bg=C['err_bg'], padx=int(NOTE_PAD * self.k), pady=int(10 * self.k))
            f.pack(fill='x', anchor='w', pady=(0, 16))
            self._wrapped(f, f'The selected Python does not have {need}, so this step cannot run yet. '
                             'Choose the right interpreter in Settings.', self.F['small'], C['err'],
                          inset=2 * NOTE_PAD + 140).pack(side='left', fill='x')
            ttk.Button(f, text='Open Settings', style='Small.TButton',
                       command=lambda: self.select('settings')).pack(side='right')

    def _stop_buttons(self, row, key):
        """Stop (and, when working through sessions, Stop after this session) for whatever `key` is running."""
        running_here = self.busy and self.current == key
        queue_here = running_here and self.queue is not None
        if queue_here:
            q = self.queue
            self.stop_after_btn = ttk.Button(row, text='Stopping after this session' if q.stop_requested
                                             else 'Stop after this session', command=self.stop_after_session,
                                             state='disabled' if q.stop_requested else 'normal')
            self.stop_after_btn.pack(side='left', padx=(8, 0))
        self.stop_btn = ttk.Button(row, text='■  Stop now' if queue_here else '■  Stop', command=self.stop,
                                   state='normal' if running_here else 'disabled')
        self.stop_btn.pack(side='left', padx=(8, 0))

    def _running_area(self, p, key):
        """Elapsed time, progress bar and the latest output line for the step that is running."""
        box = tk.Frame(p, bg=C['bg'])
        box.pack(anchor='w', fill='x', pady=(0, 18))
        top = tk.Frame(box, bg=C['bg'])
        top.pack(fill='x')
        self.elapsed_lbl = label(top, '', self.F['small_bold'], fg=C['accent'])
        self.elapsed_lbl.pack(side='left')
        if self.queue is not None:
            self.progress = ttk.Progressbar(box, mode='determinate', maximum=max(self.queue.total, 1),
                                            value=self.queue.done_count, style='Accent.Horizontal.TProgressbar')
            self.progress.pack(fill='x', pady=(4, 4))
        else:
            self.progress = ttk.Progressbar(box, mode='indeterminate', style='Accent.Horizontal.TProgressbar')
            self.progress.pack(fill='x', pady=(4, 4))
            self.progress.start(12)
        self.latest_lbl = label(box, self.latest, self.F['small'], fg=C['muted'])
        self.latest_lbl.pack(anchor='w', fill='x')
        label(box, 'This PC stays awake until this finishes. On a laptop, keep it plugged in with the lid open.',
              self.F['small'], fg=C['muted']).pack(anchor='w', fill='x', pady=(2, 0))
        self._tick_running()

    def _wrapped(self, parent, text, font, fg=None, inset=0):
        """A label that wraps to the page width; `inset` is the padding of the containers it sits in."""
        lbl = label(parent, text, font, fg=fg)
        self._wrap.append((lbl, inset))
        return lbl

    def _card(self, parent, title=None, pady=(0, 16)):
        outer = tk.Frame(parent, bg=C['surface'], highlightbackground=C['border'], highlightthickness=1)
        outer.pack(fill='x', anchor='w', pady=pady)
        inner = tk.Frame(outer, bg=C['surface'], padx=int(CARD_PAD * self.k), pady=int(18 * self.k))
        inner.pack(fill='both', expand=True)
        if title:
            label(inner, title, self.F['h2']).pack(anchor='w', pady=(0, 10))
        return inner

    def _empty(self, parent, text):
        c = self._card(parent)
        label(c, 'Not run yet', self.F['body_bold'], fg=C['muted']).pack(anchor='w')
        self._wrapped(c, text, self.F['body'], C['muted'], inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x',
                                                                                     pady=(2, 0))

    def _note(self, parent, text, kind='warn', pady=(0, 14), in_card=False):
        f = tk.Frame(parent, bg=C[f'{kind}_bg'], padx=int(NOTE_PAD * self.k), pady=int(10 * self.k))
        f.pack(fill='x', anchor='w', pady=pady)
        inset = 2 * NOTE_PAD + (2 * CARD_PAD + 2 if in_card else 0)
        self._wrapped(f, text, self.F['small'], C[kind], inset=inset).pack(anchor='w', fill='x')

    def _tiles(self, parent, items):
        row = tk.Frame(parent, bg=C['surface'])
        row.pack(anchor='w', fill='x', pady=(0, 12))
        for value, caption in items:
            t = tk.Frame(row, bg=C['surface'])
            t.pack(side='left', padx=(0, 40))
            label(t, str(value), self.F['big']).pack(anchor='w')
            label(t, caption, self.F['small'], fg=C['muted']).pack(anchor='w')

    def _table(self, parent, columns, rows, widths, height=None, tags=None, flex=None, optional=()):
        """`flex`: indices of the text columns that give up width when the page is narrow (default: all);
        the other columns (short numbers) keep their width so they are never cut off.
        `optional`: columns hidden altogether when they could only be shown squeezed below legibility."""
        f = tk.Frame(parent, bg=C['surface'])
        f.pack(fill='both', expand=True, anchor='w')
        tv = ttk.Treeview(f, columns=columns, show='headings', height=height or min(len(rows), 10))
        avail = self._page_width() - int((2 * CARD_PAD + 2 + (18 if len(rows) > (height or 10) else 0)) * self.k)
        flex = set(range(len(columns)) if flex is None else flex)
        shown = list(range(len(columns)))

        def fit():
            fixed = sum(widths[i] for i in shown if i not in flex) * self.k
            flexible = sum(widths[i] for i in shown if i in flex) * self.k
            return min(1.0, (avail - fixed) / flexible) if flexible else 1.0

        scale = fit()
        if scale < 0.7 and optional:
            shown = [i for i in shown if i not in optional]
            scale = fit()
        scale = max(scale, 0.5)
        tv.configure(displaycolumns=[columns[i] for i in shown])
        for i, (c, w) in enumerate(zip(columns, widths)):
            tv.heading(c, text=c, anchor='w')
            tv.column(c, width=int(w * self.k * (scale if i in flex else 1)), minwidth=int(30 * self.k),
                      anchor='w', stretch=i == shown[-1])
        for i, r in enumerate(rows):
            tv.insert('', 'end', values=r, tags=((tags[i],) if tags else ()) + (('odd',) if i % 2 else ()))
        tv.tag_configure('odd', background='#f7f9fc')
        tv.tag_configure('ok', foreground=C['ok'])
        tv.tag_configure('err', foreground=C['err'])
        tv.tag_configure('muted', foreground=C['muted'])
        tv.tag_configure('run', foreground=C['accent'], font=self.F['body_bold'])
        if len(rows) > (height or 10):
            sb = ttk.Scrollbar(f, command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
        tv.pack(side='left', fill='both', expand=True)
        return tv

    def _render_list(self, p):
        s = state.list_summary(self.out_dir)
        if not s:
            return self._empty(p, 'Click “Run step 1” to download the session tables.')
        c = self._card(p, 'Sessions')
        self._tiles(c, [(s['n_sessions'], 'sessions'), (s['n_mice'], 'mice'),
                        (s['n_paired'], 'mice with both sessions'), (s['n_test'], 'test mice')])
        label(c, f'Pilot mice (used only to set the beta band):  {", ".join(map(str, s["pilot"]))}',
              self.F['body']).pack(anchor='w')
        label(c, f'Test sessions to analyse:  {s["n_test_sessions"]}   ·   finished {state.ago(s["when"])}',
              self.F['small'], fg=C['muted']).pack(anchor='w', pady=(4, 0))
        if s['n_test'] != GUIDE_TEST_MICE and not self.demo.get():
            dl, kept = disk_estimate(s['n_test_sessions'], self.max_probes.get())
            self._note(c, f'The guide plans {GUIDE_TEST_MICE} test mice (32 sessions), but vbn_p4.py puts every '
                          f'remaining mouse with both sessions in the test set: {s["n_test"]} mice, '
                          f'{s["n_test_sessions"]} sessions. Step 4 would download about {dl:.0f} GB and keep '
                          f'about {kept:.0f} GB even with LFP clean-up. Which mice to test is a preregistration '
                          'decision: settle it (and change the script if needed) before step 3.', 'warn',
                       pady=(12, 0), in_card=True)
        c = self._card(p, 'Recording order')
        o = s['order']
        days = [str(d) for d in o.columns]
        self._table(c, ['Images'] + [f'Day {d}' for d in days],
                    [[lvl] + [int(v) for v in o.loc[lvl].values] for lvl in o.index], [160] + [110] * len(days))
        if 'Novel' in o.index and '1' in days and int(o.loc['Novel', '1']) > 0:
            self._note(c, 'Some mice saw the novel images first, so the order is not balanced across mice. That is '
                          'why the within-session novelty contrast is the primary test, and why the between-session '
                          'contrast is also reported adjusted for order.', 'warn', pady=(12, 0), in_card=True)

    def _render_band(self, p):
        b = state.band_summary(self.out_dir)
        if not b:
            return self._empty(p, 'Run this after step 1. It uses only the two pilot mice, so the band is fixed '
                                  'before any test data are seen.')
        c = self._card(p, 'Beta band')
        label(c, f'{b["low"]:.1f} – {b["high"]:.1f} Hz', self.F['big'], fg=C['accent']).pack(anchor='w')
        bits = [f'peak {b["peak"]:.1f} Hz' if b['peak'] is not None else '',
                f'{b["n_channels"]} channels' if b['n_channels'] else '',
                f'1/f slope {b["slope"]:.2f}' if b['slope'] is not None else '', f'finished {state.ago(b["when"])}']
        label(c, '   ·   '.join(x for x in bits if x), self.F['small'], fg=C['muted']).pack(anchor='w', pady=(2, 12))
        if b['fallback']:
            self._note(c, 'No clear beta peak stands out from the 1/f background in 12–40 Hz, so this is the '
                          "guide's fallback band of 15–30 Hz. State that in the preregistration.", 'warn', pady=(0, 8),
                       in_card=True)
            if os.path.exists(b['pilot_plot']):
                ttk.Button(c, text='Open the pilot spectrum plot',
                           command=lambda: self._open(b['pilot_plot'])).pack(anchor='w')
        else:
            self._note(c, 'Check that this looks plausible before preregistering: a peak in the high teens or '
                          'twenties is typical of mouse visual cortex.', 'ok', pady=0, in_card=True)

    def _render_prereg(self, p):
        b, s = state.band_summary(self.out_dir), state.list_summary(self.out_dir)
        if not b:
            return self._empty(p, 'Finish steps 1 and 2 first: the preregistration records the beta band and the '
                                  'mouse split they produce.')
        c = self._card(p, 'What to upload to OSF')
        rows = [('Beta band', f'{b["low"]:.1f} – {b["high"]:.1f} Hz   (band.json)'),
                ('Mouse split', f'{len(s["pilot"])} pilot mice, {s["n_test"]} test mice   (split.json)'),
                ('Script', 'vbn_p4.py, exactly as it will be run')]
        for k, v in rows:
            r = tk.Frame(c, bg=C['surface'])
            r.pack(anchor='w', fill='x', pady=3)
            label(r, k, self.F['body_bold'], width=14).pack(side='left')
            label(r, v, self.F['body']).pack(side='left')
        r = tk.Frame(c, bg=C['surface'])
        r.pack(anchor='w', fill='x', pady=(10, 3))
        label(r, 'Script checksum (SHA-256)', self.F['body_bold']).pack(side='left')
        ttk.Button(r, text='Copy', style='Small.TButton', command=self._copy_hash).pack(side='left', padx=10)
        h = self.script_hash
        grouped = ' '.join(h[i:i + 8] for i in range(0, len(h), 8)) if h else 'vbn_p4.py not found'
        self._wrapped(c, grouped, self.F['mono'], C['text'] if h else C['err'],      # groups of 8 can wrap
                      inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x')
        r = tk.Frame(c, bg=C['surface'])
        r.pack(anchor='w', fill='x', pady=(16, 0))
        ttk.Button(r, text='⇩  Export bundle (.zip)', style='Accent.TButton', command=self.export_bundle).pack(side='left')
        ttk.Button(r, text='Open output folder', command=lambda: self._open(self.out_dir)).pack(side='left', padx=8)
        self._wrapped(c, 'The bundle holds vbn_p4.py, band.json, split.json, the session tables, prereg.md (the '
                         "guide's Section 6 table filled in with these values) and a checksum list. Upload it to OSF "
                         'with a time stamp.', self.F['small'], C['muted'],
                      inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(8, 0))
        last = sorted(glob.glob(os.path.join(glob.escape(self.out_dir), 'prereg', 'prereg_bundle_*.zip')))
        if last:
            label(c, f'Last exported: {os.path.basename(last[-1])}  ({state.ago(os.path.getmtime(last[-1]))})',
                  self.F['small'], fg=C['ok']).pack(anchor='w', pady=(4, 0))

        st, rec, changed = self.prereg
        c = self._card(p, 'Lock after uploading')
        if st == 'none':
            self._wrapped(c, 'Once the bundle is on OSF, paste its link here and lock. The window then records the '
                             'checksums of vbn_p4.py, band.json and split.json; if any of them changes later, steps '
                             '4 and 5 are blocked for real data.', self.F['small'], C['muted'],
                          inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(0, 8))
            label(c, 'OSF link', self.F['body_bold']).pack(anchor='w')
            r = tk.Frame(c, bg=C['surface'])
            r.pack(anchor='w', fill='x', pady=(2, 0))
            self.osf_var = tk.StringVar()
            ttk.Entry(r, textvariable=self.osf_var, font=self.F['body']).pack(side='left', fill='x', expand=True,
                                                                             ipady=int(3 * self.k))
            ttk.Button(r, text='🔒  Lock preregistration', command=self.lock_prereg).pack(side='left', padx=(8, 0))
        else:
            ok = st == 'ok'
            box = tk.Frame(c, bg=C['ok_bg' if ok else 'err_bg'], padx=int(NOTE_PAD * self.k), pady=int(10 * self.k))
            box.pack(anchor='w', fill='x')
            label(box, ('✓  Preregistered and locked' if ok else '✗  Changed since it was locked'),
                  self.F['body_bold'], fg=C['ok' if ok else 'err']).pack(anchor='w')
            detail = f'Locked {prereg.nice_date(rec.get("locked_at"))}'
            if rec.get('osf_url'):
                detail += f'   ·   {rec["osf_url"]}'
            label(box, detail, self.F['small'], fg=C['ok' if ok else 'err']).pack(anchor='w', pady=(2, 0))
            if not ok:
                self._wrapped(box, f'Changed: {", ".join(changed)}. If the change was a mistake, restore the '
                                   'preregistered file (it is inside the exported bundle). If it was deliberate, the '
                                   'preregistration has to be withdrawn and done again.', self.F['small'], C['err'],
                              inset=2 * NOTE_PAD + 2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(4, 0))
            r = tk.Frame(c, bg=C['surface'])
            r.pack(anchor='w', fill='x', pady=(10, 0))
            if rec.get('osf_url', '').startswith('http'):
                ttk.Button(r, text='Open on OSF', command=lambda: webbrowser.open(rec['osf_url'])).pack(side='left')
            ttk.Button(r, text='Withdraw lock…', command=self.withdraw_prereg,
                       state='disabled' if self.busy else 'normal').pack(side='left', padx=8)
        if self.demo.get():
            self._note(p, 'Demo data: preregistration is optional here, so you can try every step. With real data, '
                          'step 4 stays locked until this is done.', 'ok')

    def _render_analyze(self, p):
        a = state.analyze_summary(self.out_dir)
        if not a or self.states['analyze'] == 'locked':
            if self.states.get('band') == 'done' and not self.demo.get():
                return self._empty(p, 'Preregister first (step 3): with real data, test sessions are analysed only '
                                      'after the preregistration is locked.')
            return self._empty(p, 'Finish steps 1 and 2 first.')
        if a['errors']:
            self._note(p, 'A session with no driven channels: check that the probe reached visual cortex and look at '
                          'its evoked potential before changing the rule. Any change to the rule has to be made on '
                          'pilot data, before preregistration.', 'warn')
        c = self._card(p, 'Progress')
        label(c, f'{a["done"]} of {a["total"]} sessions done', self.F['h2']).pack(anchor='w')
        pb = ttk.Progressbar(c, maximum=max(a['total'], 1), value=a['done'] + a['errors'],
                             style='Ok.Horizontal.TProgressbar')
        pb.pack(fill='x', pady=(8, 6))
        info = tk.Frame(c, bg=C['surface'])
        info.pack(anchor='w', fill='x', pady=(0, 12))
        label(info, f'{a["errors"]} failed   ·   {a["waiting"]} waiting', self.F['small'],
              fg=C['err'] if a['errors'] else C['muted']).pack(side='left')
        if a['errors'] and not self.busy:
            ttk.Button(info, text='Retry failed sessions', style='Small.TButton',
                       command=self.retry_failed).pack(side='left', padx=12)
        if not self.demo.get() and os.path.isdir(self.cache_dir):
            label(info, f'Download cache: {storage.folder_gb(self.cache_dir):.1f} GB used', self.F['small'],
                  fg=C['muted']).pack(side='right')
        if a['waiting'] and not self.demo.get():
            dl, kept = disk_estimate(a['waiting'], self.max_probes.get(), self.cleanup.get())
            free = cfg.free_gb(self.cache_dir)
            short = free is not None and kept > free
            label(c, f'Still to download: about {dl:.0f} GB for {a["waiting"]} sessions   ·   kept on disk afterwards: '
                     f'about {kept:.0f} GB   ·   free now: {free:.0f} GB' if free is not None else '',
                  self.F['small'], fg=C['err'] if short else C['muted']).pack(anchor='w', pady=(0, 10))
        running = self.queue.current if self.queue else None
        rows, tags = [], []
        for r in a['rows']:
            if r['session'] == running:
                st, tag = '▶  Running now', 'run'
            else:
                st = {'done': f'✓  Done  ({r["detail"]})', 'error': f'✗  {r["detail"]}',
                      'waiting': 'Waiting'}[r['status']]
                tag = {'done': 'ok', 'error': 'err', 'waiting': 'muted'}[r['status']]
            rows.append([r['session'], r['mouse'], r['level'], r['day'], st])
            tags.append(tag)
        tv = self._table(c, ['Session', 'Mouse', 'Images', 'Day', 'Status'], rows, [130, 90, 100, 60, 300],
                         height=min(len(rows), 8), tags=tags, flex=(4,))

        done = [r['session'] for r in a['rows'] if r['status'] == 'done']
        if not done:
            return
        if self.detail_session not in done:
            self.detail_session = done[-1]
        c = self._card(p, 'Session detail')
        self._wrapped(c, 'Mean beta envelope around change flashes and around the flash just before each change. '
                         'Shaded: the during and after windows. Dashed lines: image on (0 ms) and image off (250 ms). '
                         'Click a session in the table above to show it; hover over the chart to read values.',
                      self.F['small'], C['muted'], inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(0, 4))
        label(c, f'Session {self.detail_session}', self.F['body_bold']).pack(anchor='w', pady=(4, 0))
        host = tk.Frame(c, bg=C['surface'])
        host.pack(anchor='w', fill='x')
        res = state._json(os.path.join(self.out_dir, 'sessions', f'{self.detail_session}.json'))
        try:
            plots.envelope(host, res, self._page_width() - int((2 * CARD_PAD + 2) * self.k), self.k)
        except Exception as e:                       # a damaged result file must not break the page
            label(host, f'Could not draw this session: {e}', self.F['small'], fg=C['err']).pack(anchor='w')
        for item in tv.get_children():
            if int(tv.item(item, 'values')[0]) == self.detail_session:
                tv.selection_set(item)
                tv.see(item)

        def pick(event):
            sel = tv.selection()
            if sel:
                sid = int(tv.item(sel[0], 'values')[0])
                if sid in done and sid != self.detail_session:
                    self.detail_session = sid
                    self._redraw_keep_scroll()
        tv.bind('<<TreeviewSelect>>', pick)

    def _render_group(self, p):
        g = state.group_summary(self.out_dir)
        if not g:
            return self._empty(p, 'Run this once the test sessions are analysed.')
        decision = g.get('decision', '')
        main, _, note = decision.partition(' (')
        kind = 'ok' if 'supported' in main and not main.startswith('inconclusive') else 'warn'
        ban = tk.Frame(p, bg=C[f'{kind}_bg'], padx=int(CARD_PAD * self.k), pady=int(16 * self.k))
        ban.pack(fill='x', anchor='w', pady=(0, 16))
        label(ban, 'DECISION', self.F['small_bold'], fg=C[kind]).pack(anchor='w')
        label(ban, main[:1].upper() + main[1:], self.F['big'], fg=C[kind]).pack(anchor='w')
        if note:
            self._wrapped(ban, note.rstrip(')').capitalize(), self.F['small'], C[kind],
                          inset=2 * CARD_PAD).pack(anchor='w', fill='x', pady=(4, 0))
        label(ban, f'Precision clause: {g.get("precision_clause", "?")}   ·   computed {state.ago(g.get("when"))}',
              self.F['small'], fg=C[kind]).pack(anchor='w', pady=(6, 0))
        rev = g.get('reversals') or {}
        against = [f'{reading}-driven: {", ".join(items)}' if reading != 'precision' else f'precision: {", ".join(items)}'
                   for reading, items in rev.items() if items]
        if against:
            self._note(p, 'Reversal of a predicted sign, which the preregistered rule counts as disconfirmation.  '
                       + '   ·   '.join(against), 'err')

        c = self._card(p, 'Tests across mice')
        rows, tags = [], []
        for key, name, what in CONTRASTS:
            d = g.get(key)
            if not d:
                continue
            pv, mean = d.get('p'), d.get('mean')
            sig = pv is not None and pv < 0.05
            res = ('▲  significant' if mean > 0 else '▼  significant') if sig else 'not significant'
            rows.append([name, what, d.get('n', ''), '' if mean is None else f'{mean:+.3f}', fmt_p(pv), res])
            tags.append('ok' if sig else 'muted')
        self._table(c, ['Contrast', 'What it measures', 'Mice', 'Mean', 'p', 'Result'], rows,
                    [240, 330, 55, 80, 75, 125], tags=tags, flex=(0, 1), optional=(1,))
        btns = tk.Frame(c, bg=C['surface'])
        btns.pack(anchor='w', pady=(12, 0))
        ttk.Button(btns, text='Open per-session table',
                   command=lambda: self._open(os.path.join(self.out_dir, 'per_session.csv'))).pack(side='left')
        ttk.Button(btns, text='Open output folder', command=lambda: self._open(self.out_dir)).pack(side='left', padx=8)

        try:
            per_session = pd.read_csv(os.path.join(self.out_dir, 'per_session.csv'))
        except (OSError, ValueError):
            per_session = None
        if per_session is not None and len(per_session):
            c = self._card(p, 'Each mouse')
            self._wrapped(c, "Each dot is one test mouse (its sessions averaged, as the group step does). The black "
                             'bar is the mean and its 95% interval; the dashed line is zero. Hover over a dot to see '
                             'the mouse.', self.F['small'], C['muted'],
                          inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(0, 6))
            host = tk.Frame(c, bg=C['surface'])
            host.pack(anchor='w', fill='x')
            try:
                plots.per_mouse(host, per_session, self._page_width() - int((2 * CARD_PAD + 2) * self.k), self.k)
            except Exception as e:
                label(host, f'Could not draw the chart: {e}', self.F['small'], fg=C['err']).pack(anchor='w')
        self._note(p, 'Reading the result: a positive result for either reading is informative, and a null result '
                      'rules out the rebound-driven reading, but a null result says little about the reset-driven '
                      'reading unless the task-related beta is strong.', 'warn')
        self._note(p, 'This tests P4(a) only. The hippocampal arm, P4(b), uses a different dataset (CRCNS hc-11), which '
                      'needs a free CRCNS account, and this script does not cover it.', 'ok')

    # ================================================================== Data page
    def _render_data(self, p):
        label(p, 'DATA', self.F['small_bold'], fg=C['accent']).pack(anchor='w')
        label(p, 'Data files', self.F['title']).pack(anchor='w', pady=(2, 6))
        demo_note = '  In demo mode nothing is really downloaded: small placeholder files stand in.' \
            if self.demo.get() else ''
        self._wrapped(p, 'Download the sample set, or tick any sessions from the Allen dataset, then download and '
                         'run just those.' + demo_note, self.F['body']).pack(anchor='w', fill='x', pady=(0, 16))
        running_here = self.busy and self.current == 'data'
        no_env = self.env_ready() is False
        rows = state.data_rows(self.out_dir, self.cache_dir)
        self._data_rows = {r['session']: r for r in rows}
        self.selected_sessions &= set(self._data_rows) if rows else set()

        # --- the sample set
        c = self._card(p, 'Sample set')
        sample = [r for r in rows if r['group'] == 'Sample']
        n = len(sample) or 4
        todo = sum(state.missing_gb(r, SAMPLE_PROBES) for r in sample) if sample else \
            n * (state.SESSION_GB + SAMPLE_PROBES * state.LFP_GB)
        self._wrapped(c, f'The {n} sessions of the 2 pilot mice, with {SAMPLE_PROBES} probes each: real recordings '
                         'you can run freely, because the pilot mice are never part of the test. Step 2 uses the same '
                         'files, so it will not download them again.', self.F['small'], C['muted'],
                      inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(0, 8))
        r0 = tk.Frame(c, bg=C['surface'])
        r0.pack(anchor='w', fill='x')
        blocked = 'disabled' if self.busy or no_env or (sample and todo < 0.01) else 'normal'
        self.sample_btn = ttk.Button(r0, text='⇩  Download sample set', style='Accent.TButton',
                                     command=self.download_sample, state=blocked)
        self.sample_btn.pack(side='left')
        label(r0, f'About {todo:.0f} GB still to download' if todo >= 0.01 else '✓  All sample files are here',
              self.F['small'], fg=C['muted'] if todo >= 0.01 else C['ok'], bg=C['surface']).pack(side='left', padx=12)

        if running_here:
            row = tk.Frame(p, bg=C['bg'])
            row.pack(anchor='w', fill='x', pady=(0, 10))
            self._stop_buttons(row, 'data')
            self._running_area(p, 'data')
        elif self.busy:
            label(p, f'“{STEP[self.current]["title"]}” is running. Things run one at a time.', self.F['small'],
                  fg=C['muted']).pack(anchor='w', pady=(0, 14))
        if no_env:
            self._note(p, 'The selected Python cannot download or run these yet. Choose the right interpreter in '
                          'Settings.', 'err')

        old = storage.old_lfp_releases(self.cache_dir)
        if old:
            self._note(p, f'LFP files from dataset release {", ".join(old)} are in this cache. Release 0.5.0 corrected an '
                          'LFP amplitude-scaling error, so do not reuse them: delete them, or choose a new cache folder.',
                       'err')

        # --- pick sessions
        c = self._card(p, 'Choose sessions')
        if not rows:
            self._wrapped(c, 'The list of sessions comes from step 1. Download sample set runs it for you, or:',
                          self.F['body'], C['muted'], inset=2 * CARD_PAD + 2).pack(anchor='w', fill='x', pady=(0, 8))
            ttk.Button(c, text='Get the session list (step 1, about 160 MB)',
                       state='disabled' if self.busy or no_env else 'normal',
                       command=lambda: self.run_step('list')).pack(anchor='w')
            return
        bar = tk.Frame(c, bg=C['surface'])
        bar.pack(anchor='w', fill='x', pady=(0, 8))
        label(bar, 'Show', self.F['small'], fg=C['muted']).pack(side='left', padx=(0, 8))
        for g in ('All',) + state.GROUPS:
            ttk.Radiobutton(bar, text=g, value=g, variable=self.data_filter, style='Card.TRadiobutton',
                            command=self._apply_data_filter).pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='Clear', style='Small.TButton', command=self._select_none).pack(side='right')
        ttk.Button(bar, text='Tick all shown', style='Small.TButton',
                   command=self._select_shown).pack(side='right', padx=6)
        table_rows = [[self._tick_mark(r['session']), r['session'], r['mouse'], r['level'], r['day'], r['group'],
                       self._files_text(r), state.result_text(r)] for r in rows]
        tags = ['err' if r['result'] and 'error' in r['result'] else 'ok' if r['result'] else '' for r in rows]
        self.data_table = self._table(c, DATA_COLUMNS, table_rows, [34, 112, 78, 80, 44, 72, 150, 240], height=11,
                                      tags=[t or 'plain' for t in tags], flex=(6, 7))
        self.data_table.configure(selectmode='none')
        self.data_table.bind('<ButtonRelease-1>', self._on_data_click)
        self._data_iids = dict(zip(self.data_table.get_children(), [r['session'] for r in rows]))
        self._iid_of = {sid: iid for iid, sid in self._data_iids.items()}
        self._apply_data_filter()

        self.data_summary = label(c, '', self.F['body_bold'], bg=C['surface'])
        self.data_summary.pack(anchor='w', pady=(12, 8))
        opts = tk.Frame(c, bg=C['surface'])
        opts.pack(anchor='w', fill='x', pady=(0, 6))
        label(opts, 'Probes per session', self.F['small'], fg=C['muted']).pack(side='left', padx=(0, 8))
        for v in (1, 2):
            ttk.Radiobutton(opts, text=str(v), value=v, variable=self.max_probes, style='Card.TRadiobutton',
                            state='disabled' if self.busy else 'normal',
                            command=self._update_data_buttons).pack(side='left', padx=(0, 8))
        ttk.Checkbutton(opts, text="Delete a session's LFP files after its result is saved", variable=self.cleanup,
                        style='Card.TCheckbutton', state='disabled' if self.busy else 'normal').pack(side='left',
                                                                                                padx=(16, 0))
        btns = tk.Frame(c, bg=C['surface'])
        btns.pack(anchor='w', fill='x', pady=(4, 0))
        self.dl_btn = ttk.Button(btns, text='⇩  Download selected', command=self.download_selected)
        self.dl_btn.pack(side='left')
        self.run_sel_btn = ttk.Button(btns, text='▶  Run selected', style='Accent.TButton', command=self.run_selected)
        self.run_sel_btn.pack(side='left', padx=8)
        self.del_btn = ttk.Button(btns, text='Delete their files…', command=self.delete_selected)
        self.del_btn.pack(side='left')
        self.data_reason = label(c, '', self.F['small'], fg=C['warn'], bg=C['surface'],
                                 wraplength=max(self._page_width() - int((2 * CARD_PAD + 20) * self.k), 300))
        self.data_reason.pack(anchor='w', fill='x', pady=(8, 0))
        self._update_data_buttons()

        # --- results of sessions run from here
        done = [r for r in rows if r['result'] and 'error' not in r['result']]
        sample_results = [r for r in done if r['group'] != 'Test']
        if sample_results:
            self._note(p, f'Sample and other sessions are saved separately in {state.sample_dir(self.out_dir)}, so '
                          'they never count in step 5.', 'ok')
        if done:
            if self.data_detail not in [r['session'] for r in done]:
                self.data_detail = done[-1]['session'] if not sample_results else sample_results[-1]['session']
            c = self._card(p, 'Result of a session')
            bar = tk.Frame(c, bg=C['surface'])
            bar.pack(anchor='w', fill='x', pady=(0, 6))
            label(bar, 'Session', self.F['small'], fg=C['muted']).pack(side='left', padx=(0, 8))
            box = ttk.Combobox(bar, values=[r['session'] for r in done], state='readonly', width=14,
                               font=self.F['small'])
            box.set(self.data_detail)
            box.pack(side='left')
            box.bind('<<ComboboxSelected>>', lambda e: self._show_data_detail(box.get()))
            host = tk.Frame(c, bg=C['surface'])
            host.pack(anchor='w', fill='x')
            try:
                plots.envelope(host, self._data_rows[self.data_detail]['result'],
                               self._page_width() - int((2 * CARD_PAD + 2) * self.k), self.k)
            except Exception as e:                   # a damaged result file must not break the page
                label(host, f'Could not draw this session: {e}', self.F['small'], fg=C['err']).pack(anchor='w')

    def _tick_mark(self, sid):
        return '☑' if sid in self.selected_sessions else '☐'

    @staticmethod
    def _files_text(r):
        if not r['has_session'] and not r['n_lfp']:
            return 'Not downloaded'
        parts = (['session'] if r['has_session'] else []) + ([f'{r["n_lfp"]} LFP'] if r['n_lfp'] else [])
        return ' + '.join(parts) + (f'  ({r["gb"]:.1f} GB)' if r['gb'] >= 0.05 else '')

    def _show_data_detail(self, sid):
        self.data_detail = int(sid)
        self._redraw_keep_scroll()

    def _on_data_click(self, event):
        tv = self.data_table
        iid = tv.identify_row(event.y)
        if not iid or tv.identify_region(event.x, event.y) not in ('cell', 'tree'):
            return
        sid = self._data_iids[iid]
        self.selected_sessions.symmetric_difference_update({sid})
        tv.set(iid, DATA_COLUMNS[0], self._tick_mark(sid))
        self._update_data_buttons()

    def _shown_sessions(self):
        g = self.data_filter.get()
        return [sid for sid, r in self._data_rows.items() if g == 'All' or r['group'] == g]

    def _apply_data_filter(self):
        """Show only the chosen group. Rows are detached, not deleted, so ticks survive switching."""
        tv, shown = self.data_table, set(self._shown_sessions())
        for i, (iid, sid) in enumerate(self._data_iids.items()):
            if sid in shown:
                tv.move(iid, '', i)
            else:
                tv.detach(iid)

    def _select_shown(self):
        self.selected_sessions |= set(self._shown_sessions())
        self._refresh_ticks()

    def _select_none(self):
        self.selected_sessions.clear()
        self._refresh_ticks()

    def _refresh_ticks(self):
        for iid, sid in self._data_iids.items():
            self.data_table.set(iid, DATA_COLUMNS[0], self._tick_mark(sid))
        self._update_data_buttons()

    def _run_block_reason(self):
        """Why the selected sessions cannot be run right now, or None."""
        sel = [self._data_rows[s] for s in self.selected_sessions]
        if not sel:
            return None
        if not os.path.exists(os.path.join(self.out_dir, 'band.json')):
            return 'Running needs the beta band: finish step 2 first.'
        tests = [r for r in sel if r['group'] == 'Test']
        if tests and not self.demo.get() and self.states.get('analyze') == 'locked':
            return (f'{len(tests)} selected session(s) are test sessions, which stay locked until the preregistration '
                    'is locked (step 3): that keeps the plan on record before any test data are analysed. Untick them '
                    'to run the others now. Downloading them is fine.')
        return None

    def _update_data_buttons(self):
        sel = [self._data_rows[s] for s in self.selected_sessions if s in self._data_rows]
        probes = self.max_probes.get()
        todo = sum(state.missing_gb(r, probes) for r in sel)
        free = cfg.free_gb(self.cache_dir)
        text = f'{len(sel)} selected' if sel else 'Nothing selected: click a row to tick it'
        if sel:
            text += f'   ·   about {todo:.0f} GB to download'
            if free is not None:
                text += f'   ·   {free:.0f} GB free'
        short = bool(sel) and free is not None and todo > free and not self.demo.get()
        self.data_summary.configure(text=text, fg=C['err'] if short else C['text'])
        no = self.busy or self.env_ready() is False or not sel
        reason = self._run_block_reason()
        self.dl_btn.configure(state='disabled' if no or todo < 0.01 else 'normal')
        self.run_sel_btn.configure(state='disabled' if no or reason else 'normal')
        self.del_btn.configure(state='disabled' if self.busy or not any(r['gb'] > 0 or r['has_session'] or r['n_lfp']
                                                                        for r in sel) else 'normal')
        self.data_reason.configure(text=reason or '')

    def download_sample(self):
        if self.busy:
            return
        rows = state.data_rows(self.out_dir, self.cache_dir)
        if not rows:                                  # step 1 first, then come back
            self._after_ok = self.download_sample
            return self.run_step('list')
        ids = [r['session'] for r in rows if r['group'] == 'Sample']
        self.selected = 'data'
        self.selected_sessions |= set(ids)
        self._start_download(ids, SAMPLE_PROBES, rows)

    def download_selected(self):
        if not self.busy:
            self._start_download(sorted(self.selected_sessions), self.max_probes.get(),
                                 state.data_rows(self.out_dir, self.cache_dir))

    def _start_download(self, ids, probes, rows):
        by_id = {r['session']: r for r in rows}
        need = sum(state.missing_gb(by_id[s], probes) for s in ids)
        free = cfg.free_gb(self.cache_dir)
        if not self.demo.get() and free is not None and free < need + 5:
            if not messagebox.askyesno('Low disk space', f'These files are about {need:.0f} GB and the drive has '
                                                         f'{free:.0f} GB free.\n\nStart anyway?', icon='warning'):
                return
        self.current, self.started, self.polls_while_running = 'data', time.time(), 0
        self.selected = 'data'
        self._launch(self._download_args(ids, probes), log_name='download',
                     display=f'Download {len(ids)} session(s), {probes} probe(s) each')
        self.refresh()
        self._set_status(f'Downloading {len(ids)} session(s)…')

    def run_selected(self):
        if self.busy or not self.selected_sessions or self._run_block_reason():
            return
        groups = state.session_groups(self.out_dir)
        sample = state.sample_dir(self.out_dir)
        again = [s for s in self.selected_sessions if groups[s] != 'Test'
                 and os.path.exists(os.path.join(sample, 'sessions', f'{s}.json'))]
        if again and messagebox.askyesno('Run again?', f'{len(again)} of the selected sample session(s) already have a '
                                                       'result. Run them again and replace it?'):
            for s in again:
                os.remove(os.path.join(sample, 'sessions', f'{s}.json'))
        if any(groups[s] != 'Test' for s in self.selected_sessions):
            os.makedirs(sample, exist_ok=True)         # vbn_p4.py analyze reads band.json from its output folder
            shutil.copy2(os.path.join(self.out_dir, 'band.json'), os.path.join(sample, 'band.json'))
        order = [r['session'] for r in state.data_rows(self.out_dir, self.cache_dir)
                 if r['session'] in self.selected_sessions]
        queue = SessionQueue(order, self.out_dir, self.cache_dir, max_probes=self.max_probes.get(),
                             cleanup=self.cleanup.get(), check_space=not self.demo.get(),
                             out_for=lambda s: self.out_dir if groups[s] == 'Test' else sample)
        if not queue.pending:
            return self._set_status('Every selected session already has a result.')
        self.queue = queue
        self.current, self.started, self.polls_while_running = 'data', time.time(), 0
        self.selected = 'data'
        self._append(f'Running {queue.total} selected session(s), one at a time.', 'ok')
        self._queue_next()

    def delete_selected(self):
        sel = [self._data_rows[s] for s in self.selected_sessions if self._data_rows[s]['gb'] > 0
               or self._data_rows[s]['has_session'] or self._data_rows[s]['n_lfp']]
        if not sel or self.busy:
            return
        gb = sum(r['gb'] for r in sel)
        if not messagebox.askyesno('Delete downloaded files', f'Delete the downloaded files of {len(sel)} session(s), '
                                   f'about {gb:.1f} GB?\n\nResults are kept. You can download the files again later.'):
            return
        freed = 0.0
        for r in sel:
            freed += storage.delete_session_files(self.cache_dir, r['session'])[1]
        self._append(f'Deleted the downloaded files of {len(sel)} session(s) ({freed:.1f} GB).', 'ok')
        self._redraw_keep_scroll()
        self._set_status(f'Freed {freed:.1f} GB.')

    def _render_settings(self, p):
        label(p, 'SETTINGS', self.F['small_bold'], fg=C['accent']).pack(anchor='w')
        label(p, 'Settings', self.F['title']).pack(anchor='w', pady=(2, 6))
        self._wrapped(p, 'Which Python runs the analysis, and where the downloaded data and the results go. '
                         'Changes are saved automatically.', self.F['body']).pack(anchor='w', fill='x', pady=(0, 18))
        card_in = 2 * CARD_PAD + 2

        # --- Python environment
        c = self._card(p, 'Python environment')
        self._wrapped(c, 'vbn_p4.py runs in its own Python 3.10 environment with AllenSDK 2.16.2. Demo data '
                         'need only numpy, scipy, pandas and xarray.', self.F['small'], C['muted'],
                      inset=card_in).pack(anchor='w', fill='x', pady=(0, 8))
        self._path_row(c, 'Interpreter', self.python_var, kind='python')
        e = self.env
        if e is None:
            text, kind = 'Checking… (importing AllenSDK can take a minute)', 'muted'
        else:
            kind = 'ok' if e['real_ok'] else ('warn' if e['demo_ok'] else 'err')
            text = ('✓  ' if e['real_ok'] else '⚠  ' if e['demo_ok'] else '✗  ') + e['message']
        row = tk.Frame(c, bg=C['surface'])
        row.pack(anchor='w', fill='x', pady=(6, 0))
        label(row, text, self.F['body_bold'], fg=C['muted'] if kind == 'muted' else C[kind]).pack(side='left')
        ttk.Button(row, text='Check again', style='Small.TButton', command=self.check_env,
                   state='disabled' if e is None else 'normal').pack(side='left', padx=10)
        if e is not None and not e['real_ok']:
            found = cfg.detect_python()
            if found and os.path.normcase(found) != os.path.normcase(self.python):
                r = tk.Frame(c, bg=C['surface'])
                r.pack(anchor='w', fill='x', pady=(8, 0))
                label(r, f'Found a vbn environment:  {found}', self.F['small']).pack(side='left')
                ttk.Button(r, text='Use this', style='Small.TButton',
                           command=lambda: self._set_python(found)).pack(side='left', padx=8)
            box = tk.Frame(c, bg=C['bg'], padx=int(NOTE_PAD * self.k), pady=int(10 * self.k))
            box.pack(anchor='w', fill='x', pady=(12, 0))
            top = tk.Frame(box, bg=C['bg'])
            top.pack(anchor='w', fill='x')
            label(top, f'To set it up, run these in a terminal in {ROOT} (needs uv). The window then finds '
                       '.venv-vbn by itself:', self.F['small'], fg=C['muted']).pack(side='left')
            ttk.Button(top, text='Copy', style='Small.TButton',
                       command=lambda: self._copy('\n'.join(cfg.INSTALL_COMMANDS), 'Commands')).pack(side='right')
            for line in cfg.INSTALL_COMMANDS:
                label(box, line, self.F['mono']).pack(anchor='w')
            label(box, 'With conda instead: conda create -n vbn python=3.10, then pip install allensdk==2.16.2 hdmf==3.14.6 scipy '
                       'xarray inside it.', self.F['small'], fg=C['muted']).pack(anchor='w', pady=(6, 0))

        gb = cfg.ram_gb()
        if gb is not None:
            enough = gb >= cfg.RAM_RECOMMENDED - 2                 # a "32 GB" computer reports a little under 32
            label(c, f'{"✓" if enough else "⚠"}  {gb:.0f} GB of memory.  Each probe\'s LFP file (~4.5 GB) is loaded into '
                     f'memory; the guide asks for at least {cfg.RAM_MINIMUM} GB, {cfg.RAM_RECOMMENDED} GB recommended.',
                  self.F['small'], fg=C['ok'] if enough else C['err'] if gb < cfg.RAM_MINIMUM - 2 else C['warn'],
                  bg=C['surface']).pack(anchor='w', pady=(10, 0))

        # --- folders
        c = self._card(p, 'Folders for real data')
        self._path_row(c, 'Download cache', self.cache_var, kind='dir', on_commit=self._update_free)
        self._free_lbl = label(c, '', self.F['small'])
        self._free_lbl.pack(anchor='w', pady=(2, 10))
        self._update_free()
        self._path_row(c, 'Results', self.out_var, kind='dir', open_btn=True)
        self._note(c, f'Demo runs always use {os.path.normpath(os.path.join(self.base, "demo_run"))}, so synthetic '
                      'results never mix '
                      'with real ones.', 'ok', pady=(12, 0), in_card=True)

        # --- analysis
        c = self._card(p, 'Analysis')
        label(c, 'Probes per session (step 4)', self.F['body_bold']).pack(anchor='w')
        r = tk.Frame(c, bg=C['surface'])
        r.pack(anchor='w', pady=(4, 4))
        for v, t in ((1, '1 probe  (~7.5 GB per session)'), (2, '2 probes  (~12 GB per session, more channels)')):
            ttk.Radiobutton(r, text=t, value=v, variable=self.max_probes, style='Card.TRadiobutton',
                            state='disabled' if self.busy else 'normal').pack(anchor='w')
        self._wrapped(c, 'The guide uses 1 probe. Two probes double the channels and the downloads.', self.F['small'],
                      C['muted'], inset=card_in).pack(anchor='w', fill='x')
        label(c, 'Disk space during step 4', self.F['body_bold']).pack(anchor='w', pady=(14, 0))
        ttk.Checkbutton(c, text="Delete each session's LFP files once its result is saved", variable=self.cleanup,
                        style='Card.TCheckbutton', state='disabled' if self.busy else 'normal').pack(anchor='w',
                                                                                                pady=(4, 4))
        self._wrapped(c, 'Only files named like probe_…_lfp.nwb inside that session\'s cache folder are deleted, and '
                         'only after its result file exists without an error. Sessions that failed keep their files '
                         'for checking. The 3 GB session file is always kept, so each finished session takes 3 GB instead of '
                         '~7.7 GB (the ~50 GB pilot download from step 2 is not affected).', self.F['small'], C['muted'],
                      inset=card_in).pack(anchor='w', fill='x')

    def _path_row(self, parent, title, var, kind='dir', on_commit=None, open_btn=False):
        """Label, text box, Browse (and Open). Typing is committed on Enter or when the box loses focus."""
        label(parent, title, self.F['body_bold']).pack(anchor='w', pady=(4, 2))
        r = tk.Frame(parent, bg=C['surface'])
        r.pack(anchor='w', fill='x')
        ent = ttk.Entry(r, textvariable=var, font=self.F['body'])
        ent.pack(side='left', fill='x', expand=True, ipady=int(3 * self.k))

        def commit(event=None):
            self._settings_changed(var)
            if on_commit:
                on_commit()
        ent.bind('<Return>', commit)
        ent.bind('<FocusOut>', commit)

        def browse():
            cur = var.get().strip()
            if kind == 'python':
                start = os.path.dirname(cur) if cur else os.path.expanduser('~')
                ft = [('Python', 'python.exe'), ('All files', '*.*')] if sys.platform == 'win32' else None
                path = filedialog.askopenfilename(title='Choose the Python interpreter', initialdir=start,
                                                  **({'filetypes': ft} if ft else {}))
            else:
                path = filedialog.askdirectory(title=f'Choose the {title.lower()} folder', mustexist=False,
                                               initialdir=cur if os.path.isdir(cur) else os.path.expanduser('~'))
            if path:
                var.set(os.path.normpath(path))
                commit()
        ttk.Button(r, text='Browse…', command=browse).pack(side='left', padx=(8, 0))
        if open_btn:
            ttk.Button(r, text='Open', command=lambda: self._open(var.get().strip())).pack(side='left', padx=(6, 0))

    def _settings_changed(self, var):
        if var is self.python_var:
            changed = self.env is None or os.path.normcase(self.env.get('path') or '') != os.path.normcase(self.python)
            self.save_settings()
            if changed:
                self.check_env()
                if self.selected == 'settings':
                    self._render_page()
        else:
            self.save_settings()
            self.refresh_chrome()              # the results folder decides which steps are done

    def _set_python(self, path):
        self.python_var.set(path)
        self._settings_changed(self.python_var)

    def _update_free(self):
        if not getattr(self, '_free_lbl', None) or not self._free_lbl.winfo_exists():
            return
        gb = cfg.free_gb(self.cache_var.get().strip() or '.')
        if gb is None:
            self._free_lbl.configure(text='Drive not found.', fg=C['err'])
            return
        # what the whole run needs: the pilot (~50 GB, step 2) + what step 4 keeps + one session in progress.
        # Uses the real number of test sessions once step 1 has run, otherwise the guide's 32.
        ls = state.list_summary(os.path.normpath(self.out_var.get().strip()))
        n = ls['n_test_sessions'] if ls else 2 * GUIDE_TEST_MICE
        kept = disk_estimate(n, self.max_probes.get(), self.cleanup.get())[1]
        need = 50 + kept + cfg.space_needed_gb('analyze', self.max_probes.get())
        basis = (f'{n} test sessions{" from step 1" if ls else " (the guide)"}, LFP clean-up '
                 f'{"on" if self.cleanup.get() else "off"}')
        if gb < 60:
            text, kind = f'{gb:.0f} GB free on this drive: not enough even for the pilot (~50 GB).', 'err'
        elif gb < need:
            text, kind = (f'{gb:.0f} GB free on this drive; the whole run needs about {need:.0f} GB ({basis}). '
                          'Enough for the pilot, not for all of step 4: choose a bigger drive.'), 'warn'
        else:
            text, kind = f'{gb:.0f} GB free on this drive; the whole run needs about {need:.0f} GB ({basis}).', 'ok'
        self._free_lbl.configure(text=text, fg=C[kind], wraplength=max(self._page_width() - int(80 * self.k), 300))

    # ================================================================== actions
    def run_step(self, step=None):
        if self.busy:
            return
        step = step or self.selected
        if step not in ('list', 'band', 'analyze', 'group'):
            return
        if step == 'analyze':
            return self.start_queue()
        cache, out = self.cache_dir, self.out_dir
        if not self.demo.get():
            need = cfg.space_needed_gb(step, self.max_probes.get())
            free = cfg.free_gb(cache)
            if need >= 1 and free is not None and free < need + 5:
                if not messagebox.askyesno('Low disk space',
                                           f'The download cache drive has {free:.0f} GB free, and this step needs '
                                           f'about {need:.0f} GB.\n\nStart anyway?', icon='warning'):
                    return
        self.current, self.started, self.polls_while_running = step, time.time(), 0
        self.selected = step
        groups = state.session_groups(out)
        pilot = [s for s, g in groups.items() if g == 'Sample']
        if step == 'band' and pilot:                # never let the analysis meet a file from an interrupted download
            self._after_ok = lambda: self._launch_step('band')
            self._launch(self._download_args(pilot, SAMPLE_PROBES), log_name='download',
                         display=f'Check or download the {len(pilot)} pilot sessions')
            self.refresh()
            self._set_status(f'Getting the pilot files for “{STEP[step]["title"]}”…')
            return
        self._launch_step(step)

    def _launch_step(self, step):
        self._launch(step_command(self.python, step, self.cache_dir, self.out_dir), log_name=step)
        self.refresh()
        self._set_status(f'Running “{STEP[step]["title"]}”…')

    def _download_args(self, ids, probes):
        args = [self.python, '-u', DOWNLOADER, '--cache', self.cache_dir, '--max-probes', str(probes)]
        for s in ids:
            args += ['--session', str(s)]
        return args

    def _launch(self, args, log_name, display=None):
        env = demo_env(self.scenario.get()) if self.demo.get() else None
        self._append((f'$ {display}' if display else f'$ vbn_p4.py {" ".join(args[3:])}')
                     + (f'    [demo data: {self.scenario.get()}]' if env else ''), 'cmd')
        self.latest = 'Starting…'
        self.runner.start(args, env=env, log_path=log_path_for(self.out_dir, log_name))

    # ------------------------------------------------------------------ step 4: one session at a time
    def start_queue(self):
        a = state.analyze_summary(self.out_dir)
        todo = [r['session'] for r in (a or {}).get('rows', []) if r['status'] == 'waiting']
        if not todo:
            self._set_status('Every test session already has a result.')
            return
        self.queue = SessionQueue(todo, self.out_dir, self.cache_dir, max_probes=self.max_probes.get(),
                                  cleanup=self.cleanup.get(), check_space=not self.demo.get())
        self.current, self.started, self.polls_while_running = 'analyze', time.time(), 0
        self.selected = 'analyze'
        self._append(f'Analysing {len(todo)} session{"s" if len(todo) > 1 else ""}, one at a time '
                     f'({self.max_probes.get()} probe{"s" if self.max_probes.get() > 1 else ""} each).', 'ok')
        self._queue_next()

    def _queue_next(self):
        q = self.queue
        sid = q.next()
        if sid is None:
            return self._queue_done()
        self.session_started = time.time()
        self._queue_phase = 'download'              # first make sure its files are whole, then analyse it
        self._launch(self._download_args([sid], q.max_probes), log_name=f'download_{sid}',
                     display=f'Check or download session {sid}')
        self.refresh()
        self._set_status(f'Analysing session {sid}  ({q.done_count + 1} of {q.total})…')

    def _queue_done(self):
        q, self.queue = self.queue, None
        secs = time.time() - self.started
        n_ok = len([r for r in q.results if r[1] == 'ok'])
        n_err = len([r for r in q.results if r[1] == 'error'])
        freed = f'  Freed {q.freed_gb:.1f} GB of LFP files.' if q.freed_gb >= 0.05 else ''
        if q.paused_reason:
            msg, tag = q.paused_reason, 'err'
        elif q.stop_requested and q.pending:
            msg, tag = f'Stopped after {n_ok + n_err} session(s), as asked. {len(q.pending)} still to do.', 'ok'
        else:
            msg, tag = f'Finished: {n_ok} session(s) analysed' + (f', {n_err} without a usable result' if n_err else
                                                                  '') + f' in {fmt_elapsed(secs)}.', 'ok'
        self._append(msg + freed, tag)
        self.refresh()
        self._set_status(msg + freed)
        if tag == 'err':
            self.toggle_log(True)

    # ------------------------------------------------------------------ step 3: preregistration
    def export_bundle(self):
        try:
            path = prereg.export_bundle(self.out_dir)
        except (OSError, ValueError) as e:
            messagebox.showerror('Export failed', str(e))
            return
        self._append(f'Preregistration bundle written: {path}', 'ok')
        self._set_status(f'Bundle exported: {os.path.basename(path)}')
        self._redraw_keep_scroll()
        self._open(os.path.dirname(path))

    def lock_prereg(self):
        url = self.osf_var.get().strip()
        if not url:
            if not messagebox.askyesno('No OSF link', 'Lock without an OSF link? The lock record will not include '
                                                      'one.'):
                return
        elif not url.startswith('http'):
            messagebox.showwarning('OSF link', 'The OSF link should start with https://')
            return
        if not messagebox.askyesno('Lock preregistration',
                                   'Lock now? The checksums of vbn_p4.py, band.json and split.json are recorded. '
                                   'If any of them changes later, steps 4 and 5 are blocked for real data.\n\n'
                                   'Only lock after the bundle is uploaded to OSF.'):
            return
        rec = prereg.lock(self.out_dir, url)
        self._append(f'Preregistration locked {prereg.nice_date(rec["locked_at"])}'
                     + (f' ({url})' if url else ''), 'ok')
        self.refresh()
        self._set_status('Preregistration locked. Step 4 can run now.')

    def withdraw_prereg(self):
        if not messagebox.askyesno('Withdraw lock', 'Withdraw the preregistration lock?\n\nDo this only if the '
                                   'preregistration itself is withdrawn or replaced on OSF. The old lock is kept '
                                   'as a record in the output folder.', icon='warning'):
            return
        prereg.withdraw(self.out_dir)
        self._append('Preregistration lock withdrawn (kept as prereg_lock.withdrawn_*.json).', 'err')
        self.refresh()

    def stop_after_session(self):
        if self.queue:
            self.queue.stop_after_current()
            self._append('Will stop when the current session finishes.', 'ok')
            self._redraw_keep_scroll()

    def retry_failed(self):
        """Delete the result files that recorded an error, so step 4 runs those sessions again."""
        a = state.analyze_summary(self.out_dir)
        failed = [r['session'] for r in (a or {}).get('rows', []) if r['status'] == 'error']
        if not failed or not messagebox.askyesno(
                'Retry failed sessions', f'Remove the result files of {len(failed)} failed session(s) so they are '
                                         'analysed again next time?\n\nTheir error messages are in the logs folder.'):
            return
        for sid in failed:
            try:
                os.remove(os.path.join(self.out_dir, 'sessions', f'{sid}.json'))
            except OSError:
                pass
        self.refresh()
        self._set_status(f'{len(failed)} session(s) will be analysed again.')

    def stop(self):
        if self.runner.is_running:
            self._append('Stopping…', 'err')
            if self.queue:
                self.queue.paused_reason = ('Stopped. Finished sessions are kept; the interrupted one will run again '
                                            'next time you click Analyse sessions.')
            self.runner.stop()

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self._in_progress = False

    def toggle_log(self, show=None):
        self.log_open = (not self.log_open) if show is None else show
        if self.log_open:
            self.log_body.pack(fill='x')
            self.log.see('end')
        else:
            self.log_body.pack_forget()
        self._update_log_toggle()

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno('Step running', f'“{STEP[self.current]["title"]}” is still running. '
                                                       'Stop it and quit?'):
                return
            self.runner.stop()
            self.runner.wait(5)
        self.root.destroy()

    def _mode_changed(self):
        self.selected_sessions.clear()              # demo and real data have different sessions
        self.save_settings()
        self.refresh()
        self._set_status('Using synthetic demo data.' if self.demo.get() else 'Using the real Allen data.')

    def _copy(self, text, what):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status(f'{what} copied to the clipboard.')

    def _copy_hash(self):
        if self.script_hash:
            self._copy(self.script_hash, 'Checksum')

    def _open(self, path):
        if os.path.exists(path):
            open_path(path)
        else:
            self._set_status(f'Not there yet: {path}')

    # ================================================================== queue polling
    def _poll(self):
        if self.busy != self.awake:
            self.awake = self.busy
            keep_awake(self.awake)
        if self.busy:
            self.polls_while_running += 1
        for _ in range(MAX_PER_TICK):
            try:
                msg = self.runner.messages.get_nowait()
            except Exception:
                break
            if msg[0] == 'line':
                text = msg[1]
                bad = text.startswith('Traceback') or 'Error' in text or text.startswith('WARNING')
                self._append(text, 'err' if bad else None)
                if 'MemoryError' in text or 'Unable to allocate' in text:
                    self._append('Out of memory while loading LFP: use 1 probe per session, close other programs, or '
                                 'use a computer with more RAM.', 'err')
                if text.strip():
                    self.latest = text.strip()
            elif msg[0] == 'progress':
                self._append(msg[1], 'dim', progress=True)
                self.latest = msg[1].strip()
            elif msg[0] == 'exit':
                self._finished(msg[1], msg[2])
        if self._env_result is not None:            # the environment check finished
            self.env, self._env_result, self._checking = self._env_result, None, False
            self.refresh_chrome()
            if self.selected == 'settings' or STEP.get(self.selected, {}).get('run'):
                self._redraw_keep_scroll()          # Run buttons and the Settings page depend on it
        if self.busy:
            self._tick_running()
        self.root.after(POLL_MS, self._poll)

    def _tick_running(self):
        el = fmt_elapsed(time.time() - self.started) if self.started else ''
        q = self.queue
        if q:
            n = min(q.done_count + 1, q.total)
            eta = q.eta_seconds()
            head = f'Session {n} of {q.total}  ·  {q.current or ""}  ·  {el} so far'
            head += f'  ·  about {fmt_elapsed(eta)} left' if eta is not None else '  ·  time left shown after the first session'
            side = f'Session {n} of {q.total}  ·  {el}'
        else:
            head = side = f'Running  ·  {el}'
        if self.elapsed_lbl is not None and self.elapsed_lbl.winfo_exists():
            self.elapsed_lbl.configure(text=head)
        if q and self.progress is not None and self.progress.winfo_exists():
            self.progress.configure(value=q.done_count)
        if self.latest_lbl is not None and self.latest_lbl.winfo_exists():
            t = self.latest
            self.latest_lbl.configure(text=t if len(t) < 140 else t[:137] + '…')
        it = self.step_items.get(self.current)
        if it:
            it['sub'].configure(text=side)

    def _finished(self, code, secs):
        if self.queue is not None:                  # step 4: record this session, then start the next one
            q = self.queue
            if self._queue_phase == 'download' and code == 0:
                self._queue_phase = 'analyze'
                self._launch(step_command(self.python, 'analyze', self.cache_dir, q.out_for(q.current),
                                          session=q.current, max_probes=q.max_probes),
                             log_name=f'analyze_{q.current}')
                return
            msg = self.queue.finished(code)
            self._append(msg, 'ok' if code == 0 else 'err')
            self.refresh_chrome()
            return self._queue_next()
        ok = code == 0
        title = STEP[self.current]['title']
        after, self._after_ok = self._after_ok, None
        if ok and after:                            # the next part of what was asked for
            return after()
        self._append(f'{self.current} {"finished" if ok else "FAILED"} (exit {code}) in {secs:.1f} s',
                     'ok' if ok else 'err')
        self.refresh()
        if ok:
            self._set_status(f'“{title}” finished in {fmt_elapsed(secs)}.')
        else:
            self._set_status(f'“{title}” stopped or failed after {fmt_elapsed(secs)}. The log below has the details.')
            self.toggle_log(True)

    # ================================================================== widgets
    def _append(self, text, tag=None, progress=False):
        at_bottom = self.log.yview()[1] > 0.999
        self.log.configure(state='normal')
        if self._in_progress:                       # replace the previous progress-bar line
            self.log.delete('progress', 'end-1c')
        self.log.mark_set('progress', 'end-1c')
        self.log.insert('end', text + '\n', tag or ())
        self._in_progress = progress
        self.log.configure(state='disabled')
        if at_bottom:
            self.log.see('end')

    def _update_log_toggle(self):
        self.log_toggle.configure(text=('▾  Hide technical log' if self.log_open else '▸  Show technical log'))

    def _set_status(self, text):
        self.status.configure(text=text)

    def _page_width(self):
        """Usable width inside the page margins, from the canvas width (or a default before the window is shown)."""
        w = self.canvas.winfo_width()
        return (w if w > 50 else int(1000 * self.k)) - 2 * int(PAGE_PAD * self.k)

    def _on_resize(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)
        if self._drawn_w is not None and abs(event.width - self._drawn_w) > 4:
            if self._redraw_job:
                self.root.after_cancel(self._redraw_job)
            self._redraw_job = self.root.after(150, self._redraw_keep_scroll)

    def _redraw_keep_scroll(self):
        self._redraw_job = None
        top = self.canvas.yview()[0]
        self._render_page()
        self.root.update_idletasks()
        self.canvas.yview_moveto(top)

    def _on_wheel(self, event):
        """Scroll the page when the pointer is over it (tables and the log keep their own scrolling)."""
        w = event.widget
        if isinstance(w, str) or isinstance(w, ttk.Treeview) or w is self.log:
            return
        if str(w).startswith(str(self._holder)) and self.page_host.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), 'units')

    def _rewrap(self, event=None):
        """Wrap each long label to the page width minus the padding of the cards/notes it sits in."""
        page = self._page_width()
        for lbl, inset in self._wrap:
            if lbl.winfo_exists():
                lbl.configure(wraplength=max(min(page - int((inset + 16) * self.k), int(1000 * self.k)), 200))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='python -m p4_app', description='P4 analysis window')
    ap.add_argument('--demo', action='store_true', help='start with synthetic data selected')
    ap.add_argument('--scenario', choices=SCENARIOS, help='demo scenario (default: the saved one)')
    ap.add_argument('--python', help='interpreter that has allensdk installed (default: the saved one)')
    a = ap.parse_args(argv)
    enable_dpi_awareness()
    if sys.platform == 'win32':                 # group the window under its own taskbar icon, not under Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('P4Analysis.App')
        except Exception:
            pass
    root = tk.Tk()
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'p4.ico')
    if sys.platform == 'win32' and os.path.exists(icon):
        root.iconbitmap(default=icon)
    app = App(root, demo=True if a.demo else None, scenario=a.scenario, python=a.python)

    def report_error(exc, value, tb):           # an installed app has no console: show errors in the window
        app._append(''.join(traceback.format_exception(exc, value, tb)).rstrip(), 'err')
        app._set_status('Something went wrong; the log below has the details.')
        app.toggle_log(True)
    root.report_callback_exception = report_error
    root.mainloop()
