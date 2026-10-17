"""Service handlers for RF Device Manager.

Services registered on the domain (available as soon as the integration is
loaded):

* ``send_code``  - transmit a stored command of a managed device
* ``send_raw``   - transmit ad-hoc RF data (raw/tri-state/binary/Pronto)
* ``learn_code`` - capture RF signals and return them as response data
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from . import rf_analysis
from .const import (
    ATTR_CODE,
    ATTR_COMMAND,
    ATTR_DEVICE_ID,
    ATTR_FREQUENCY,
    ATTR_REPEAT,
    ATTR_TIMEOUT,
    ATTR_TIMINGS,
    ATTR_TRANSMITTER,
    CONF_B0_CODE,
    CONF_BINARY_CODE,
    CONF_PRONTO_CODE,
    CONF_PULSE_LENGTH,
    CONF_RC_PROTOCOL,
    CONF_TRISTATE_CODE,
    DEFAULT_FREQUENCY,
    DEFAULT_LEARN_TIMEOUT,
    DEFAULT_REPEAT,
    DOMAIN,
    SERVICE_LEARN_CODE,
    SERVICE_SEND_CODE,
    SERVICE_SEND_RAW,
)
from .device_manager import get_domain_data

_LOGGER = logging.getLogger(__name__)

SEND_CODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_CODE): cv.string,
        vol.Optional(ATTR_COMMAND): cv.string,
        vol.Optional(ATTR_REPEAT): vol.All(vol.Coerce(int), vol.Range(min=0, max=50)),
    }
)

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TRANSMITTER): cv.entity_id,
        vol.Optional(ATTR_FREQUENCY, default=DEFAULT_FREQUENCY): vol.All(
            vol.Coerce(int), vol.Range(min=100_000_000, max=1_000_000_000)
        ),
        vol.Optional(ATTR_TIMINGS): cv.string,
        vol.Optional(CONF_B0_CODE): cv.string,
        vol.Optional(CONF_TRISTATE_CODE): cv.string,
        vol.Optional(CONF_BINARY_CODE): cv.string,
        vol.Optional(CONF_PRONTO_CODE): cv.string,
        vol.Optional(CONF_PULSE_LENGTH): vol.All(
            vol.Coerce(int), vol.Range(min=50, max=5000)
        ),
        vol.Optional(CONF_RC_PROTOCOL, default=1): vol.All(
            vol.Coerce(int), vol.In(sorted(rf_analysis.RC_PROTOCOLS))
        ),
        vol.Optional(ATTR_REPEAT, default=DEFAULT_REPEAT): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=50)
        ),
    }
)

LEARN_CODE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_TIMEOUT, default=DEFAULT_LEARN_TIMEOUT): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=300)
        ),
    }
)


def _resolve_rf_device_id(hass: HomeAssistant, ha_device_id: str) -> str:
    """Map a Home Assistant device registry id to the managed RF device id."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(ha_device_id)
    if device is not None:
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                return identifier
    # Allow passing the internal RF device id directly.
    if get_domain_data(hass).store.async_get_device(ha_device_id) is not None:
        return ha_device_id
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="device_not_found",
        translation_placeholders={"device_id": ha_device_id},
    )


async def _async_handle_send_code(call: ServiceCall) -> None:
    """Handle the ``send_code`` service."""
    hass = call.hass
    domain_data = get_domain_data(hass)
    rf_device_id = _resolve_rf_device_id(hass, call.data[ATTR_DEVICE_ID])
    device = domain_data.store.async_get_device(rf_device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": rf_device_id},
        )
    code = domain_data.device_manager.async_find_code(
        rf_device_id, call.data[ATTR_CODE]
    )
    if code is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="code_not_found",
            translation_placeholders={"code": call.data[ATTR_CODE]},
        )
    slot = call.data.get(ATTR_COMMAND) or code.primary_slot()
    command = code.commands.get(slot) if slot else None
    if command is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="slot_not_configured",
            translation_placeholders={"slot": slot or "?", "name": code.name},
        )
    await domain_data.rf_manager.async_send(
        command, device.transmitter, repeat=call.data.get(ATTR_REPEAT)
    )


async def _async_handle_send_raw(call: ServiceCall) -> None:
    """Handle the ``send_raw`` service."""
    hass = call.hass
    domain_data = get_domain_data(hass)
    pulse_length = call.data.get(CONF_PULSE_LENGTH)
    rc_protocol = call.data[CONF_RC_PROTOCOL]

    try:
        if timings_text := call.data.get(ATTR_TIMINGS):
            timings = rf_analysis.parse_timings_text(timings_text)
        elif b0_code := call.data.get(CONF_B0_CODE):
            timings = rf_analysis.parse_tasmota_rfraw(b0_code)
        elif tristate := call.data.get(CONF_TRISTATE_CODE):
            timings = rf_analysis.encode_tristate(
                tristate, pulse_length=pulse_length, protocol=rc_protocol
            )
        elif binary := call.data.get(CONF_BINARY_CODE):
            timings = rf_analysis.encode_binary(
                binary, pulse_length=pulse_length, protocol=rc_protocol
            )
        elif pronto := call.data.get(CONF_PRONTO_CODE):
            timings, _carrier = rf_analysis.parse_pronto(pronto)
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_rf_data"
            )
    except rf_analysis.RFInputError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_rf_data",
            translation_placeholders={"error": str(err)},
        ) from err

    await domain_data.rf_manager.async_send_raw(
        call.data[ATTR_TRANSMITTER],
        timings,
        frequency=call.data[ATTR_FREQUENCY],
        repeat=call.data[ATTR_REPEAT],
    )


async def _async_handle_learn_code(call: ServiceCall) -> ServiceResponse:
    """Handle the ``learn_code`` service; returns the captured signals."""
    hass = call.hass
    domain_data = get_domain_data(hass)
    session = domain_data.rf_manager.new_learn_session(
        timeout=call.data[ATTR_TIMEOUT]
    )
    try:
        signals = await session.async_capture()
    except TimeoutError:
        signals = []
    response: dict[str, Any] = {
        "signals": [signal.to_dict() for signal in signals],
        "count": len(signals),
    }
    return response


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_CODE, _async_handle_send_code, schema=SEND_CODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_RAW, _async_handle_send_raw, schema=SEND_RAW_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_CODE,
        _async_handle_learn_code,
        schema=LEARN_CODE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
