"""
Metadata tables and stimulus tables shaped like the AllenSDK 2.16 Visual Behavior Neuropixels objects.

Sizes are configurable through environment variables so tests stay fast:
    FAKE_VBN_MICE      mice with both a familiar and a novel session (default 10: 2 pilot + 8 test)
    FAKE_VBN_FLASHES   flashes in the task block of each session (default 600; real sessions have ~4800)
"""
import functools
import os

import numpy as np
import pandas as pd

from .signals import Flash

FAMILIAR_SET = ('im036_r', 'im012_r', 'im044_r', 'im047_r', 'im078_r', 'im115_r', 'im083_r', 'im111_r')
NOVEL_SET = ('im104_r', 'im114_r', 'im005_r', 'im087_r', 'im024_r', 'im034_r', 'im083_r', 'im111_r')
SHARED = ('im083_r', 'im111_r')          # in both sets, so familiar in both sessions
FLASH_ON, FLASH_PERIOD = 0.25, 0.75
T_START = 5.0                            # first task flash (s)
CHANGE_AFTER = (5, 11)                   # a change comes 5-11 flashes after the previous one
P_OMIT = 0.03

# probe name -> (structures, has_lfp, visual area for its cortical channels)
PROBES = {'probeA': ('VISp', 'CA1', True),
          'probeB': ('VISl', 'LP', True),
          'probeC': ('CA1', 'DG', True),
          'probeD': ('VISam', 'CA3', False)}
N_CHAN = 64                              # channels per probe; the first 48 are in the first structure
LFP_EVERY, LFP_OFFSET = 4, 2             # real LFP files hold channels whose probe_channel_number % 4 == 2


def seed():
    """FAKE_VBN_SEED: changes every random draw (stimulus sequences and noise), to check results are robust."""
    return int(os.environ.get('FAKE_VBN_SEED', 0))


def n_mice():
    return int(os.environ.get('FAKE_VBN_MICE', 10))


def n_flashes():
    return int(os.environ.get('FAKE_VBN_FLASHES', 600))


def session_table():
    rows = []
    for m in range(n_mice()):
        novel_first = m % 3 == 2         # some mice see the novel set on day 1, as in one Allen recording path
        for level in ('Familiar', 'Novel'):
            day = 1 if (level == 'Novel') == novel_first else 2
            rows.append(dict(ecephys_session_id=1050000000 + 10000 * m + day, mouse_id=500000 + 1111 * m,
                             experience_level=level, session_number=day,
                             image_set='H' if level == 'Novel' else 'G', genotype='wt/wt'))
    # one mouse with only a familiar session: must be left out of both pilot and test sets
    rows.append(dict(ecephys_session_id=1059990001, mouse_id=599999, experience_level='Familiar',
                     session_number=1, image_set='G', genotype='wt/wt'))
    st = pd.DataFrame(rows).set_index('ecephys_session_id')
    st['file_id'] = st.index                # the real table has one file id per session
    return st


def probe_table():
    rows = []
    for sid in session_table().index:
        for k, (name, (s1, s2, lfp)) in enumerate(PROBES.items()):
            rows.append(dict(ecephys_probe_id=int(sid) * 10 + k, ecephys_session_id=int(sid), name=name,
                             has_lfp_data=lfp, structure_acronyms=str([s1, s2]), file_id=int(sid) * 10 + k))
    return pd.DataFrame(rows).set_index('ecephys_probe_id')


def channel_table():
    return _channel_table(n_mice()).copy()


@functools.lru_cache(maxsize=4)
def _channel_table(_n_mice):
    """Laid out like the real AllenSDK 2.16.2 table: column ecephys_probe_id (not probe_id), and Neuropixels
    geometry with two channels per row, so channels share probe_vertical_position in pairs."""
    rows = []
    for pid, p in probe_table().iterrows():
        s1, s2, _ = PROBES[p['name']]
        for n in range(N_CHAN):
            rows.append(dict(ecephys_channel_id=int(pid) * 100 + n, ecephys_probe_id=int(pid),
                             ecephys_session_id=int(p['ecephys_session_id']), probe_channel_number=n,
                             probe_vertical_position=20 * (n // 2) + 20, probe_horizontal_position=11 + 32 * (n % 2),
                             structure_acronym=s2 if n < N_CHAN // 4 else s1,     # cortex on the upper 3/4
                             valid_data=n != 37))   # one bad channel per probe
    return pd.DataFrame(rows).set_index('ecephys_channel_id')


def lfp_channel_ids(probe_id):
    """The channels an LFP file holds: as in the real files, every 4th."""
    ch = channel_table()
    ch = ch[ch['ecephys_probe_id'] == int(probe_id)]
    return ch[ch['probe_channel_number'] % LFP_EVERY == LFP_OFFSET]


def true_hazard(since):
    """P(change at this flash | `since` flashes since the last change), for a change uniform in CHANGE_AFTER."""
    lo, hi = CHANGE_AFTER
    return 0.0 if since < lo else 1.0 / (hi - since + 1)


def allensdk_flashes_since_change(sp):
    """Same rule as allensdk 2.16.2 stimulus_processing.get_flashes_since_change:
    0 on a change flash, +1 on each later flash, omitted flashes copy the previous value."""
    out = np.zeros(len(sp), int)
    for i, (omitted, image, change) in enumerate(zip(sp['omitted'], sp['image_name'], sp['is_change'])):
        if image == 'omitted' or (omitted is True):
            out[i] = out[i - 1]
        elif change is True or i == 0:
            out[i] = 0
        else:
            out[i] = out[i - 1] + 1
    return out


def stimulus_presentations(session_id):
    """Task block (active) followed by a short passive replay, plus a parallel list of Flash records
    (None for omitted flashes) that the LFP generator uses to plant the beta signal."""
    s = session_table().loc[session_id]
    rng = np.random.default_rng([int(session_id), seed()])
    images = NOVEL_SET if s['experience_level'] == 'Novel' else FAMILIAR_SET
    n = n_flashes()
    rows, flashes = [], []
    image = str(rng.choice(images))
    since, k_next = 0, rng.integers(CHANGE_AFTER[0], CHANGE_AFTER[1] + 1)
    for i in range(n):
        t = T_START + i * FLASH_PERIOD
        if i > 0 and since + 1 < k_next and rng.random() < P_OMIT:
            rows.append(dict(start_time=t, image_name='omitted', active=True, omitted=True, is_change=False))
            flashes.append(None)
            continue
        change = bool(i > 0 and since + 1 == k_next)
        haz = true_hazard(since + 1) if i > 0 else 0.0
        if change:
            image = str(rng.choice([im for im in images if im != image]))
            since, k_next = 0, rng.integers(CHANGE_AFTER[0], CHANGE_AFTER[1] + 1)
        else:
            since += 1
        rows.append(dict(start_time=t, image_name=image, active=True, omitted=False, is_change=change))
        flashes.append(Flash(is_change=change, novel_image=s['experience_level'] == 'Novel' and image not in SHARED,
                             frac=i / (n - 1), hazard=haz))
    t0 = T_START + n * FLASH_PERIOD + 10.0
    for i in range(100):                 # passive replay: not analysed; is_change/omitted left NaN as in some releases
        rows.append(dict(start_time=t0 + i * FLASH_PERIOD, image_name=images[i % len(images)], active=False,
                         omitted=np.nan, is_change=np.nan))
        flashes.append(None)
    sp = pd.DataFrame(rows)
    sp['end_time'] = sp['start_time'] + FLASH_ON
    sp['duration'] = FLASH_ON
    sp['flashes_since_change'] = allensdk_flashes_since_change(sp)
    sp['stimulus_block'] = np.where(sp['active'], 0, 5)
    sp.index.name = 'stimulus_presentations_id'
    return sp, flashes
