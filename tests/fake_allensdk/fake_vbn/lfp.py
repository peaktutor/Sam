"""
Synthetic LFP: 1/f background + a beta rhythm whose amplitude follows the scenario's gain + an evoked
potential on visual-cortex channels. Returned as an xarray.DataArray (time, channel) like
allensdk's session.get_lfp().

The scenario comes from the FAKE_VBN_SCENARIO environment variable (reset, rebound, null).
"""
import os

import numpy as np
import scipy.fft as sp_fft
import xarray as xr

from . import signals, world

FS = 1250.0              # Hz, the LFP sampling rate of the real dataset
BETA_HZ, BETA_SD = 22.0, 2.5
BETA_AMP = 0.15          # beta std relative to the 1/f background (gives a clear spectral peak)
ERP_AMP = 1.0
VOLTS = 1e-4             # real LFP is stored in volts
CLOCK_OFFSET_HZ = (-0.0019, +0.0019)     # per-probe timestamp clock offset, as measured on the real pilot probes


def scenario():
    name = os.environ.get('FAKE_VBN_SCENARIO', 'reset')
    if name not in signals.SCENARIOS:
        raise ValueError(f'FAKE_VBN_SCENARIO must be one of {sorted(signals.SCENARIOS)}, not {name!r}')
    return name


def _shaped_noise(rng, n, amplitude, rows=1):
    """`rows` series of Gaussian noise with spectral amplitude amplitude(f), each scaled to unit variance.
    Drawn directly in the frequency domain (random complex spectrum, one inverse FFT), at a fast FFT length."""
    nfft = sp_fft.next_fast_len(n, real=True)
    a = amplitude(np.fft.rfftfreq(nfft, 1 / FS))
    spec = (rng.standard_normal((rows, a.size)) + 1j * rng.standard_normal((rows, a.size))) * a
    x = sp_fft.irfft(spec, nfft, axis=1, workers=-1)[:, :n]
    return x / x.std(axis=1, keepdims=True)


def _pink(f):
    return 1.0 / np.maximum(f, 1.0)                      # power ~ 1/f^2 above 1 Hz


def _beta(f):
    return np.exp(-0.5 * ((f - BETA_HZ) / BETA_SD) ** 2)


def _per_flash_trace(sp, n, fn):
    """Sum fn(rel_t, i) over flashes into a session-length trace, using each flash's -0.25..0.75 s window."""
    rel = np.arange(int(-0.25 * FS), int(0.75 * FS))
    out = np.zeros(n)
    for i, t in enumerate(sp['start_time'].values):
        idx = int(round(t * FS)) + rel
        ok = (idx >= 0) & (idx < n)
        v = fn(rel / FS, i)
        if v is not None:
            out[idx[ok]] += v[ok]
    return out


def _erp(rel_t):
    return -np.exp(-0.5 * ((rel_t - 0.06) / 0.015) ** 2) + 0.6 * np.exp(-0.5 * ((rel_t - 0.15) / 0.04) ** 2)


def session_lfp(session_id, probe_id, channel_ids, visual):
    """LFP for one probe. `visual` marks the channels in visual cortex (they get the ERP and the task beta)."""
    sp, flashes = world.stimulus_presentations(session_id)
    gain_fn = signals.SCENARIOS[scenario()]
    n = int((sp['end_time'].max() + 2.0) * FS)
    # each real probe's clock runs a little fast or slow (1249.998-1250.006 Hz on the pilot probes), which is
    # enough to change int(2 * fs) by one; the timestamps here do the same (the signal itself is generated at FS)
    t = np.arange(n) / (FS + CLOCK_OFFSET_HZ[int(probe_id) % 2])

    gain = 1.0 + _per_flash_trace(sp, n, lambda r, i: None if flashes[i] is None else gain_fn(r, flashes[i]))
    gain = np.maximum(gain, 0.0)
    change = sp['is_change'].astype(object).eq(True).values
    omitted = sp['omitted'].astype(object).eq(True).values
    erp = _per_flash_trace(sp, n, lambda r, i: None if omitted[i] else (2.0 if change[i] else 1.0) * _erp(r))

    beta_shared = _shaped_noise(np.random.default_rng([int(session_id), world.seed(), 0]), n, _beta)[0]  # all channels
    rng = np.random.default_rng([int(probe_id), world.seed(), 1])
    beta_local = _shaped_noise(rng, n, _beta, rows=len(channel_ids))
    pink = _shaped_noise(rng, n, _pink, rows=len(channel_ids))
    X = np.empty((n, len(channel_ids)), np.float32)
    for j, vis in enumerate(visual):
        beta = 0.8 * beta_shared + 0.6 * beta_local[j]
        x = pink[j]
        if vis:
            x += BETA_AMP * gain * beta + ERP_AMP * erp
        else:
            x += 0.3 * BETA_AMP * beta
        X[:, j] = VOLTS * x
    return xr.DataArray(X, dims=('time', 'channel'), coords=dict(time=t, channel=np.asarray(channel_ids)))
