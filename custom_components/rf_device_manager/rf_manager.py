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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import logging
from typing import Any, NamedTuple

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
from homeassistant.helpers.storage import Store
from rf_protocols import ModulationType, RadioFrequencyCommand

from . import rf_analysis
from .command_rotation import CommandRotation
from .const import (
    DEFAULT_LEARN_TIMEOUT,
    DEFAULT_REPEAT,
    DOMAIN,
    ESPHOME_DOMAIN,
    ESPHOME_FALLBACK_EVENT,
    LEARN_COLLECT_WINDOW,
    LEARN_MAX_SIGNALS,
    MIN_SIGNAL_PULSES,
    RADIO_FREQUENCY_DOMAIN,
    RECEIVER_MAINTENANCE_INTERVAL,
    ROTATION_SAVE_DELAY,
    ROTATION_STORAGE_KEY,
    ROTATION_STORAGE_VERSION,
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


class _ReceiverCache(NamedTuple):
    """Receiver metadata tied to one ESPHome API connection."""

    runtime_data: Any
    client: Any
    node_name: str
    receivers: dict[tuple[int, int], _ReceiverMeta]


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

    Receiver metadata is cached for the lifetime of an ESPHome API connection.
    The hub only maintains live subscriptions while it has consumers.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the hub."""
        self._hass = hass
        self._subscribers: set[SignalCallback] = set()
        self._esphome_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._device_unsubs: dict[str, tuple[Any, CALLBACK_TYPE]] = {}
        # Empty receiver maps are successful, cacheable discovery results.
        self._receiver_cache: dict[str, _ReceiverCache] = {}
        self._disconnect_callbacks: dict[str, tuple[Any, CALLBACK_TYPE]] = {}
        self._discovery_locks: dict[str, asyncio.Lock] = {}
        self._connection_generations: dict[str, int] = {}
        self._attach_in_progress: set[str] = set()
        self._discovery_failures: dict[str, int] = {}
        self._subscription_failures: dict[str, int] = {}
        self._receive_api_missing: set[str] = set()
        self._bus_unsub: CALLBACK_TYPE | None = None
        self._timer_unsub: CALLBACK_TYPE | None = None
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
        """Return receiver names from the current discovery cache."""
        return sorted(
            f"{cached.node_name}: {meta.name}" if meta.name else cached.node_name
            for cached in self._receiver_cache.values()
            for meta in cached.receivers.values()
        )

    async def async_discover(
        self,
    ) -> dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]]:
        """Discover receivers, reusing results from the current connection."""
        discovered: dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]] = {}
        loaded_ids: set[str] = set()

        if HAS_AIOESPHOMEAPI:
            for entry in self._hass.config_entries.async_entries(ESPHOME_DOMAIN):
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                loaded_ids.add(entry.entry_id)
                runtime_data = getattr(entry, "runtime_data", None)
                client = getattr(runtime_data, "client", None)
                if (
                    runtime_data is None
                    or client is None
                    or not getattr(runtime_data, "available", False)
                ):
                    if (
                        entry.entry_id in self._receiver_cache
                        or entry.entry_id in self._disconnect_callbacks
                    ):
                        self._async_invalidate_connection(
                            entry.entry_id, entry.title, unsubscribe=client is None
                        )
                    continue

                node_name = (
                    getattr(getattr(runtime_data, "device_info", None), "name", None)
                    or entry.title
                )
                receivers = await self._async_get_receivers(
                    entry, runtime_data, client, node_name
                )
                if receivers:
                    discovered[entry.entry_id] = (node_name, receivers)

        # Entries can be removed while the hub is stopped.
        tracked_ids = (
            set(self._receiver_cache)
            | set(self._disconnect_callbacks)
            | set(self._esphome_unsubs)
            | set(self._device_unsubs)
            | set(self._discovery_failures)
        )
        for entry_id in tracked_ids - loaded_ids:
            self._async_remove_entry(entry_id)

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
        self, entry_id: str, client: Any, node_name: str
    ) -> dict[tuple[int, int], _ReceiverMeta] | None:
        """Query the node; return None on failure so the result is retried."""
        try:
            entities, _services = await client.list_entities_services()
        except Exception as err:  # noqa: BLE001 - connection may drop mid-call
            failures = self._discovery_failures.get(entry_id, 0) + 1
            self._discovery_failures[entry_id] = failures
            if failures == 1:
                _LOGGER.warning(
                    "Could not discover RF receivers on ESPHome node %s; "
                    "discovery will be retried",
                    node_name,
                    exc_info=True,
                )
            else:
                _LOGGER.debug(
                    "RF receiver discovery on ESPHome node %s is still failing: %s",
                    node_name,
                    err,
                )
            return None

        return self._build_receiver_metas(
            info for info in entities if isinstance(info, RadioFrequencyInfo)
        )

    def _watch_disconnect(
        self, entry_id: str, runtime_data: Any, node_name: str
    ) -> None:
        """Invalidate connection-bound state as soon as ESPHome disconnects."""
        current = self._disconnect_callbacks.get(entry_id)
        if current is not None and current[0] is runtime_data:
            return
        if current is not None:
            current[0].disconnect_callbacks.discard(current[1])

        disconnect_callback = partial(
            self._handle_disconnect, entry_id, runtime_data, node_name
        )
        runtime_data.disconnect_callbacks.add(disconnect_callback)
        self._disconnect_callbacks[entry_id] = (runtime_data, disconnect_callback)
        _LOGGER.debug("Tracking disconnect lifecycle of ESPHome node %s", node_name)

    @callback
    def _handle_disconnect(
        self, entry_id: str, runtime_data: Any, node_name: str
    ) -> None:
        """Forget state belonging to the disconnected API connection."""
        current = self._disconnect_callbacks.get(entry_id)
        if current is None or current[0] is not runtime_data:
            return
        # Home Assistant has already cleared disconnect_callbacks. The old
        # receive unsubscribe handle belongs to the dead API connection.
        _LOGGER.debug(
            "ESPHome node %s disconnected; invalidating RF receiver state",
            node_name,
        )
        self._async_invalidate_connection(entry_id, node_name)

    async def _async_get_receivers(
        self, entry: Any, runtime_data: Any, client: Any, node_name: str
    ) -> dict[tuple[int, int], _ReceiverMeta] | None:
        """Return cached receivers or discover them once for this connection."""
        entry_id = entry.entry_id
        cached = self._receiver_cache.get(entry_id)
        if (
            cached is not None
            and cached.runtime_data is runtime_data
            and cached.client is client
        ):
            return cached.receivers
        if cached is not None:
            self._async_invalidate_connection(entry_id, node_name, unsubscribe=True)

        lock = self._discovery_locks.get(entry_id)
        if lock is None:
            lock = self._discovery_locks[entry_id] = asyncio.Lock()
        async with lock:
            cached = self._receiver_cache.get(entry_id)
            if (
                cached is not None
                and cached.runtime_data is runtime_data
                and cached.client is client
            ):
                return cached.receivers
            if cached is not None:
                self._async_invalidate_connection(
                    entry_id, node_name, unsubscribe=True
                )

            current_entry = self._hass.config_entries.async_get_entry(entry_id)
            if (
                current_entry is not entry
                or entry.state is not ConfigEntryState.LOADED
                or getattr(entry, "runtime_data", None) is not runtime_data
                or getattr(runtime_data, "client", None) is not client
                or not getattr(runtime_data, "available", False)
            ):
                return None

            generation = self._connection_generations.get(entry_id, 0)
            self._watch_disconnect(entry_id, runtime_data, node_name)
            receivers = self._receivers_from_info(runtime_data)
            if not receivers:
                receivers = await self._receivers_from_device(
                    entry_id, client, node_name
                )
            if receivers is None:
                return None

            # Reject a result if this connection changed while discovery awaited.
            current_entry = self._hass.config_entries.async_get_entry(entry_id)
            if (
                current_entry is not entry
                or getattr(current_entry, "runtime_data", None) is not runtime_data
                or getattr(runtime_data, "client", None) is not client
                or not getattr(runtime_data, "available", False)
                or generation != self._connection_generations.get(entry_id, 0)
            ):
                return None

            failures = self._discovery_failures.pop(entry_id, 0)
            if failures:
                _LOGGER.info(
                    "RF receiver discovery on ESPHome node %s recovered after "
                    "%d failed attempt(s)",
                    node_name,
                    failures,
                )
            if not receivers:
                _LOGGER.debug("No RF receiver found on ESPHome node %s", node_name)

            self._receiver_cache[entry_id] = _ReceiverCache(
                runtime_data, client, node_name, receivers
            )
            return receivers

    @callback
    def _async_start(self) -> None:
        """Attach capture sources and start receiver maintenance."""
        self._generation += 1
        self._bus_unsub = self._hass.bus.async_listen(
            ESPHOME_FALLBACK_EVENT, self._handle_bus_event
        )
        self._timer_unsub = async_track_time_interval(
            self._hass,
            self._async_maintain,
            timedelta(seconds=RECEIVER_MAINTENANCE_INTERVAL),
        )
        _LOGGER.debug("Receiver hub started")
        self._async_maintain()

    @callback
    def _async_stop(self) -> None:
        """Detach live subscriptions while retaining valid discovery cache."""
        self._generation += 1
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
        for _runtime_data, unsub in self._device_unsubs.values():
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error removing ESPHome connection listener", exc_info=True
                )
        self._device_unsubs.clear()
        self._subscription_failures.clear()
        _LOGGER.debug("Receiver hub stopped")

    @callback
    def _async_maintain(self, _now: Any = None) -> None:
        """Notice new entries and retry incomplete receiver attachments."""
        if self._subscribers:
            self._async_sync_esphome_entries(self._generation)

    @callback
    def _async_sync_esphome_entries(self, generation: int) -> None:
        """Track loaded ESPHome entries and attach receivers when needed."""
        if not self._is_running(generation) or not HAS_AIOESPHOMEAPI:
            return

        loaded_ids: set[str] = set()
        for entry in self._hass.config_entries.async_entries(ESPHOME_DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            loaded_ids.add(entry.entry_id)
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is None:
                self._async_remove_entry(entry.entry_id)
                continue

            device_unsub = self._device_unsubs.get(entry.entry_id)
            if device_unsub is not None and device_unsub[0] is not runtime_data:
                try:
                    device_unsub[1]()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "Error replacing ESPHome connection listener",
                        exc_info=True,
                    )
                self._async_invalidate_connection(
                    entry.entry_id, entry.title, unsubscribe=True
                )
                device_unsub = None

            if device_unsub is None:
                unsub = runtime_data.async_subscribe_device_updated(
                    partial(
                        self._handle_device_update,
                        entry.entry_id,
                        generation,
                    )
                )
                self._device_unsubs[entry.entry_id] = (runtime_data, unsub)

            if not getattr(runtime_data, "available", False):
                if (
                    entry.entry_id in self._receiver_cache
                    or entry.entry_id in self._esphome_unsubs
                    or entry.entry_id in self._disconnect_callbacks
                ):
                    self._async_invalidate_connection(entry.entry_id, entry.title)
                continue

            cached = self._receiver_cache.get(entry.entry_id)
            disconnect_callback = self._disconnect_callbacks.get(entry.entry_id)
            if (
                cached is not None
                and (
                    cached.runtime_data is not runtime_data
                    or cached.client is not getattr(runtime_data, "client", None)
                )
            ) or (
                disconnect_callback is not None
                and disconnect_callback[0] is not runtime_data
            ):
                self._async_invalidate_connection(
                    entry.entry_id, entry.title, unsubscribe=True
                )
                cached = None

            if cached is None or (
                cached.receivers
                and entry.entry_id not in self._esphome_unsubs
                and entry.entry_id not in self._receive_api_missing
            ):
                self._hass.async_create_task(
                    self._async_attach_entry(entry.entry_id, generation)
                )

        tracked_ids = (
            set(self._receiver_cache)
            | set(self._disconnect_callbacks)
            | set(self._esphome_unsubs)
            | set(self._device_unsubs)
        )
        for entry_id in tracked_ids - loaded_ids:
            self._async_remove_entry(entry_id)

    @callback
    def _handle_device_update(self, entry_id: str, generation: int) -> None:
        """Immediately invalidate or reattach after an ESPHome state update."""
        if not self._is_running(generation):
            return
        entry = self._hass.config_entries.async_get_entry(entry_id)
        runtime_data = getattr(entry, "runtime_data", None) if entry else None
        node_name = (
            getattr(getattr(runtime_data, "device_info", None), "name", None)
            or (entry.title if entry else entry_id)
        )
        if runtime_data is None or not getattr(runtime_data, "available", False):
            self._async_invalidate_connection(entry_id, node_name)
            return

        _LOGGER.debug(
            "ESPHome node %s became available; refreshing RF receiver subscription",
            node_name,
        )
        self._async_invalidate_connection(entry_id, node_name, unsubscribe=True)
        self._hass.async_create_task(self._async_attach_entry(entry_id, generation))

    def _is_running(self, generation: int) -> bool:
        """Return whether an operation belongs to the current hub run."""
        return (
            bool(self._subscribers) and generation == self._generation
        )

    def _async_invalidate_connection(
        self, entry_id: str, node_name: str, *, unsubscribe: bool = False
    ) -> None:
        """Discard connection-bound cache, callbacks and receive subscription."""
        self._connection_generations[entry_id] = (
            self._connection_generations.get(entry_id, 0) + 1
        )
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
        callback_data = self._disconnect_callbacks.pop(entry_id, None)
        if callback_data is not None:
            callback_data[0].disconnect_callbacks.discard(callback_data[1])
        self._receiver_cache.pop(entry_id, None)
        self._discovery_failures.pop(entry_id, None)
        self._subscription_failures.pop(entry_id, None)
        self._receive_api_missing.discard(entry_id)

    def _async_remove_entry(self, entry_id: str) -> None:
        """Remove all receiver state and lifecycle listeners for an entry."""
        cached = self._receiver_cache.get(entry_id)
        node_name = cached.node_name if cached is not None else entry_id
        self._async_invalidate_connection(entry_id, node_name, unsubscribe=True)
        if device_unsub := self._device_unsubs.pop(entry_id, None):
            try:
                device_unsub[1]()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error removing ESPHome connection listener", exc_info=True
                )

    async def _async_attach_entry(self, entry_id: str, generation: int) -> None:
        """Discover and subscribe to receivers on one ESPHome connection."""
        if not self._is_running(generation) or entry_id in self._attach_in_progress:
            return
        self._attach_in_progress.add(entry_id)
        connection_generation = self._connection_generations.get(entry_id, 0)
        try:
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                return
            runtime_data = getattr(entry, "runtime_data", None)
            client = getattr(runtime_data, "client", None)
            if (
                runtime_data is None
                or client is None
                or not getattr(runtime_data, "available", False)
            ):
                return
            node_name = (
                getattr(getattr(runtime_data, "device_info", None), "name", None)
                or entry.title
            )
            receivers = await self._async_get_receivers(
                entry, runtime_data, client, node_name
            )
            if receivers is None or not self._is_running(generation) or not receivers:
                return

            cached = self._receiver_cache.get(entry_id)
            if (
                cached is None
                or cached.runtime_data is not runtime_data
                or cached.client is not client
                or cached.receivers is not receivers
            ):
                return

            connection_generation = self._connection_generations.get(entry_id, 0)
            if entry_id in self._esphome_unsubs:
                return
            subscribe = getattr(client, "subscribe_infrared_rf_receive", None)
            if subscribe is None:
                if entry_id not in self._receive_api_missing:
                    self._receive_api_missing.add(entry_id)
                    _LOGGER.warning(
                        "ESPHome node %s exposes RF receivers but has no receive "
                        "subscription API",
                        node_name,
                    )
                return

            try:
                unsub = subscribe(
                    partial(self._handle_esphome_event, receivers, node_name)
                )
            except Exception as err:  # noqa: BLE001 - connection may drop mid-call
                failures = self._subscription_failures.get(entry_id, 0) + 1
                self._subscription_failures[entry_id] = failures
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

            current_entry = self._hass.config_entries.async_get_entry(entry_id)
            if (
                not self._is_running(generation)
                or current_entry is not entry
                or getattr(current_entry, "runtime_data", None) is not runtime_data
                or getattr(runtime_data, "client", None) is not client
                or not getattr(runtime_data, "available", False)
                or connection_generation
                != self._connection_generations.get(entry_id, 0)
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
            failures = self._subscription_failures.pop(entry_id, 0)
            if failures:
                _LOGGER.info(
                    "RF receiver subscription on ESPHome node %s recovered after "
                    "%d failed attempt(s)",
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
            self._attach_in_progress.discard(entry_id)
            if (
                self._subscribers
                and self._is_entry_available(entry_id)
                and (
                    generation != self._generation
                    or connection_generation
                    != self._connection_generations.get(entry_id, 0)
                )
            ):
                self._hass.async_create_task(
                    self._async_attach_entry(entry_id, self._generation)
                )

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
            return
        timings = list(getattr(event, "timings", []) or [])
        self._async_dispatch(
            timings,
            source=f"{node_name}: {meta.name}",
            frequency=meta.frequency,
        )

    @callback
    def _handle_bus_event(self, event: Event) -> None:
        """Handle the esphome.rf_code_received fallback bus event."""
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
        try:
            frequency = int(event.data.get("frequency") or 0) or None
        except (TypeError, ValueError):
            # The frequency is only metadata; keep the capture itself.
            frequency = None
        self._async_dispatch(
            timings,
            source=str(event.data.get("source", "event")),
            frequency=frequency,
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

        self.signals = rf_analysis.cluster_learn_signals(
            self.signals, self.max_signals
        )
        return self.signals


# ---------------------------------------------------------------------------
# Received-code matcher
# ---------------------------------------------------------------------------

MatchCallback = Callable[[str, CapturedSignal, int], None]

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
    variant_index: int
    fingerprint: str | None
    bits: str | None
    callback: MatchCallback
    group: str | None = None


class CodeMatcher:
    """Routes received RF signals to the entities whose code they match.

    Every entity registers each command variant here (and re-registers whenever
    its commands change). A single hub subscription then routes each incoming capture
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
        slots: list[tuple[str, int, str | None, str | None]],
        match_callback: MatchCallback,
        group: str | None = None,
    ) -> CALLBACK_TYPE:
        """Register commands as ``(slot, index, fingerprint, bits)`` tuples.

        ``group`` (the RF device id) ties entities whose codes come from the
        same remote together: per signal, only the closest-matching entities
        of a group fire (see ``_dispatch``).

        Returns a callback that removes exactly these registrations. The hub
        subscription starts on the first registration and stops with the last.
        """
        added = [
            _MatchEntry(
                slot, variant_index, fingerprint, bits, match_callback, group
            )
            for slot, variant_index, fingerprint, bits in slots
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
        """Route a capture to the best-matching command variant per entity.

        Crucially, at most **one** slot per entity is triggered per signal -
        the closest match. Sibling buttons of one remote often differ by a
        single bit, so a received code lands within tolerance of several
        slots of the same entity; dispatching all of them would flip the
        state back and forth (on/off/on/off) once per repeated frame.

        The same applies *across* entities of one device: a remote whose
        buttons are learned as separate entities (e.g. stateless buttons)
        has sibling codes only a bit or two apart, so one press would fire
        every button within tolerance. Within a ``group`` only the entities
        at the group's best distance fire - an exact hit wins outright,
        while several entities sharing an identical code still all fire.
        """
        fingerprint = signal.fingerprint
        bits = signal.analysis.get("bits")

        # callback (entity) -> (best distance, best command variant)
        best: dict[MatchCallback, tuple[int, _MatchEntry]] = {}
        for entry in self._entries:
            distance = self._match_distance(entry, fingerprint, bits)
            if distance is None:
                continue
            current = best.get(entry.callback)
            if current is None or distance < current[0]:
                best[entry.callback] = (distance, entry)

        group_best: dict[object, int] = {}
        for distance, entry in best.values():
            key = entry.group if entry.group is not None else entry.callback
            if distance < group_best.get(key, distance + 1):
                group_best[key] = distance

        for distance, entry in best.values():
            key = entry.group if entry.group is not None else entry.callback
            if distance > group_best[key]:
                continue  # a sibling code of the same device matched better
            try:
                entry.callback(entry.slot, signal, entry.variant_index)
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
        self._variant_rotation = CommandRotation()
        self._rotation_store: Store[dict[str, Any]] | None = None
        self._rotation_group_resolver: (
            Callable[[str], Iterable[str] | None] | None
        ) = None

    async def async_send(
        self,
        command: RFCommandData,
        transmitter: str,
        *,
        frequency: int | None = None,
        repeat: int | None = None,
        log_context: str | None = None,
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
        # the transmitter repeat it a handful of times. ``repeat`` counts
        # *extra* transmissions, so 0 sends exactly once; new devices and
        # codes default to DEFAULT_REPEAT (7).
        effective_repeat = repeat if repeat is not None else command.repeat
        if effective_repeat is None or effective_repeat < 0:
            effective_repeat = DEFAULT_REPEAT
        rf_command = _StoredCommand(
            frequency=frequency or command.frequency,
            timings=timings,
            repeat_count=effective_repeat,
        )
        if log_context:
            _LOGGER.debug(
                "Sending RF command for %s: %d pulses at %d Hz via %s (repeat=%d)",
                log_context,
                len(timings),
                rf_command.frequency,
                transmitter,
                rf_command.repeat_count,
            )
        else:
            _LOGGER.debug(
                "Sending %d pulses at %d Hz via %s (repeat=%d)",
                len(timings),
                rf_command.frequency,
                transmitter,
                rf_command.repeat_count,
            )
        await rf_async_send_command(self._hass, transmitter, rf_command)

    # ------------------------------------------------------------------
    # Command-variant rotation
    # ------------------------------------------------------------------

    async def async_load_rotation(self) -> None:
        """Load the persisted rotation state (call once after construction).

        Rotation state lives in its own small store
        (``.storage/rf_device_manager_rotation``) because it changes on every
        send; rewriting the large code store each time would be wasteful.
        """
        if self._rotation_store is None:
            self._rotation_store = Store(
                self._hass, ROTATION_STORAGE_VERSION, ROTATION_STORAGE_KEY
            )
        data = await self._rotation_store.async_load()
        self._variant_rotation.load((data or {}).get("next"))

    @callback
    def set_rotation_group_resolver(
        self, resolver: Callable[[str], Iterable[str] | None] | None
    ) -> None:
        """Set how to find the sibling codes whose rotation a press resets.

        The resolver gets a code id and returns the ids of all codes of the
        same device when that device has "reset rotation on other button"
        enabled, else ``None``.
        """
        self._rotation_group_resolver = resolver

    @callback
    def _rotation_data(self) -> dict[str, Any]:
        """Return the rotation state to persist."""
        return {"next": self._variant_rotation.to_dict()}

    @callback
    def _async_schedule_rotation_save(self) -> None:
        """Persist rotation state soon (debounced; flushed on HA shutdown)."""
        if self._rotation_store is not None:
            self._rotation_store.async_delay_save(
                self._rotation_data, ROTATION_SAVE_DELAY
            )

    @callback
    def _async_reset_sibling_rotation(self, code_id: str, slot: str) -> bool:
        """Restart the sequences of the device's other commands, if enabled.

        Returns whether any rotation state changed.
        """
        if self._rotation_group_resolver is None:
            return False
        code_ids = self._rotation_group_resolver(code_id)
        if code_ids is None:
            return False
        changed = self._variant_rotation.reset_others(code_ids, code_id, slot)
        if changed:
            _LOGGER.debug(
                "Reset command-variant rotation of the other commands of the "
                "device of code %s (activity on slot %s)",
                code_id,
                slot,
            )
        return changed

    async def async_send_variants(
        self,
        variants: list[RFCommandData],
        transmitter: str,
        *,
        code_id: str,
        slot: str,
        context: str,
        repeat: int | None = None,
    ) -> None:
        """Send the next command variant and advance its persisted rotation.

        A failed send leaves the rotation where it was (the variant did not
        go on air) and does not reset sibling rotations.
        """
        if not variants:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="empty_command"
            )
        count = len(variants)
        # Advance before awaiting transmission so concurrent sends reserve
        # different variants rather than racing on the same current index.
        index = self._variant_rotation.next_index(code_id, slot, count)
        command = variants[index]
        log_context = (
            f"{context}, slot {slot}, variant {index + 1}/{count}, "
            f"fingerprint {command.fingerprint or 'n/a'}"
        )
        try:
            await self.async_send(
                command, transmitter, repeat=repeat, log_context=log_context
            )
        except Exception:
            if self._variant_rotation.rollback(code_id, slot, index, count):
                _LOGGER.debug(
                    "Send failed for %s; the next send retries variant %d/%d",
                    log_context,
                    index + 1,
                    count,
                )
            raise
        finally:
            if count > 1:
                self._async_schedule_rotation_save()
        if self._async_reset_sibling_rotation(code_id, slot):
            self._async_schedule_rotation_save()

    @callback
    def sync_variant(
        self, code_id: str, slot: str, variant_index: int, variant_count: int
    ) -> None:
        """Set the next send to follow a command variant received remotely."""
        self._variant_rotation.sync_after_receive(
            code_id, slot, variant_index, variant_count
        )
        siblings_reset = self._async_reset_sibling_rotation(code_id, slot)
        if variant_count > 1 or siblings_reset:
            self._async_schedule_rotation_save()

    @callback
    def next_variant_index(self, code_id: str, slot: str, count: int) -> int:
        """Return the variant index the next send of a command will use."""
        return self._variant_rotation.peek_index(code_id, slot, count)

    @callback
    def reset_command_rotation(self, code_id: str, slot: str) -> None:
        """Reset one command's next variant to the first entry."""
        self._variant_rotation.reset_command(code_id, slot)
        self._async_schedule_rotation_save()

    @callback
    def reset_code_rotation(self, code_id: str) -> None:
        """Remove all rotation state for a deleted RF code."""
        self._variant_rotation.reset_code(code_id)
        self._async_schedule_rotation_save()

    async def async_send_raw(
        self,
        transmitter: str,
        timings: list[int],
        frequency: int,
        repeat: int = DEFAULT_REPEAT,
    ) -> None:
        """Send raw timings (service helper)."""
        command = RFCommandData(data=timings, frequency=frequency, repeat=repeat)
        await self.async_send(command, transmitter)

    def new_learn_session(
        self, timeout: float = DEFAULT_LEARN_TIMEOUT
    ) -> LearnSession:
        """Create a fresh learning session."""
        return LearnSession(hub=self.hub, timeout=timeout)
