# CLAUDE.md — RF Device Manager

Guidance for AI assistants (and humans) working in this repository. Read this
before changing code. It captures the architecture, the non-obvious logic, and
the invariants that keep the integration working.

## What this is

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

See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and data model, and
[README.md](README.md) for user-facing docs.

## Golden rules / invariants

1. **`decoders.py` and `rf_analysis.py` must stay pure Python** (stdlib only, no
   `homeassistant`/`aioesphomeapi` imports). This is what makes
   `tests/test_decoders.py` runnable without HA. Do not add HA imports there.
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
6. **Python 3.11 locally** (HA runtime is newer). Avoid 3.12-only syntax like
   `type X = ...` aliases in code that the local validation scripts parse.
7. **Keep `strings.json` and `translations/en.json` identical.** `en.json` is a
   copy of `strings.json`. After editing `strings.json`, copy it over `en.json`
   and mirror every key into `translations/de.json`. The validation script
   fails on any key mismatch.

## Data model (models.py)

- **`RFDeviceConfig`** — one managed device. Persisted in the config entry
  (`entry.data` holds only `device_id`; `entry.options` holds the mutable
  settings) *and* mirrored into the central store.
- **`RFCode`** — one code = **one entity**. Has an `entity_type` and a dict of
  **command slots** → `RFCommandData`.
- **`RFCommandData`** — one transmittable/matchable command. `data` is always
  normalized raw timings. `protocol`/`code` keep the original input form;
  `extra` holds decode results (`encoding`, `bits`, `hex`, `fingerprint`, …).
  `fingerprint` is the key used for receive-matching.
- **Command slots** are derived from the entity type by
  `models.build_slots(entity_type, settings)`, e.g. switch → `on`/`off`; cover →
  `open`/`close`/`stop`; fan → `off`/`speed_1..n`/`oscillate`/direction; select →
  `option:<name>`; number/sensor → `value:<v>`. Slot keys are the contract
  between the learn wizard, the platforms, and the matcher.

Storage: `storage.py` (`RFDeviceStore`, HA Storage API, versioned + migratable).
Devices/codes are JSON dicts via `to_dict`/`from_dict` (tolerant of missing
keys).

## Protocol decoding (decoders.py) — the core logic

`decode_frame(frame)` builds a `FrameContext` once (mark/space pairs) and walks
the `DECODERS` chain **in order**; first structural match wins:

| Decoder | Structure | Examples |
|---|---|---|
| `space_distance` | constant mark, short/long space | EV1527, PT2262 |
| `mark_width` | constant space, short/long mark | SIRC-style |
| `pwm` | mark+space share the bit (rc-switch) | rc-switch P1–8 (+ guess) |
| `manchester` | every bit is a level transition | Oregon, some fans |

Detection is **structural, not a protocol database** — unknown remotes decode as
long as they use one of these schemes. Anything else still works via the raw
fallback (no `bits`, matched by frame similarity instead).

**To add a decoder:** write `_decode_x(ctx) -> DecodedFrame | None` (return
`None` unless the frame *cleanly* matches — a false positive poisons
fingerprints), document its waveform, register it in `DECODERS` (order =
priority, most restrictive first), add an `ENCODINGS[...]` label, and add a test
case in `tests/test_decoders.py` (ideally from a real ESPHome
`remote_receiver` log line, like the EV1527 case).

Manchester note: a trailing space half-bit gets swallowed by the inter-frame
gap; it's reconstructed as the complement of the last half (every bit is a
transition). Don't "simplify" that away.

## Analysis pipeline (rf_analysis.py)

- `normalize()` = `ensure_signed` → `remove_glitches` (merge sub-80µs spikes) →
  `trim_idle`. Run before anything else.
- `split_frames()` cuts on the inter-frame gap. Threshold is adaptive:
  `max(2400µs, 1.7 × 95th-percentile pulse)` — tuned so short (~2.8ms) EV1527
  sync gaps are recognized. Changing these constants changes what counts as a
  frame boundary; retest against `tests/test_decoders.py`.
- `fingerprint()` = hash of decoded bits when decodable (jitter-immune,
  `"<n>b:<sha>"`), else quantized-frame hash (`"<n>:<sha>"`). **Fingerprints are
  the receive-matching key**; if you change fingerprinting, stored codes stop
  matching received signals until re-learned.
- `same_code(a, b)` clusters captures: identical decoded bits, else
  `frames_similar` (±30% per-pulse tolerance). Used by the learn session.
- Manual input parsers: `parse_timings_text`, `parse_tasmota_rfraw` (B0/B1
  bucket format), `encode_binary`/`encode_tristate` (rc-switch), `parse_pronto`.

### Tasmota RfRaw B0/B1 (parse_tasmota_rfraw)

Format: `AA B0 <len> <nbucket> <repeats> <bucket×n (2 bytes µs)> <data> 55`
(B1: `AA B1 <nbucket> <buckets> <data> 55`). Each **data nibble**: low 3 bits =
bucket index; bit `0x8` set = **mark**, clear = **space**. This was reverse-
engineered from a real Sonoff Bridge code (see the B0 test) — the level-bit
polarity is the non-obvious part.

## RF communication (rf_manager.py)

- **`RFManager`** — facade: `hub` (ReceiverHub), `matcher` (CodeMatcher),
  `async_send()`, `new_learn_session()`.
- **`async_send()`** — normalizes timings, checks the transmitter isn't
  `unavailable` (note: `unknown` is a valid "ready" state — a transmitter that
  has never sent reports `unknown`), builds `_StoredCommand`, applies
  `DEFAULT_SEND_REPEAT` (7 extra → 8 frames on air, matching a short button
  hold) when the command's repeat is 0/unset, then calls the core helper.
- **`ReceiverHub`** — multiplexes captures from all sources to subscribers.
  Sources: ESPHome nodes (native API `subscribe_infrared_rf_receive`, discovered
  via `list_entities_services()` because core does not cache RF *receiver*
  infos) + the `esphome.rf_code_received` bus event. Sources attach on first
  subscriber, detach on last; a 30s maintenance timer re-attaches after ESPHome
  reconnects. `async_discover()` refreshes the receiver-name cache shown in the
  learn UI.
- **`CodeMatcher`** — registered slots `(slot, fingerprint, bits, callback)`;
  one hub subscription routes each received capture to the matching entities.
  Matching is **tolerant**: exact fingerprint = distance 0, else decoded-bit
  Hamming distance ≤ `_MATCH_BIT_TOLERANCE` (15%) — real receptions jitter and
  corrupt trailing/sync bits, so exact-only misses genuine presses.
  **At most one slot per entity fires per signal — the closest match** (ties
  favor the earlier-registered slot, and `build_slots` puts `on` before `off`).
  This is essential: sibling remote buttons often differ by a single bit, so a
  received code lands within tolerance of several slots of one entity;
  dispatching all of them would flip state on/off/on/off once per repeated
  frame. **The same holds across the entities of one device**: entities
  register with `group=device_id`, and per signal only the entities at the
  group's best distance fire (an exact hit beats near-misses on sibling
  buttons; identical codes on several entities still all fire). Without this,
  one press on a remote learned as separate button entities fired every
  button within tolerance. Codes learned before bit-decoding existed only match exactly until
  re-learned. Per-slot reception is debounced in the entity
  (`RECV_DEBOUNCE_S`) since one press arrives as many repeated frames.
- **`LearnSession`** — waits for the first signal (timeout), then collects for a
  short window, dedups by exact fingerprint, then `_cluster_learn_signals`
  merges same-code captures (`same_code`) and ranks by reception count. This is
  why a single button press yields one top-ranked entry instead of ~8
  noise/jitter duplicates.

## Entities (entity_factory.py + platform files)

- **`RFCodeEntity`** (base, `CoordinatorEntity` + `RestoreEntity`): one entity
  per `RFCode`. `has_entity_name`, `assumed_state`. Reads the live code from the
  coordinator, so store edits (rename, re-learn) apply without reload.
- **Dynamic add/remove:** `async_setup_code_platform` adds a coordinator
  listener that diffs stored codes and adds new entities live; a deleted code
  makes its entity self-remove in `_handle_coordinator_update`.
- **Sending:** platforms call `_async_send_slot(slot)`. **Optimistic state is
  written *before* awaiting the send** (`_attr_* = ...; async_write_ha_state()`
  then `await ...`) so the UI reacts instantly even if the ESP round-trip is
  slow. Keep this ordering when adding actions.
- **Receiving (all entities):** the base registers its commands' fingerprints
  with the `CodeMatcher` in `async_added_to_hass` and re-registers on every
  coordinator update (fingerprints change after re-learn). On a match,
  `_handle_received` fires the `rf_device_manager_code_received` bus event and
  calls `_on_rf_match(slot, signal)`.
  - Send platforms (switch/light/cover/fan/select/number) override
    `_on_rf_match` to update optimistic state from the matched slot — this is
    how an external remote press is reflected in HA.
  - `button` has no state → base no-op.
  - `sensor`/`binary_sensor` are receive-only (`RECEIVE_ONLY_TYPES`, no
    transmitter needed) and use `_on_rf_match` as their primary state source
    (`RFReceiveCodeEntity` is just a marker subclass now).
- **Availability:** send entities are unavailable when the transmitter is
  unavailable (tracked via `async_track_state_change_event`); receive-only
  entities ignore the transmitter.

## Config/Options flow (config_flow.py)

- Config flow: create device / duplicate device / reconfigure.
- Options flow (`RFDeviceManagerOptionsFlow`) is the management UI:
  - device settings; add-code wizard; manage codes (view/edit/relearn/test/
    toggle/delete).
  - **Add-code wizard** walks the slots from `build_slots`: per slot →
    `slot_menu` (learn/manual/skip) → `slot_learn` (`async_show_progress` on the
    `LearnSession` task) → `slot_learn_done` (pick a clustered signal) →
    `slot_confirm` (menu: **Save / Test / Discard** buttons) → next slot →
    `save_code`. Manual entry (`slot_manual`) supports raw / Tasmota B0 /
    tri-state / binary / Pronto.
  - **No-receiver guard:** `slot_learn` refreshes `hub.async_discover()` and,
    if no receiver is found, routes to `slot_no_receiver` (warn + retry /
    manual / skip) instead of waiting for a signal that can never arrive.
    `slot_menu` also refreshes discovery so "Active receivers" is accurate.
  - **No "enabled" toggle** — new codes default to enabled.
  - **Signal testing is button-based**: select a signal → confirmation step has
    the "Test" button. There is intentionally no "test only" toggle.
- **Menu step titles must be static** (no `{placeholders}`): HA only applies
  `description_placeholders` to a menu's description, not its title —
  placeholders in a menu title raise a formatjs MISSING_VALUE error. Put dynamic
  info in the description.
- **ICU escaping:** in translation strings, wrap placeholders that sit next to
  literal quotes with typographic quotes (`„…"` / `"…"`), never straight
  `'…'` — single quotes are ICU escape characters and hide the placeholder.

## Services (services.py)

`send_code` (stored command by device+code+slot), `send_raw` (ad-hoc
raw/B0/tri-state/binary/Pronto), `learn_code` (returns captured signals as
response data).

## Testing & validation

- `python tests/test_decoders.py` — pure-Python decoder/analysis suite (also in
  CI `.github/workflows/validate.yaml`). Covers the real EV1527 capture,
  Manchester jitter, rc-switch, tri-state, B0. **Add a case whenever you touch
  decoding.**
- CI also runs `hassfest` and HACS validation (the HACS brands check passes via
  the local `custom_components/rf_device_manager/brand/icon.png`).
- No HA test harness in this repo; validate flows by reasoning + hassfest.

## Repo / workflow notes

- Published at `github.com/Gifford47/rf_device_manager`. Git over HTTPS needs
  `git config http.sslBackend schannel` in this environment (corporate TLS).
- Commit messages: do **not** add a Co-Authored-By trailer (user preference).
- Brand images live in `custom_components/rf_device_manager/brand/` (local
  brand images, supported since HA 2026.3 — no home-assistant/brands PR).
  Files: `icon.png` 256², `icon@2x.png` 512², `logo.png`/`logo@2x.png`
  (128/256 px high, landscape), `dark_logo.png`/`dark_logo@2x.png`. Only these
  filenames (plus `dark_icon*.png`) are served by HA; missing ones fall back
  (`logo` → `icon`, `dark_*` → light variant).
- `settings.json` in the repo root is local Claude Code permissions — gitignored,
  not part of the integration.

## Known hardware caveats (not integration bugs)

- Stock Sonoff RF Bridge routes the radio through the EFM8, not the ESP GPIOs;
  `remote_transmitter` needs the direct/Portisch mod and
  `carrier_duty_percent: 100%` to actually transmit.
- `Buffer overflow` in ESPHome logs = receiver overloaded; raise `idle`, add
  `filter:` and `buffer_size:` on the `remote_receiver`.
- Rolling-code remotes (KeeLoq, Somfy RTS) change their code each press —
  record/replay cannot work by design.
