# CLAUDE.md — RF Device Manager

Guidance for AI assistants (and humans) working in this repository. Read this
before changing code. It captures the architecture, the non-obvious logic, the
invariants that keep the integration working, and the **mandatory rules for
pull requests and new protocols** (sections 10 and 11).

## 1. What this is

A Home Assistant custom integration (`custom_components/rf_device_manager`,
domain `rf_device_manager`, HA ≥ 2026.5) that manages sub-GHz RF devices
entirely from the UI: learn, receive, edit, test, send RF codes — no YAML.

- **Sending** uses HA core's `radio_frequency` entity platform (new in 2026.5):
  `async_send_command(hass, entity_id, RadioFrequencyCommand)`. Transmitters are
  ESPHome `ir_rf_proxy` nodes (CC1101/SX127x) and similar.
- **Receiving/learning** uses the ESPHome native API directly
  (`aioesphomeapi`), because HA core has RF *transmitter* entities but no RF
  *receiver* entities (as of 2026.7). Fallback: HA bus event
  `esphome.rf_code_received`.

See [ARCHITECTURE.md](ARCHITECTURE.md) (German) for the module map and data
model, [README.md](README.md) for user-facing docs and
[CHANGELOG.md](CHANGELOG.md) for release and upgrade notes.

## 2. Golden rules / invariants

1. **Pure-Python core.** `decoders.py`, `rf_analysis.py` and
   `command_rotation.py` use the stdlib only (no `homeassistant` /
   `aioesphomeapi` imports). This is what makes `tests/test_decoders.py`,
   `tests/test_models.py` and `tests/test_rotation.py` runnable without HA.
2. **`aioesphomeapi` is an optional import.** ESPHome is only an
   `after_dependency`. All uses are guarded by `HAS_AIOESPHOMEAPI` in
   `rf_manager.py`. Never import it at module top level elsewhere.
3. **`_StoredCommand` deliberately skips `super().__init__()`.** The
   `rf-protocols` library changed its `RadioFrequencyCommand` constructor across
   releases (2.x→4.x). We set the attributes the transmitter reads directly
   (`frequency`, `modulation`, `repeat_count`, `get_raw_timings()`). Keep it that
   way — do not "fix" it to call the parent constructor.
4. **Timings are signed alternating microseconds**: positive = mark (carrier
   on), negative = space (carrier off). Every module assumes this.
5. **Send raw, do not "clean".** Learned/entered timings are transmitted as
   captured (only `normalize()`: sign, glitch-merge, idle-trim). An earlier
   frame-rotation + timing-quantization step was removed because it corrupted
   signals; do not reintroduce waveform reshaping on the send path.
6. **Stored timings are never rewritten.** Migrations and re-decoding may
   change metadata (`fingerprint`, `extra`, `repeat`, list shape), never
   `data`. Sending must stay bit-identical across upgrades.
7. **Fingerprints are the receive-matching key.** Any change that alters the
   `bits`/`fingerprint` of an existing capture requires a
   `DECODER_REVISION` bump (section 4). The golden test enforces this.
8. **Standalone scripts stay Python 3.11 compatible** (the maintainer runs
   them locally on 3.11; the HA runtime and CI use 3.13/3.14). Avoid 3.12+
   syntax such as `type X = ...` in the pure modules and `tests/test_*.py`.
9. **`strings.json` and `translations/en.json` are identical**, and
   `translations/de.json` has exactly the same key set. After editing
   `strings.json`, copy it over `en.json` and mirror every key into `de.json`.
10. **Menu step titles must be static** (no `{placeholders}`): HA only applies
    `description_placeholders` to a menu's description. Placeholders in a menu
    title raise a formatjs `MISSING_VALUE` error.
11. **ICU escaping:** next to a placeholder use typographic quotes (`„…“`,
    `“…”`), never straight `'…'` — single quotes are ICU escape characters and
    hide the placeholder.

## 3. Data model (models.py)

- **`RFDeviceConfig`** — one managed device = one config entry.
  `entry.data` holds only `device_id`; `entry.options` holds the mutable
  settings, mirrored into the central store by `_sync_store_from_entry`
  (`__init__.py`). Extra settings (e.g. `rotation_reset_on_other`) live in
  `RFDeviceConfig.settings`.
- **`RFCode`** — one code = **one entity**. Has an `entity_type` and a dict of
  **command slots** → ordered `list[RFCommandData]` (**command variants**).
- **`RFCommandData`** — one transmittable/matchable command. `data` is always
  normalized raw timings. `protocol`/`code`/`pulse_length` keep the original
  input form; `extra` holds decode results (`STORED_DECODE_KEYS`: `encoding`,
  `bits`, `bit_count`, `hex`, `tristate`, `protocol_guess`, `frame_count`, plus
  `times_seen` for learned commands). `fingerprint` is the receive-matching
  key. `redecode()` refreshes fingerprint + decode keys from `data`.
- **Command slots** come from `models.build_slots(entity_type, settings)`,
  e.g. switch → `on`/`off`; cover → `open`/`close`/`stop`; fan →
  `off`/`speed_1..n`/`oscillate`/direction; select → `option:<name>`;
  number/sensor → `value:<v>`; button → `press`. **Slot keys are the contract**
  between the learn wizard, the platforms and the matcher — never rename one
  without a storage migration.
- **Command variants** (rotating-code remotes, issue #2): a slot holds 1..n
  variants in their first-received order. Re-learning a slot replaces the
  whole list. Sends rotate through the list per (code, slot); every variant is
  registered for receive matching.

### Storage (storage.py)

`RFDeviceStore` uses the HA Storage API (`.storage/rf_device_manager`,
`STORAGE_VERSION` 1, `STORAGE_MINOR_VERSION` 3). The payload is
`{"devices": …, "codes": …, "decoder_revision": N}`. Migrations in
`_RFStore._async_migrate_func` run **one minor version per step**:

| Step | Change | Origin |
|---|---|---|
| 1.1 → 1.2 | `repeat` 0 meant "default (7)", now means "send once": stored zeros → 7 | PR #4 |
| 1.2 → 1.3 | each slot's single command object → list of variants | PR #12 |

`RFCode.from_dict` additionally tolerates both shapes. Config entries have
their own version (`RFDeviceManagerConfigFlow.VERSION/MINOR_VERSION`, now 1.2,
`async_migrate_entry`: device `default_repeat` 0 → 7).

**Rotation state** lives in a separate store
(`.storage/rf_device_manager_rotation`, `{"next": {code_id: {slot: index}}}`)
because it changes on every send. It is saved debounced
(`ROTATION_SAVE_DELAY`) and flushed on HA shutdown (`EVENT_HOMEASSISTANT_FINAL_WRITE`).

**Downgrades are not supported**: older releases cannot read schema 1.3.
The CHANGELOG upgrade notes say so; keep them accurate.

## 4. Protocol decoding (decoders.py) — the core logic

`decode_frame(frame)` builds a `FrameContext` once (mark/space pairs) and walks
the `DECODERS` chain **in order**; the first structural match wins:

| Decoder | Structure | Examples |
|---|---|---|
| `space_distance` | constant mark, short/long space | EV1527, PT2262 |
| `mark_width` | constant space, short/long mark | SIRC-style, rc-switch P8 |
| `pwm` | mark+space share the bit (rc-switch) | rc-switch P1–5, P7 (+ guess) |
| `manchester` | every bit is a level transition | Oregon, some fans |

Detection is **structural, not a protocol database** — unknown remotes decode
as long as they use one of these schemes. Anything else still works via the
raw fallback (no `bits`, matched by frame similarity instead).

- **Protocol 8 polarity:** rc-switch P8 maps the *longer* mark to bit 0. The
  profile is identified from its timing ratios and its polarity comes from
  `RC_PROTOCOLS`; the base pulse is derived from the constant 16T spaces.
- **Manchester:** a trailing space half-bit is swallowed by the inter-frame
  gap; it is reconstructed as the complement of the last half. Don't
  "simplify" that away.
- `RC_PROTOCOLS` (P1–5, 7, 8) is used for manual binary/tri-state encoding and
  for protocol guesses.

### Decoder revision (rf_analysis.DECODER_REVISION)

Stored commands carry the fingerprint/bits computed by the decoders at learn
time. When a decoder change alters them, those codes would silently stop
matching received signals. Therefore:

- `DECODER_REVISION` (now **2**) is saved in the store. On load, a lower
  stored value (or none) triggers `RFDeviceStore._redecode_all()`, which calls
  `RFCommandData.redecode()` on every variant and saves.
- History: 1 = up to 1.1.x; 2 = P4 2.28 ms sync gap splits frames (#10), P8
  polarity fixed (#11).
- `tests/test_decoders.py` section 10 holds **golden fingerprints** for a fixed
  capture set (real EV1527, raw fallback, Manchester, tri-state, rc-switch
  P1–5/7/8). If one changes, the test fails and tells you to bump.

## 5. Analysis pipeline (rf_analysis.py)

- `normalize()` = `ensure_signed` → `remove_glitches` (merge sub-80µs spikes)
  → `trim_idle`. Run before anything else.
- `split_frames()` cuts on the inter-frame gap: `max(2000 µs, 1.7 × P95 of all
  pulses)`. The 2000 µs floor admits rc-switch P4's 2.28 ms sync; the adaptive
  term keeps longer in-frame spaces (P8's 3.2 ms data LOW) intact. Changing
  these constants changes frame boundaries → fingerprints → revision bump.
- `fingerprint()` = hash of decoded bits when decodable (jitter-immune,
  `"<n>b:<sha>"`), else quantized-frame hash (`"<n>:<sha>"`).
- `same_code(a, b)`: identical decoded bits, else `frames_similar` (±30 % per
  pulse). `cluster_learn_signals()` merges same-code captures, keeps the
  `max_signals` most frequently received, and returns them in **first-seen
  order** (the order of a rotating remote is meaningful).
- `preselected_signal_indices()` picks the learn-wizard defaults: every
  candidate seen ≥ `max(2, 25 % of the strongest)` times; if every candidate
  was seen exactly once (receiver `idle` packs a press into one capture), all
  candidates with the same encoding and bit length as the first one.
- Manual input: `parse_manual_variants()` keeps the entered order for raw,
  Tasmota B0/B1, tri-state, binary and Pronto.

### Tasmota RfRaw B0/B1 (parse_tasmota_rfraw)

Format: `AA B0 <len> <nbucket> <repeats> <bucket×n (2 bytes µs)> <data> 55`
(B1: `AA B1 <nbucket> <buckets> <data> 55`). Each **data nibble**: low 3 bits =
bucket index; bit `0x8` set = **mark**, clear = **space** (reverse-engineered
from a real Sonoff Bridge code, see the B0 test).

## 6. RF communication (rf_manager.py, command_rotation.py)

- **`RFManager`** — facade: `hub` (ReceiverHub), `matcher` (CodeMatcher),
  `async_send()`, `async_send_variants()`, `sync_variant()`,
  `new_learn_session()`, rotation helpers.
- **`async_send()`** — normalizes timings, checks the transmitter isn't
  `unavailable` (`unknown` is a valid ready state — a transmitter that has never
  sent reports `unknown`), builds `_StoredCommand` with the command's `repeat`
  (extra repeats; **0 = send exactly once**; unset/negative → `DEFAULT_REPEAT`
  = 7 → 8 frames on air), then calls the core helper.
- **`async_send_variants()`** — picks the next variant via `CommandRotation`
  (advanced *before* awaiting so concurrent sends reserve different variants),
  applies the sibling reset, schedules a rotation save, sends.
- **`sync_variant()`** — after a matched physical-remote reception (past the
  self-echo/debounce guards) the next send uses the variant *after* the
  received one.
- **Sibling reset** — device option `rotation_reset_on_other`: every send or
  reception of one (code, slot) resets the rotation of all *other* commands of
  the device (`CommandRotation.reset_others`; device grouping is resolved by
  `device_manager._rotation_reset_group`, injected via
  `set_rotation_group_resolver`). Off by default.
- Rotation is dropped for deleted codes/devices and reset when a slot is
  re-learned (`reset_code_rotation`, `reset_command_rotation`).
- **`ReceiverHub`** — multiplexes captures from all sources to subscribers.
  Sources: ESPHome nodes (native API `subscribe_infrared_rf_receive`,
  discovered via `list_entities_services()` because core does not cache RF
  *receiver* infos) + the `esphome.rf_code_received` bus event. Sources attach
  on first subscriber and detach on last. ESPHome `disconnect_callbacks`
  invalidate cached metadata and subscriptions; device-update notifications
  re-attach immediately after a reconnect; a 30 s maintenance scan picks up new
  ESPHome entries and retries failures. Receiver metadata (including empty
  results) is cached per ESPHome connection; failed discovery is not cached.
  Discovery is serialized per entry.
- **`CodeMatcher`** — registered variants `(slot, index, fingerprint, bits,
  callback, group)`. Matching is **tolerant**: exact fingerprint = distance 0,
  else decoded-bit Hamming distance ≤ `_MATCH_BIT_TOLERANCE` (15 %, min 2).
  - **At most one slot per entity fires per signal — the closest match**
    (ties favor the earlier-registered slot; `build_slots` puts `on` before
    `off`). Sibling buttons often differ by one bit.
  - **The same holds across the entities of one device** (`group=device_id`):
    only entities at the group's best distance fire; identical codes on
    several entities still all fire.
  - The matched variant index is passed to the callback (rotation sync).
- **`LearnSession`** — waits for the first signal (timeout), collects for
  `LEARN_COLLECT_WINDOW` (10 s, long enough for several presses of a rotating
  button), dedups by exact fingerprint (`times_seen`), then clusters.

## 7. Entities (entity_factory.py + platform files)

- **`RFCodeEntity`** (base, `CoordinatorEntity` + `RestoreEntity`): one entity
  per `RFCode`, `has_entity_name`, `assumed_state`. Reads the live code from the
  coordinator, so store edits apply without reload.
- **Dynamic add/remove:** `async_setup_code_platform` diffs stored codes and
  adds entities live; a deleted code makes its entity self-remove.
- **Sending:** platforms call `_async_send_slot(slot)`, which sends through
  `async_send_variants`. **Optimistic state is written *before* awaiting the
  send** so the UI reacts instantly. Keep this ordering when adding actions.
  `_command(slot)` returns the *first* variant (for "is this slot configured"
  checks only).
- **Receiving:** the base registers all variants with the matcher on add and
  on every coordinator update. `_handle_received(slot, signal, variant_index)`
  applies self-echo suppression (`SELF_ECHO_SUPPRESS_S`) and per-slot debounce
  (`RECV_DEBOUNCE_S`), syncs the rotation, fires
  `rf_device_manager_code_received` and calls `_on_rf_match(slot, signal)`.
  Send platforms update optimistic state there; `button` is a no-op;
  `sensor`/`binary_sensor` are receive-only (`RECEIVE_ONLY_TYPES`).
- **Attributes:** `commands` (first variant summary per slot, kept for
  backward compatibility) and `command_variants` (all variants).
- **Availability:** send entities follow the transmitter state; receive-only
  entities ignore it.

## 8. Config/Options flow (config_flow.py)

- Config flow: create / duplicate / reconfigure a device. The shared device
  schema includes `rotation_reset_on_other`.
- Options flow (`RFDeviceManagerOptionsFlow`) is the management UI: device
  settings, add code, **add buttons** (bulk), manage codes (view/edit/relearn/
  test/toggle/delete). The options menu does **not** run receiver discovery;
  `slot_menu`/`slot_learn` do (cached per connection).
- **Add-code wizard** per slot: `slot_menu` (learn/manual/skip) →
  `slot_learn` (`async_show_progress` on the `LearnSession` task; no receiver →
  `slot_no_receiver`) → `slot_learn_done` (multi-select, plausible candidates
  preselected, order = first-seen order) → `slot_confirm` (menu **Save / Test /
  Discard**; lists every variant; Test sends the first one; duplicate-code
  warning) → next slot → `save_code`.
- **Manual entry:** format dropdown. Core ≥ 2026.11
  (`_HAS_CONDITIONAL_FORM_FIELDS`) shows only the selected format's editor and
  settings on one page (`data_entry_flow.Required/Optional(visible=…)`,
  `probatio.Schema`); older cores use the two-step `slot_manual` →
  `slot_manual_data`. Both share selector builders and the parser. Unset repeat
  inherits the code/device setting; 0 sends once.
- **Bulk buttons** (`add_buttons`): names → queue of `button` codes; each goes
  straight to `slot_learn` (skip always offered), is saved immediately
  (`_bulk_active`), ending in `bulk_done`.
- `_duplicate_warning` compares every pending variant with every stored
  variant of the device (all wizards).
- No "enabled" toggle for new codes; signal testing is button-based (Test in
  `slot_confirm`), there is intentionally no "test only" toggle.

## 9. Services (services.py)

`send_code` (stored command by device+code+slot, rotates variants),
`send_raw` (ad-hoc raw/B0/tri-state/binary/Pronto; `repeat` default 7),
`learn_code` (returns captured signals as response data).

## 10. Rules for pull requests (mandatory)

Every PR — from a human or an AI agent — follows these rules. A PR that
breaks one is not merged until fixed.

### 10.1 Scope and branch

1. **One concern per PR.** Bug fix, feature, refactor and decoder change are
   separate PRs. Drive-by fixes go into their own commit with their own reason.
2. **Base on current `main`.** Do not include other PRs' commits or temporary
   merge commits "for testing". If a PR needs another one, say so at the top of
   the description ("Depends on #NN"), open it as **draft**, and rebase onto
   `main` once the dependency has landed.
3. **Check open PRs for overlap before starting** (`gh pr list`), especially
   for `config_flow.py`, `rf_manager.py`, `storage.py`, `const.py` and the
   translation files. Reuse instead of duplicating; mention overlaps.
4. Branch names: `fix/<topic>`, `feat/<topic>`, `decoder/<topic>`,
   `docs/<topic>`.

### 10.2 Code

5. Keep the invariants of section 2. Never import HA into the pure modules.
6. **Storage changes need a migration step**: bump `STORAGE_MINOR_VERSION` by
   exactly one, add `if old_major_version == 1 and old_minor_version < N:` to
   `_RFStore._async_migrate_func` (one function per step, chained), keep
   `from_dict` tolerant of the old shape, and add the step to the table in
   section 3. **Before bumping, check open PRs for the same number**; two PRs
   must never claim the same version — the later one takes the next number.
7. Config entry option changes: new options get a default everywhere they
   are read (`entry.options.get(KEY, default)`); semantic changes of existing
   options need an `async_migrate_entry` step and a `MINOR_VERSION` bump.
8. Changes to decoding/fingerprints follow section 11.5 (`DECODER_REVISION`).
9. New user-facing strings: `strings.json` → copy to `en.json` → same keys in
   `de.json` (German, informal "du"). Menu titles stay static.
10. Send path: optimistic state before `await`; never modify timings.
11. No new runtime requirement in `manifest.json` without a strong reason
    (HACS users cannot easily resolve conflicts).

### 10.3 Tests (required, all must pass)

```bash
python tests/test_decoders.py      # pure decoder/analysis suite
python tests/test_models.py        # storage shape, redecode
python tests/test_rotation.py      # rotation state/persistence
pip install -r requirements_test.txt
pytest tests/integration           # real HA core (pinned by the harness)
pip install homeassistant rf-protocols && python tests/smoke_latest_ha.py
```

12. **Every behavior change gets a test** that fails without the change.
    Prefer `tests/integration` for anything touching HA (flows, entities,
    storage, matching); pure logic goes into the standalone scripts.
13. Integration tests use the fixtures in `tests/integration/conftest.py`
    (`transmitter`, `sent`, `setup_device`, `receive`, `no_receive_guards`).
    Event listeners in tests must be `@callback` (plain functions run in the
    executor → nondeterministic order).
14. Storage changes: add a test that loads data in the **previous** format
    and asserts the migrated result (see `test_storage.py`).
15. Flow changes on code paths that differ by core version
    (`_HAS_CONDITIONAL_FORM_FIELDS`) must keep both paths working: pytest
    covers the harness' core, `tests/smoke_latest_ha.py` the newest core.
16. Lint before pushing: `ruff check --select F,E9,B custom_components tests`.
17. CI (`.github/workflows/validate.yaml`) must be green: decoder tests,
    integration tests, smoke test on latest HA, hassfest, HACS.

### 10.4 Docs and description

18. Update in the same PR: this file (if architecture/invariants change),
    `ARCHITECTURE.md` (German), `README.md` (user-facing behavior) and
    `CHANGELOG.md` (entry under the top "unreleased" heading; anything a user
    must know before updating goes under **Upgrade notes**).
19. PR description sections: **Summary**, **Why** (issue link), **How**,
    **Tests** (what was run, incl. hardware if any), **Upgrade impact**
    (storage/entry version, fingerprints, changed defaults — or "none").
20. The maintainer bumps `manifest.json` `version` when releasing; PRs don't.

### 10.5 Commits

21. Imperative subject ≤ 72 chars, body explains *why*.
22. **No `Co-Authored-By` or other AI attribution trailers** in commit
    messages (maintainer preference).

## 11. Developing a new protocol / decoder (mandatory procedure)

Most remotes need **no** protocol work: raw record/replay works for every
fixed-code remote. Protocol work only improves decoding (display, jitter-immune
fingerprints, manual entry). Do it in a `decoder/<name>` PR, nothing else in it.

### 11.1 Collect real data first

1. Capture with ESPHome `remote_receiver` (`dump: raw`), noting the receiver
   settings (`idle`, `filter`, `tolerance`, `buffer_size`) and frequency.
2. At least **3 presses each of ≥ 2 different buttons**; for rotating remotes,
   ≥ 2 full cycles of one button.
3. Record the expected bits/hex from an independent reference (rc-switch
   library output, rtl_433, the remote's label/manual). A decoder without an
   independently known expected value is not testable — don't merge it.
4. Check the capture with the current code first (`rf_analysis.analyze`):
   often it already decodes, or only a threshold is off.

### 11.2 Choose the smallest change

In this order of preference:

1. **New rc-switch timing profile** → one line in `RC_PROTOCOLS` (+ the
   `rc_protocol` description strings listing supported protocols).
2. **Threshold/analysis fix** (frame gap, base unit) in `rf_analysis.py`.
3. **New structural decoder** in `decoders.py` — only for a genuinely new
   encoding scheme, not for one remote brand.

### 11.3 Decoder contract

- Signature `_decode_x(ctx: FrameContext) -> DecodedFrame | None`, pure.
- Return `None` unless the frame **cleanly** matches; a false positive poisons
  fingerprints of other remotes. Reject on ambiguous timing ratios.
- Docstring documents the waveform (marks/spaces per bit in units of T, sync,
  bit order, **polarity** — which pulse means 1).
- Register in `DECODERS`; order = priority, most restrictive first. Justify the
  position in the PR.
- Add an `ENCODINGS[...]` label (en + de).
- If the encoding can be generated from bits, add an encoder for manual entry.

### 11.4 Tests for a protocol change (all required)

- **Real captures** from 11.1 with the expected bits (like the EV1527 and
  SURNICE P4 cases): `encoding`, `bits`, `bit_count`, `protocol_guess`.
- **Synthetic round trip** encode → decode at several pulse lengths and with
  timing jitter (±10 %), checking bits and reported pulse length.
- **Frame splitting**: multi-frame captures split into the right frame count;
  long in-frame spaces are not mistaken for gaps.
- **No regressions / no false positives**: all existing cases, the golden
  set and `NOISE` still decode as before (unless the PR intentionally
  changes them — see 11.5).
- **Matching**: for remotes with sibling codes a few bits apart, check the
  Hamming distances against `_MATCH_BIT_TOLERANCE`; add an integration test
  with `receive()` if matching behavior is affected.

### 11.5 If existing decodes change

If any golden fingerprint in `tests/test_decoders.py` (section 10) changes:

1. Bump `rf_analysis.DECODER_REVISION` by one and add a history line next to
   it (what changed, PR number).
2. Update `GOLDEN_REVISION` and the golden values in the same commit.
3. Add a storage test with commands decoded the old way (like
   `test_migrates_1_1_store_and_redecodes_legacy_codes`) asserting the new
   fingerprint and that receiving the signal matches again.
4. Add a CHANGELOG upgrade note (displayed hex/bits change, re-decode happens
   once on start).

Never change the `fingerprint()` hash scheme or `STORED_DECODE_KEYS` without
the same procedure.

### 11.6 Docs

Update the decoder table (section 4 here and README "Protocol detection") and
the CHANGELOG.

## 12. Repo / workflow notes

- Published at `github.com/Gifford47/rf_device_manager`. On the maintainer's
  Windows machine, Git over HTTPS needs `git config http.sslBackend schannel`
  (corporate TLS).
- Brand images live in `custom_components/rf_device_manager/brand/` (local
  brand images, supported since HA 2026.3 — no home-assistant/brands PR).
  Files: `icon.png` 256², `icon@2x.png` 512², `logo.png`/`logo@2x.png`
  (128/256 px high, landscape), `dark_logo.png`/`dark_logo@2x.png`. Only these
  filenames (plus `dark_icon*.png`) are served by HA; missing ones fall back
  (`logo` → `icon`, `dark_*` → light variant).
- `settings.json` in the repo root is local Claude Code permissions —
  gitignored, not part of the integration.
- `pytest.ini` restricts pytest to `tests/integration`; the `tests/test_*.py`
  files are standalone scripts (they run at import), not pytest modules.

## 13. Known hardware caveats (not integration bugs)

- Stock Sonoff RF Bridge routes the radio through the EFM8, not the ESP GPIOs;
  `remote_transmitter` needs the direct/Portisch mod and
  `carrier_duty_percent: 100%` to actually transmit.
- `Buffer overflow` in ESPHome logs = receiver overloaded; raise `idle`, add
  `filter:` and `buffer_size:` on the `remote_receiver`.
- True rolling-code remotes (KeeLoq, Somfy RTS) change their code each press
  with authentication — record/replay cannot work by design. Remotes that
  cycle through a **fixed** set of codes are supported via command variants.
