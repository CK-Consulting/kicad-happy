#!/usr/bin/env python3
"""One decision, four places it is written down, and they must agree.

A part's lifecycle verdict leaves the audit three ways - the JSON finding a
report consumes, the row in the project's table, and the row recalc rewrites
later from the cache - and every review round on this code found two of them
drifting apart: a finding saying active beside a table saying obsolete, a
rescore counting sources the audit never asked, a departed marker one reader
honoured and another did not. Each was fixed where it was found. This test is
the property behind all of them, checked across the cases that broke it:

    the finding, the table row and the recalculated row agree on status,
    score, raw score, sources, whether an acknowledgement is outstanding, and
    whether the part is still on the board.

Run directly (``python3 skills/test_lifecycle_consistency.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

import lifecycle_audit  # noqa: E402
import lifecycle_recalc  # noqa: E402
from lifecycle_table import is_departed, read_table, render_table  # noqa: E402

OTHER = "STM32U5G9NJH6Q"


@contextmanager
def _distributors(digikey, fail_if_called=False, lcsc_range=None):
    """DigiKey answers ``digikey`` (None: carries the part, gives no status);
    every other source carries it with no status, as most of them do. LCSC
    can also be given an operating-temperature range to report."""
    saved = dict(lifecycle_audit._API_FNS)

    def answer(name):
        def query(mpn, timeout=10.0):
            if fail_if_called:
                raise AssertionError("%s queried on a fully cached run" % name)
            if name == "digikey" and digikey:
                return {"status": digikey}
            if name == "lcsc" and lcsc_range:
                return {"provides_status": False, "temp_min_c": lcsc_range[0],
                        "temp_max_c": lcsc_range[1]}
            return {"provides_status": False}
        return query
    for src in saved:
        lifecycle_audit._API_FNS[src] = answer(src)
    try:
        yield
    finally:
        lifecycle_audit._API_FNS.clear()
        lifecycle_audit._API_FNS.update(saved)


def _audit(proj, mpns, digikey="Active", cached=False, lcsc_range=None, **kw):
    bom = {"bom": [{"mpn": m, "references": ["U%d" % i]}
                   for i, m in enumerate(mpns, start=1)]}
    with _distributors(digikey, fail_if_called=cached, lcsc_range=lcsc_range):
        return lifecycle_audit.audit_bom(bom, project_dir=proj, **kw)


def _edit(proj, mpn, **cells):
    """A person filling in the human columns of one row."""
    table = os.path.join(proj, "lifecycle.md")
    rows = read_table(table)
    rows[mpn].update(cells)
    with open(table, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))


def _recalc(proj):
    saved = sys.argv
    sys.argv = ["lifecycle_recalc.py", proj]
    try:
        assert lifecycle_recalc.main() == 0
    finally:
        sys.argv = saved


def _row(proj, mpn):
    rows = read_table(os.path.join(proj, "lifecycle.md"))
    return next(r for m, r in rows.items() if m.upper() == mpn.upper())


def _verdict_of_row(row):
    return {"status": row["Status"], "computed": float(row["Computed"]),
            "raw": float(row["Raw"]), "sources": row["Sources"],
            "ack": row["Ack?"] == "YES", "departed": is_departed(row)}


def _assert_agree(proj, result, mpn, expect):
    """The property. ``expect`` pins the cases down so agreement on a wrong
    answer cannot pass."""
    findings = [f for f in result["findings"]
                if f.get("category") == "lifecycle" and f.get("mpn", "").upper() == mpn.upper()]
    table = _verdict_of_row(_row(proj, mpn))
    _recalc(proj)
    recalc = _verdict_of_row(_row(proj, mpn))

    assert table == recalc, (mpn, table, recalc)
    if table["departed"]:
        assert findings == [], "a part off the board still has a finding"
    else:
        assert len(findings) == 1, findings
        f = findings[0]
        # The table shows the score to whole points; the finding carries it
        # unrounded. Same number, compared at the table's precision.
        emitted = {"status": f["status"],
                   "computed": float("%.0f" % f["computed_confidence"]),
                   "ack": f["needs_ack"]}
        assert emitted == {k: table[k] for k in emitted}, (mpn, emitted, table)
    for key, want in expect.items():
        assert table[key] == want, (mpn, key, table[key], want)


TODAY = time.strftime("%Y-%m-%d")
REF = "https://vendor.example/pcn"


def test_api_answer_alone():
    proj = tempfile.mkdtemp()
    r = _audit(proj, ["TPS62840DLCR"], "Active")
    _assert_agree(proj, r, "TPS62840DLCR",
                  {"status": "active", "computed": 80.0, "ack": False})


def test_api_says_obsolete():
    proj = tempfile.mkdtemp()
    r = _audit(proj, ["TPS62840DLCR"], "Obsolete")
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "obsolete"})


def test_no_status_anywhere():
    proj = tempfile.mkdtemp()
    r = _audit(proj, ["MM8108-MF15457"], None)
    _assert_agree(proj, r, "MM8108-MF15457",
                  {"status": "unknown", "computed": 0.0, "ack": True})


def test_human_check_alone():
    proj = tempfile.mkdtemp()
    _audit(proj, ["MM8108-MF15457"], None)
    _edit(proj, "MM8108-MF15457", **{"User Status": "active", "Reference": REF,
                                     "Checked On": TODAY})
    r = _audit(proj, ["MM8108-MF15457"], None)
    _assert_agree(proj, r, "MM8108-MF15457", {"status": "active"})


def test_human_check_agrees_with_the_api():
    proj = tempfile.mkdtemp()
    _audit(proj, ["TPS62840DLCR"])
    _edit(proj, "TPS62840DLCR", **{"User Status": "active", "Reference": REF,
                                   "Checked On": TODAY})
    r = _audit(proj, ["TPS62840DLCR"])
    _assert_agree(proj, r, "TPS62840DLCR",
                  {"status": "active", "computed": 90.0, "ack": False})


def test_human_check_disagrees_with_the_api():
    proj = tempfile.mkdtemp()
    _audit(proj, ["TPS62840DLCR"])
    _edit(proj, "TPS62840DLCR", **{"User Status": "obsolete", "Reference": REF,
                                   "Checked On": TODAY})
    r = _audit(proj, ["TPS62840DLCR"])
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "obsolete", "ack": True})


def test_disagreement_acknowledged():
    proj = tempfile.mkdtemp()
    _audit(proj, ["TPS62840DLCR"])
    _edit(proj, "TPS62840DLCR", **{"User Status": "obsolete", "Reference": REF,
                                   "Checked On": TODAY,
                                   "Acknowledged": "SC " + TODAY})
    r = _audit(proj, ["TPS62840DLCR"])
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "obsolete", "ack": False})


def test_departed():
    proj = tempfile.mkdtemp()
    _audit(proj, ["MM8108-MF15457", OTHER], None)
    r = _audit(proj, [OTHER], None)
    _assert_agree(proj, r, "MM8108-MF15457", {"departed": True, "ack": False})


def test_departed_and_returned():
    proj = tempfile.mkdtemp()
    _audit(proj, ["MM8108-MF15457", OTHER], None)
    _audit(proj, [OTHER], None)
    r = _audit(proj, ["MM8108-MF15457", OTHER], None)
    _assert_agree(proj, r, "MM8108-MF15457", {"departed": False, "ack": True})


def test_served_from_the_cache():
    proj = tempfile.mkdtemp()
    fetched = _audit(proj, ["TPS62840DLCR"], "Obsolete")
    before = _verdict_of_row(_row(proj, "TPS62840DLCR"))
    r = _audit(proj, ["TPS62840DLCR"], "Obsolete", cached=True)
    assert _verdict_of_row(_row(proj, "TPS62840DLCR")) == before
    assert ([f["status"] for f in r["findings"] if f.get("mpn") == "TPS62840DLCR"]
            == [f["status"] for f in fetched["findings"] if f.get("mpn") == "TPS62840DLCR"])
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "obsolete"})


def test_mixed_case_across_revisions():
    proj = tempfile.mkdtemp()
    _audit(proj, ["NRF52840-QIAA"])
    _edit(proj, "NRF52840-QIAA", **{"User Status": "obsolete", "Reference": REF,
                                    "Checked On": TODAY})
    r = _audit(proj, ["nRF52840-QIAA"])
    _assert_agree(proj, r, "nRF52840-QIAA", {"status": "obsolete", "ack": True,
                                             "departed": False})



def test_human_check_gone_stale():
    proj = tempfile.mkdtemp()
    _audit(proj, ["TPS62840DLCR"])
    _edit(proj, "TPS62840DLCR", **{"User Status": "active", "Reference": REF,
                                   "Checked On": "2024-01-15"})
    r = _audit(proj, ["TPS62840DLCR"])
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "active", "ack": True})


def test_human_override_on_a_cached_run():
    proj = tempfile.mkdtemp()
    _audit(proj, ["TPS62840DLCR"])
    _edit(proj, "TPS62840DLCR", **{"User Status": "nrnd", "Reference": REF,
                                   "Checked On": TODAY})
    r = _audit(proj, ["TPS62840DLCR"], cached=True)
    _assert_agree(proj, r, "TPS62840DLCR", {"status": "nrnd", "ack": True})


def _temperature_findings(result, mpn):
    return [(f["rule_id"], f["component_range"]) for f in result["findings"]
            if f.get("category") == "temperature" and f.get("mpn") == mpn]


def test_a_confident_cached_status_does_not_cut_off_temperature_evidence():
    """Reaching the confidence exit on a cached lifecycle answer skipped every
    remaining source, including the one carrying the only temperature range,
    so LT-001 appeared on a fetched run and vanished on a partly cached one."""
    mpn = "TPS62840DLCR"
    kw = {"temp_range": (-40.0, 85.0), "confidence_exit": 0.5,
          "sources": ["digikey", "lcsc"]}
    fetched = _audit(tempfile.mkdtemp(), [mpn], lcsc_range=(-20.0, 70.0), **kw)

    proj = tempfile.mkdtemp()
    _audit(proj, [mpn], sources=["digikey"])         # DigiKey's answer cached
    partly_cached = _audit(proj, [mpn], lcsc_range=(-20.0, 70.0), **kw)

    expected = [("LT-001", {"min_c": -20.0, "max_c": 70.0})]
    assert _temperature_findings(fetched, mpn) == expected
    assert _temperature_findings(partly_cached, mpn) == expected
    _assert_agree(proj, partly_cached, mpn, {"status": "active"})

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print("FAIL %s: %s" % (name, exc))
        else:
            print("ok   %s" % name)
    print("\n%d failed" % failures if failures else "\nall passed")
    sys.exit(1 if failures else 0)
