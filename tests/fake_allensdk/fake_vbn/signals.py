"""
The planted "truth" in the synthetic data: how beta amplitude changes around each flash.

Each scenario is a function gain(rel_t, flash) -> array, giving the change in beta amplitude
(added to a baseline of 1) at times rel_t, in seconds relative to flash onset (-0.25 to 0.75 s).
Flashes are 250 ms on, then 500 ms gray. The script's analysis windows are:
    during = 50-250 ms     after = 300-550 ms     pre = -250-0 ms
The gains of neighbouring flashes add, so the "pre" window of one flash overlaps the end of the
previous flash's cycle.
"""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Flash:
    is_change: bool      # the image differs from the previous flash
    novel_image: bool    # the image is from the novel set (the two shared images count as familiar)
    frac: float          # position within the task block: 0 = first flash, 1 = last
    hazard: float        # true probability that this flash is a change, given the flashes since the last change


def bump(rel_t, t0, t1):
    """Smooth 0 -> 1 -> 0 window (Hann) between t0 and t1 seconds after onset, zero elsewhere."""
    x = (rel_t - t0) / (t1 - t0)
    return np.where((x > 0) & (x < 1), np.sin(np.pi * x) ** 2, 0.0)


def mismatch(f):
    """How unexpected a flash is: maximal at a change, high for novel images early on, low otherwise."""
    if f.is_change:
        return 1.0
    if f.novel_image:
        return 0.2 + 0.8 * np.exp(-f.frac / 0.2)
    return 0.2


def reset_gain(rel_t, f):
    """Reset-driven reading: beta rises *during* the flash, in proportion to mismatch.
    Also plants the precision clause: pre-flash beta falls as a change becomes more likely."""
    g = 1.2 * mismatch(f) * bump(rel_t, 0.05, 0.25)
    g = g - 0.4 * f.hazard * bump(rel_t, -0.25, 0.0)
    return g


def rebound_gain(rel_t, f):
    """Rebound-driven reading: beta follows stimulus *offset* (the image turns off at 250 ms).

    Expected results from vbn_p4.py on these data:
        timing index (during - after)   negative
        novelty contrast                not positive
        change contrast                 near zero
    """
    # The same rise after every offset: no dependence on mismatch, novelty or change, so the novelty and
    # change contrasts stay near zero. It ends by 580 ms, before the next flash's pre window starts at 500 ms + 250 ms.
    return 1.0 * bump(rel_t, 0.28, 0.58)


def null_gain(rel_t, f):
    """No task-related beta at all: the decision rule should come out inconclusive."""
    return np.zeros_like(rel_t)


SCENARIOS = dict(reset=reset_gain, rebound=rebound_gain, null=null_gain)
