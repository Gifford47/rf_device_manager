# RF Device Manager — Gesamtarchitektur

Custom Integration `rf_device_manager` für Home Assistant ≥ 2026.5.
Vollständige RF-Geräteverwaltung (erstellen, anlernen, empfangen, bearbeiten, senden)
ausschließlich über die Web-UI — keine YAML- oder Python-Konfiguration.

## 1. Technische Basis (verifiziert gegen HA-Core, Stand 2026-07)

### Senden — offizielle `radio_frequency`-Plattform (neu in HA 2026.5)

```
homeassistant.components.radio_frequency
├─ RadioFrequencyTransmitterEntity        # Basisklasse für Sender (z. B. ESPHome ir_rf_proxy)
├─ async_get_transmitters(hass, frequency, modulation) -> list[entity_id]
└─ async_send_command(hass, entity_id_or_uuid, command)   # validiert Frequenz + Modulation
```

Commands sind `rf_protocols.RadioFrequencyCommand`-Instanzen mit den Attributen
`frequency` (Hz), `modulation` (`ModulationType.OOK`), `repeat_count` (zusätzliche
Wiederholungen), `symbol_rate`, `output_power` und der Methode
`get_raw_timings() -> list[int]` (signierte, alternierende Mikrosekunden:
positiv = Mark, negativ = Space). Wie die Referenz-Integration `rf_fan` subclassen
wir defensiv und überspringen `super().__init__`, um gegen Konstruktor-Änderungen
zwischen `rf-protocols`-Releases (2.x → 4.x) immun zu sein.

ESPHome-Sender entstehen aus dem ESPHome-`ir_rf_proxy` (`radio_frequency:`-Plattform
mit `remote_transmitter_id`, CC1101/SX127x-Hardware) und erscheinen in HA als
`radio_frequency.*`-Entities (Transmitter-Capability).

### Empfangen / Anlernen — ESPHome native API

HA-Core besitzt (Stand 2026.7) **keine** RF-Receiver-Entities — nur IR-Receiver.
Der Empfangspfad läuft über die ESPHome-Integration:

```
esphome ConfigEntry (state LOADED)
└─ entry.runtime_data: RuntimeEntryData
   ├─ client: aioesphomeapi.APIClient
   │   └─ subscribe_infrared_rf_receive(cb) -> unsub     # geteilt für IR + RF
   │        cb(InfraredRFReceiveEvent(device_id, key, timings))
   ├─ available: bool
   └─ info: dict[type[EntityInfo], dict[key, EntityInfo]]
        └─ RadioFrequencyInfo(capabilities, frequency_min, frequency_max, ...)
             RadioFrequencyCapability.TRANSMITTER | RECEIVER
```

Der `ReceiverHub` dieser Integration abonniert genau wie Core-`esphome/infrared.py`
den Client-Stream und filtert Events auf Keys von `RadioFrequencyInfo`-Einträgen mit
`RECEIVER`-Capability. Alle aioesphomeapi-Importe sind defensiv (ESPHome ist nur
`after_dependency`). Zusätzlicher Fallback: HA-Bus-Event `esphome.rf_code_received`
(`data.timings`), das Nutzer per ESPHome-YAML (`remote_receiver.on_raw` →
`homeassistant.event`) feuern können.

## 2. Datenmodell (Storage API, `.storage/rf_device_manager`)

Ein **Config Entry pro RF-Gerät** (natives Umbenennen/Deaktivieren/Löschen über die
HA-UI), Geräte-Identität in `entry.data`, veränderliche Einstellungen in
`entry.options`. RF-Codes (potenziell große Timing-Arrays) liegen im zentralen
`Store` (helpers.storage), synchronisiert über `async_remove_entry`.

```jsonc
// Gerät (entry.data + entry.options, gespiegelt im Store)
{ "id": "uuid", "name": "Garagentor", "transmitter": "radio_frequency.esp_garage_rf",
  "settings": { "frequency": 433920000, "default_repeat": 7, "description": "..." } }

// Code (Store) — 1 Code == 1 Entity, mit 1..n Befehls-Slots
{ "id": "uuid", "device_id": "uuid", "name": "Tor", "entity_type": "cover",
  "rf_data": { "commands": {
      "open":  { "protocol": "raw", "frequency": 433920000, "pulse_length": 350,
                 "data": [350, -1050, ...], "repeat": 7, "code": null,
                 "fingerprint": "24:ab12cd34ef56", "source": "learned:esphome" },
      "close": { ... }, "stop": { ... } } },
  "settings": { "device_class": "garage", "icon": null, "enabled": true } }
```

Alle Eingabeformate (Raw-Timings, Tri-State, Binär+Protokoll, Pronto) werden beim
Speichern nach Raw-Timings normalisiert (`data`); das Ursprungsformat bleibt zur
Anzeige/Bearbeitung erhalten (`protocol`, `code`, `pulse_length`).

## 3. Module

```
custom_components/rf_device_manager/
├─ manifest.json          # dependencies=[radio_frequency], after_dependencies=[esphome]
├─ const.py               # Domain, Entity-Typen, Frequenz-Presets, Defaults, Events
├─ models.py              # Dataclasses: RFCommandData, RFCode, RFDeviceConfig,
│                         #   CapturedSignal, SlotSpec + build_slots()
├─ decoders.py            # Protokoll-Erkennung: Dekoder-Kette (DECODERS-Registry) mit
│                         #   je einem dokumentierten Detektor pro Kodierungs-Muster
│                         #   (space_distance, mark_width, pwm/rc-switch, manchester),
│                         #   EncodingInfo-Labels fürs UI, RC_PROTOCOLS-Tabelle.
│                         #   Rein stdlib → standalone testbar (tests/test_decoders.py)
├─ rf_analysis.py         # Normalisierung (Glitch-Filter), Frame-Splitting, Basiseinheit,
│                         #   Fingerprints (bit-basiert, jitter-immun), Ähnlichkeit/
│                         #   Clustering-Primitiven, UI-Zusammenfassungen, Encoder
│                         #   (tristate/binär/rc-switch), Pronto-/Manual-Input-Parser
├─ storage.py             # RFDeviceStore (Store v1, delay-save, CRUD, Migration)
├─ rf_manager.py          # RFManager (Senden via radio_frequency-Helper, Transmitter-
│                         #   Discovery), ReceiverHub (ESPHome-Subscribe + Bus-Fallback,
│                         #   Re-Subscribe-Wartung), CodeMatcher (fingerprint→Entity-Index
│                         #   für Empfang externer Codes), LearnSession (Cluster+Ranking)
├─ device_manager.py      # CRUD Geräte/Codes, Registry-Sync, Duplizieren, Events
├─ coordinator.py         # RFDeviceCoordinator (DataUpdateCoordinator, push-basiert)
├─ entity_factory.py      # RFCodeEntity-Basis (CoordinatorEntity + RestoreEntity),
│                         #   Sende-Helfer, Empfangs-Mixin (Fingerprint-Match),
│                         #   dynamisches Add/Remove, EntityDescriptions je Typ
├─ switch.py light.py fan.py cover.py button.py
│  select.py number.py sensor.py binary_sensor.py     # 9 Plattformen
├─ config_flow.py         # Menü: Gerät erstellen / duplizieren; Reconfigure;
│                         #   OptionsFlow: Einstellungen, Code hinzufügen (Anlernen mit
│                         #   async_show_progress, Signal-Auswahl, Test, Manuell),
│                         #   Codes verwalten (bearbeiten/neu anlernen/testen/löschen)
├─ services.py services.yaml   # send_code, send_raw, learn_code (mit Response-Daten)
├─ repairs.py             # Fixable Issue: fehlender Transmitter → Ersatz wählen
├─ diagnostics.py         # Entry-Diagnose (gekürzte Timings)
├─ icons.json
├─ strings.json + translations/{en,de}.json
└─ __init__.py            # Setup, Runtime-Data, Plattform-Forwarding, Migration
```

## 4. Datenfluss

**Senden:** Entity/Service → `RFManager.async_send()` → normalisierte Timings →
`_StoredCommand(RadioFrequencyCommand)` → `radio_frequency.async_send_command()` →
ESPHome-Transmitter. Verfügbarkeit der Entities folgt dem Transmitter-State
(`async_track_state_change_event`).

**Anlernen (Options Flow):** Schrittanzeige je Slot („Schritt 2/4 – Code für
‚Schließen‘“) → `async_show_progress` mit LearnSession-Task („Warte auf RF-Signal…“)
→ Hub liefert Signale → Dedupe per Fingerprint → Auswahlliste mit Analyse-Vorschau
(Pulse, Basiseinheit, Bits, Hex, Protokollvermutung) → Menü Übernehmen/Testen/
Wiederholen → nächster Slot → Speichern im Store → Entities erscheinen live
(Coordinator-Diff, kein Reload).

**Empfangen (alle Entities):** Jede Entity registriert die Fingerprints ihrer
Befehls-Slots beim zentralen `CodeMatcher` (fingerprint→Entity, O(1)-Lookup). Eine
einzige Hub-Subscription routet empfangene Captures an die passende Entity →
`_on_rf_match(slot, signal)` passt den Zustand an (Schalter kippt, Cover öffnet,
Ventilatorstufe ändert sich …) + Bus-Event `rf_device_manager_code_received`. So
werden auch externe Auslösungen (Original-Fernbedienung) in HA sichtbar;
sensor/binary_sensor nutzen denselben Pfad als primäre Zustandsquelle (Binary
Sensor optional mit Auto-Off `off_delay`).

**Reaktionszeit:** Sende-Aktionen schreiben den optimistischen Zustand *vor* dem
Await der Übertragung → sofortiges UI-Feedback.

## 5. Erweiterbarkeit

- **IR:** `rf_manager` kapselt Transport; ein `InfraredTransport` kann die Core-
  `infrared`-Helper (`async_send_command`, `async_subscribe_receiver`) nutzen.
- **MQTT/weitere Quellen:** `ReceiverHub`-Quellen und Sende-Transports sind Listen —
  neue Quellen registrieren sich mit `(name, subscribe_fn)`.
- **Protokolle:** Die Dekoder-Kette in `decoders.py` (`DECODERS`-Registry) ist rein
  additiv erweiterbar — ein neuer Detektor ist eine Funktion `FrameContext →
  DecodedFrame | None` plus Registry-Eintrag, Label und Testfall (Anleitung im
  Modul-Docstring). `RC_PROTOCOLS` (rc-switch) und das `protocol`-Feld im Storage
  sind offen.
- **Gruppen/Szenen/Cloud/Backup:** Store-Schema versioniert (`STORAGE_VERSION`,
  `async_migrate`), Diagnostics liefert Export; Codes sind reine Daten.
