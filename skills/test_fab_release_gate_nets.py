#!/usr/bin/env python3
"""The release gate's net count compares nets that join two or more pads.

A pin left unconnected is a one-pin net in the schematic, and a one-pad net on
the board only if the tool that wrote the board gave it one, so comparing
totals reported a board that matched its schematic as 36 nets short. The count
is of nets with two or more pins on both sides, and on the schematic side only
pins that become pads: the net map keeps #PWR and #FLG pins, and a connector
pin tied to a VCC symbol is one pad.

Run directly (``python3 skills/test_fab_release_gate_nets.py``) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent / "kicad" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fab_release_gate import check_consistency  # noqa: E402


def _pin(ref: str, num: str = "1") -> dict:
    return {"component": ref, "pin_number": num}


def _net_check(sch: dict, pcb: dict) -> dict:
    return next(c for c in check_consistency(sch, pcb) if c["check_id"] == "net_count")


def _pcb(pads: dict) -> dict:
    """Footprints from {ref: [net per pad]}, in the compact pad_nets form."""
    return {"footprints": [
        {"reference": ref, "pad_nets": {str(i + 1): {"net": n} for i, n in enumerate(nets)}}
        for ref, nets in pads.items()
    ]}


def test_unconnected_pins_do_not_count_on_either_side():
    sch = {"nets": {
        "SIG": {"pins": [_pin("R1", "1"), _pin("U1", "3")]},
        "__unnamed_0": {"pins": [_pin("U1", "4")]},
    }}
    pcb = _pcb({"R1": ["SIG", None], "U1": ["SIG", None]})
    assert _net_check(sch, pcb)["status"] == "pass"


def test_a_power_symbol_pin_is_not_a_pad():
    sch = {"nets": {
        "VCC": {"pins": [_pin("J1", "1"), _pin("#PWR01")]},
        "GND": {"pins": [_pin("J1", "2"), _pin("#PWR02"), _pin("#FLG01")]},
    }}
    pcb = _pcb({"J1": ["VCC", "GND"]})
    check = _net_check(sch, pcb)
    assert check["status"] == "pass", check


def test_a_real_missing_net_still_shows():
    sch = {"nets": {
        "A": {"pins": [_pin("R1", "1"), _pin("R2", "1")]},
        "B": {"pins": [_pin("R1", "2"), _pin("R2", "2")]},
    }}
    pcb = _pcb({"R1": ["A", None], "R2": ["A", None]})
    assert _net_check(sch, pcb)["status"] != "pass"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
