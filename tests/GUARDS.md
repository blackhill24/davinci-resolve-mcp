# tests/GUARDS.md — doubles, meta-tests, live-harness rules (ICM Layer 3)

Depth behind two rows of `tests/CONTEXT.md`. Read this before writing a Resolve double, a
repo-wide guard, or a `live_*.py` harness; skip it for ordinary domain tests.

The premise (#119, #121): a green suite proves nothing on its own. Every guard here exists
because something shipped past a green suite, and each guard is itself checked — the
question is always *if this were broken, would anything fail?*

## The one faithful double

- `bridge_double.py` — the ONE `PyRemoteObject` double. Use it for any Resolve object,
  **never `MagicMock`**. `_has_method` tests `dir()`, and a mock's `dir()` lists only the
  children a test touched, so every unconfigured method reads as absent and the test
  silently exercises the capability-missing branch instead of the real one (#119).
- Pinned by `core/test_bridge_double_fidelity.py`. Hand-rolled doubles must not
  re-implement its fabrication behaviour — `test_hand_rolled_double_audit.py` enforces that.
- Constants vs methods: `dir()` for METHODS, `getattr` for CONSTANTS. Getting it backwards
  broke every timeline/LUT export while the offline suite stayed green (#118); four
  `api_constant_*` mutations now pin it.

## Meta-tests — the guards on the guards

`scripts/mutation_gate.py` (15 mutations, run on every publish) re-introduces the exact
defect each guard was written to catch and fails if the suite stays green. Five of its
mutations blind a guard's own scanner (`*_scan_blind`) — because a guard whose glob silently
matches nothing reads as safety while providing none (#110: two drift guards had
`ImportError`'d out of every CI run unnoticed). Any new scanning guard asserts on its own
scan result (`assertGreater(len(files), N)`) and earns a mutation here.

| Guard | Catches |
|---|---|
| `test_live_harness_exit_codes.py` | a `live_*.py` that can only exit 0 — status discarded, no reachable nonzero exit, unguarded `input()` (#119 §5 found two) |
| `test_live_probe_phases.py` | a two-phase GUI probe whose no-argument invocation runs the interactive `setup` phase. The sweep calls every harness with no arguments, so such a probe builds its disposable project, reports PASS, and never reaches the `cleanup` that would delete it — five did, on every run, for months (#154). Nothing fails; the only evidence is a project list nobody reads |
| `test_live_harness_naming.py` | a live harness named `test_*` (breaks pytest at COLLECTION on Resolve-less CI, so publish never ran the suite — #111), and any `test_*.py` importing `DaVinciResolveScript` |
| `test_hand_rolled_double_audit.py` | a new double re-implementing `bridge_double`'s fabrication |
| `test_vacuous_assertion_audit.py` | the three shapes swept in #121 §3 — assert-on-a-self-configured-mock, error-envelope-passes-for-the-wrong-reason, swallowed exception |
| `test_import_time_connect_guard.py` | a module under `src/` calling `scriptapp()` while its body runs. A **wedged** Resolve (up, holding its socket, never answering — #153) then blocks the importer forever, taking out the whole pytest COLLECTION with no output and no timeout (#158). Invisible under every normal condition: with Resolve closed *or* healthy the suite is green either way. Importing the bridge module is fine; connecting is not |
| `test_env_leak_guard.py` | the `conftest.py` env guard silently ceasing to detect leaks |
| `test_locale_guard.py` | a `scriptapp()` call site with no `locale_guard.restore()` within three lines |
| `test_text_encoding_guard.py` | a `subprocess(text=True)` / `open()` / `read_text()` under `src/` that names no `encoding=`, so its decoding follows the process locale — ASCII once a native library resets that locale mid-process, as `scriptapp()` did (#124; a C/POSIX *startup* locale is harmless, it turns UTF-8 mode on — #127). Keys on a literal `text=True`, never on a `text=` kwarg. `_ALLOWLIST` is empty by design |
| `test_granular_guard_drift.py` | a destructive or AI/render tool on the granular (`--full`) surface that skips the confirm-token gate or the AI-ops ledger — #138/#139, where the same Resolve call was guarded through the compound tool and unguarded through `--full`. Registries live in `src/granular/guards.py`; `test_granular_guards.py` is the behavioural half |
| `test_*_drift.py` set | action list, agent rules, destructive registry, discarded mutator returns |

## Assert on the reason, not the shape

`_error_envelope_helpers.assert_error_mentions` pins an error envelope to the SPECIFIC
cause. A bare "it returned an error" passes for the wrong reason: two such tests patched
`src.server._check`, which stopped owning the dispatch in the #52 restructure, so they were
reaching a **running Resolve** and asserting on whatever it happened to return (#121 §3).

## Test isolation

`conftest.py` carries an autouse guard that **fails** (never warns) any test leaking an
`os.environ` write — use `monkeypatch.setenv` / `mock.patch.dict`. Exempt:
`PYTEST_CURRENT_TEST` (pytest rewrites it every phase) and
`RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB` (`src/resolve_mcp_server.py` sets them at import
by design).

Mutations that patch a root-level guard must import it as a **top-level** module
(`import test_action_list_drift`), not `tests.test_...`: there is no `tests/__init__.py`, so
pytest imports these under bare names, and patching the dotted name patches a different
module object — the mutation then survives silently.

## Live harnesses

- `preflight.py` — pre-run Resolve status gate (closed / open_no_project / open_project);
  `--require open|project|timeline`, `--json`; exit 0 ready, 2 not ready, 3 no scripting.
  Every `live_*` `__main__` calls `gate()` — new harnesses must too.
- `scripts/run_live_suite.py` runs the whole set. Never sweep them with a hand-rolled shell
  loop: harnesses that pass alone fail back-to-back because each inherits the project the
  last one left behind, and a harness reading stdin eats the rest of the work list (#151).
  The runner re-establishes a scratch project + timeline between harnesses, gives each
  `stdin=DEVNULL`, and names the harness that leaked a disposable project.
- `--vitals` samples Resolve's `/proc` vitals between harnesses via
  `scripts/resolve_vitals.py`, for the Resolve that terminates by itself mid-sweep with no
  OOM and no segfault (#153). Use it on any sweep meant to be diagnostic, not just green:
  once the process is gone there is nothing left to read, so an un-instrumented sweep that
  hits the exit costs a full re-run and yields nothing. `tests/test_resolve_vitals.py` pins
  the sampler against a synthetic `/proc` tree.
- A harness exercising a **confirm-gated** op must send the token AND assert the refusal:
  asserting only the confirmed call passes on an ungated build, and asserting only the
  refusal never reaches Resolve. `live_resolve21_stage2_render_validation.py` is the
  worked example; it was always-fail for want of the token (#150).
- A **two-phase GUI probe** (`setup` → a human edits something in Resolve → `diff` →
  `cleanup`) must default its no-argument invocation to a `sweep` phase — `setup` then
  `cleanup` via `tests/probe_phases.run_sweep` — never to `setup`. The sweep passes no
  arguments and no human is there to do the GUI step, so defaulting to `setup` leaks the
  disposable project every run while still reporting PASS (#154). Record the project name
  the moment it exists, not at the end of setup, or a setup that fails midway leaks harder
  than one that succeeds. Delete through `probe_phases.delete_probe_project`, which is the
  only caller shape that cannot forget `resolve=` — without it the helper never parks off
  the Fusion page, where a delete terminates Resolve outright (#153/#157).
- `live_*` are kept out of offline CI **by filename alone** (pytest's `test_*.py` glob), so
  a misnamed harness is collected and run. Live-validation process:
  `docs/process/release-process.md`.
- `live_api_probe.py` is a read-only API-surface probe (`--allow-mutation` opts into a
  scratch timeline); `live_resolve20_api.py` mutates unconditionally.
- `live_timeline_end_frame_probe.py` — read-only; settles whether
  `Timeline.GetEndFrame()` is inclusive, the one open question behind
  `core/timeline_lookup.timeline_frame_duration` (#141 finding 6). Needs a
  current timeline that ends on a clip, so it gates `--require timeline`.

## Coverage

Floors are per-module and named, never a repo average — a module at 90% whose tests assert
on mocks they configured themselves is worse than one at 40% with real assertions.
`scripts/coverage_floor.py` + `.coveragerc` (which omits `*_live_probe.py` and the other
never-offline paths). Raising a floor is routine; lowering one is the thing to argue about.

> Upkeep: when a guard, double, or live harness is added/removed/renamed, fix this file and
> `tests/CONTEXT.md` in the same session, then run `python3 .icm/drift-check.py --update`
> from the root.
