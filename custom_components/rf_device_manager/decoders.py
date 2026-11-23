"""Frame decoders: turn a raw OOK pulse frame into bits + protocol info.

This module is the single place where RF encodings are recognized. It is
deliberately self-contained (stdlib only) so it can be unit-tested outside
Home Assistant.

Architecture
============

A *frame* is one repetition of a code: signed, alternating microsecond
durations (positive = mark/carrier on, negative = space/carrier off),
usually terminated by a long sync gap.

``decode_frame(frame)`` builds a :class:`FrameContext` (pairs, marks,
spaces - computed once) and walks the :data:`DECODERS` chain in order. The
first decoder that recognizes the frame's *structure* wins. Decoders match
structural patterns, not a database of named protocols - that is why
remotes with unknown protocols still decode, as long as they use one of
the common OOK encoding schemes.

Adding a new decoder
====================

1. Write a function ``_decode_<name>(context: FrameContext) ->
   DecodedFrame | None``. Return ``None`` whenever the frame does not
   *cleanly* match your pattern - a wrong ``None`` is harmless (the raw
   fallback still works), a wrong decode pollutes fingerprints.
2. Document the waveform in the docstring (see existing decoders).
3. Register it in :data:`DECODERS`. Order matters: put the most
   restrictive pattern first (see the comment at the registry).
4. Add an :class:`EncodingInfo` entry so the UI can label it.
5. Add a test case in the repository's decoder tests.

The decoded ``bits`` feed the jitter-immune fingerprints used for signal
clustering while learning and for receive-side matching (sensors), so a
decoder must produce *identical* bits for jittered captures of the same
button.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# rc-switch compatible protocol timing table (used by the PWM decoder for
# the protocol guess and by rf_analysis for manual binary/tri-state entry).
# Each waveform is (high units, low units) in multiples of the base pulse;
# a frame is all data bits followed by the sync waveform. Protocol 6
# (HT6P20B) is inverted and intentionally not supported.
# ---------------------------------------------------------------------------

RC_PROTOCOLS: dict[int, dict[str, Any]] = {
    1: {"pulse": 350, "sync": (1, 31), "zero": (1, 3), "one": (3, 1)},
    2: {"pulse": 650, "sync": (1, 10), "zero": (1, 2), "one": (2, 1)},
    3: {"pulse": 100, "sync": (30, 71), "zero": (4, 11), "one": (9, 6)},
    4: {"pulse": 380, "sync": (1, 6), "zero": (1, 3), "one": (3, 1)},
    5: {"pulse": 500, "sync": (6, 14), "zero": (1, 2), "one": (2, 1)},
    7: {"pulse": 150, "sync": (2, 62), "zero": (1, 6), "one": (6, 1)},
    8: {"pulse": 200, "sync": (3, 130), "zero": (7, 16), "one": (3, 16)},
}

# PT2262/EV1527 tri-state mapping (each tri-state symbol is two data bits).
TRISTATE_TO_BITS = {"0": "00", "1": "11", "F": "01"}
BITS_TO_TRISTATE = {"00": "0", "11": "1", "01": "F"}


# ---------------------------------------------------------------------------
# Encoding metadata (UI labels)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EncodingInfo:
    """Display metadata of one supported encoding."""

    key: str
    name_en: str
    name_de: str

    def label(self, language: str = "en") -> str:
        """Return the localized display name."""
        return self.name_de if language.lower().startswith("de") else self.name_en


ENCODINGS: dict[str, EncodingInfo] = {
    "space_distance": EncodingInfo(
        key="space_distance",
        name_en="Space distance (EV1527/PT2262-style)",
        name_de="Space-Distance (EV1527/PT2262-artig)",
    ),
    "mark_width": EncodingInfo(
        key="mark_width",
        name_en="Pulse width (mark-modulated)",
        name_de="Pulsbreite (Mark-moduliert)",
    ),
    "pwm": EncodingInfo(
        key="pwm",
        name_en="PWM (rc-switch)",
        name_de="PWM (rc-switch)",
    ),
    "manchester": EncodingInfo(
        key="manchester",
        name_en="Manchester",
        name_de="Manchester",
    ),
}


def encoding_label(key: str | None, language: str = "en") -> str:
    """Return the display name of an encoding key (falls back to the key)."""
    if key is None:
        return "Raw"
    info = ENCODINGS.get(key)
    return info.label(language) if info else key


# ---------------------------------------------------------------------------
# Decoder input/output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """Result of a successful frame decode."""

    encoding: str
    bits: str
    pulse_length: int
    protocol_guess: int | None = None


@dataclass(frozen=True, slots=True)
class FrameContext:
    """Pre-computed views of one frame, shared by all decoders.

    ``bit_pairs`` are the (mark, space) pairs carrying data; the final pair
    (``sync_pair``) carries the inter-frame sync gap and is excluded from
    bit decoding.
    """

    frame: list[int]
    bit_pairs: list[tuple[int, int]]
    sync_pair: tuple[int, int]
    marks: list[int]
    spaces: list[int]

    @classmethod
    def from_frame(cls, frame: list[int]) -> FrameContext | None:
        """Build a context; ``None`` when the frame is too short."""
        values = frame[1:] if frame and frame[0] < 0 else list(frame)
        pairs: list[tuple[int, int]] = []
        for index in range(0, len(values) - 1, 2):
            mark, space = values[index], values[index + 1]
            if mark > 0 and space < 0:
                pairs.append((mark, -space))
        if len(pairs) < 5:
            return None
        *bit_pairs, sync_pair = pairs
        if len(bit_pairs) < 4:
            return None
        return cls(
            frame=list(frame),
            bit_pairs=bit_pairs,
            sync_pair=sync_pair,
            marks=[mark for mark, _ in bit_pairs],
            spaces=[space for _, space in bit_pairs],
        )


# ---------------------------------------------------------------------------
# Shared timing helpers
# ---------------------------------------------------------------------------


def _is_roughly_constant(values: list[int], max_ratio: float = 1.5) -> bool:
    """Return whether all durations belong to a single timing cluster."""
    return bool(values) and max(values) / max(min(values), 1) <= max_ratio


def _split_bimodal(
    values: list[int],
) -> tuple[float, float, list[bool]] | None:
    """Split durations into a clean short/long population.

    Returns ``(short_mean, long_mean, is_long_flags)`` or ``None`` when the
    values are not clearly bimodal (two tight clusters, ratio >= 1.8).
    """
    if not values:
        return None
    threshold = min(values) * 1.6
    shorts = [value for value in values if value <= threshold]
    longs = [value for value in values if value > threshold]
    if not shorts or not longs:
        return None
    short_mean = sum(shorts) / len(shorts)
    long_mean = sum(longs) / len(longs)
    if long_mean / short_mean < 1.8:
        return None
    if max(shorts) > short_mean * 1.5 or max(longs) > long_mean * 1.5:
        return None  # smeared clusters - not a clean binary timing code
    return short_mean, long_mean, [value > threshold for value in values]


def _guess_rc_protocol(short: float, long: float, sync_low: float) -> int | None:
    """Match measured PWM ratios against the rc-switch protocol table."""
    if short <= 0:
        return None
    long_ratio = long / short
    sync_ratio = sync_low / short
    best: tuple[float, int] | None = None
    for protocol_id, spec in RC_PROTOCOLS.items():
        zero_high, zero_low = spec["zero"]
        expected_long = max(zero_low, spec["one"][0]) / min(zero_high, spec["one"][1])
        expected_sync = spec["sync"][1] / min(zero_high, spec["one"][1])
        error = abs(long_ratio - expected_long) / expected_long + abs(
            sync_ratio - expected_sync
        ) / max(expected_sync, 1)
        if error < 0.35 and (best is None or error < best[0]):
            best = (error, protocol_id)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Decoders (one function per encoding scheme)
# ---------------------------------------------------------------------------


def _decode_space_distance(context: FrameContext) -> DecodedFrame | None:
    """Space-distance coding: constant mark, data in the space length.

    Used by EV1527, PT2262 receivers in "distance" mode, many cheap 433 MHz
    remotes and NEC-style protocols::

        bit 0:  ▔▔▁▁          mark, short space
        bit 1:  ▔▔▁▁▁▁▁▁▁▁    mark, long space

    Matches when all marks sit in one timing cluster and the spaces form a
    clean short/long population.
    """
    if not _is_roughly_constant(context.marks):
        return None
    split = _split_bimodal(context.spaces)
    if split is None:
        return None
    _short, _long, is_long = split
    return DecodedFrame(
        encoding="space_distance",
        bits="".join("1" if flag else "0" for flag in is_long),
        pulse_length=round(sum(context.marks) / len(context.marks)),
    )


def _guess_mark_width_protocol(
    context: FrameContext, short_mean: float, long_mean: float
) -> int | None:
    """Identify a mark-width rc-switch profile from its timing ratios.

    Protocol 8 has constant 16T spaces, with 3T marks for bit 1 and 7T
    marks for bit 0. Match against the protocol table rather than assuming
    that a long mark always represents a one. Deriving T from the spaces
    also keeps the match valid when a custom pulse length is used.
    """
    space_mean = sum(context.spaces) / len(context.spaces)
    for protocol_id, spec in RC_PROTOCOLS.items():
        zero_high, zero_low = spec["zero"]
        one_high, one_low = spec["one"]
        if zero_low != one_low or zero_high == one_high:
            continue

        pulse_length = space_mean / zero_low
        expected_short = min(zero_high, one_high) * pulse_length
        expected_long = max(zero_high, one_high) * pulse_length
        if (
            abs(short_mean - expected_short) <= expected_short * 0.30
            and abs(long_mean - expected_long) <= expected_long * 0.30
        ):
            return protocol_id
    return None


def _decode_mark_width(context: FrameContext) -> DecodedFrame | None:
    """Pulse-width coding: constant space, data in the mark length.

    The mirror image of space-distance coding (e.g. Sony SIRC-style)::

        bit 0:  ▔▔▁▁          short mark, space
        bit 1:  ▔▔▔▔▔▔▁▁      long mark, space

    RC-switch Protocol 8 also uses mark-width coding, but its protocol
    definition maps the longer mark to zero. Recognize that profile from
    its timing ratios and use the protocol table's bit mapping.
    """
    if not _is_roughly_constant(context.spaces):
        return None
    split = _split_bimodal(context.marks)
    if split is None:
        return None
    short_mean, long_mean, is_long = split
    protocol_guess = _guess_mark_width_protocol(context, short_mean, long_mean)

    if protocol_guess is None:
        bits = "".join("1" if flag else "0" for flag in is_long)
    else:
        zero_high = RC_PROTOCOLS[protocol_guess]["zero"][0]
        one_high = RC_PROTOCOLS[protocol_guess]["one"][0]
        long_mark_is_zero = zero_high > one_high
        bits = "".join(
            "0" if flag == long_mark_is_zero else "1" for flag in is_long
        )

    pulse_length = (
        round(
            sum(context.spaces) / len(context.spaces)
            / RC_PROTOCOLS[protocol_guess]["zero"][1]
        )
        if protocol_guess is not None
        else round(short_mean)
    )
    return DecodedFrame(
        encoding="mark_width",
        bits=bits,
        pulse_length=pulse_length,
        protocol_guess=protocol_guess,
    )


def _decode_pwm(context: FrameContext) -> DecodedFrame | None:
    """PWM pair coding (rc-switch style): mark and space share the bit.

    Every bit occupies one mark+space pair; exactly one of the two is long::

        bit 0:  ▔▔▁▁▁▁▁▁      short mark, long space
        bit 1:  ▔▔▔▔▔▔▁▁      long mark, short space

    Matches when all durations split into short/long and every pair
    contains exactly one long half. Also guesses the rc-switch protocol
    number from the timing ratios (useful for re-encoding).
    """
    durations = [duration for pair in context.bit_pairs for duration in pair]
    shorts = [d for d in durations if d <= min(durations) * 1.6]
    longs = [d for d in durations if d > min(durations) * 1.6]
    if not shorts or not longs:
        return None
    short = sum(shorts) / len(shorts)
    long_mean = sum(longs) / len(longs)
    if long_mean / short < 1.4:
        return None
    bits: list[str] = []
    for mark, space in context.bit_pairs:
        mark_is_long = abs(mark - long_mean) < abs(mark - short)
        space_is_long = abs(space - long_mean) < abs(space - short)
        if mark_is_long == space_is_long:
            return None  # not a clean short/long PWM pair
        bits.append("1" if mark_is_long else "0")
    return DecodedFrame(
        encoding="pwm",
        bits="".join(bits),
        pulse_length=round(short),
        protocol_guess=_guess_rc_protocol(short, long_mean, context.sync_pair[1]),
    )


def _decode_manchester(context: FrameContext) -> DecodedFrame | None:
    """Manchester (transition) coding: every bit is a level transition.

    Used by Oregon Scientific sensors, some fan remotes and rolling-code
    preambles. Each bit period consists of two half-periods with opposite
    levels (G.E. Thomas convention: mark→space = ``1``)::

        bit 1:  ▔▔▁▁    mark half, space half
        bit 0:  ▁▁▔▔    space half, mark half

    Consecutive equal halves merge into a single duration of twice the
    half-period, so a valid frame contains only 1x and 2x durations. The
    half-bit stream is paired into bits at the phase where every pair is a
    transition; a trailing space half swallowed by the inter-frame gap is
    reconstructed as the complement of the last half.
    """
    # Strip the trailing inter-frame gap before period estimation.
    frame = context.frame
    core = frame[:-1] if frame and frame[-1] < 0 else list(frame)
    if len(core) < 15:
        return None
    absolutes = [abs(value) for value in core]
    shortest = min(absolutes)
    short_population = [value for value in absolutes if value <= shortest * 1.5]
    half_period = sum(short_population) / len(short_population)

    halves: list[bool] = []
    for value in core:
        ratio = abs(value) / half_period
        if 0.6 <= ratio <= 1.4:
            count = 1
        elif 1.6 <= ratio <= 2.5:
            count = 2
        else:
            return None  # durations outside 1x/2x half-period: not Manchester
        halves.extend([value > 0] * count)
    if len(halves) < 16:
        return None

    for phase in (0, 1):
        sequence = halves[phase:]
        if len(sequence) % 2 == 1:
            # A trailing space half merges into the inter-frame gap; since
            # every bit is a transition, the swallowed half is its complement.
            sequence = [*sequence, not sequence[-1]]
        pairs = [
            (sequence[i], sequence[i + 1]) for i in range(0, len(sequence), 2)
        ]
        if pairs and all(first != second for first, second in pairs):
            return DecodedFrame(
                encoding="manchester",
                bits="".join("1" if first else "0" for first, _ in pairs),
                pulse_length=round(half_period),
            )
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order matters - most restrictive structure first:
# * space_distance / mark_width require one constant population, so they
#   cannot misfire on PWM or Manchester frames (whose marks AND spaces vary).
# * pwm before manchester: rc-switch protocol 2 frames (1:2 ratio) are also
#   valid Manchester streams; classifying them as PWM keeps the protocol
#   guess available for re-encoding.
# * manchester last, as the most permissive pattern.
DECODERS: tuple[tuple[str, Callable[[FrameContext], DecodedFrame | None]], ...] = (
    ("space_distance", _decode_space_distance),
    ("mark_width", _decode_mark_width),
    ("pwm", _decode_pwm),
    ("manchester", _decode_manchester),
)


def decode_frame(frame: list[int]) -> dict[str, Any]:
    """Decode one frame by walking the decoder chain.

    Returns a dict with ``encoding``, ``bits``, ``bit_count``, ``hex``,
    ``pulse_length`` and optionally ``tristate`` / ``protocol_guess`` -
    or an empty dict when no decoder matches (the raw timing fallback in
    ``rf_analysis`` then still handles the signal).
    """
    context = FrameContext.from_frame(frame)
    if context is None:
        return {}

    for _key, decoder in DECODERS:
        decoded = decoder(context)
        if decoded is None:
            continue
        result: dict[str, Any] = {
            "encoding": decoded.encoding,
            "bits": decoded.bits,
            "bit_count": len(decoded.bits),
            "hex": f"0x{int(decoded.bits, 2):X}",
            "pulse_length": decoded.pulse_length,
        }
        if decoded.protocol_guess is not None:
            result["protocol_guess"] = decoded.protocol_guess
        if len(decoded.bits) % 2 == 0:
            groups = [
                decoded.bits[i : i + 2] for i in range(0, len(decoded.bits), 2)
            ]
            if all(group in BITS_TO_TRISTATE for group in groups):
                result["tristate"] = "".join(BITS_TO_TRISTATE[g] for g in groups)
        return result
    return {}
