# Changelog

All notable changes to RF Device Manager. Versions follow the `version` in
`custom_components/rf_device_manager/manifest.json`.

## 1.2.0 (unreleased)

### ⚠️ Upgrade notes — read before updating

- **No downgrade without a backup.** On first start, 1.2.0 migrates
  `.storage/rf_device_manager` to schema **1.3** (every command slot becomes a
  list of command variants) and config entries to **1.2**. Versions ≤ 1.1.x
  cannot read the new format. Make a Home Assistant backup before updating;
  to go back, restore that backup instead of just installing the old version.
- **`repeat: 0` now means "send exactly once".** Previously 0 silently meant
  "use the default of 7 extra repeats". Stored zeros (device default, code
  settings, commands) are migrated to 7, so existing devices keep sending
  exactly as before. Automations calling `send_raw`/`send_code` with an
  explicit `repeat: 0` now send a single frame.
- **`send_code` refuses disabled codes and devices** (new errors
  `code_disabled` / `device_disabled`). Before, the service sent them even
  though their entities were unavailable.
- **Stored codes are re-decoded once** (decoder revision 1 → 2). Codes learned
  with rc-switch Protocol 8 (inverted bit polarity before) or Protocol 4 (sync
  gap not split before) get new fingerprints and displayed hex/bits, so they
  keep matching received signals. Sent timings are not changed. Only the
  displayed values (entity `commands` attribute, code details) change.

### Added

- **Rotating-code remotes** (#2, #12): one command slot can hold several
  ordered code variants. Learning keeps every distinct code in first-seen
  order; sending rotates through the variants per code/slot; receiving
  matches every variant and synchronizes the next send. Manual entry supports
  multiple variants in every input format.
- The rotation position is **persisted** in `.storage/rf_device_manager_rotation`
  and survives restarts.
- Device option **"Restart other buttons' code sequence"**: sending or
  receiving one command resets the rotation of all other commands of the
  device (remotes that restart a button's sequence on another button press).
- The learn wizard **preselects every plausible candidate** (received at least
  twice and at least 25 % as often as the strongest one) instead of only the
  first.
- **"Add buttons" wizard** (#7): learn a whole multi-button remote as
  stateless buttons in one go; duplicate-code warning on confirmation (covers
  all variants).
- Integration test suite against a real Home Assistant core
  (`tests/integration`, run in CI).

### Changed

- `repeat` semantics: 0 = send once, unset = inherit (#4).
- Received signals fire only the closest-matching entities of a device (#6).
- Receiver discovery is cached per ESPHome connection; reconnects are handled
  immediately; shared initialization is concurrency-safe (#9, includes #5).
- Learn collect window raised from 4 s to 10 s, so several presses of a
  rotating-code button fit into one learning session.

### Fixed

- RC-Switch Protocol 4 sync gap (2.28 ms) is recognized as a frame boundary
  (#10).
- RC-Switch Protocol 8 bit polarity and pulse-length detection (#11).
- A failed send no longer skips a command variant: the rotation is rolled
  back and sibling rotations are not reset, so two-variant remotes do not
  repeat the last accepted code after a transmitter error.
- A quick double press of a rotating remote (inside the 1 s receive
  debounce) keeps the next send's variant in step.
- Editing a code no longer briefly stops and restarts the receiver hub and
  its ESPHome subscriptions when the device has a single entity.
- Reconfiguring a disabled device no longer re-enables it.
- Re-learning an already configured command offers "Skip" after a learn
  timeout, keeping the stored command.
- Clearing the repeat field when editing a code uses the device default
  instead of silently keeping the old value.
- A non-numeric `frequency` in an `esphome.rf_code_received` event no longer
  drops the capture.
