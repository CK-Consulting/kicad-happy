#!/usr/bin/env python3
"""Rescoring the table from the cache, with no network.

The recalculation is only useful if it finds what the audit wrote: the cache
where the audit put it, and each part's cached answers under the key the cache
filed them by. Miss either and it rescored a table it could not see the data
for, which is worse than not running.

Run directly (``python3 skills/test_lifecycle_recalc.py``) or under pytest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

import lifecycle_audit  # noqa: E402
import lifecycle_recalc  # noqa: E402
from lifecycle_cache import LifecycleCache  # noqa: E402
from lifecycle_table import read_table, render_table, write_table  # noqa: E402


def _project(mpn, cache_rel):
    """A project with one unscored row in its table and one cached answer."""
    proj = tempfile.mkdtemp()
    write_table(os.path.join(proj, "lifecycle.md"),
                {mpn: {"refs": ["U1"], "status": "unknown", "computed": 0.0,
                       "raw": 0.0, "responding": 0, "capable": 1,
                       "needs_ack": True}})
    cache = LifecycleCache(os.path.join(proj, *cache_rel))
    cache.put(mpn, "digikey", {"status": "Active"})
    cache.save()
    return proj


def _recalc(*argv):
    saved = sys.argv
    sys.argv = ["lifecycle_recalc.py", *argv]
    try:
        return lifecycle_recalc.main()
    finally:
        sys.argv = saved


def test_finds_the_cache_where_the_audit_writes_it():
    """For a project root the audit writes <project>/analysis/; recalc looked
    only under .pipeline/ and reported the cache unreadable."""
    proj = _project("TPS62840DLCR", ("analysis", "lifecycle_cache.json"))
    assert _recalc(proj) == 0
    assert read_table(os.path.join(proj, "lifecycle.md"))["TPS62840DLCR"]["Status"] == "active"


def test_still_finds_a_cache_beside_a_schematic_in_pipeline():
    """Where the schematic sits in .pipeline/, the audit is handed that
    directory and its cache lands under it."""
    proj = _project("TPS62840DLCR", (".pipeline", "analysis", "lifecycle_cache.json"))
    assert _recalc(proj) == 0
    assert read_table(os.path.join(proj, "lifecycle.md"))["TPS62840DLCR"]["Status"] == "active"


def test_a_mixed_case_mpn_finds_its_cached_answers():
    """The cache files keys upper-cased; the table keeps the part's own
    spelling. Looking one up by the other rescored the part as unknown."""
    proj = _project("nRF52840-QIAA", (".pipeline", "analysis", "lifecycle_cache.json"))
    assert _recalc(proj) == 0
    row = read_table(os.path.join(proj, "lifecycle.md"))["nRF52840-QIAA"]
    assert row["Status"] == "active"
    assert row["Sources"] == "1/1"



def test_a_human_check_is_scored_when_no_distributor_gave_a_status():
    """Where only stock-only sources answered, the cache holds no status at
    all, and a person's referenced check is the only evidence there is. Recalc
    refused to run on that cache, so the check was never scored."""
    proj = tempfile.mkdtemp()
    table = os.path.join(proj, "lifecycle.md")
    write_table(table, {"MM8108-MF15457": {
        "refs": ["U1"], "status": "unknown", "computed": 0.0, "raw": 0.0,
        "responding": 0, "capable": 1, "needs_ack": True}})
    rows = read_table(table)
    rows["MM8108-MF15457"].update({"User Status": "active",
                                   "Reference": "https://morsemicro.com/mm8108",
                                   "Checked On": time.strftime("%Y-%m-%d")})
    with open(table, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))
    cache = LifecycleCache(os.path.join(proj, "analysis", "lifecycle_cache.json"))
    cache.put("MM8108-MF15457", "lcsc", {"provides_status": False, "in_stock": True})
    cache.save()

    assert _recalc(proj) == 0
    row = read_table(table)["MM8108-MF15457"]
    assert row["Status"] == "active"
    assert float(row["Computed"]) > 0
    assert row["Sources"] == "1/2"

# -- the same score the audit wrote ---------------------------------------

@contextmanager
def _distributors(**statuses):
    """Every source answers; the ones named carry that lifecycle status."""
    saved = dict(lifecycle_audit._API_FNS)

    def answer(status):
        return lambda mpn, timeout=10.0: ({"status": status} if status
                                          else {"provides_status": False})
    for src in saved:
        lifecycle_audit._API_FNS[src] = answer(statuses.get(src))
    try:
        yield
    finally:
        lifecycle_audit._API_FNS.clear()
        lifecycle_audit._API_FNS.update(saved)


def _audit(proj, *argv, **statuses):
    """Run the audit CLI on a one-part BOM in proj; return its table row."""
    analysis = os.path.join(proj, "schematic.json")
    with open(analysis, "w") as fh:
        json.dump({"file": os.path.join(proj, "board.kicad_sch"),
                   "bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}, fh)
    saved = sys.argv
    sys.argv = ["lifecycle_audit.py", analysis,
                "-o", os.path.join(proj, "lifecycle.json"), *argv]
    try:
        with _distributors(**statuses):
            lifecycle_audit.main()
    finally:
        sys.argv = saved
    return dict(read_table(os.path.join(proj, "lifecycle.md"))["TPS62840DLCR"])


def _audit_then_recalc(*argv, **statuses):
    """The audit's table row for one part, and the row recalc makes of it."""
    proj = tempfile.mkdtemp()
    audited = _audit(proj, *argv, **statuses)
    assert _recalc(proj) == 0
    return audited, read_table(os.path.join(proj, "lifecycle.md"))["TPS62840DLCR"]


def _scores(row):
    return row["Computed"], row["Ack?"], row["Sources"]


def test_recalc_reproduces_a_default_audit():
    """A default audit asks DigiKey alone for status, so one active answer is
    80 and settled. Recalc assumed Nexar too, and rewrote it as 57 and YES."""
    audited, recalced = _audit_then_recalc(digikey="Active")
    assert _scores(audited) == ("80", "", "1/1")
    assert _scores(recalced) == _scores(audited)


def test_recalc_reproduces_an_audit_that_asked_nexar():
    """The source set the audit used is recorded with the cache, so a run
    that opted into Nexar is rescored against two capable sources, not one."""
    audited, recalced = _audit_then_recalc("--nexar", digikey="Active")
    assert audited["Sources"] == "1/2"
    assert _scores(recalced) == _scores(audited)



def test_a_stale_answer_from_a_source_the_audit_dropped_is_not_scored():
    """A Nexar run leaves its answer in the shared cache; the default audit
    after it ignores Nexar. Recalc fed every cached status to the score, so
    the old Nexar "obsolete" overrode DigiKey's current "active"."""
    proj = tempfile.mkdtemp()
    _audit(proj, "--nexar", digikey="Active", nexar="Obsolete")
    audited = _audit(proj, digikey="Active")
    assert audited["Status"] == "active"
    assert _recalc(proj) == 0
    recalced = read_table(os.path.join(proj, "lifecycle.md"))["TPS62840DLCR"]
    assert recalced["Status"] == "active"
    assert _scores(recalced) == _scores(audited)

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
