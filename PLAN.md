# P4 Analysis App — Programming Plan

Goal: turn `vbn_p4.py` (Appendix A of *Leven_P4_Allen_data_guide*) into a program Sam can run from a
simple desktop window instead of four command lines, without weakening the preregistration.

---

## 1. What the program does (one paragraph)

`vbn_p4.py` tests prediction P4 on the Allen Visual Behavior Neuropixels dataset. It has four steps,
each writing into an output folder (`p4_out`):

| Step | Command | Downloads | Writes |
|---|---|---|---|
| 1 List | `list` | a few MB of metadata | `sessions.csv`, `day_order.csv`, `split.json` |
| 2 Band | `band` | 4 pilot sessions (~50 GB) | `band.json` |
| — Preregister | *(manual: OSF upload)* | — | — |
| 3 Analyze | `analyze [--session ID] [--max-probes N]` | 32 test sessions (~240–390 GB) | `sessions/<id>.json` |
| 4 Group | `group` | none | `group_results.json`, `per_session.csv` |

The `analyze` step can be stopped and restarted: it skips any session that already has a result file.
The app relies on this.

---

## 2. Findings from this machine (checked 2026-10-31)

| Item | Found | Needed | Action |
|---|---|---|---|
| Python | 3.13 only, no conda | 3.10 + AllenSDK 2.16.2 | Install Miniconda, create the `vbn` env (Phase 0) |
| RAM | 31.4 GB | 16 GB min, 32 GB recommended | OK. Use `--max-probes 1` |
| Disk (C:) | 226 GB free | ~50 GB pilot, ~240 GB test | **Not enough at once.** Delete each session's LFP after its result file is written (Phase 4), or use an external drive for the cache |
| Script | Extracted to `vbn_p4.py`, compiles | — | See checksum note below |

**Checksum note.** The preregistration records the SHA-256 of `vbn_p4.py` *exactly as run*.
The copy extracted from the docx has hash `3ea5cf83…f0c712c6…` (LF line endings). If Sam has the
separately supplied `vbn_p4.py`, use that file and record its hash instead. The two can differ by
whitespace alone.

---

## 3. Core design decision: the UI wraps the script and never edits it

The preregistration freezes the code by checksum. So:

- **`vbn_p4.py` is never imported, patched or edited by the app.** The GUI runs it as a subprocess
  (`python vbn_p4.py <step> --cache … --out …`), exactly as the guide's commands do.
- The GUI only **reads** what the script writes (`p4_out/*.json`, `*.csv`) to show progress and results.
- The GUI hashes `vbn_p4.py` at startup. After preregistration is marked done, it **refuses to run
  `analyze` or `group` if the hash has changed**.

Wrapping also means that a crash in the science code shows up as a non-zero exit code and a
traceback in the log pane, instead of taking down the window.

---

## 4. Architecture

```
Sam/
├── vbn_p4.py               # UNCHANGED science script (checksummed)
├── p4_app/
│   ├── __main__.py         # entry point: python -m p4_app
│   ├── gui.py              # Tkinter window, tabs, buttons, log pane
│   ├── runner.py           # subprocess launcher + live stdout streaming via queue
│   ├── state.py            # inspects p4_out/ to work out which steps are done
│   ├── settings.py         # load/save app_settings.json (paths, python exe, max probes)
│   ├── storage.py          # disk-space check, LFP cleanup after a session finishes
│   ├── prereg.py           # SHA-256, prereg bundle (band.json + hash + table) as a zip
│   └── plots.py            # matplotlib figures embedded in Tk
├── tests/
│   ├── fake_allensdk/      # stand-in AllenSDK package that produces synthetic data
│   └── test_pipeline.py    # runs all four steps end to end on fake data
├── run_p4_app.bat          # double-click launcher (activates conda env)
└── PLAN.md
```

**Why Tkinter:** it ships with Python, so there is nothing extra to install into an env that is
already pinned for AllenSDK. It runs well as a step-by-step desktop tool, and it copes with
multi-hour jobs better than Streamlit, which re-runs the whole page on each interaction.
Matplotlib (already installed with AllenSDK) draws inside it through `FigureCanvasTkAgg`.

### Data flow

```
 [Button click] ──► runner.start([py, vbn_p4.py, step, args])
                        │  (thread reads stdout line by line)
                        ▼
                   queue.Queue ──► gui.after(100ms) poll ──► log pane
                        │
                  process exits ──► state.refresh() ──► tab status ✓ / ✗ ──► results view
```

The UI thread never blocks: `subprocess.Popen` plus a reader thread plus a `queue`, polled with
`root.after()`. Tkinter is not thread-safe, so only the main thread touches widgets.

---

## 5. UI design (simple: one window, five tabs)

```
┌─ P4 Analysis ───────────────────────────────────────────────────────────┐
│ Cache folder: [D:\vbn_cache          ] [Browse]   Free: 226 GB          │
│ Output folder:[C:\Users\you\Sam\p4_out]   [Browse] Max probes: (•)1 ( )2 │
│ Python:       [C:\...\envs\vbn\python.exe] ✓ allensdk 2.16.2            │
│ Script SHA-256: 3ea5cf83…  [Copy]                                       │
├──────────┬──────────┬──────────────┬────────────┬──────────┬────────────┤
│ 1 List ✓ │ 2 Band ✓ │ 3 Prereg ⚠   │ 4 Analyze  │ 5 Group  │            │
├──────────┴──────────┴──────────────┴────────────┴──────────┴────────────┤
│  (tab content: what this step does, [Run] button, results summary)      │
│                                                                         │
│  Analyze tab example:                                                   │
│   Sessions: 32   Done: 7   Errors: 1   Remaining: 24                    │
│   ┌ session ─────┬ mouse ─┬ level ──┬ status ─┬ time ┐                  │
│   │ 1064644573   │ 524760 │ Novel   │ ✓ ok    │ 14 m │                  │
│   │ 1065437523   │ 524760 │ Familiar│ ✗ no dr…│  9 m │                  │
│   └──────────────┴────────┴─────────┴─────────┴──────┘                  │
│   [Run all remaining] [Run selected] [Stop after current]               │
│   [x] Delete LFP files after each session   ETA ≈ 5 h                   │
├─────────────────────────────────────────────────────────────────────────┤
│ Log ▼                                                                   │
│ manifest: visual-behavior-neuropixels_project_manifest_v0.5.0.json      │
│ 1064644573 ok 842 s                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

| Tab | Run button calls | After it finishes, shows |
|---|---|---|
| 1 List | `list` | sessions/mice count, the day-order crosstab (`day_order.csv`), pilot and test mouse IDs, estimated download size |
| 2 Band | `band` | beta band, peak, aperiodic slope, n channels; warning if the 15–30 Hz fallback was used |
| 3 Prereg | *(no script call)* | the prereg table pre-filled from `band.json` + hash; **[Export bundle .zip]**; checkbox "I have uploaded to OSF" + URL field. Ticking it **locks the hash** |
| 4 Analyze | `analyze --session ID` in a loop, one per session | live session table, per-session envelope plot (change vs pre-change) |
| 5 Group | `group` | five contrast rows (n, mean, t, p), order-adjusted row, **decision** in large text, precision clause, `[Open per_session.csv]` |

Steps are gated: a tab's Run button is disabled until the previous step's output file exists,
and Analyze also needs the prereg lock.

**Why the app loops `analyze --session ID` itself** rather than running `analyze` once: the script
is unchanged, but between sessions the app gets a point where it can (a) update progress, (b) delete
that session's LFP files, (c) check free disk before starting the next one, and (d) stop cleanly.

---

## 6. Build phases and acceptance criteria

### Phase 0: environment (½ day). ✅ Done 2026-10-31, with uv instead of Miniconda
`uv` was already installed, so the environment is `.venv-vbn` in this folder: Python 3.10.20 with AllenSDK
2.16.2, numpy 1.23.5, pandas 1.5.3 and xarray 2023.1.0. pytest was added so the test suite runs on the versions
the guide targets. The window detects `.venv-vbn` automatically. To remove it, delete the folder.

The original plan, kept for reference (conda works too):
1. Install Miniconda for Windows.
2. Install the packages:
   ```
   conda create -n vbn python=3.10 -y
   conda activate vbn
   pip install allensdk==2.16.2 hdmf==3.14.6 scipy xarray
   python -c "import allensdk; print(allensdk.__version__)"
   ```
3. Check that `import tkinter` and `import matplotlib` work in the env.

**Done when** `python vbn_p4.py --help` prints the docstring from the `vbn` env.

### Phase 1: synthetic test harness (1 day). Build this first. ✅ Done 2026-10-31, except `rebound_gain` (Sam)
Run it with `.venv-test\Scripts\python -m pytest tests -v` (~2½ min), or
`python tests/fake_pipeline.py --scenario reset --seed 1` for one scenario. Status: band, reset and null
scenarios pass, and the reset result holds on 4/4 noise seeds. The precision clause is an expected failure
(§7 item 0). The rebound test is skipped until `rebound_gain` is written.

The guide says the script was checked on synthetic data, but that harness was not supplied. Build
`tests/fake_allensdk/`, a package laid out like
`allensdk.brain_observatory.behavior.behavior_project_cache.behavior_neuropixels_project_cache`
that defines `VisualBehaviorNeuropixelsProjectCache` with the methods the script calls:
`from_s3_cache`, `load_latest_manifest`, `current_manifest`, `get_ecephys_session_table`,
`get_probe_table`, `get_channel_table`, `get_ecephys_session` → object with
`.stimulus_presentations` and `.get_lfp(probe_id)` → `xarray.DataArray(time, channel)`.

Put it first on `PYTHONPATH` when running the real `vbn_p4.py`. This tests the unchanged script
without downloading anything. Plant a 22 Hz bump in the LFP spectrum and a during-flash beta increase
on novel early flashes.

**Done when** all four steps run on fake data in under 2 minutes, `band.json` finds ~17–27 Hz, and
`group` prints "reset-driven reading supported" for the planted signal. Every later phase is tested
against this harness.

### Phase 2: runner and log pane (½ day). ✅ Done 2026-10-31
Start the window with `.venv-test\Scripts\python -m p4_app --demo` (synthetic data), or without `--demo` for
real data. `tests/test_app_runner.py` (6 tests, ~6 s) covers live streaming, progress-bar redraws, stop,
a missing interpreter, the guide's command lines, and the window staying responsive during a real step.
**Readability redesign, pulled forward from Phase 3:**
- a sidebar showing the 5 steps with done/ready/locked state;
- each step page explains what it does and how much it downloads;
- results appear as cards and tables (`p4_app/state.py` reads the output files);
- the decision is shown as a banner, the technical log is collapsible, and the layout is DPI-aware and scrollable.
Still for Phase 3: choosing folders and the interpreter, and saving settings.

`runner.py`: `start(args, env)`, `stop()`, `is_running`, with output streamed through a queue. Run
with `-u` (unbuffered) so `print` lines show up straight away. Exit code and elapsed time go to the log.
Save each run's full log to `p4_out/logs/<step>_<timestamp>.log`, which is useful for the methods record.

**Done when** running `list` on the fake harness streams output live and the window stays responsive.

### Phase 3: settings bar, tabs 1, 2, 5 (1 day). ✅ Done 2026-10-31
- **Settings page:** opened from the "⚙ Settings" button in the sidebar. It covers the interpreter, the
  download cache and results folders, and probes per session. Changes are saved automatically to
  `app_settings.json`, written in a way that never leaves a half-written file. Startup options override
  the saved values.
- **Interpreter detection:** a conda `vbn` environment is found automatically if one exists. The chosen
  interpreter is checked in the background and runs with `PYTHONPATH` removed, so the test stand-in can
  never pass for AllenSDK. The result shows in the sidebar and the status bar.
- **Run gating:** in real mode Run is disabled until AllenSDK is found, with a link to Settings. The
  Settings page shows the three install commands with a Copy button.
- **Disk space:** free space on the cache drive is shown, against the guide's sizes. In real mode there's a
  warning before a step if the drive has less room than that step needs.
- **Tests:** `tests/test_app_settings.py` (12 tests).

Settings saved to `app_settings.json`. Interpreter check (`python -c "import allensdk; print(allensdk.__version__)"`).
`state.py` reads `p4_out/` and decides each tab's status. Results views read the JSON and CSV files.

**Done when** the List → Band → Group tabs work on fake data and their status survives closing and reopening the app.

### Phase 4: Analyze tab, cleanup, disk guard (1–1½ days). ✅ Done 2026-10-31
- **One session at a time:** `p4_app/session_queue.py` (it doesn't depend on the window) runs step 4 as
  `analyze --session ID`, one session after another.
- **Cleanup:** `p4_app/storage.py` deletes only `*lfp*.nwb` files in the folder named after that session, and
  only when the result has no error. Failed sessions keep their files, and the session file is always kept.
  This can be switched off on the analyse page and in Settings.
- **Disk:** before each session the queue checks free space and pauses if there isn't enough.
- **Stopping:** **Stop after this session** finishes the current session first. **Stop now** puts the
  interrupted session back in the queue, and a crashed session is also put back and pauses the queue.
- **Progress:** "Session n of N", an estimate of the time left, and the running row highlighted. One log file
  per session.
- **Retry failed sessions:** removes only result files that recorded an error.
- **Tests:** `tests/test_app_queue.py` (12 tests), including demo runs through the window: all sessions,
  stopping after one and resuming, and stopping now.

The original plan:
- Session list = `sessions.csv` filtered to `split.json["test"]`, joined with `sessions/*.json` status.
- Loop: disk check (need ≥ 3 GB + 4.7 GB × max_probes + margin), then `analyze --session ID`, then
  if a result file exists and "delete LFP" is ticked, remove `*probe*_lfp.nwb` in that session's cache folder.
- "Stop after current" sets a flag that the loop checks between sessions. A hard **Stop** kills the subprocess.
  That is safe because the script only writes the JSON after a session completes.
- Sessions whose JSON contains `"error"` are shown in red. `group` silently drops them, so the app has to make them visible.

**Done when** on fake data you can stop mid-run, restart, see completed sessions skipped, and the fake LFP files get deleted.

### Phase 5: Prereg tab and plots (1 day). ✅ Done 2026-10-31
- **Bundle:** `p4_app/prereg.py` exports a zip containing `vbn_p4.py`, `band.json`, `split.json`, the session
  tables, `prereg.md` (the guide's Section 6 table filled in with this run's values) and `SHA256SUMS.txt`.
- **Lock:** `prereg_lock.json` records the OSF link and the checksums of `vbn_p4.py`, `band.json` and
  `split.json`. If any of them changes, you get a red banner on every page, a red "!" on step 3, and (for real
  data) steps 4 and 5 are locked. Withdrawing the lock keeps the old one as a record.
- **Gating:** with real data, step 4 needs the lock. Demo data aren't held up by this.
- **Charts (`p4_app/plots.py`):**
  - each session's envelope around change flashes and the flash before them, with the windows shaded;
  - a per-mouse strip chart for each contrast, with the mean and 95% interval;
  - hovering shows the values;
  - the colours are checked with the dataviz palette validator.
- **Tests:** `tests/test_app_prereg.py` (9 tests).

The original plan:
- `prereg.py`: SHA-256 of `vbn_p4.py`, export zip = `vbn_p4.py` + `band.json` + `split.json` +
  `day_order.csv` + `prereg.md` (the guide's Section 6 table with values filled in) + `sha256.txt`.
- Lock file `p4_out/prereg_lock.json` = {hash, osf_url, timestamp}. At startup, a hash mismatch → red banner, Analyze/Group disabled.
- Plots: per session, `mean_envelope_change` vs `mean_envelope_prechange` over `time`, with the
  during/after windows shaded. On the group tab, a strip plot of per-mouse values for each contrast with a zero line.

**Done when** the exported zip has everything the guide's prereg table lists, and editing one byte of `vbn_p4.py` locks Analyze.

### Phase 6: real-data pilot. ✅ Run 2026-10-31; the Section 5 check fails, so the script needs decisions (§7)
- **Step 1 (`list`):** 55 s, 157 MB of tables, manifest v0.5.0, 103 sessions, 54 mice. It found §7 item 0b
  (47 test mice, not 16).
- **Step 2 (`band`):** took four attempts.
  1. The environment's `hdmf` was too new (§7 item 0e).
  2. `KeyError: 'probe_id'` (fixed, item 0d).
  3. Spectra of unequal length after 1 h 56 min of downloads (fixed, item 0f).
  4. It completed, giving 35–45 Hz with the peak on the window edge. Not plausible: item 0g.
- **Pilot checks** (`tools/pilot_check.py`, on the downloaded pilot files): confirmed item 0 (hazard) on
  real data, measured item 0c (channel selection), and found that every analysed channel is driven.
- **Clean-up:** as asked, the ~45 GB of pilot session and LFP files were deleted afterwards. `p4_out/`
  keeps `band.json`, `pilot_check.json`, `pilot_check_spectrum.png` and the logs.
- **To redo step 2 after a script change:** it downloads the same ~45 GB again (about 2 hours here).

The original plan: run `list` for real (metadata only). Then `band` on the pilot mice (~50 GB). Follow the guide's Section 5
check: the band is plausible (peak in the high teens or 20s), and driven channels are found.
Any fix to the script happens **here, before preregistration**, as the guide requires.

### Phase 7: packaging (½ day). ✅ Done 2026-10-31
`run_p4_app.bat` picks the first Python that has what the window needs, in this order: `.venv-vbn`, a conda
`vbn` environment, `.venv-test`, then `pyw`. It starts the window without a console. `README.md` covers how to
start, setup, the five steps, the safety features, the known issue and the tests, with screenshots in
`docs/screenshots/`.

### Phase 8: Data page and Windows installer. ✅ Done 2026-11-03
- **Data page** (`_render_data` in `p4_app/gui.py`, `tools/download_sessions.py`):
  - **Download sample set** fetches the 4 pilot sessions with 2 probes each.
  - **Choose sessions** ticks any of the 103 sessions to download, run, or delete the files of.
  - Downloads use AllenSDK's `fetch_api.cache.download_data`, so nothing is loaded into memory.
  - **Sample and other non-test results go to `p4_out/sample_runs`**, because `cmd_group` counts every file in
    `sessions/` and a pilot mouse there would break the protocol.
  - With real data, test sessions can be downloaded but not run until the preregistration is locked.
- **Installer** (`build/`): `build_installer.py` makes `dist/P4-Analysis-Setup-<version>.exe`, 160 MB to download
  and 0.74 GB installed, in about 4 minutes.
  - It bundles a portable Python 3.10 with the packages pinned in `requirements-lock.txt`, and a small launcher,
    `P4 Analysis.exe`, compiled from C#.
  - It installs per user, with no administrator rights. Data goes to `%USERPROFILE%\P4 Analysis`, not Documents
    (often synced by OneDrive), and uninstalling leaves it alone.
  - It is unsigned, so SmartScreen warns. It carries AllenSDK's license notice; that license forbids commercial
    redistribution without the Allen Institute's permission.
- **Tested** with `build/test_installed.py --real`, in a temporary folder and home:
  - silent install, the launcher opening the window, the whole demo flow, and a clean uninstall;
  - with real data, through the installed copy: step 1 lists 103 sessions, session 1087720624 downloads (8.2 GB)
    and analyses from the Data page, and the result matches the earlier run through the script (3 driven channels,
    timing +0.091). Clean-up and delete worked. Step 2's ~45 GB was not repeated: its `band.json` was reused.
  - `tests/test_app_data.py` has 12 tests for the Data page.
- **Final installer:** `dist/P4-Analysis-Setup-1.0.0.exe`, SHA-256 starting `856f53bb`. The quick test passed on this
  exact file. The real download above ran on the build just before it; the changes since are the "Starting…" splash
  and the license notices. Not yet tried on a second PC.
- **Startup:** the splash appears in 1.8 s and the window in about 11 s. Most of that wait is loading libraries, which
  is why the splash exists.
- **Windows and consoles:** tests and build scripts now hide everything, apart from the one launch in
  `test_installed.py`. Watching new windows and console starts during the pipeline tests found none. The earlier stray
  `cmd.exe` Terminal window was traced to an unrelated scheduled task on this PC. Python's
  `platform` module runs a hidden `cmd /c ver` under the app, which opened no visible window in the watched run.

**Total: ~7 working days of coding plus download and compute time** (the test run is ~32 sessions × roughly 10–20 min of compute, plus downloads).

---

## 7. Risks and issues found while reviewing `vbn_p4.py`

> **Update 2026-11-04.** Sam asked for the guide to be followed, with judgement where it is silent, so items 0, 0b, 0c
> and 0g below are now resolved in `vbn_p4.py`; the README lists how. Items below are the record of what was found.
> Two more, found on real runs: AllenSDK treats any existing file as complete, so `tools/download_sessions.py` now
> checks files against the manifest's hash and retries dropped connections, and steps 2 and 4 go through it. And a
> sleeping PC dropped downloads, so the window now keeps the PC awake while something runs.
> Later the same day: downloads now use 8 connections and resume from the pieces they have, since Amazon limits each
> connection (0.28 MB/s with one, 1-2 MB/s with 8 on the same slow line). The full pilot ran on the real data: band
> 15-30 Hz (fallback), all pilot checks passed, bundle exported. Only Sam's OSF upload and lock remain before steps 4-5.

These are notes, not edits. If any are fixed, fix them on pilot data **before** preregistering, then re-hash.

0. **The hazard slope (precision clause) can't be estimated on real data. Fix this before preregistering.**
   `analyze_session` estimates the change hazard by grouping flashes on `flashes_since_change`. In AllenSDK
   2.16.2 (`stimulus_processing.get_flashes_since_change`, line 658) that value is **0 on every change
   flash**. So every change lands in group 0, and every non-change flash gets hazard 0 except the session's
   very first flash (≈ 0.99). The regression slope then rests on that single flash. Found by the Phase 1
   harness: a strong planted pre-flash effect comes out "not supported" on 4/4 noise seeds, and
   `tests/test_pipeline.py::test_precision_clause_recovered` is marked as an expected failure until this
   is fixed. Suggested fix: on change flashes, use the previous flash's count + 1 when grouping.
0b. **The test set is 47 mice, not the 16 the guide plans. Settle this before preregistering.** Found by running
   the real step 1 (2026-10-31; manifest v0.5.0, 103 sessions, 54 mice, 49 with both a familiar and a novel
   session). `cmd_list` puts every paired mouse except the 2 pilot mice into `split["test"]`, which gives 47
   mice and 94 sessions. The guide's Section 6 table, and its disk table, assume 16 test mice and 32 sessions.
   With 94 sessions, step 4 downloads about 720 GB with 1 probe. Even with LFP cleanup it keeps about 280 GB
   of session files, more than this PC's 252 GB free. Which 16, if 16, is a preregistration decision: for
   example, the first 16 of the seeded shuffle. The window doesn't choose; it shows the numbers on the step 1
   and step 4 pages. Two smaller real-data notes:
   - step 1 downloads about 160 MB (`units.csv` is 134 MB), not "a few megabytes";
   - only 3 mice saw the novel images first, all 3 in the test set, so the order-adjusted contrast rests on 3
     mice.
0c. **The channel selection uses very few of the recorded channels. Settle this before preregistering.** This
   was found on a real pilot probe (session 1087720624, probeB). Real LFP files hold only every 4th channel
   (`probe_channel_number % 4 == 2`; 96 of 384). `visual_channels()` sorts by `probe_vertical_position` and
   takes every 2nd channel (or every 4th for the band) *before* checking which channels the LFP holds. Two
   channels share each vertical position, and pandas' default sort is not stable for ties, so the picks fall
   on LFP channels largely by chance. On that probe, 3 of 40 picks are in the LFP for analyze, and 3 of 20
   for band. "Every second LFP channel" would give 10 of the 19 visual LFP channels. On the realistic fake
   (`FAKE_VBN_LFP_LAYOUT=real`), the band selection finds none, and `cmd_band` then crashes (item 1). A
   likely fix is to intersect with the LFP's channels first, then subsample. Which subsampling to use is a
   method choice. `tools/pilot_check.py` reports the band under three channel choices, so the effect can be
   judged from the pilot files without downloading them again.

0d. **Fixed (2026-10-31): `visual_channels()` used `probe_id`; AllenSDK 2.16.2's channel table calls it
   `ecephys_probe_id`.** The docx version crashed in step 2 on real data (`KeyError: 'probe_id'`). This was the
   only column-name mismatch: every other column the script uses was checked against the real tables. One
   line in `vbn_p4.py` changed; the docx version is kept as `vbn_p4_original.py`, so the checksum changed too.
   The fake AllenSDK now uses the real column names and geometry, and
   `test_original_script_used_the_wrong_channel_column` keeps it honest.

0g. **The beta band from the real pilot mice is not plausible. Settle the band rule before preregistering.**
   Step 2 completed on 2026-10-31: 4 pilot sessions, 8 probes, 45 spectra. It gave `band.json` = 35–45 Hz,
   peak 40.0 Hz. 40 Hz is the upper edge of `cmd_band`'s 12–40 Hz search window. The periodic spectrum
   (`p4_out/pilot_check_spectrum.png`) shows a strong theta peak at about 7 Hz, then a steady rise through
   12–40 Hz into a broad gamma hump at 40–60 Hz. **There is no beta peak**, so "largest residual in 12–40 Hz"
   lands on the edge. The rule falls back to 15–30 Hz only when the maximum is below 0.05, but here it is
   0.12. The small bumps at 20, 24, 28, 32 and 36 Hz are multiples of 4 Hz, which fits harmonics of the
   1.33 Hz flash rate rather than beta. The result is the same for all three channel choices tested (the
   script's, every 4th LFP visual channel, and all 156 LFP visual spectra), so item 0c does not cause this.
   The guide says to fix this on the pilot data before preregistering. Options for Sam:
   - treat a maximum on the window edge as "no clear peak" (the 15–30 Hz fallback);
   - fit the 1/f part without the theta peak, which pulls the fit down;
   - fix the band in advance.

   The window now says this plainly on the step 2 page. The pilot data were deleted after these checks;
   `pilot_check.json` keeps the curves (`tools/pilot_check.py --replot` redraws the plot).

   Other pilot results (pilot mice only; no test data were looked at):
   - `flashes_since_change` is 0 on every change flash in all 4 sessions (146–241 changes each), which
     confirms item 0.
   - Driven channels: almost every channel that is analysed passes.
   - `analyze_session` runs on real data, giving 3, 11, 13 and 13 driven channels per session under the
     script's selection.

0f. **Fixed (2026-10-31): `cmd_band` crashed after all the downloads because the probes' spectra had different
   lengths.** Each probe's clock runs slightly fast or slow: 1249.998–1250.006 Hz across the 8 pilot probes.
   `nperseg=int(2 * fs)` truncates 2499.996 to 2499 on half of them, which gives 1250- and 1251-point
   spectra that cannot be stacked (`ValueError ... shapes (1251,) (1250,)`, after 1 h 56 min of downloading).
   One line changed to `int(round(2 * fs))`, keeping the intended 2-second window; the other uses of `fs`
   already round. The fake's probe clocks now differ by the same amounts, so the pipeline tests catch this.

0e. **Environment: pin `hdmf==3.14.6`.** AllenSDK 2.16.2 does not pin `pynwb` or `hdmf`. Left to itself, `uv`
   installed `hdmf` 6.2.0 with `pynwb` 2.8.3, and the session files then fail to open (`Can't instantiate
   abstract class NWBFile`). The Settings page now flags that combination.

1. **`cmd_band` crashes with `NameError: f`** if no pilot channel yields a PSD (`f` is only defined
   inside the loop). The app should catch this and say "no visual-cortex channels found in pilot sessions".
2. **`group` silently drops error sessions.** A mouse can lose its Novel session and leave the
   novelty test without anyone noticing. The app lists them (Phase 4).
3. **Result JSON contains `NaN`** (e.g. `hazard_slope` when there are ≤ 50 usable flashes). Python reads this
   fine, but strict JSON tools don't. The app should parse it with Python's `json` module.
4. **`--max-probes` defaults to 2**, but the guide's test command uses 1. The app passes it explicitly
   so the value is always on record, and saves it in the run log.
5. **Disk:** the 3 GB session NWB files are never deleted (32 × 3 GB ≈ 96 GB). With LFP cleanup on,
   peak use is about 96 GB + 4.7 GB. That fits in 226 GB, but only with cleanup on.
6. **Old LFP files** from before manifest v0.5.0 have an amplitude-scaling bug. The app should show the manifest
   line from the log and warn if it is below v0.5.0.
7. **Windows paths:** AllenSDK's S3 cache works on Windows, but keep the cache path short
   (e.g. `D:\vbn_cache`) to avoid the 260-character path limit.

---

## 8. Good places for Sam to write code himself

These pieces carry real decisions, so they are worth owning rather than having them generated:

- **`storage.should_delete(session_id, result)`**: the cleanup policy. Delete LFP only, or the session
  NWB too? Keep files for sessions that errored, for debugging? It trades disk space against re-download time.
- **The synthetic signal in `fake_allensdk`**: what a "reset-like" and a "rebound-like" signal look like
  is the scientific heart of the model-recovery check. Sam knows the theory best.
- **The decision display on the Group tab**: how to present "inconclusive" so readers don't take it
  as "rebound ruled out". The guide's Section 8 asymmetry should be visible in the UI.
