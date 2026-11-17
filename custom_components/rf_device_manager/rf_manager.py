"""RF communication layer: sending, receiving and learning.

Sending goes through the official ``radio_frequency`` entity platform that
Home Assistant 2026.5 introduced (``async_send_command`` helper +
``rf_protocols.RadioFrequencyCommand``), so any compatible transmitter -
most prominently ESPHome nodes with the ``ir_rf_proxy`` component and
sub-GHz hardware such as a CC1101 - works out of the box.

Receiving/learning uses the ESPHome native API directly: Home Assistant core
does not (yet) expose RF *receiver* entities, but the ESPHome integration's
runtime data provides the ``aioesphomeapi`` client whose
``subscribe_infrared_rf_receive`` stream delivers raw captures from any
``ir_rf_proxy`` receiver (``RadioFrequencyInfo`` with the RECEIVER
capability) - the exact mechanism core uses for its infrared receivers.
A Home Assistant bus event (``esphome.rf_code_received``) is supported as an
additional, hardware-agnostic capture source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.components.radio_frequency import (
    async_get_transmitters as rf_async_get_transmitters,
    async_send_command as rf_async_send_command,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from rf_protocols import ModulationType, RadioFrequencyCommand

from . import rf_analysis
from .const import (
    DEFAULT_LEARN_TIMEOUT,
    DEFAULT_SEND_REPEAT,
    DOMAIN,
    ESPHOME_DOMAIN,
    ESPHOME_FALLBACK_EVENT,
    LEARN_COLLECT_WINDOW,
    LEARN_MAX_SIGNALS,
    MIN_SIGNAL_PULSES,
    RADIO_FREQUENCY_DOMAIN,
    RECEIVER_MAINTENANCE_INTERVAL,
)
from .models import CapturedSignal, RFCommandData

try:  # ESPHome is only an after_dependency; degrade gracefully without it.
    from aioesphomeapi import RadioFrequencyCapability, RadioFrequencyInfo

    HAS_AIOESPHOMEAPI = True
except ImportError:  # pragma: no cover - environment without ESPHome
    RadioFrequencyCapability = None  # type: ignore[assignment]
    RadioFrequencyInfo = None  # type: ignore[assignment]
    HAS_AIOESPHOMEAPI = False

_LOGGER = logging.getLogger(__name__)

SignalCallback = Callable[[CapturedSignal], None]


class _StoredCommand(RadioFrequencyCommand):
    """A stored/learned RF code replayed as raw OOK timings.

    We subclass the stable top-level ``rf_protocols.RadioFrequencyCommand``
    (the same import Home Assistant core uses) and set the attributes the
    transmitter reads directly. ``super().__init__`` is deliberately skipped
    to stay immune to constructor changes across rf-protocols releases; the
    transmitter only consumes ``frequency``, ``modulation``, ``repeat_count``
    and ``get_raw_timings()``.
    """

    def __init__(self, frequency: int, timings: list[int], repeat_count: int) -> None:
        """Initialize from normalized raw timings."""
        self.frequency = frequency
        self.modulation = ModulationType.OOK
        self.repeat_count = repeat_count
        self.symbol_rate = None
        self.output_power = None
        self._timings = timings

    def get_raw_timings(self) -> list[int]:
        """Return the signed alternating microsecond timings."""
        return self._timings


@dataclass(slots=True)
class TransmitterInfo:
    """Description of an available RF transmitter entity."""

    entity_id: str
    name: str
    available: bool
    device_id: str | None = None


@dataclass(slots=True)
class _ReceiverMeta:
    """Metadata of one ESPHome RF receiver."""

    name: str
    frequency: int | None


# ---------------------------------------------------------------------------
# Transmitter discovery / validation
# ---------------------------------------------------------------------------


@callback
def async_list_transmitters(hass: HomeAssistant) -> list[TransmitterInfo]:
    """Return all ``radio_frequency`` transmitter entities known to HA.

    The entity registry is the primary source (covers currently unavailable
    transmitters); plain states are merged in for completeness.
    """
    transmitters: dict[str, TransmitterInfo] = {}
    ent_reg = er.async_get(hass)

    for entry in ent_reg.entities.values():
        if entry.domain != RADIO_FREQUENCY_DOMAIN or entry.disabled:
            continue
        state = hass.states.get(entry.entity_id)
        transmitters[entry.entity_id] = TransmitterInfo(
            entity_id=entry.entity_id,
            name=entry.name or entry.original_name or entry.entity_id,
            available=state is not None and state.state != STATE_UNAVAILABLE,
            device_id=entry.device_id,
        )

    for entity_id in hass.states.async_entity_ids(RADIO_FREQUENCY_DOMAIN):
        if entity_id in transmitters:
            continue
        state = hass.states.get(entity_id)
        transmitters[entity_id] = TransmitterInfo(
            entity_id=entity_id,
            name=(state and state.name) or entity_id,
            available=state is not None and state.state != STATE_UNAVAILABLE,
        )

    return sorted(transmitters.values(), key=lambda info: info.name.casefold())


@callback
def async_validate_transmitter(
    hass: HomeAssistant, entity_id: str, frequency: int
) -> str | None:
    """Validate that a transmitter exists and supports a frequency.

    Returns ``None`` when valid, otherwise an error key
    (``transmitter_not_found`` / ``unsupported_frequency``).
    """
    known = {info.entity_id for info in async_list_transmitters(hass)}
    if entity_id not in known:
        return "transmitter_not_found"
    try:
        compatible = rf_async_get_transmitters(hass, frequency, ModulationType.OOK)
    except HomeAssistantError:
        # Component not loaded yet or no transmitters registered; the
        # registry check above already guarantees existence.
        return None
    if compatible and entity_id not in compatible:
        return "unsupported_frequency"
    return None


# ---------------------------------------------------------------------------
# Receiver hub
# ---------------------------------------------------------------------------


class ReceiverHub:
    """Multiplex RF captures and manage ESPHome receive subscriptions.

    ESPHome subscriptions are bound to one API connection. The ESPHome
    integration notifies us when that connection becomes available or
    unavailable; we use those notifications to discard stale subscriptions
    and attach to the new connection.

    A periodic scan remains necessary to notice ESPHome config entries that
    are loaded while this hub is already running. It also retries failed
    discovery/subscription attempts, but does not re-enumerate a node whose
    receivers have already been discovered for its current connection.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the hub."""
        self._hass = hass
        self._subscribers: set[SignalCallback] = set()
        self._esphome_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._esphome_device_unsubs: dict[
            str, tuple[Any, CALLBACK_TYPE]
        ] = {}
        # The presence of an entry is significant even when its receiver map
        # is empty: an empty map means discovery succeeded and found no RF
        # receiver on the current connection.
        self._esphome_receivers: dict[
            str, dict[tuple[int, int], _ReceiverMeta]
        ] = {}
        self._esphome_node_names: dict[str, str] = {}
        self._esphome_attach_in_progress: set[str] = set()
        self._esphome_discovery_locks: dict[str, asyncio.Lock] = {}
        self._esphome_connection_generations: dict[str, int] = {}
        self._esphome_discovery_failures: dict[str, int] = {}
        self._esphome_subscription_failures: dict[str, int] = {}
        self._esphome_receive_api_missing: set[str] = set()
        self._esphome_connection_callback_missing: set[str] = set()
        self._bus_unsub: CALLBACK_TYPE | None = None
        self._timer_unsub: CALLBACK_TYPE | None = None
        self._cached_names: list[str] = []
        self._running = False
        self._generation = 0

    @callback
    def async_subscribe(self, signal_callback: SignalCallback) -> CALLBACK_TYPE:
        """Subscribe to captured signals; returns an unsubscribe callback."""
        self._subscribers.add(signal_callback)
        if len(self._subscribers) == 1:
            self._async_start()

        @callback
        def _unsubscribe() -> None:
            self._subscribers.discard(signal_callback)
            if not self._subscribers:
                self._async_stop()

        return _unsubscribe

    @callback
    def async_receiver_names(self) -> list[str]:
        """Return the receiver names found by the last discovery pass."""
        return list(self._cached_names)

    async def async_discover(
        self,
    ) -> dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]]:
        """Detect RF receivers on all connected ESPHome nodes.

        The ESPHome platform currently filters RadioFrequencyInfo down to
        transmitters before storing it in runtime_data.info. RF receivers are
        therefore discovered through list_entities_services() unless a future
        ESPHome integration version exposes their static infos.

        This explicit discovery method is also used by the diagnostics
        download. Normal receiver maintenance uses the per-connection cache
        below instead of repeatedly enumerating already-discovered nodes.
        """
        discovered: dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]] = {}
        names: list[str] = []
        if HAS_AIOESPHOMEAPI:
            for entry in self._hass.config_entries.async_entries(ESPHOME_DOMAIN):
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                runtime_data = getattr(entry, "runtime_data", None)
                if runtime_data is None or not getattr(
                    runtime_data, "available", False
                ):
                    continue
                client = getattr(runtime_data, "client", None)
                if client is None:
                    continue
                device_info = getattr(runtime_data, "device_info", None)
                node_name = getattr(device_info, "name", None) or entry.title
                receivers = self._receivers_from_info(runtime_data)
                if not receivers:
                    receivers = await self._receivers_from_device(
                        client, node_name, entry.entry_id
                    )
                if receivers is None or not receivers:
                    continue
                discovered[entry.entry_id] = (node_name, receivers)
                names.extend(
                    f"{node_name}: {meta.name}" if meta.name else node_name
                    for meta in receivers.values()
                )
        self._cached_names = sorted(names)
        return discovered

    @staticmethod
    def _build_receiver_metas(infos: Any) -> dict[tuple[int, int], _ReceiverMeta]:
        """Build receiver metadata from RadioFrequencyInfo objects."""
        receivers: dict[tuple[int, int], _ReceiverMeta] = {}
        for info in infos:
            capabilities = int(getattr(info, "capabilities", 0))
            if not capabilities & int(RadioFrequencyCapability.RECEIVER):
                continue
            freq_min = int(getattr(info, "frequency_min", 0))
            freq_max = int(getattr(info, "frequency_max", 0))
            frequency = freq_min if freq_min and freq_min == freq_max else None
            receivers[(int(getattr(info, "device_id", 0)), int(info.key))] = (
                _ReceiverMeta(
                    name=getattr(info, "name", "") or "RF receiver",
                    frequency=frequency,
                )
            )
        return receivers

    @classmethod
    def _receivers_from_info(
        cls, runtime_data: Any
    ) -> dict[tuple[int, int], _ReceiverMeta]:
        """Extract receivers from the ESPHome integration's static infos."""
        info_map = getattr(runtime_data, "info", None)
        if not isinstance(info_map, dict):
            return {}
        rf_infos = info_map.get(RadioFrequencyInfo) or {}
        return cls._build_receiver_metas(rf_infos.values())

    async def _receivers_from_device(
        self, client: Any, node_name: str, entry_id: str
    ) -> dict[tuple[int, int], _ReceiverMeta] | None:
        """Ask the ESPHome node for RF receiver entities.

        Return None on failure so the caller can distinguish an unsuccessful
        request (which should be retried) from a successful listing with no RF
        receivers (which should not be repeated until the next connection).
        """
        # Diagnostics and automatic attachment may overlap. Serialize the
        # entity-list request per ESPHome node so the API is never queried
        # concurrently by two RF Device Manager operations.
        lock = self._esphome_discovery_locks.setdefault(entry_id, asyncio.Lock())
        async with lock:
            try:
                entities, _services = await client.list_entities_services()
            except Exception as err:  # noqa: BLE001 - connection may drop mid-call
                failures = self._esphome_discovery_failures.get(entry_id, 0) + 1
                self._esphome_discovery_failures[entry_id] = failures
                if failures == 1:
                    _LOGGER.warning(
                        "Could not list entities of ESPHome node %s while discovering "
                        "RF receivers; discovery will be retried",
                        node_name,
                        exc_info=True,
                    )
                else:
                    _LOGGER.debug(
                        "RF receiver discovery on ESPHome node %s is still failing "
                        "(attempt %d): %s",
                        node_name,
                        failures,
                        err,
                    )
                return None

        failures = self._esphome_discovery_failures.pop(entry_id, 0)
        if failures:
            _LOGGER.info(
                "RF receiver discovery on ESPHome node %s recovered after "
                "%d failed attempt(s)",
                node_name,
                failures,
            )
        return self._build_receiver_metas(
            info for info in entities if isinstance(info, RadioFrequencyInfo)
        )

    @callback
    def _async_start(self) -> None:
        """Attach all capture sources and begin looking for ESPHome nodes."""
        self._generation += 1
        self._running = True
        self._bus_unsub = self._hass.bus.async_listen(
            ESPHOME_FALLBACK_EVENT, self._handle_bus_event
        )
        self._timer_unsub = async_track_time_interval(
            self._hass,
            self._async_maintain,
            timedelta(seconds=RECEIVER_MAINTENANCE_INTERVAL),
        )
        _LOGGER.debug("Receiver hub started")
        # Scan immediately; later scans only find newly loaded entries or
        # retry a discovery/subscription that did not complete successfully.
        self._async_maintain()

    @callback
    def _async_stop(self) -> None:
        """Detach all capture sources and ESPHome lifecycle callbacks."""
        self._generation += 1
        self._running = False
        if self._bus_unsub is not None:
            self._bus_unsub()
            self._bus_unsub = None
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None
        for unsub in self._esphome_unsubs.values():
            try:
                unsub()
            except Exception:  # noqa: BLE001 - client may already be gone
                _LOGGER.debug("Error detaching ESPHome subscription", exc_info=True)
        self._esphome_unsubs.clear()
        for _runtime_data, unsub in self._esphome_device_unsubs.values():
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error detaching ESPHome connection listener", exc_info=True
                )
        self._esphome_device_unsubs.clear()
        self._esphome_receivers.clear()
        self._esphome_node_names.clear()
        self._esphome_discovery_failures.clear()
        self._esphome_subscription_failures.clear()
        self._esphome_receive_api_missing.clear()
        self._esphome_connection_callback_missing.clear()
        self._esphome_connection_generations.clear()
        self._cached_names.clear()
        _LOGGER.debug("Receiver hub stopped")

    @callback
    def _async_maintain(self, _now: Any = None) -> None:
        """Find newly loaded nodes and retry incomplete receiver attachments."""
        if not self._running or not self._subscribers:
            return
        self._async_sync_esphome_entries(self._generation)

    @callback
    def _async_sync_esphome_entries(self, generation: int) -> None:
        """Track loaded ESPHome entries and attach receivers when needed."""
        if not self._is_running(generation) or not HAS_AIOESPHOMEAPI:
            return

        loaded_entry_ids: set[str] = set()
        for entry in self._hass.config_entries.async_entries(ESPHOME_DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            loaded_entry_ids.add(entry.entry_id)
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is None:
                self._async_remove_esphome_entry(entry.entry_id)
                continue

            device_subscription = self._esphome_device_unsubs.get(entry.entry_id)
            if (
                device_subscription is None
                or device_subscription[0] is not runtime_data
            ):
                if device_subscription is not None:
                    self._esphome_connection_generations[entry.entry_id] = (
                        self._esphome_connection_generations.get(entry.entry_id, 0)
                        + 1
                    )
                    self._async_invalidate_esphome_connection(
                        entry.entry_id, entry.title, unsubscribe=True
                    )
                    try:
                        device_subscription[1]()
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Error replacing ESPHome connection listener",
                            exc_info=True,
                        )
                    self._esphome_device_unsubs.pop(entry.entry_id, None)
                    self._esphome_connection_callback_missing.discard(entry.entry_id)
                subscribe_updates = getattr(
                    runtime_data, "async_subscribe_device_updated", None
                )
                if subscribe_updates is None:
                    if (
                        entry.entry_id
                        not in self._esphome_connection_callback_missing
                    ):
                        self._esphome_connection_callback_missing.add(entry.entry_id)
                        _LOGGER.warning(
                            "ESPHome node %s does not expose connection update "
                            "callbacks; receiver recovery will rely on periodic checks",
                            entry.title,
                        )
                else:
                    try:
                        unsub = subscribe_updates(
                            partial(
                                self._handle_esphome_device_update,
                                entry.entry_id,
                                generation,
                            )
                        )
                    except Exception:  # noqa: BLE001
                        if (
                            entry.entry_id
                            not in self._esphome_connection_callback_missing
                        ):
                            self._esphome_connection_callback_missing.add(
                                entry.entry_id
                            )
                            _LOGGER.warning(
                                "Could not monitor connection updates for ESPHome "
                                "node %s",
                                entry.title,
                                exc_info=True,
                            )
                    else:
                        self._esphome_connection_callback_missing.discard(
                            entry.entry_id
                        )
                        self._esphome_device_unsubs[entry.entry_id] = (
                            runtime_data,
                            unsub,
                        )
                        _LOGGER.debug(
                            "Tracking connection lifecycle of ESPHome node %s",
                            entry.title,
                        )

            if not getattr(runtime_data, "available", False):
                if (
                    entry.entry_id in self._esphome_unsubs
                    or entry.entry_id in self._esphome_receivers
                    or entry.entry_id in self._esphome_attach_in_progress
                ):
                    self._esphome_connection_generations[entry.entry_id] = (
                        self._esphome_connection_generations.get(entry.entry_id, 0) + 1
                    )
                self._async_invalidate_esphome_connection(
                    entry.entry_id, entry.title
                )
                continue

            receivers = self._esphome_receivers.get(entry.entry_id)
            if receivers is None or (
                receivers
                and entry.entry_id not in self._esphome_unsubs
                and entry.entry_id not in self._esphome_receive_api_missing
            ):
                self._hass.async_create_task(
                    self._async_attach_esphome_entry(entry.entry_id, generation)
                )

        # Config entries can be removed or unloaded while the receiver hub is
        # active. Unregister their callbacks and discard their cached metadata.
        tracked_entry_ids = (
            set(self._esphome_device_unsubs)
            | set(self._esphome_unsubs)
            | set(self._esphome_receivers)
            | set(self._esphome_connection_callback_missing)
        )
        for entry_id in tracked_entry_ids - loaded_entry_ids:
            self._async_remove_esphome_entry(entry_id)

    @callback
    def _handle_esphome_device_update(
        self, entry_id: str, generation: int
    ) -> None:
        """Handle ESPHome connect/disconnect notifications from Home Assistant."""
        if not self._is_running(generation):
            return
        entry = self._hass.config_entries.async_get_entry(entry_id)
        runtime_data = getattr(entry, "runtime_data", None) if entry else None
        node_name = (
            getattr(getattr(runtime_data, "device_info", None), "name", None)
            or (entry.title if entry else entry_id)
        )
        self._esphome_connection_generations[entry_id] = (
            self._esphome_connection_generations.get(entry_id, 0) + 1
        )
        if not runtime_data or not getattr(runtime_data, "available", False):
            _LOGGER.debug(
                "ESPHome node %s disconnected; invalidating its RF receiver "
                "connection state",
                node_name,
            )
            self._async_invalidate_esphome_connection(entry_id, node_name)
            return

        # Normally the disconnect callback has already cleared the previous
        # subscription. Clear it defensively in case an availability transition
        # was missed while the hub was starting or stopping.
        self._async_invalidate_esphome_connection(
            entry_id, node_name, unsubscribe=True
        )
        _LOGGER.debug(
            "ESPHome node %s connected; discovering RF receivers on the new "
            "API connection",
            node_name,
        )
        self._hass.async_create_task(
            self._async_attach_esphome_entry(entry_id, generation)
        )

    def _is_running(self, generation: int) -> bool:
        """Return whether an asynchronous operation belongs to this hub run."""
        return (
            self._running
            and bool(self._subscribers)
            and generation == self._generation
        )

    def _async_invalidate_esphome_connection(
        self,
        entry_id: str,
        node_name: str,
        *,
        unsubscribe: bool = False,
    ) -> None:
        """Forget connection-bound receiver state after a disconnect/reconnect."""
        receiver_unsub = self._esphome_unsubs.pop(entry_id, None)
        if receiver_unsub is not None and unsubscribe:
            try:
                receiver_unsub()
            except Exception:  # noqa: BLE001 - old API connection may be closed
                _LOGGER.debug(
                    "Error detaching old RF receiver subscription from %s",
                    node_name,
                    exc_info=True,
                )

        had_connection_state = (
            receiver_unsub is not None or entry_id in self._esphome_receivers
        )
        self._esphome_receivers.pop(entry_id, None)
        self._esphome_node_names.pop(entry_id, None)
        self._esphome_discovery_failures.pop(entry_id, None)
        self._esphome_subscription_failures.pop(entry_id, None)
        self._esphome_receive_api_missing.discard(entry_id)
        self._refresh_cached_names()
        if had_connection_state and not unsubscribe:
            # Match Home Assistant's ESPHome receiver entities: a subscription
            # belongs to the disconnected API connection, so discard its
            # unsubscribe handle rather than trying to use it on a dead session.
            if receiver_unsub is not None:
                _LOGGER.debug(
                    "ESPHome node %s disconnected; invalidated its RF receiver "
                    "subscription",
                    node_name,
                )
            else:
                _LOGGER.debug(
                    "ESPHome node %s disconnected; cleared its RF receiver "
                    "discovery state",
                    node_name,
                )

    def _async_remove_esphome_entry(self, entry_id: str) -> None:
        """Remove all callbacks and cached state for an unloaded ESPHome entry."""
        node_name = self._esphome_node_names.get(entry_id, entry_id)
        self._async_invalidate_esphome_connection(
            entry_id, node_name, unsubscribe=True
        )
        self._esphome_connection_generations[entry_id] = (
            self._esphome_connection_generations.get(entry_id, 0) + 1
        )
        if device_subscription := self._esphome_device_unsubs.pop(entry_id, None):
            try:
                device_subscription[1]()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error removing ESPHome connection listener", exc_info=True
                )
        self._esphome_connection_callback_missing.discard(entry_id)

    async def _async_attach_esphome_entry(
        self, entry_id: str, generation: int
    ) -> None:
        """Discover and subscribe to RF receivers on one ESPHome connection."""
        if not self._is_running(generation):
            return
        if entry_id in self._esphome_attach_in_progress:
            return
        self._esphome_attach_in_progress.add(entry_id)
        connection_generation = self._esphome_connection_generations.get(entry_id, 0)
        try:
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                return
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is None or not getattr(runtime_data, "available", False):
                return
            client = getattr(runtime_data, "client", None)
            if client is None:
                return
            device_info = getattr(runtime_data, "device_info", None)
            node_name = getattr(device_info, "name", None) or entry.title

            receivers = self._esphome_receivers.get(entry_id)
            if receivers is None:
                _LOGGER.debug(
                    "Discovering RF receivers on ESPHome node %s", node_name
                )
                receivers = self._receivers_from_info(runtime_data)
                if not receivers:
                    receivers = await self._receivers_from_device(
                        client, node_name, entry_id
                    )
                if receivers is None:
                    return
                if not self._is_running(generation):
                    return
                # Do not associate results from a request made on a connection
                # that has since gone away with a later connection.
                current_entry = self._hass.config_entries.async_get_entry(
                    entry_id
                )
                if (
                    current_entry is not entry
                    or getattr(current_entry, "runtime_data", None) is not runtime_data
                    or getattr(runtime_data, "client", None) is not client
                    or not self._is_entry_available(entry_id)
                    or connection_generation
                    != self._esphome_connection_generations.get(entry_id, 0)
                ):
                    return
                self._esphome_receivers[entry_id] = receivers
                self._esphome_node_names[entry_id] = node_name
                self._refresh_cached_names()
                if not receivers:
                    _LOGGER.debug(
                        "No RF receiver found on ESPHome node %s", node_name
                    )
                    return
            elif not receivers:
                return
            else:
                node_name = self._esphome_node_names.get(entry_id, node_name)

            if entry_id in self._esphome_unsubs:
                return
            subscribe = getattr(client, "subscribe_infrared_rf_receive", None)
            if subscribe is None:
                if entry_id not in self._esphome_receive_api_missing:
                    self._esphome_receive_api_missing.add(entry_id)
                    _LOGGER.warning(
                        "ESPHome node %s exposes RF receivers but this "
                        "aioesphomeapi client has no receive subscription API",
                        node_name,
                    )
                return

            try:
                unsub = subscribe(
                    partial(self._handle_esphome_event, receivers, node_name)
                )
            except Exception as err:  # noqa: BLE001 - connection may drop mid-call
                failures = self._esphome_subscription_failures.get(entry_id, 0) + 1
                self._esphome_subscription_failures[entry_id] = failures
                if failures == 1:
                    _LOGGER.warning(
                        "Could not subscribe to RF receivers on ESPHome node %s; "
                        "subscription will be retried",
                        node_name,
                        exc_info=True,
                    )
                else:
                    _LOGGER.debug(
                        "RF receiver subscription on ESPHome node %s is still "
                        "failing (attempt %d): %s",
                        node_name,
                        failures,
                        err,
                    )
                return

            if not self._is_running(generation) or not self._is_entry_available(
                entry_id
            ):
                try:
                    unsub()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "Error detaching RF subscription after connection changed",
                        exc_info=True,
                    )
                return

            self._esphome_unsubs[entry_id] = unsub
            failures = self._esphome_subscription_failures.pop(entry_id, 0)
            if failures:
                _LOGGER.info(
                    "RF receiver subscription on ESPHome node %s recovered "
                    "after %d failed attempt(s)",
                    node_name,
                    failures,
                )
            else:
                _LOGGER.debug(
                    "Subscribed to %d RF receiver(s) on %s",
                    len(receivers),
                    node_name,
                )
        finally:
            self._esphome_attach_in_progress.discard(entry_id)
            # A reconnect can happen while entity enumeration is awaiting the
            # device. The new connection's update callback may have found this
            # entry still in progress; retry now against the new connection.
            if (
                self._running
                and self._subscribers
                and self._is_entry_available(entry_id)
                and (
                    generation != self._generation
                    or connection_generation
                    != self._esphome_connection_generations.get(entry_id, 0)
                )
            ):
                self._hass.async_create_task(
                    self._async_attach_esphome_entry(entry_id, self._generation)
                )

    def _refresh_cached_names(self) -> None:
        """Refresh receiver names after discovery or connection changes."""
        names = [
            f"{self._esphome_node_names[entry_id]}: {meta.name}"
            if meta.name
            else self._esphome_node_names[entry_id]
            for entry_id, receivers in self._esphome_receivers.items()
            if entry_id in self._esphome_node_names
            for meta in receivers.values()
        ]
        self._cached_names = sorted(names)

    def _is_entry_available(self, entry_id: str) -> bool:
        """Return whether an ESPHome config entry is loaded and connected."""
        entry = self._hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return False
        runtime_data = getattr(entry, "runtime_data", None)
        return bool(runtime_data and getattr(runtime_data, "available", False))

    @callback
    def _handle_esphome_event(
        self,
        receivers: dict[tuple[int, int], _ReceiverMeta],
        node_name: str,
        event: Any,
    ) -> None:
        """Handle one InfraredRFReceiveEvent from an ESPHome node."""
        key = (int(getattr(event, "device_id", 0)), int(getattr(event, "key", 0)))
        meta = receivers.get(key)
        if meta is None:
            return  # IR receiver or unknown proxy entity on the same node
        timings = list(getattr(event, "timings", []) or [])
        self._async_dispatch(
            timings,
            source=f"{node_name}: {meta.name}",
            frequency=meta.frequency,
        )

    @callback
    def _handle_bus_event(self, event: Event) -> None:
        """Handle the ``esphome.rf_code_received`` fallback bus event."""
        raw = (
            event.data.get("timings")
            or event.data.get("raw")
            or event.data.get("data")
        )
        if raw is None:
            return
        if isinstance(raw, str):
            try:
                timings = rf_analysis.parse_timings_text(raw)
            except rf_analysis.RFInputError:
                _LOGGER.debug("Ignoring unparsable bus event payload: %s", raw)
                return
        elif isinstance(raw, (list, tuple)):
            try:
                timings = [int(value) for value in raw]
            except (TypeError, ValueError):
                return
        else:
            return
        frequency = event.data.get("frequency")
        self._async_dispatch(
            timings,
            source=event.data.get("source", "event"),
            frequency=int(frequency) if frequency else None,
        )

    @callback
    def _async_dispatch(
        self, timings: list[int], source: str, frequency: int | None
    ) -> None:
        """Analyze a capture and fan it out to all subscribers."""
        normalized = rf_analysis.normalize(timings)
        if len(normalized) < MIN_SIGNAL_PULSES:
            return
        signal = CapturedSignal(
            timings=normalized,
            source=source,
            frequency=frequency,
            analysis=rf_analysis.analyze(normalized),
        )
        _LOGGER.debug(
            "RF capture from %s: %d pulses, fingerprint %s",
            source,
            len(normalized),
            signal.fingerprint,
        )
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(signal)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break others
                _LOGGER.exception("Error in RF signal subscriber")


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def _cluster_learn_signals(
    signals: list[CapturedSignal], max_signals: int
) -> list[CapturedSignal]:
    """Group captures that carry the same RF code and rank by frequency.

    Real button presses repeat their frame many times while 433 MHz noise
    and neighbouring devices produce one-off captures, so after clustering
    (identical decoded bits, or dominant frames within timing tolerance)
    the wanted signal reliably sorts to the top.
    """
    clusters: list[list[CapturedSignal]] = []
    for signal in signals:
        for cluster in clusters:
            if rf_analysis.same_code(cluster[0].analysis, signal.analysis):
                cluster.append(signal)
                break
        else:
            clusters.append([signal])

    merged: list[CapturedSignal] = []
    for cluster in clusters:
        representative = max(
            cluster,
            key=lambda sig: (
                sig.analysis.get("times_seen", 1),
                sig.analysis.get("frame_length", 0),
            ),
        )
        representative.analysis["times_seen"] = sum(
            sig.analysis.get("times_seen", 1) for sig in cluster
        )
        if len(cluster) > 1:
            representative.analysis["variants"] = len(cluster)
        merged.append(representative)

    merged.sort(
        key=lambda sig: (
            sig.analysis.get("times_seen", 1),
            sig.analysis.get("frame_length", 0),
        ),
        reverse=True,
    )
    return merged[:max_signals]


@dataclass
class LearnSession:
    """One learning pass: wait for signals, collect, cluster.

    ``async_capture`` blocks until the first signal arrives (or the timeout
    expires), then keeps collecting for a short window. Captures are first
    deduplicated by exact fingerprint, then clustered by code similarity so
    jittery repeats of the same button collapse into one entry.
    """

    hub: ReceiverHub
    timeout: float = DEFAULT_LEARN_TIMEOUT
    collect_window: float = LEARN_COLLECT_WINDOW
    max_signals: int = LEARN_MAX_SIGNALS
    signals: list[CapturedSignal] = field(default_factory=list)

    async def async_capture(self) -> list[CapturedSignal]:
        """Capture signals; raises ``TimeoutError`` when nothing is received."""
        first_signal = asyncio.Event()
        seen: dict[str, CapturedSignal] = {}
        raw_capacity = self.max_signals * 3

        @callback
        def _collect(signal: CapturedSignal) -> None:
            key = signal.fingerprint or f"raw-{len(self.signals)}"
            if (existing := seen.get(key)) is not None:
                existing.analysis["times_seen"] = (
                    existing.analysis.get("times_seen", 1) + 1
                )
                return
            if len(self.signals) >= raw_capacity:
                return
            signal.analysis.setdefault("times_seen", 1)
            seen[key] = signal
            self.signals.append(signal)
            first_signal.set()

        unsubscribe = self.hub.async_subscribe(_collect)
        try:
            async with asyncio.timeout(self.timeout):
                await first_signal.wait()
            await asyncio.sleep(self.collect_window)
        finally:
            unsubscribe()

        self.signals = _cluster_learn_signals(self.signals, self.max_signals)
        return self.signals


# ---------------------------------------------------------------------------
# Received-code matcher
# ---------------------------------------------------------------------------

MatchCallback = Callable[[str, CapturedSignal], None]

# A received code matches a stored one when their decoded bit strings differ
# by at most this fraction of their length. Real receptions jitter and often
# corrupt the last few (trailing/sync) bits, so an exact match misses many
# genuine presses; this tolerance recovers them without matching unrelated
# codes (different remotes differ in far more bits).
_MATCH_BIT_TOLERANCE = 0.15


@dataclass(slots=True)
class _MatchEntry:
    """One registered command slot to match received signals against."""

    slot: str
    fingerprint: str | None
    bits: str | None
    callback: MatchCallback


class CodeMatcher:
    """Routes received RF signals to the entities whose code they match.

    Every entity registers its command slots here (and re-registers whenever
    they change). A single hub subscription then routes each incoming capture
    to the matching entities, so an external remote press updates the Home
    Assistant entity state. Matching is tolerant: an exact fingerprint hit is
    preferred, otherwise decoded bit strings are compared with a small Hamming
    distance so jittery/partly-corrupted receptions still match.
    """

    def __init__(self, hub: ReceiverHub) -> None:
        """Initialize the matcher on top of a receiver hub."""
        self._hub = hub
        self._entries: list[_MatchEntry] = []
        self._hub_unsub: CALLBACK_TYPE | None = None

    @callback
    def async_register(
        self,
        slots: list[tuple[str, str | None, str | None]],
        match_callback: MatchCallback,
    ) -> CALLBACK_TYPE:
        """Register an entity's slots as ``(slot, fingerprint, bits)`` tuples.

        Returns a callback that removes exactly these registrations. The hub
        subscription starts on the first registration and stops with the last.
        """
        added = [
            _MatchEntry(slot, fingerprint, bits, match_callback)
            for slot, fingerprint, bits in slots
            if fingerprint or bits
        ]
        self._entries.extend(added)

        if self._hub_unsub is None and self._entries:
            self._hub_unsub = self._hub.async_subscribe(self._dispatch)

        @callback
        def _unregister() -> None:
            for entry in added:
                try:
                    self._entries.remove(entry)
                except ValueError:  # already gone
                    pass
            if not self._entries and self._hub_unsub is not None:
                self._hub_unsub()
                self._hub_unsub = None

        return _unregister

    @callback
    def _dispatch(self, signal: CapturedSignal) -> None:
        """Route a captured signal to the best-matching slot of each entity.

        Crucially, at most **one** slot per entity is triggered per signal -
        the closest match. Sibling buttons of one remote often differ by a
        single bit, so a received code lands within tolerance of several
        slots of the same entity; dispatching all of them would flip the
        state back and forth (on/off/on/off) once per repeated frame.
        """
        fingerprint = signal.fingerprint
        bits = signal.analysis.get("bits")

        # callback (entity) -> (best distance, best entry)
        best: dict[MatchCallback, tuple[int, _MatchEntry]] = {}
        for entry in self._entries:
            distance = self._match_distance(entry, fingerprint, bits)
            if distance is None:
                continue
            current = best.get(entry.callback)
            if current is None or distance < current[0]:
                best[entry.callback] = (distance, entry)

        for _distance, entry in best.values():
            try:
                entry.callback(entry.slot, signal)
            except Exception:  # noqa: BLE001 - one bad handler must not break others
                _LOGGER.exception("Error handling received RF code")

    @staticmethod
    def _match_distance(
        entry: _MatchEntry, fingerprint: str | None, bits: str | None
    ) -> int | None:
        """Return the match distance (0 = exact) or ``None`` if no match."""
        if entry.fingerprint and fingerprint and entry.fingerprint == fingerprint:
            return 0
        if bits and entry.bits and len(bits) == len(entry.bits):
            distance = sum(a != b for a, b in zip(bits, entry.bits, strict=False))
            if distance <= max(2, round(len(bits) * _MATCH_BIT_TOLERANCE)):
                return distance
        return None


# ---------------------------------------------------------------------------
# Manager facade
# ---------------------------------------------------------------------------


class RFManager:
    """Facade bundling transmit, receive and learn functionality."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager, its receiver hub and the code matcher."""
        self._hass = hass
        self.hub = ReceiverHub(hass)
        self.matcher = CodeMatcher(self.hub)

    async def async_send(
        self,
        command: RFCommandData,
        transmitter: str,
        *,
        frequency: int | None = None,
        repeat: int | None = None,
    ) -> None:
        """Send a stored command through a ``radio_frequency`` transmitter.

        Raises:
            HomeAssistantError: If the command holds no timings, the
                transmitter is missing/unavailable or transmission fails.
        """
        timings = rf_analysis.normalize(command.data)
        if not timings:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="empty_command"
            )
        # Note: a transmitter that has never sent reports "unknown" - that is
        # a valid, ready state; only "unavailable" (or a missing entity)
        # blocks sending.
        state = self._hass.states.get(transmitter)
        if state is None or state.state == STATE_UNAVAILABLE:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="transmitter_unavailable",
                translation_placeholders={"transmitter": transmitter},
            )
        # Send the captured timings exactly as received (only normalized:
        # sign convention, glitch removal, idle trim). Real remotes and
        # tools like Tasmota's RfRaw simply replay the raw waveform and let
        # the transmitter repeat it a handful of times, so apply a sensible
        # minimum repetition when the stored command has none.
        effective_repeat = repeat if repeat is not None else command.repeat
        if effective_repeat is None or effective_repeat <= 0:
            effective_repeat = DEFAULT_SEND_REPEAT
        rf_command = _StoredCommand(
            frequency=frequency or command.frequency,
            timings=timings,
            repeat_count=effective_repeat,
        )
        _LOGGER.debug(
            "Sending %d pulses at %d Hz via %s (repeat=%d)",
            len(timings),
            rf_command.frequency,
            transmitter,
            rf_command.repeat_count,
        )
        await rf_async_send_command(self._hass, transmitter, rf_command)

    async def async_send_raw(
        self,
        transmitter: str,
        timings: list[int],
        frequency: int,
        repeat: int = 0,
    ) -> None:
        """Send raw timings (service helper)."""
        command = RFCommandData(data=timings, frequency=frequency, repeat=repeat)
        await self.async_send(command, transmitter)

    def new_learn_session(
        self, timeout: float = DEFAULT_LEARN_TIMEOUT
    ) -> LearnSession:
        """Create a fresh learning session."""
        return LearnSession(hub=self.hub, timeout=timeout)
