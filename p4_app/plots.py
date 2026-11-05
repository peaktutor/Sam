"""
Charts drawn inside the window (matplotlib in Tk), with a hover read-out.

Colours: the two series use slots 1-2 of the validated reference palette (blue, orange; checked with the
dataviz validator: CVD dE 24.7, normal-vision dE 33.6, both >= 3:1 on the card surface). Text uses ink colours,
never the series colour. matplotlib is imported only when a chart is first drawn (it takes a moment to load).
"""
import numpy as np

from .theme import C

SERIES = ('#2a78d6', '#eb6834')                 # change flash, flash before the change
WINDOW_FILL = '#eef1f5'
GRID = '#e3e7ee'


def _mpl():
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib import font_manager
    from matplotlib.figure import Figure
    have = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in ('Segoe UI', 'Helvetica Neue', 'Arial') if f in have), 'DejaVu Sans')
    matplotlib.rcParams.update({'font.family': family, 'font.size': 9,
                                'axes.edgecolor': GRID, 'axes.labelcolor': C['muted'],
                                'xtick.color': C['muted'], 'ytick.color': C['muted'],
                                'axes.titlesize': 10, 'axes.titleweight': 'bold'})
    return Figure, FigureCanvasTkAgg


def _clean(ax):
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.spines['left'].set_color(GRID)
    ax.spines['bottom'].set_color(GRID)
    ax.tick_params(length=0, pad=4)
    ax.grid(axis='y', color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _embed(parent, fig):
    _, Canvas = _mpl()
    canvas = Canvas(fig, master=parent)
    canvas.draw()
    w = canvas.get_tk_widget()
    w.configure(background=C['surface'], highlightthickness=0)
    w.pack(anchor='w', fill='x')
    return canvas


def _tooltip(ax):
    tip = ax.annotate('', xy=(0, 0), xytext=(10, 10), textcoords='offset points', fontsize=9, color=C['text'],
                      bbox=dict(boxstyle='round,pad=0.4', fc='white', ec=GRID, lw=1), zorder=10)
    tip.set_visible(False)
    return tip


def envelope(parent, result, width_px, k):
    """Mean beta envelope around change flashes and the flashes just before them, for one session."""
    Figure, _ = _mpl()
    t = np.asarray(result['time'], float) * 1000
    ch = np.asarray(result['mean_envelope_change'], float)
    pre = np.asarray(result['mean_envelope_prechange'], float)
    w_in = width_px / (96 * k)          # matplotlib's Tk backend multiplies by the display scale itself
    fig = Figure(figsize=(w_in, 3.1), dpi=96, facecolor=C['surface'])
    ax = fig.add_subplot(111, facecolor=C['surface'])
    fig.subplots_adjust(left=0.75 / w_in, right=1 - 1.15 / w_in, top=0.78, bottom=0.17)
    _clean(ax)
    for (a, b), name in (((50, 250), 'during'), ((300, 550), 'after')):
        ax.axvspan(a, b, color=WINDOW_FILL, lw=0, zorder=0)
        ax.text((a + b) / 2, 1.01, name, transform=ax.get_xaxis_transform(), ha='center', va='bottom',
                color=C['muted'], fontsize=8.5)
    for x in (0, 250):                                      # image on / image off (explained in the caption)
        ax.axvline(x, color=C['faint'], lw=1, ls=(0, (3, 3)), zorder=1)
    ax.plot(t, pre, color=SERIES[1], lw=2, zorder=3, label='Flash before the change')
    ax.plot(t, ch, color=SERIES[0], lw=2, zorder=4, label='Change flash')
    for y, name, col in ((ch, 'Change flash', SERIES[0]), (pre, 'Flash before', SERIES[1])):   # direct labels
        if np.isfinite(y[-1]):
            ax.plot([t[-1] + 12], [y[-1]], marker='o', ms=5, color=col, clip_on=False, zorder=5)
            ax.annotate(name, (t[-1], y[-1]), xytext=(18, 0), textcoords='offset points', va='center',
                        color=C['text'], fontsize=8.5, annotation_clip=False)
    ax.set_xlim(t[0], t[-1])
    ax.set_xlabel('Time from flash onset (ms)')
    ax.set_ylabel('Beta envelope (× median)')
    ax.legend(loc='lower left', bbox_to_anchor=(0, 1.07), ncol=2, frameon=False, fontsize=8.5, borderaxespad=0,
              labelcolor=C['text'], handlelength=1.6)
    canvas = _embed(parent, fig)

    tip, cross = _tooltip(ax), ax.axvline(0, color=C['muted'], lw=0.8, visible=False, zorder=6)

    def move(e):
        if e.inaxes is not ax or e.xdata is None:
            if tip.get_visible():
                tip.set_visible(False), cross.set_visible(False), canvas.draw_idle()
            return
        i = int(np.clip(np.searchsorted(t, e.xdata), 0, len(t) - 1))
        cross.set_xdata([t[i], t[i]])
        tip.xy = (t[i], np.nanmax([ch[i], pre[i]]))
        tip.set_text(f'{t[i]:.0f} ms\nChange flash  {ch[i]:.3f}\nFlash before  {pre[i]:.3f}')
        tip.set_horizontalalignment('right' if t[i] > t[0] + 0.6 * (t[-1] - t[0]) else 'left')
        tip.set_position((-10, 10) if t[i] > t[0] + 0.6 * (t[-1] - t[0]) else (10, 10))
        tip.set_visible(True), cross.set_visible(True)
        canvas.draw_idle()
    canvas.mpl_connect('motion_notify_event', move)
    return canvas


MOUSE_PANELS = [   # column, title, what each reading predicts, sessions used
    ('timing_index', 'Timing index', 'reset: + · rebound: −', None),
    ('novelty_within', 'Novelty (within)', 'reset: +', 'Novel'),
    ('change_during', 'Change contrast', 'reset: + · rebound: ≈ 0', None),
    ('hazard_slope', 'Hazard slope', 'precision: −', None),
]


def per_mouse_values(per_session):
    """{column: Series indexed by mouse}, averaged over each mouse's sessions as vbn_p4.py's group step does."""
    out = {}
    for col, _, _, level in MOUSE_PANELS:
        d = per_session if level is None else per_session[per_session['level'] == level]
        out[col] = d.groupby('mouse')[col].mean().dropna()
    return out


def per_mouse(parent, per_session, width_px, k):
    """One small panel per contrast: each dot is a mouse, the bar is the mean with its 95% interval."""
    Figure, _ = _mpl()
    from scipy import stats
    vals = per_mouse_values(per_session)
    w_in = width_px / (96 * k)
    fig = Figure(figsize=(w_in, 3.0), dpi=96, facecolor=C['surface'])
    fig.subplots_adjust(left=0.6 / w_in, right=1 - 0.1 / w_in, top=0.8, bottom=0.06, wspace=0.55)
    rng = np.random.default_rng(0)
    hover = []
    for j, (col, title, pred, _) in enumerate(MOUSE_PANELS):
        ax = fig.add_subplot(1, len(MOUSE_PANELS), j + 1, facecolor=C['surface'])
        _clean(ax)
        v = vals[col]
        x = rng.uniform(-0.18, 0.18, len(v))
        ax.axhline(0, color=C['faint'], lw=1, ls=(0, (3, 3)), zorder=1)
        ax.scatter(x, v.values, s=46, color=SERIES[0], edgecolors=C['surface'], linewidths=1.5, zorder=3)
        if len(v) > 2:
            m, se = v.mean(), v.std(ddof=1) / np.sqrt(len(v))
            ci = stats.t.ppf(0.975, len(v) - 1) * se
            ax.plot([0.32, 0.32], [m - ci, m + ci], color=C['text'], lw=1.4, zorder=4)
            ax.plot([0.24, 0.40], [m, m], color=C['text'], lw=2.2, zorder=4, solid_capstyle='round')
        ax.set_xlim(-0.5, 0.6)
        ax.set_xticks([])
        ax.set_title(title, loc='left', color=C['text'], pad=18)
        ax.text(0, 1.04, pred, transform=ax.transAxes, color=C['muted'], fontsize=8.5)
        hover.append((ax, x, v))
    canvas = _embed(parent, fig)
    tips = {ax: _tooltip(ax) for ax, _, _ in hover}

    def move(e):
        changed = False
        for ax, x, v in hover:
            tip = tips[ax]
            show = False
            if e.inaxes is ax and len(v):
                px = ax.transData.transform(np.column_stack([x, v.values]))
                d = np.hypot(px[:, 0] - e.x, px[:, 1] - e.y)
                i = int(np.argmin(d))
                if d[i] < 10 * k:
                    tip.xy = (x[i], v.values[i])
                    tip.set_text(f'mouse {v.index[i]}\n{v.values[i]:+.3f}')
                    show = True
            if show != tip.get_visible() or show:
                tip.set_visible(show)
                changed = True
        if changed:
            canvas.draw_idle()
    canvas.mpl_connect('motion_notify_event', move)
    return canvas
