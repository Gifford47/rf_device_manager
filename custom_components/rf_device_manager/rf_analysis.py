"""RF signal analysis and encoding helpers.

Pure-Python utilities (no Home Assistant imports) that operate on raw OOK
timing trains: signed, alternating microseconds where positive values are
marks (carrier on) and negative values are spaces (carrier off).

Responsibilities of this module:

* normalizing captures (sign, glitch removal, idle trimming)
* splitting captures into repeated frames and picking the dominant one
* jitter-tolerant fingerprints and signal similarity/clustering primitives
* human-readable signal summaries for the UI
* encoding binary / tri-state codes back into raw timings
* parsing Pronto hex and free-form manual timing input

Protocol *decoding* lives in :mod:`.decoders` - one documented detector per
encoding scheme, walked as a chain. See that module's docstring for how to
add support for further encodings.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any, Protocol, TypeVar

from .const import (
    IDLE_TRIM_US,
    MIN_SIGNAL_PULSES,
    PROTOCOL_B0,
    PROTOCOL_BINARY,
    PROTOCOL_PRONTO,
    PROTOCOL_RAW,
    PROTOCOL_TRISTATE,
)
from .decoders import (  # noqa: F401  (re-exported for flows/services)
    RC_PROTOCOLS,
    TRISTATE_TO_BITS,
    decode_frame,
    encoding_label,
)

# Frame splitting uses an adaptive threshold with an absolute floor:
# max(_FRAME_GAP_MIN_US, _FRAME_GAP_FACTOR * P95(all pulse durations)).
# The 2,000 µs floor admits RC-Switch P4's nominal 2.28 ms sync LOW;
# the adaptive term protects longer in-frame spaces, e.g. P8's 3.2 ms data LOW.
_FRAME_GAP_MIN_US = 2_000
_FRAME_GAP_FACTOR = 1.7

# Quantization limits
_MAX_QUANT = 127

# Pronto clock: one unit equals ``frequency_divider * 0.241246`` microseconds.
_PRONTO_CLOCK = 0.241246


class RFInputError(ValueError):
    """Raised when user-provided RF data cannot be parsed.

    ``error_key`` maps to a translation key used by the config flow.
    """

    def __init__(self, error_key: str, message: str) -> None:
        """Initialize with a translation key and a technical message."""
        super().__init__(message)
        self.error_key = error_key


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def ensure_signed(timings: list[int]) -> list[int]:
    """Return timings as signed alternating marks/spaces.

    Sources that deliver unsigned durations are converted by assuming the
    train starts with a mark and alternates strictly.
    """
    cleaned = [int(value) for value in timings if int(value) != 0]
    if not cleaned:
        return []
    if any(value < 0 for value in cleaned):
        return cleaned
    return [
        value if index % 2 == 0 else -value for index, value in enumerate(cleaned)
    ]


def trim_idle(timings: list[int], threshold: int = IDLE_TRIM_US) -> list[int]:
    """Drop leading/trailing idle gaps longer than ``threshold`` microseconds.

    Long idle periods around a capture are not part of the code and desync
    some receivers when re-transmitted.
    """
    trimmed = list(timings)
    while trimmed and abs(trimmed[0]) > threshold:
        trimmed.pop(0)
    while trimmed and abs(trimmed[-1]) > threshold:
        trimmed.pop()
    return trimmed


def coalesce(timings: list[int]) -> list[int]:
    """Merge adjacent pulses of the same polarity into one duration."""
    out: list[int] = []
    for value in timings:
        if out and (out[-1] > 0) == (value > 0):
            out[-1] += value
        else:
            out.append(value)
    return out


def remove_glitches(timings: list[int], min_us: int = 80) -> list[int]:
    """Absorb sub-``min_us`` spikes into their neighbouring pulses.

    A too-short pulse of opposite polarity splits what should be one long
    pulse (e.g. ``+500, -30, +480`` → ``+1010``); merging these makes
    captures - and therefore fingerprints - far more stable.
    """
    values = [value for value in timings if value != 0]
    out: list[int] = []
    index = 0
    while index < len(values):
        value = values[index]
        if abs(value) >= min_us or not out or index == len(values) - 1:
            out.append(value)
            index += 1
            continue
        nxt = values[index + 1]
        sign = 1 if out[-1] > 0 else -1
        if (out[-1] > 0) == (nxt > 0):
            out[-1] += sign * (abs(value) + abs(nxt))
            index += 2
        else:
            out[-1] += sign * abs(value)
            index += 1
    return coalesce(out)


def normalize(timings: list[int]) -> list[int]:
    """Fully normalize a raw capture (signed, de-glitched, idle-trimmed)."""
    return trim_idle(remove_glitches(ensure_signed(timings)))


# ---------------------------------------------------------------------------
# Similarity / clustering primitives
# ---------------------------------------------------------------------------


def frames_similar(
    frame_a: list[int],
    frame_b: list[int],
    tolerance: float = 0.30,
    floor_us: int = 150,
) -> bool:
    """Return whether two frames are the same code within timing jitter.

    Frames must have the same edge count and matching polarity; each pulse
    may deviate by ``tolerance`` (relative) or ``floor_us`` (absolute),
    whichever is larger. The trailing sync gap is allowed twice the
    tolerance because receivers truncate it inconsistently.
    """
    if not frame_a or not frame_b or len(frame_a) != len(frame_b):
        return False
    last = len(frame_a) - 1
    for index, (a, b) in enumerate(zip(frame_a, frame_b, strict=False)):
        if (a > 0) != (b > 0):
            return False
        abs_a, abs_b = abs(a), abs(b)
        allowed = max(floor_us, tolerance * max(abs_a, abs_b))
        if index == last:
            allowed *= 2
        if abs(abs_a - abs_b) > allowed:
            return False
    return True


def same_code(analysis_a: dict[str, Any], analysis_b: dict[str, Any]) -> bool:
    """Return whether two analyzed captures carry the same RF code.

    Decoded bit strings are the strongest signal (stable across timing
    jitter and truncated frames); otherwise the dominant frames are compared
    with timing tolerance.
    """
    bits_a, bits_b = analysis_a.get("bits"), analysis_b.get("bits")
    if bits_a and bits_b:
        return bits_a == bits_b
    return frames_similar(
        analysis_a.get("dominant_frame") or [],
        analysis_b.get("dominant_frame") or [],
    )


class _LearnCapture(Protocol):
    """Minimal capture shape needed to group learned signals."""

    received_at: float
    analysis: dict[str, Any]


_LearnCaptureT = TypeVar("_LearnCaptureT", bound=_LearnCapture)


def cluster_learn_signals(
    signals: list[_LearnCaptureT], max_signals: int
) -> list[_LearnCaptureT]:
    """Group equivalent captures, rank by frequency, and return in first-seen order.

    Repeated frames and timing jitter are clustered so a single physical code
    appears only once. Candidates are ranked by reception count to filter
    noise, but the retained candidates are returned in the order their codes
    first appeared. Some remotes cycle through a fixed sequence of codes, so
    their first-reception order is meaningful and must survive learning.
    """
    clusters: list[list[_LearnCaptureT]] = []
    for signal in signals:
        for cluster in clusters:
            if same_code(cluster[0].analysis, signal.analysis):
                cluster.append(signal)
                break
        else:
            clusters.append([signal])

    ranked: list[tuple[float, _LearnCaptureT]] = []
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
        ranked.append((min(signal.received_at for signal in cluster), representative))

    # Keep the most frequently received candidates (to suppress one-off
    # interference), then restore their chronological first-seen order.
    ranked.sort(
        key=lambda item: (
            item[1].analysis.get("times_seen", 1),
            item[1].analysis.get("frame_length", 0),
        ),
        reverse=True,
    )
    selected = ranked[:max_signals]
    selected.sort(key=lambda item: item[0])
    return [signal for _, signal in selected]


# ---------------------------------------------------------------------------
# Frame handling
# ---------------------------------------------------------------------------


def split_frames(timings: list[int]) -> list[list[int]]:
    """Split a pulse train into frames on long inter-frame gaps.

    The terminating gap (sync space) stays attached to its frame so frames
    remain directly transmittable.
    """
    if not timings:
        return []
    absolutes = sorted(abs(value) for value in timings)
    percentile_95 = absolutes[int(0.95 * (len(absolutes) - 1))]
    gap_threshold = max(_FRAME_GAP_MIN_US, _FRAME_GAP_FACTOR * percentile_95)

    frames: list[list[int]] = []
    current: list[int] = []
    for value in timings:
        current.append(value)
        if value < 0 and abs(value) >= gap_threshold:
            frames.append(current)
            current = []
    if current:
        frames.append(current)
    return frames


def dominant_frame(frames: list[list[int]]) -> list[int]:
    """Return the most representative frame of a capture.

    Frames are grouped by length; from the most common length group the
    median-duration frame is picked, which suppresses truncated first/last
    frames and single-bit glitches.
    """
    if not frames:
        return []
    if len(frames) == 1:
        return frames[0]

    length_counts = Counter(len(frame) for frame in frames)
    best_length = max(length_counts, key=lambda length: (length_counts[length], length))
    candidates = [frame for frame in frames if len(frame) == best_length]
    candidates.sort(key=lambda frame: sum(abs(value) for value in frame))
    return candidates[len(candidates) // 2]


def estimate_base_unit(frame: list[int]) -> int:
    """Estimate the base pulse unit (shortest timing cluster) of a frame."""
    durations = sorted(abs(value) for value in frame)
    if not durations:
        return 0
    # Drop the sync gap for unit estimation.
    if len(durations) > 4:
        durations = durations[:-1]

    clusters: list[list[int]] = [[durations[0]]]
    for duration in durations[1:]:
        cluster = clusters[-1]
        mean = sum(cluster) / len(cluster)
        if duration <= mean * 1.35:
            cluster.append(duration)
        else:
            clusters.append([duration])

    for cluster in clusters:
        if len(cluster) >= 2 or len(clusters) == 1:
            return round(sum(cluster) / len(cluster))
    return round(sum(clusters[0]) / len(clusters[0]))


def quantize_frame(frame: list[int], unit: int) -> list[int]:
    """Quantize frame durations to multiples of the base unit (signed)."""
    if unit <= 0:
        return []
    quantized: list[int] = []
    for value in frame:
        steps = min(_MAX_QUANT, max(1, round(abs(value) / unit)))
        quantized.append(steps if value > 0 else -steps)
    return quantized


def fingerprint(timings: list[int]) -> str | None:
    """Return a jitter-tolerant fingerprint of a capture, or ``None``.

    When the dominant frame decodes to a bit string, the fingerprint hashes
    the bits - fully immune to timing jitter. Otherwise the frame is
    quantized relative to its own base unit as a fallback.
    """
    normalized = normalize(timings)
    if len(normalized) < MIN_SIGNAL_PULSES:
        return None
    frame = dominant_frame(split_frames(normalized))

    if bits := decode_frame(frame).get("bits"):
        digest = hashlib.sha1(bits.encode()).hexdigest()[:12]
        return f"{len(bits)}b:{digest}"

    unit = estimate_base_unit(frame)
    symbols = quantize_frame(frame, unit)
    if not symbols:
        return None
    digest = hashlib.sha1(
        ",".join(str(symbol) for symbol in symbols).encode()
    ).hexdigest()[:12]
    return f"{len(symbols)}:{digest}"


def matches(timings: list[int], known_fingerprint: str | None) -> bool:
    """Return whether a capture matches a stored fingerprint."""
    if not known_fingerprint:
        return False
    return fingerprint(timings) == known_fingerprint


# ---------------------------------------------------------------------------
# Analysis pipeline + UI summaries
# ---------------------------------------------------------------------------


def analyze(timings: list[int]) -> dict[str, Any]:
    """Run the full analysis pipeline over a capture.

    Normalizes, splits into frames, decodes the dominant frame via the
    :mod:`.decoders` chain and returns a JSON-serializable dict used for
    previews, storage metadata and service responses.
    """
    normalized = normalize(timings)
    frames = split_frames(normalized)
    frame = dominant_frame(frames)
    unit = estimate_base_unit(frame)

    analysis: dict[str, Any] = {
        "pulse_count": len(normalized),
        "frame_count": len(frames),
        "frame_length": len(frame),
        "base_unit_us": unit,
        "duration_ms": round(sum(abs(v) for v in normalized) / 1000, 1),
        "fingerprint": fingerprint(normalized),
        "dominant_frame": frame,
    }
    analysis.update(decode_frame(frame))
    return analysis


def describe_signal(analysis: dict[str, Any], language: str = "en") -> str:
    """Return a compact one-line summary (signal selection list entries).

    Leads with the detected protocol so different remotes are easy to tell
    apart at a glance.
    """
    german = language.lower().startswith("de")
    parts: list[str] = []

    if encoding := analysis.get("encoding"):
        parts.append(encoding_label(encoding, language))
    if protocol := analysis.get("protocol_guess"):
        parts.append(f"rc-switch P{protocol}")
    if hex_code := analysis.get("hex"):
        bit_count = analysis.get("bit_count")
        parts.append(f"{hex_code} ({bit_count} Bit)" if german and bit_count
                     else f"{hex_code} ({bit_count} bit)" if bit_count
                     else hex_code)
    if tristate := analysis.get("tristate"):
        parts.append(f"Tri-State {tristate}")
    if not analysis.get("encoding"):
        parts.append("Raw")
        if unit := analysis.get("base_unit_us"):
            parts.append(f"~{unit} µs")

    parts.append(
        f"{analysis.get('pulse_count', 0)} {'Pulse' if german else 'pulses'}"
    )
    if (frames := analysis.get("frame_count", 0)) > 1:
        parts.append(f"{frames} Frames")
    return " · ".join(parts)


def signal_details(analysis: dict[str, Any], language: str = "en") -> str:
    """Return a structured multi-line markdown summary of a signal.

    Used by the learn wizard's confirmation step; ``analysis`` is the dict
    produced by :func:`analyze` (or an equivalent subset for stored
    commands).
    """
    german = language.lower().startswith("de")

    if encoding := analysis.get("encoding"):
        protocol = encoding_label(encoding, language)
    else:
        protocol = "Raw (nicht dekodiert)" if german else "Raw (not decoded)"
    if guess := analysis.get("protocol_guess"):
        protocol += f" · rc-switch P{guess}"
    lines = [f"**{'Protokoll' if german else 'Protocol'}:** {protocol}"]

    if hex_code := analysis.get("hex"):
        code_line = f"`{hex_code}`"
        if bit_count := analysis.get("bit_count"):
            code_line += f" ({bit_count} {'Bit' if german else 'bit'})"
        if tristate := analysis.get("tristate"):
            code_line += f" · Tri-State `{tristate}`"
        lines.append(f"**Code:** {code_line}")

    timing_parts: list[str] = []
    if unit := analysis.get("base_unit_us") or analysis.get("pulse_length"):
        timing_parts.append(
            f"{'Basispuls' if german else 'base pulse'} ~{unit} µs"
        )
    if pulses := analysis.get("pulse_count"):
        timing_parts.append(f"{pulses} {'Pulse' if german else 'pulses'}")
    if frames := analysis.get("frame_count"):
        timing_parts.append(f"{frames} Frames")
    if (seen := analysis.get("times_seen", 1)) > 1:
        timing_parts.append(f"{seen}× {'empfangen' if german else 'received'}")
    if timing_parts:
        lines.append(f"**Timing:** {' · '.join(timing_parts)}")
    return "\n".join(lines)


def preview_timings(timings: list[int], limit: int = 20) -> str:
    """Return the first ``limit`` timings as a display string."""
    shown = " ".join(str(value) for value in timings[:limit])
    return f"{shown} …" if len(timings) > limit else shown


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_binary(
    bits: str,
    pulse_length: int | None = None,
    protocol: int = 1,
    frames: int = 4,
) -> list[int]:
    """Encode a binary string into raw timings using an rc-switch protocol.

    ``frames`` complete frames (bits + sync) are concatenated so a single
    transmission is reliably picked up by typical OOK receivers.
    """
    spec = RC_PROTOCOLS.get(protocol)
    if spec is None:
        raise RFInputError("invalid_protocol", f"Unknown rc protocol {protocol}")
    if not bits or any(bit not in "01" for bit in bits):
        raise RFInputError("invalid_binary", f"Not a binary string: {bits!r}")

    pulse = int(pulse_length or spec["pulse"])
    if pulse <= 0:
        raise RFInputError("invalid_pulse_length", "Pulse length must be positive")

    frame: list[int] = []
    for bit in bits:
        high, low = spec["one"] if bit == "1" else spec["zero"]
        frame.extend((high * pulse, -low * pulse))
    sync_high, sync_low = spec["sync"]
    frame.extend((sync_high * pulse, -sync_low * pulse))
    return frame * max(1, frames)


def tristate_to_bits(code: str) -> str:
    """Convert a PT2262/EV1527 tri-state code (0/1/F) into a bit string."""
    bits: list[str] = []
    for char in code.strip().upper():
        if char not in TRISTATE_TO_BITS:
            raise RFInputError(
                "invalid_tristate", f"Invalid tri-state character: {char!r}"
            )
        bits.append(TRISTATE_TO_BITS[char])
    if not bits:
        raise RFInputError("invalid_tristate", "Empty tri-state code")
    return "".join(bits)


def encode_tristate(
    code: str, pulse_length: int | None = None, protocol: int = 1, frames: int = 4
) -> list[int]:
    """Encode a tri-state code into raw timings."""
    return encode_binary(
        tristate_to_bits(code), pulse_length=pulse_length, protocol=protocol,
        frames=frames,
    )


# ---------------------------------------------------------------------------
# Parsers for manual input
# ---------------------------------------------------------------------------


def parse_timings_text(text: str) -> list[int]:
    """Parse manual raw-timing input (JSON array or separated integers)."""
    stripped = text.strip()
    if not stripped:
        raise RFInputError("invalid_timings", "Empty timing input")

    values: list[int]
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as err:
            raise RFInputError("invalid_timings", f"Invalid JSON: {err}") from err
        if not isinstance(parsed, list):
            raise RFInputError("invalid_timings", "JSON input is not a list")
        raw_items = parsed
    else:
        raw_items = re.split(r"[\s,;]+", stripped)

    try:
        values = [int(float(item)) for item in raw_items if str(item).strip()]
    except (TypeError, ValueError) as err:
        raise RFInputError("invalid_timings", f"Non-numeric timing value: {err}") from err

    values = ensure_signed(values)
    if len(values) < MIN_SIGNAL_PULSES:
        raise RFInputError("too_few_pulses", "Timing train too short")
    if any(abs(value) > 10_000_000 or abs(value) < 1 for value in values):
        raise RFInputError("invalid_timings", "Timing value out of range")
    return values


def parse_tasmota_rfraw(text: str) -> list[int]:
    """Parse a Tasmota/Portisch RfRaw ``B0``/``B1`` bucket code into timings.

    Format (spaces optional, ``0x`` prefix tolerated)::

        AA B0 <len> <nbucket> <repeats> <bucket_0..n> <data> 55   (compressed)
        AA B1 <nbucket> <bucket_0..n> <data> 55                   (sniffed)

    Each bucket is a 16-bit big-endian duration in microseconds. The data
    section is a nibble stream; per nibble the low three bits index a
    bucket and bit ``0x8`` selects the level: set = mark (carrier on),
    clear = space. This is the exact form Tasmota's ``RfRaw`` and the
    Sonoff RF Bridge (Portisch) emit, e.g.::

        AA B0 4C 04 08 0136 0AD2 0528 29EA 3818082828... 55
    """
    compact = re.sub(r"(?i)^0x", "", text.strip().replace(",", " "))
    compact = re.sub(r"\s+", "", compact)
    if len(compact) % 2 != 0:
        raise RFInputError("invalid_b0", "Odd number of hex digits")
    try:
        data = bytes.fromhex(compact)
    except ValueError as err:
        raise RFInputError("invalid_b0", f"Invalid hex: {err}") from err

    if len(data) < 6 or data[0] != 0xAA or data[-1] != 0x55:
        raise RFInputError(
            "invalid_b0", "Expected an AA … 55 framed B0/B1 code"
        )

    command = data[1]
    if command == 0xB0:
        # data[2] = length, data[3] = bucket count, data[4] = repeats
        nbucket = data[3]
        pos = 5
    elif command == 0xB1:
        nbucket = data[2]
        pos = 3
    else:
        raise RFInputError(
            "invalid_b0", f"Unsupported RfRaw command 0x{command:02X}"
        )

    if not 1 <= nbucket <= 8 or pos + nbucket * 2 > len(data) - 1:
        raise RFInputError("invalid_b0", "Invalid bucket count")
    buckets = [
        (data[pos + 2 * i] << 8) | data[pos + 2 * i + 1] for i in range(nbucket)
    ]
    pos += nbucket * 2

    timings: list[int] = []
    for byte in data[pos:-1]:
        for nibble in (byte >> 4, byte & 0x0F):
            index = nibble & 0x07
            if index >= nbucket:
                # A trailing zero nibble is padding; anything else is a fault.
                if nibble == 0 and byte == data[-2]:
                    continue
                raise RFInputError(
                    "invalid_b0", f"Bucket index {index} out of range"
                )
            duration = buckets[index]
            timings.append(duration if nibble & 0x08 else -duration)

    timings = normalize(timings)
    if len(timings) < MIN_SIGNAL_PULSES:
        raise RFInputError("too_few_pulses", "Decoded B0 signal too short")
    return timings


def parse_pronto(text: str) -> tuple[list[int], int]:
    """Parse a Pronto hex code into raw timings.

    Returns ``(timings, carrier_hz)``. Only learned-format codes (preamble
    ``0000``/``0100``) with a valid clock word are supported; the burst pairs
    are converted to microseconds using the encoded clock.
    """
    try:
        words = [int(word, 16) for word in text.split()]
    except ValueError as err:
        raise RFInputError("invalid_pronto", f"Invalid Pronto hex: {err}") from err

    if len(words) < 6:
        raise RFInputError("invalid_pronto", "Pronto code too short")
    preamble, clock, once_pairs, repeat_pairs = words[:4]
    if preamble not in (0x0000, 0x0100):
        raise RFInputError("invalid_pronto", f"Unsupported preamble {preamble:04X}")
    if clock == 0:
        raise RFInputError("invalid_pronto", "Pronto clock word is zero")

    unit_us = clock * _PRONTO_CLOCK
    carrier_hz = round(1_000_000 / unit_us)

    data = words[4:]
    pair_count = once_pairs if once_pairs else repeat_pairs
    needed = pair_count * 2
    if needed == 0 or len(data) < needed:
        raise RFInputError("invalid_pronto", "Pronto burst length mismatch")

    burst = data[:needed]
    timings = [
        round(value * unit_us) if index % 2 == 0 else -round(value * unit_us)
        for index, value in enumerate(burst)
    ]
    timings = [value if value != 0 else (1 if index % 2 == 0 else -1)
               for index, value in enumerate(timings)]
    if len(timings) < MIN_SIGNAL_PULSES:
        raise RFInputError("too_few_pulses", "Pronto burst too short")
    return timings, carrier_hz



def parse_manual_variants(
    input_format: str,
    values: list[str],
    *,
    pulse_length: int | None = None,
    protocol: int = 1,
) -> list[tuple[list[int], str | None]]:
    """Parse an ordered list of manually supplied variants.

    Each input value is one complete command in the selected format. The
    returned list preserves entry order and contains (timings, code_text)
    pairs ready for conversion into RFCommandData objects.
    """
    if not values:
        raise RFInputError(
            "manual_variant_required", "At least one command variant is required"
        )

    if input_format not in {
        PROTOCOL_RAW,
        PROTOCOL_B0,
        PROTOCOL_TRISTATE,
        PROTOCOL_BINARY,
        PROTOCOL_PRONTO,
    }:
        raise RFInputError(
            "invalid_input_format", f"Unsupported input format: {input_format!r}"
        )

    parsed: list[tuple[list[int], str | None]] = []
    for index, raw_value in enumerate(values, start=1):
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise RFInputError(
                "manual_variant_required", f"Variant {index} is empty"
            )

        code_text: str | None = None
        if input_format == PROTOCOL_RAW:
            timings = parse_timings_text(raw_value)
        elif input_format == PROTOCOL_B0:
            code_text = raw_value.strip()
            timings = parse_tasmota_rfraw(code_text)
        elif input_format == PROTOCOL_TRISTATE:
            code_text = raw_value.strip().upper()
            timings = encode_tristate(
                code_text, pulse_length=pulse_length, protocol=protocol
            )
        elif input_format == PROTOCOL_BINARY:
            code_text = raw_value.strip()
            timings = encode_binary(
                code_text, pulse_length=pulse_length, protocol=protocol
            )
        else:  # PROTOCOL_PRONTO
            code_text = raw_value.strip()
            timings, _carrier = parse_pronto(code_text)
        parsed.append((timings, code_text))

    return parsed
