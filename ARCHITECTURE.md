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
// Store-Wurzel (Schema 1.3): { "devices": {...}, "codes": {...}, "decoder_revision": 2 }

// Gerät (entry.data + entry.options, gespiegelt im Store)
{ "id": "uuid", "name": "Garagentor", "transmitter": "radio_frequency.esp_garage_rf",
  "settings": { "frequency": 433920000, "default_repeat": 7, "description": "...",
                "rotation_reset_on_other": false } }

// Code (Store) — 1 Code == 1 Entity, mit 1..n Befehls-Slots
{ "id": "uuid", "device_id": "uuid", "name": "Tor", "entity_type": "cover",
  "rf_data": { "commands": {
      "open":  [{ "protocol": "raw", "frequency": 433920000, "pulse_length": 350,
                  "data": [350, -1050, ...], "repeat": 7, "code": null,
                  "fingerprint": "24:ab12cd34ef56", "source": "learned:esphome" }],
      "close": [{ ... }], "stop": [{ ... }] } },
  "settings": { "device_class": "garage", "icon": null, "enabled": true } }
```

Alle Eingabeformate (Raw-Timings, Tasmota B0/B1, Tri-State, Binär+Protokoll,
Pronto) werden beim Speichern nach Raw-Timings normalisiert (`data`); das
Ursprungsformat bleibt zur Anzeige/Bearbeitung erhalten (`protocol`, `code`,
`pulse_length`). Manuell eingegebene Varianten werden als geordnete Liste
gespeichert. Ab Core 2026.11 zeigt ein Format-Dropdown den jeweils passenden
Editor und die benötigten Kodierungseinstellungen auf einer Seite; ältere
unterstützte Core-Versionen verwenden dafür einen kompatiblen zweistufigen Ablauf.
Alle Formate nutzen eine wiederholbare `ObjectSelector`-Liste mit derselben
Überschrift und Hinzufügen-Aktion. Binär-/Tri-State-Werte bleiben einzeilig;
Raw-/B0/B1-/Pronto-Eingaben verwenden mehrzeilige Editoren.

**Schema-Versionen / Migrationen** (`storage.py`, je Schritt genau eine Minor-Version):

| Von → nach | Änderung |
|---|---|
| 1.1 → 1.2 | `repeat: 0` hieß „Standard (7)“, heißt jetzt „einmal senden“ → gespeicherte Nullen werden 7 |
| 1.2 → 1.3 | jeder Befehls-Slot enthält eine **Liste** von Varianten statt eines Objekts |

Config-Entry 1.1 → 1.2 (`async_migrate_entry`): Geräte-`default_repeat` 0 → 7.

**Decoder-Revision:** `rf_analysis.DECODER_REVISION` steht als `decoder_revision`
im Store. Ist der gespeicherte Wert kleiner, dekodiert `RFDeviceStore` beim Laden
jeden gespeicherten Befehl neu aus seinen Roh-Timings (`RFCommandData.redecode`:
Fingerprint + `STORED_DECODE_KEYS`; die Timings selbst bleiben unverändert). So
passen gelernte Codes nach Decoder-Korrekturen (z. B. P4-Frame-Gap, P8-Polarität)
weiterhin zu empfangenen Signalen.

**Rotations-Zustand:** eigener kleiner Store `.storage/rf_device_manager_rotation`
(`{"next": {code_id: {slot: index}}}`), verzögert gespeichert (5 s) und beim
HA-Shutdown geschrieben. Getrennt vom Code-Store, weil er sich bei jedem Senden
ändert.

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
├─ storage.py             # RFDeviceStore (Store 1.3, delay-save, CRUD, Migrationen,
│                         #   Re-Decode bei neuer DECODER_REVISION)
├─ command_rotation.py    # CommandRotation: nächste Variante je (Code, Slot),
│                         #   Reset/Persistenz-Serialisierung (rein stdlib)
├─ rf_manager.py          # RFManager (Senden via radio_frequency-Helper, Transmitter-
│                         #   Discovery), ReceiverHub (ESPHome-Subscribe + Bus-Fallback,
│                         #   Connection-Lifecycle-Callbacks + Reconnect-Scan), CodeMatcher
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

**Senden:** Entities und `send_code` wählen über
`RFManager.async_send_variants()` den nächsten Eintrag je Code-Slot und senden
ihn über `async_send()`; `send_raw` und der Lerntest senden weiterhin direkt.
Die Rotationsposition wird persistiert und übersteht HA-Neustarts. Erkannte
Fernbedienungs-Eingaben setzen den nächsten Eintrag auf den Nachfolger des
empfangenen Codes. Mit der Geräte-Option `rotation_reset_on_other` setzt jedes
Senden/Empfangen eines Befehls die Rotation aller *anderen* Befehle des Geräts
auf die erste Variante zurück (Fernbedienungen, die die Codefolge einer Taste
beim Drücken einer anderen neu starten). Der eigentliche Versand läuft
über `_StoredCommand(RadioFrequencyCommand)` und
`radio_frequency.async_send_command()` zum ESPHome-Transmitter. Die Verfügbarkeit
der Entities folgt dem Transmitter-State (`async_track_state_change_event`).

**Anlernen (Options Flow):** Schrittanzeige je Slot („Schritt 2/4 – Code für
‚Schließen‘“) → `async_show_progress` mit LearnSession-Task („Warte auf RF-Signal…“)
→ Hub liefert Signale → Dedupe per Fingerprint und Code-Clustering → Auswahlliste
mit Analyse-Vorschau (Pulse, Basiseinheit, Bits, Hex, Protokollvermutung) und
Mehrfachauswahl (alle plausiblen Kandidaten vorausgewählt:
`rf_analysis.preselected_signal_indices`) → stärkste Kandidaten werden in ihrer
Erstempfangsreihenfolge gespeichert → Test der ersten ausgewählten Variante → nächster Slot → Store/Entities live
(Coordinator-Diff, kein Reload).

**Empfangen (alle Entities):** Jede Entity registriert die Fingerprints und
Bitfolgen ihrer Befehlsvarianten beim zentralen `CodeMatcher`. Eine einzige
Hub-Subscription vergleicht eingehende Captures mit den registrierten Varianten
und wählt pro Entity den besten Treffer. Der Matcher übergibt auch den Index der
passenden Kommandovariante; damit wird die nächste Sendeposition synchronisiert.
Anschließend passt `_on_rf_match(slot, signal)` den Zustand an (Schalter kippt,
Cover öffnet, Ventilatorstufe ändert sich …) und feuert das Bus-Event
`rf_device_manager_code_received`. So werden auch externe Auslösungen
(Original-Fernbedienung) in HA sichtbar; sensor/binary_sensor nutzen denselben
Pfad als primäre Zustandsquelle (Binary Sensor optional mit Auto-Off `off_delay`).

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
