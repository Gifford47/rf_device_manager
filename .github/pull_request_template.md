<!-- Read CLAUDE.md section 10 (PR rules) and, for decoder changes, section 11. -->

Depends on: <!-- #NN, or "nothing". If it depends on an open PR, open this as draft. -->

## Summary

## Why
<!-- Issue link / problem -->

## How

## Tests
- [ ] `python tests/test_decoders.py`
- [ ] `python tests/test_models.py`
- [ ] `python tests/test_rotation.py`
- [ ] `pytest tests/integration`
- [ ] `python tests/smoke_latest_ha.py` (newest HA core)
- [ ] New/changed behavior has a test that fails without this PR
- [ ] Hardware test (describe receiver/transmitter, remote) — or "none"

## Upgrade impact
<!-- Storage / config entry version, DECODER_REVISION, changed defaults or
     fingerprints, translations. Write "none" if there is none. -->

## Checklist
- [ ] Based on current `main`, no foreign or temporary merge commits
- [ ] Checked open PRs for overlap (and storage/entry version numbers)
- [ ] `strings.json` == `translations/en.json`, `de.json` has the same keys
- [ ] CHANGELOG (unreleased), README / ARCHITECTURE.md / CLAUDE.md updated as needed
- [ ] No AI attribution trailers in commit messages
