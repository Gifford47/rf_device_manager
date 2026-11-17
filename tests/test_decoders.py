"""Standalone decoder and analysis tests - no Home Assistant required.

Run from the repository root::

    python tests/test_decoders.py

``decoders.py`` and ``rf_analysis.py`` are deliberately pure Python, so the
whole detection pipeline (normalizing, frame splitting, the decoder chain,
fingerprints and clustering) is testable in isolation. When you add or
change a decoder in ``decoders.py``, add a case here - ideally from a real
ESPHome ``remote_receiver`` log line like the built-in EV1527 capture
below.
"""

from __future__ import annotations

import importlib.util
import pathlib
import random
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "rf_device_manager"


def _load_rf_analysis():
    """Import decoders + rf_analysis without pulling in Home Assistant.

    A stub ``const`` module provides the two pure constants the analysis
    layer needs (the real ``const.py`` imports Home Assistant).
    """
    package = types.ModuleType("rfdm")
    package.__path__ = [str(COMPONENT)]
    sys.modules["rfdm"] = package

    const = types.ModuleType("rfdm.const")
    const.IDLE_TRIM_US = 20_000
    const.MIN_SIGNAL_PULSES = 8
    sys.modules["rfdm.const"] = const

    for name in ("decoders", "rf_analysis"):
        spec = importlib.util.spec_from_file_location(
            f"rfdm.{name}", COMPONENT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"rfdm.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["rfdm.rf_analysis"]


rf_analysis = _load_rf_analysis()
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failed expectation."""
    if not condition:
        failures.append(message)


def parse(text: str) -> list[int]:
    """Parse comma separated timings as logged by ESPHome remote_receiver."""
    return [int(v.strip()) for v in text.replace("\n", " ").split(",") if v.strip()]


# ---------------------------------------------------------------------------
# 1. Real EV1527-style capture (three receiver events of one button press,
#    logged by an ESPHome remote_receiver at 433.92 MHz) + real noise event.
#    Expectation: identical bits/fingerprints, one cluster, noise separate.
# ---------------------------------------------------------------------------

EV1527_A = parse("""
324, -2791, 321, -240, 323, -1333, 328, -1327, 316, -252, 330, -249, 320,
-1335, 317, -1323, 327, -252, 321, -252, 322, -1334, 319, -1325, 319, -262,
318, -252, 319, -1340, 318, -248, 315, -1362, 310, -253, 316, -1339, 311,
-260, 312, -1338, 318, -258, 310, -1345, 309, -263, 311, -1342, 313, -258,
311, -1344, 312, -259, 311, -1341, 318, -255, 312, -1354, 298, -1341, 303,
-285, 309, -1334, 315, -268, 308, -264, 312, -1344, 308, -1337, 306, -273,
311, -1333, 311, -267, 313, -269, 299, -1344, 310, -269, 301, -1349, 310,
-260, 309, -1347, 306, -265, 307, -1361, 309, -1333, 309, -273, 306, -267,
307, -1347, 307, -265, 307, -1349, 304, -1339, 308, -271, 308, -262, 306,
-1350, 307, -267, 304, -1349, 306, -262, 309, -1346, 309, -260, 310, -1338,
307
""")

EV1527_B = parse("""
328, -2778, 329, -241, 332, -1323, 330, -1313, 333, -249, 329, -245, 325,
-1326, 328, -1316, 332, -249, 329, -246, 326, -1325, 327, -1316, 331, -254,
326, -245, 324, -1328, 326, -243, 327, -1344, 324, -247, 325, -1334, 318,
-249, 322, -1331, 323, -246, 325, -1331, 323, -249, 322, -1330, 326, -252,
316, -1330, 327, -246, 322, -1334, 322, -249, 319, -1334, 321, -1330, 318,
-268, 322, -1325, 321, -261, 319, -259, 310, -1337, 318, -1324, 320, -263,
316, -1329, 315, -274, 308, -257, 312, -1344, 310, -258, 313, -1339, 316,
-258, 311, -1350, 303, -262, 305, -1363, 308, -1334, 309, -271, 309, -264,
304, -1349, 307, -262, 306, -1352, 304, -1338, 308, -277, 299, -271, 302,
-1351, 303, -275, 292, -1359, 298, -274, 295, -1362, 293, -273, 298, -1349,
294
""")

EV1527_C = parse("""
321, -2781, 330, -244, 321, -1329, 328, -1319, 325, -255, 324, -250, 318,
-1339, 316, -1323, 322, -257, 322, -257, 314, -1339, 315, -1327, 319, -261,
317, -256, 315, -1336, 318, -257, 313, -1356, 312, -258, 309, -1343, 315,
-254, 315, -1345, 310, -259, 310, -1344, 312, -258, 313, -1338, 314, -265,
305, -1345, 313, -256, 313, -1349, 307, -263, 306, -1345, 310, -1334, 310,
-284, 308, -1334, 312, -268, 310, -268, 302, -1347, 311, -1338, 306, -280,
300, -1336, 307, -272, 308, -270, 300, -1353, 300, -270, 303, -1347, 307,
-268, 301, -1353, 303, -270, 299, -1369, 300, -1341, 303, -285, 296, -274,
296, -1352, 304, -269, 299, -1355, 301, -1347, 299, -280, 299, -274, 294,
-1362, 295, -273, 296, -1358, 297, -277, 294, -1359, 296, -274, 297, -1352,
292
""")

NOISE = parse("""
117, -3666, 493, -952, 539, -925, 385, -1063, 493, -1870, 876, -656, 604,
-129, 201, -309, 767, -396, 2941, -128, 572, -113, 793, -181
""")

ev_results = {name: rf_analysis.analyze(t) for name, t in
              (("A", EV1527_A), ("B", EV1527_B), ("C", EV1527_C))}
noise_result = rf_analysis.analyze(NOISE)

for name, result in ev_results.items():
    print(
        f"EV1527 {name}: encoding={result.get('encoding')} "
        f"bits={result.get('bit_count')} hex={result.get('hex')} "
        f"fp={result.get('fingerprint')}"
    )
    check(result.get("encoding") == "space_distance",
          f"EV1527 {name}: expected space_distance")

check(
    ev_results["A"].get("bits")
    == ev_results["B"].get("bits")
    == ev_results["C"].get("bits")
    is not None,
    "EV1527: bits differ between repeats",
)
check(
    ev_results["A"]["fingerprint"]
    == ev_results["B"]["fingerprint"]
    == ev_results["C"]["fingerprint"],
    "EV1527: fingerprints differ between repeats",
)
check(rf_analysis.same_code(ev_results["A"], ev_results["B"]),
      "EV1527: same_code(A,B) is False")
check(not rf_analysis.same_code(ev_results["A"], noise_result),
      "EV1527: noise matched the code")

# ---------------------------------------------------------------------------
# 2. Manchester coding: jittered synthetic frames must decode to identical
#    bits and fingerprints (including the trailing half swallowed by the
#    inter-frame gap).
# ---------------------------------------------------------------------------


def build_manchester(bits: str, half_us: int, jitter: float, seed: int) -> list[int]:
    """Encode bits as Manchester (mark→space = 1) with timing jitter."""
    rng = random.Random(seed)
    halves: list[bool] = []
    for bit in bits:
        halves.extend((True, False) if bit == "1" else (False, True))
    timings: list[int] = []
    for level in halves:
        duration = round(half_us * (1 + rng.uniform(-jitter, jitter)))
        signed = duration if level else -duration
        if timings and (timings[-1] > 0) == (signed > 0):
            timings[-1] += signed
        else:
            timings.append(signed)
    if timings[-1] > 0:
        timings.append(-8000)  # inter-frame gap
    else:
        timings[-1] = -8000
    return timings


MANCHESTER_BITS = "101100111000101101001101"
manchester_a = rf_analysis.analyze(build_manchester(MANCHESTER_BITS, 500, 0.08, 1))
manchester_b = rf_analysis.analyze(build_manchester(MANCHESTER_BITS, 500, 0.08, 2))
print(
    f"Manchester: encoding={manchester_a.get('encoding')} "
    f"bits={manchester_a.get('bits')} hex={manchester_a.get('hex')}"
)
check(manchester_a.get("encoding") == "manchester", "Manchester: wrong encoding")
check(manchester_a.get("bits") == MANCHESTER_BITS, "Manchester: bits mismatch")
check(
    manchester_a["fingerprint"] == manchester_b["fingerprint"],
    "Manchester: jittered fingerprints differ",
)

# ---------------------------------------------------------------------------
# 3. rc-switch PWM regression: encoding our own binary output must decode
#    back to the same bits including the protocol guess.
# ---------------------------------------------------------------------------

RC_BITS = "100110100101"
rc_result = rf_analysis.analyze(rf_analysis.encode_binary(RC_BITS, protocol=1))
print(
    f"rc-switch: encoding={rc_result.get('encoding')} bits={rc_result.get('bits')} "
    f"protocol_guess={rc_result.get('protocol_guess')}"
)
check(rc_result.get("encoding") == "pwm", "rc-switch: wrong encoding")
check(rc_result.get("bits") == RC_BITS, "rc-switch: bits mismatch")
check(rc_result.get("protocol_guess") == 1, "rc-switch: wrong protocol guess")

# ---------------------------------------------------------------------------
# 4. Tri-state round trip.
# ---------------------------------------------------------------------------

TRISTATE = "0FF0F0FF0001"
tri_result = rf_analysis.analyze(rf_analysis.encode_tristate(TRISTATE, protocol=1))
print(f"tri-state: decoded={tri_result.get('tristate')}")
check(tri_result.get("tristate") == TRISTATE, "tri-state: round trip failed")

# ---------------------------------------------------------------------------
# 5. Tasmota RfRaw B0: a real working code from a Sonoff RF Bridge must
#    decode to raw timings that analyze as the same space-distance code.
# ---------------------------------------------------------------------------

B0 = (
    "AA B0 4C 04 08 0136 0AD2 0528 29EA "
    "381808282808082828080828280808280828082808280828082808280828082828"
    "082808082828082808082808280828082828080828082808280828082808282808 55"
)
b0_timings = rf_analysis.parse_tasmota_rfraw(B0)
b0_analysis = rf_analysis.analyze(b0_timings)
print(
    f"RfRaw B0: {len(b0_timings)} pulses, encoding={b0_analysis.get('encoding')} "
    f"bits={b0_analysis.get('bit_count')} hex={b0_analysis.get('hex')}"
)
check(len(b0_timings) >= 120, "B0: too few pulses decoded")
marks = {v for v in b0_timings if v > 0}
check(len(marks) == 1, f"B0: marks not constant ({sorted(marks)})")
check(b0_analysis.get("encoding") == "space_distance", "B0: wrong encoding")
check(
    (b0_analysis.get("hex") or "").startswith("0x3332AAAB4D2AC"),
    f"B0: unexpected code {b0_analysis.get('hex')}",
)
# Compact form without spaces must parse identically.
check(
    rf_analysis.parse_tasmota_rfraw(B0.replace(" ", ""))
    == b0_timings,
    "B0: compact (no-space) form parsed differently",
)

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. The rc-switch Protocol 4 encoder uses a 2,280 µs sync space. It must
#    split its four 24-bit frames with the verified 2,000 µs threshold.
# ---------------------------------------------------------------------------

ROTATING_BITS = "101010101010101010101010"
rotating_timings = rf_analysis.encode_binary(
    ROTATING_BITS, pulse_length=380, protocol=4
)
rotating_frames = rf_analysis.split_frames(rotating_timings)
rotating_analysis = rf_analysis.analyze(rotating_timings)
check(
    len(rotating_frames) == 4,
    f"2.28ms sync: expected 4 frames, got {len(rotating_frames)}",
)
check(
    rotating_analysis.get("bits") == ROTATING_BITS
    and rotating_analysis.get("bit_count") == 24
    and rotating_analysis.get("protocol_guess") == 4,
    f"2.28ms sync: expected decoded 24-bit code, got {rotating_analysis.get('bits')!r}",
)

# ---------------------------------------------------------------------------
# 9. Actual RC-Switch Protocol 4 frames from five keypresses on the user's
#    SURNICE remote. These are the first complete 24-bit frame (including
#    its sync pair) from each real ESPHome Received Raw capture. The full
#    captures contain many repetitions of the same frame; one per press is
#    sufficient here to preserve the real timings while covering all codes.
# ---------------------------------------------------------------------------

REMOTE_P4_CASES = {
    "keypress 1": (
        parse("""
        1156, -342, 400, -1072, 391, -1099, 1124, -371, 1102, -365,
        400, -1069, 387, -1090, 1126, -365, 1128, -351, 1115, -357,
        380, -1116, 1112, -372, 369, -1098, 400, -1075, 1125, -355,
        390, -1091, 1130, -363, 373, -1101, 1115, -372, 369, -1096,
        1129, -342, 386, -1114, 1128, -338, 399, -1102, 363, -2216
        """),
        "100110011101001010101010",
    ),
    "keypress 2": (
        parse("""
        927, -369, 363, -1114, 372, -1110, 1123, -362, 1122, -343,
        1128, -366, 371, -1101, 1114, -366, 1140, -342, 381, -1114,
        1114, -361, 390, -1076, 1133, -365, 373, -1093, 398, -1094,
        1123, -363, 371, -1092, 399, -1092, 1123, -363, 371, -1092,
        1141, -341, 389, -1091, 1123, -364, 384, -1099, 386, -2199
        """),
        "100111011010100100101010",
    ),
    "keypress 3": (
        parse("""
        1072, -385, 370, -1116, 359, -1119, 1113, -360, 388, -1106,
        369, -1098, 364, -1118, 366, -1126, 359, -1122, 367, -1116,
        359, -1122, 364, -1092, 1120, -365, 397, -1086, 366, -1115,
        369, -1115, 370, -1122, 370, -1093, 370, -1116, 1122, -353,
        1120, -367, 368, -1128, 1098, -374, 366, -1116, 370, -2211
        """),
        "100100000000100000011010",
    ),
    "keypress 4": (
        parse("""
        1112, -336, 395, -1099, 370, -1116, 1115, -368, 363, -1118,
        367, -1109, 372, -1100, 1116, -357, 1143, -364, 366, -1099,
        385, -1094, 1133, -363, 371, -1089, 1135, -355, 391, -1091,
        1117, -366, 1113, -370, 388, -1094, 382, -1101, 1114, -370,
        1121, -366, 374, -1100, 1116, -359, 390, -1108, 369, -2203
        """),
        "100100011001010110011010",
    ),
    "keypress 5": (
        parse("""
        1036, -391, 361, -1112, 368, -1110, 1096, -376, 1116, -368,
        367, -1124, 360, -1121, 1092, -394, 1095, -366, 1120, -364,
        375, -1117, 1110, -380, 368, -1090, 369, -1115, 1120, -379,
        368, -1092, 1122, -366, 366, -1121, 1116, -369, 367, -1098,
        1115, -368, 386, -1096, 1122, -369, 365, -1117, 368, -2209
        """),
        "100110011101001010101010",
    ),
}

REMOTE_P4_FINGERPRINTS = {}
for keypress, (captured_frame, expected_bits) in REMOTE_P4_CASES.items():
    result = rf_analysis.analyze(captured_frame)
    REMOTE_P4_FINGERPRINTS[keypress] = result.get("fingerprint")
    check(
        len(captured_frame) == 50,
        f"{keypress}: expected 24 data pairs plus sync pair "
        f"(50 timings), got {len(captured_frame)}",
    )
    check(
        result.get("encoding") == "pwm"
        and result.get("protocol_guess") == 4,
        f"{keypress}: expected RC-Switch Protocol 4, got "
        f"{result.get('encoding')!r}/P{result.get('protocol_guess')!r}",
    )
    check(
        result.get("bits") == expected_bits and result.get("bit_count") == 24,
        f"{keypress}: expected 24-bit code {expected_bits}, got "
        f"{result.get('bits')!r}",
    )
    check(
        isinstance(result.get("fingerprint"), str)
        and result["fingerprint"].startswith("24b:"),
        f"{keypress}: expected a bit-based 24-bit fingerprint",
    )

check(
    REMOTE_P4_FINGERPRINTS["keypress 1"]
    == REMOTE_P4_FINGERPRINTS["keypress 5"],
    "Remote Protocol 4: repeated keypress code did not produce the same fingerprint",
)
check(
    len(set(REMOTE_P4_FINGERPRINTS.values())) == 4,
    "Remote Protocol 4: expected four distinct fingerprints across five keypresses",
)

# ---------------------------------------------------------------------------
# 10. Synthetic RC-Switch Protocol 8 frame-splitting regression.
#
# This is deliberately synthetic, not a measured remote capture. P8 has
# legitimate 16T data LOWs (3,200 µs at T=200 µs), longer than the
# 2,000 µs absolute floor. The P95 * 1.7 term must raise the threshold to
# 5,440 µs so those data spaces stay inside their frames; only the 130T
# sync LOWs (26,000 µs) may terminate frames.
# ---------------------------------------------------------------------------

PROTOCOL_8_BITS = "101010101010101010101010"
protocol_8_timings = rf_analysis.encode_binary(
    PROTOCOL_8_BITS, pulse_length=200, protocol=8, frames=4
)
protocol_8_absolutes = sorted(abs(value) for value in protocol_8_timings)
protocol_8_p95 = protocol_8_absolutes[
    int(0.95 * (len(protocol_8_absolutes) - 1))
]
protocol_8_adaptive = rf_analysis._FRAME_GAP_FACTOR * protocol_8_p95
protocol_8_threshold = max(
    rf_analysis._FRAME_GAP_MIN_US, protocol_8_adaptive
)
protocol_8_frames = rf_analysis.split_frames(protocol_8_timings)

check(
    protocol_8_p95 == 3_200,
    f"Synthetic Protocol 8: expected P95=3,200 µs, got {protocol_8_p95}",
)
check(
    protocol_8_threshold == 5_440,
    f"Synthetic Protocol 8: expected adaptive threshold=5,440 µs, got "
    f"{protocol_8_threshold}",
)
check(
    protocol_8_threshold > 3_200,
    "Synthetic Protocol 8: threshold must remain above its 3,200 µs data LOW",
)
check(
    len(protocol_8_frames) == 4
    and all(len(frame) == 50 for frame in protocol_8_frames),
    "Synthetic Protocol 8: 3,200 µs data LOWs were mistaken for frame gaps "
    f"(got {len(protocol_8_frames)} frames with lengths "
    f"{[len(frame) for frame in protocol_8_frames]})",
)
check(
    all(frame[-1] == -26_000 for frame in protocol_8_frames),
    "Synthetic Protocol 8: expected each frame to end at its 26,000 µs sync LOW",
)
if failures:
    print("\nFAILURES:")
    print("\n".join(failures))
    sys.exit(1)
print("\nOK: all decoder tests passed")
