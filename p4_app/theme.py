"""Colours, fonts and ttk styles for the P4 window, in one place."""
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

C = dict(
    bg='#f4f6f9', surface='#ffffff', sidebar='#e9edf3', sidebar_sel='#ffffff', border='#d8dee8',
    text='#1d2433', muted='#5f6b7d', faint='#97a1b0',
    accent='#2f6fde', accent_hover='#2559b8', accent_soft='#e8f0fd',
    ok='#1f8a4c', ok_bg='#e6f4ec', warn='#9a6210', warn_bg='#fdf3e1', err='#c0392b', err_bg='#fbe9e7',
    header='#1f2a44', header_muted='#aab4c8', log_bg='#1e2430', log_text='#d6dbe4', log_dim='#8792a5',
)


def enable_dpi_awareness():
    """Crisp text on scaled Windows displays (otherwise Windows bitmap-stretches the whole window)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _pick(root, *names):
    have = set(tkfont.families(root))
    return next((n for n in names if n in have), 'TkDefaultFont')


def setup(root):
    """Create named fonts and ttk styles. Returns the fonts as a dict."""
    ui = _pick(root, 'Segoe UI', 'Helvetica Neue', 'Helvetica', 'Arial')
    mono = _pick(root, 'Cascadia Mono', 'Consolas', 'Menlo', 'Courier New')
    F = dict(
        title=tkfont.Font(root, family=ui, size=17, weight='bold'),
        h2=tkfont.Font(root, family=ui, size=12, weight='bold'),
        body=tkfont.Font(root, family=ui, size=11),
        body_bold=tkfont.Font(root, family=ui, size=11, weight='bold'),
        small=tkfont.Font(root, family=ui, size=9),
        small_bold=tkfont.Font(root, family=ui, size=9, weight='bold'),
        big=tkfont.Font(root, family=ui, size=22, weight='bold'),
        step_num=tkfont.Font(root, family=ui, size=10, weight='bold'),
        mono=tkfont.Font(root, family=mono, size=9),
    )
    root.configure(background=C['bg'])
    k = root.winfo_fpixels('1i') / 96            # ttk sizes below are pixels, so scale them with the DPI
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', font=F['body'], background=C['bg'], foreground=C['text'])
    s.configure('Accent.TButton', font=F['body_bold'], foreground='white', background=C['accent'],
                bordercolor=C['accent'], lightcolor=C['accent'], darkcolor=C['accent'], padding=(18, 9))
    s.map('Accent.TButton',
          background=[('disabled', '#a9bfe8'), ('active', C['accent_hover'])],
          bordercolor=[('disabled', '#a9bfe8'), ('active', C['accent_hover'])],
          lightcolor=[('disabled', '#a9bfe8'), ('active', C['accent_hover'])],
          darkcolor=[('disabled', '#a9bfe8'), ('active', C['accent_hover'])],
          foreground=[('disabled', '#eef3fc')])
    s.configure('TButton', font=F['body'], background=C['surface'], bordercolor=C['border'],
                lightcolor=C['surface'], darkcolor=C['surface'], padding=(14, 8))
    s.map('TButton', background=[('active', C['accent_soft']), ('disabled', C['bg'])],
          foreground=[('disabled', C['faint'])])
    s.configure('Small.TButton', font=F['small'], padding=(8, 3))
    s.configure('Treeview', font=F['body'], rowheight=int(28 * k), background=C['surface'], fieldbackground=C['surface'],
                bordercolor=C['border'], lightcolor=C['border'], darkcolor=C['border'])
    s.map('Treeview', background=[('selected', C['accent_soft'])], foreground=[('selected', C['text'])])
    s.configure('Treeview.Heading', font=F['small_bold'], background=C['sidebar'], foreground=C['muted'],
                bordercolor=C['border'], lightcolor=C['sidebar'], darkcolor=C['sidebar'], padding=(8, 6))
    s.configure('Accent.Horizontal.TProgressbar', troughcolor=C['border'], background=C['accent'],
                bordercolor=C['border'], lightcolor=C['accent'], darkcolor=C['accent'], thickness=int(7 * k))
    s.configure('Ok.Horizontal.TProgressbar', troughcolor=C['border'], background=C['ok'],
                bordercolor=C['border'], lightcolor=C['ok'], darkcolor=C['ok'], thickness=int(7 * k))
    for w in ('TRadiobutton', 'TCheckbutton'):        # disabled/hover keep the background they sit on
        s.map(w, background=[('disabled', C['bg']), ('active', C['bg'])], foreground=[('disabled', C['faint'])])
        s.configure(f'Side.{w}', background=C['sidebar'], font=F['body'])
        s.map(f'Side.{w}', background=[('disabled', C['sidebar']), ('active', C['sidebar'])],
              foreground=[('disabled', C['faint'])])
        s.configure(f'Card.{w}', background=C['surface'], font=F['body'])
        s.map(f'Card.{w}', background=[('disabled', C['surface']), ('active', C['surface'])],
              foreground=[('disabled', C['faint'])])
    s.configure('TCombobox', padding=4)
    return F


def label(parent, text='', font=None, fg=None, bg=None, **kw):
    """A plain tk.Label that inherits its parent's background (ttk labels make that awkward)."""
    return tk.Label(parent, text=text, font=font, fg=fg or C['text'], bg=bg or parent['bg'],
                    anchor=kw.pop('anchor', 'w'), justify=kw.pop('justify', 'left'), **kw)
