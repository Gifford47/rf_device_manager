"""Smoke test against the newest Home Assistant core (no pytest harness).

``pytest-homeassistant-custom-component`` lags behind core releases, so the
pytest suite in ``tests/integration`` runs on the harness' pinned core. This
script boots a minimal real core of whatever version is installed and drives
the flows that depend on newer core features (the one-page manual form with
conditional field visibility, added in 2026.11):

    pip install homeassistant rf-protocols
    python tests/smoke_latest_ha.py

Exits non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
from unittest.mock import AsyncMock, patch

import homeassistant  # noqa: F401  (must be imported before anything else)
from homeassistant import auth as ha_auth, bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import translation
from homeassistant.helpers.data_entry_flow import _BaseFlowManagerView
from homeassistant.setup import async_setup_component

REPO = pathlib.Path(__file__).resolve().parents[1]
DOMAIN = "rf_device_manager"
TRANSMITTER = "radio_frequency.smoke_tx"
GIOLA_SPEED_1 = ["0x147109", "0x17720A", "0x16730B", "0x157008"]


async def _boot(config_dir: str) -> HomeAssistant:
    hass = HomeAssistant(config_dir)
    hass.config.skip_pip = True
    hass.config.language = "en"
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    hass.auth = await ha_auth.auth_manager_from_config(hass, [], [])
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, "http", {"http": {"server_port": 18123}})
    return hass


async def _run(config_dir: str) -> None:
    hass = await _boot(config_dir)
    hass.states.async_set(TRANSMITTER, "unknown")
    assert await async_setup_component(hass, DOMAIN, {}), "integration setup failed"

    from custom_components.rf_device_manager import config_flow  # noqa: PLC0415

    print(f"Home Assistant {HA_VERSION}")
    print(f"conditional form fields: {config_flow._HAS_CONDITIONAL_FORM_FIELDS}")

    with patch(
        "custom_components.rf_device_manager.rf_manager.rf_async_get_transmitters",
        return_value=[TRANSMITTER],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "name": "Fan",
                "transmitter": TRANSMITTER,
                "frequency_preset": "433920000",
                "default_repeat": 7,
                "rotation_reset_on_other": True,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        entry = result["result"]
        await hass.async_block_till_done()

        domain_data = hass.data[DOMAIN]
        options = hass.config_entries.options
        view = _BaseFlowManagerView(options)
        with patch.object(domain_data.rf_manager.hub, "async_discover", AsyncMock()):
            result = await options.async_init(entry.entry_id)
            result = await options.async_configure(
                result["flow_id"], {"next_step_id": "add_code"}
            )
            result = await options.async_configure(
                result["flow_id"], {"name": "Speed 1", "entity_type": "button"}
            )
            result = await options.async_configure(
                result["flow_id"], {"next_step_id": "slot_manual"}
            )
            assert result["step_id"] == "slot_manual", result

            # Serialize exactly like the frontend API does - must not raise.
            fields = view._prepare_result_json(result)["data_schema"]
            names = [field["name"] for field in fields]
            print(f"manual form fields: {names}")
            assert "input_format" in names
            if config_flow._HAS_CONDITIONAL_FORM_FIELDS:
                assert "variants_binary" in names
                visible = {f["name"] for f in fields if f.get("visible")}
                assert "variants_binary" in visible, "no visibility conditions"

                variants = [
                    {"value": format(int(code, 16), "024b")} for code in GIOLA_SPEED_1
                ]
                result = await options.async_configure(
                    result["flow_id"],
                    {
                        "input_format": "binary",
                        "variants_binary": variants,
                        "rc_protocol": "8",
                    },
                )
            else:
                result = await options.async_configure(
                    result["flow_id"], {"input_format": "binary"}
                )
                variants = [
                    {"value": format(int(code, 16), "024b")} for code in GIOLA_SPEED_1
                ]
                result = await options.async_configure(
                    result["flow_id"],
                    {"variants_binary": variants, "rc_protocol": "8"},
                )
            assert result["step_id"] == "slot_confirm", result
            assert result["description_placeholders"]["variant_count"] == "4"
            result = await options.async_configure(
                result["flow_id"], {"next_step_id": "slot_accept"}
            )
            result = await options.async_configure(result["flow_id"], {})
            assert result["type"] is FlowResultType.CREATE_ENTRY, result

        device_id = entry.data["device_id"]
        code = next(iter(domain_data.store.async_codes_for_device(device_id).values()))
        stored = [command.code for command in code.commands["press"]]
        assert stored == [variant["value"] for variant in variants], stored
        device = domain_data.store.async_get_device(device_id)
        assert device.settings.get("rotation_reset_on_other") is True

    for language in ("en", "de"):
        strings = await translation.async_get_translations(
            hass, language, "options", [DOMAIN]
        )
        assert any("rotation_reset_on_other" in key for key in strings), language

    await hass.async_stop(force=True)
    print("OK: smoke test passed")


def main() -> int:
    with tempfile.TemporaryDirectory() as config_dir:
        (pathlib.Path(config_dir) / "custom_components").symlink_to(
            REPO / "custom_components"
        )
        sys.path.insert(0, config_dir)
        asyncio.run(_run(config_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
