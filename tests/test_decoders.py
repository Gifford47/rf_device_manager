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
    const.PROTOCOL_B0 = "b0"
    const.PROTOCOL_BINARY = "binary"
    const.PROTOCOL_PRONTO = "pronto"
    const.PROTOCOL_RAW = "raw"
    const.PROTOCOL_TRISTATE = "tristate"
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
# 6. Learned command candidates are noise-ranked but returned in first-seen
#    order, so fixed rotating-code sequences can be stored faithfully.
# ---------------------------------------------------------------------------

learn_captures = [
    types.SimpleNamespace(
        received_at=1.0, analysis={"bits": "CODE_A", "times_seen": 4}
    ),
    types.SimpleNamespace(
        received_at=2.0, analysis={"bits": "NOISE", "times_seen": 1}
    ),
    types.SimpleNamespace(
        received_at=3.0, analysis={"bits": "CODE_C", "times_seen": 9}
    ),
    types.SimpleNamespace(
        received_at=4.0, analysis={"bits": "CODE_A", "times_seen": 2}
    ),
]
learned = rf_analysis.cluster_learn_signals(learn_captures, max_signals=2)
check(
    [signal.analysis["bits"] for signal in learned] == ["CODE_A", "CODE_C"],
    "Learning: strongest candidates were not returned in first-seen order",
)
check(
    learned[0].analysis["times_seen"] == 6,
    "Learning: duplicate captures were not counted into their cluster",
)

# ---------------------------------------------------------------------------
# 7. Manual multi-variant parsing preserves entry order across all formats.
# ---------------------------------------------------------------------------

raw_variants = rf_analysis.parse_manual_variants(
    "raw",
    [
        "100, -300, 100, -300, 100, -300, 100, -300",
        "200, -400, 200, -400, 200, -400, 200, -400",
    ],
)
check(len(raw_variants) == 2, "Manual raw: expected two variants")
check(
    raw_variants[0][0] == [100, -300, 100, -300, 100, -300, 100, -300]
    and raw_variants[1][0] == [200, -400, 200, -400, 200, -400, 200, -400],
    "Manual raw: variants or their order changed",
)

tri_codes = ["0F1F0011", "1F0F1100"]
tri_variants = rf_analysis.parse_manual_variants(
    "tristate", tri_codes, pulse_length=380, protocol=4
)
check(len(tri_variants) == 2, "Manual tri-state: expected two variants")
check(
    [code for _timings, code in tri_variants] == tri_codes,
    "Manual tri-state: code values or order changed",
)

binary_codes = [
    "101010101010101010101010",
    "101010101010101010101011",
    "101010101010101010101100",
    "101010101010101010101101",
]
binary_variants = rf_analysis.parse_manual_variants(
    "binary", binary_codes, pulse_length=380, protocol=4
)
check(len(binary_variants) == 4, "Manual binary: expected four variants")
check(
    [code for _timings, code in binary_variants] == binary_codes,
    "Manual binary: code values or order changed",
)
check(
    [rf_analysis.analyze(timings).get("bits") for timings, _code in binary_variants]
    == binary_codes,
    "Manual binary: encoded variants did not decode back to their original bits",
)

B1 = B0.replace("AA B0 4C 04 08 ", "AA B1 04 ", 1)
b0_variants = rf_analysis.parse_manual_variants("b0", [B0, B1])
check(
    len(b0_variants) == 2 and b0_variants[0][0] == b0_variants[1][0],
    "Manual B0/B1: multiple variants were not parsed equivalently",
)

PRONTO = "0000 006D 0004 0000 0010 0020 0010 0020 0010 0020 0010 0020"
PRONTO_0100 = PRONTO.replace("0000", "0100", 1)
pronto_variants = rf_analysis.parse_manual_variants(
    "pronto", [PRONTO, PRONTO_0100]
)
check(
    len(pronto_variants) == 2 and pronto_variants[0][0] == pronto_variants[1][0],
    "Manual Pronto: 0000/0100 formats were not parsed equivalently",
)

try:
    rf_analysis.parse_manual_variants("binary", [])
except rf_analysis.RFInputError as err:
    check(
        err.error_key == "manual_variant_required",
        "Manual variants: empty list returned the wrong error",
    )
else:
    check(False, "Manual variants: accepted an empty list")

try:
    rf_analysis.parse_manual_variants("binary", ["1010", ""])
except rf_analysis.RFInputError as err:
    check(
        err.error_key == "manual_variant_required",
        "Manual variants: empty-entry validation returned the wrong error",
    )
else:
    check(False, "Manual variants: accepted an empty variant")

# ---------------------------------------------------------------------------
# 8. Every exposed rc-switch protocol must round-trip through the encoder and
#    structural decoder. Protocol 6 is intentionally not in RC_PROTOCOLS.
# ---------------------------------------------------------------------------

ROTATING_BITS = "101010101010101010101010"

for protocol in rf_analysis.RC_PROTOCOLS:
    encoded = rf_analysis.encode_binary(
        ROTATING_BITS, pulse_length=None, protocol=protocol
    )
    decoded = rf_analysis.analyze(encoded)
    check(
        decoded.get("bits") == ROTATING_BITS,
        f"rc-switch P{protocol}: binary bits did not round-trip",
    )
    if protocol in {1, 2, 4, 5, 7}:
        check(
            decoded.get("protocol_guess") == protocol,
            f"rc-switch P{protocol}: expected protocol guess {protocol}, "
            f"got {decoded.get('protocol_guess')!r}",
        )

    tristate_code = "0F1F00110F1F"
    tristate_timings = rf_analysis.encode_tristate(
        tristate_code, protocol=protocol
    )
    tristate_decoded = rf_analysis.analyze(tristate_timings)
    check(
        tristate_decoded.get("bits")
        == "".join(rf_analysis.TRISTATE_TO_BITS[symbol] for symbol in tristate_code),
        f"rc-switch P{protocol}: tri-state bits did not round-trip",
    )
    if protocol in {1, 2, 4, 5, 7}:
        check(
            tristate_decoded.get("protocol_guess") == protocol,
            f"rc-switch P{protocol}: tri-state protocol guess was "
            f"{tristate_decoded.get('protocol_guess')!r}",
        )

try:
    rf_analysis.encode_binary(ROTATING_BITS, protocol=6)
except rf_analysis.RFInputError as err:
    check(
        err.error_key == "invalid_protocol",
        "rc-switch P6: unsupported protocol returned the wrong error",
    )
else:
    check(False, "rc-switch P6: unsupported protocol was accepted")

# Repeated receptions of one capture cluster are not multiple command variants.
check(
    "variants" not in learned[0].analysis,
    "Learning: repeated receptions were mislabeled as command variants",
)

if failures:
    print("\nFAILURES:")
    print("\n".join(failures))
    sys.exit(1)
print("\nOK: all decoder and manual-input tests passed")
