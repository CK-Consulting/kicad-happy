#!/usr/bin/env python3
"""Rescoring the table from the cache, with no network.

The recalculation is only useful if it finds what the audit wrote: the cache
where the audit put it, and each part's cached answers under the key the cache
filed them by. Miss either and it rescored a table it could not see the data
for, which is worse than not running.

Run directly (``python3 skills/test_lifecycle_recalc.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

import lifecycle_recalc  # noqa: E402
from lifecycle_cache import LifecycleCache  # noqa: E402
from lifecycle_table import read_table, write_table  # noqa: E402


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
    assert row["Sources"] == "1/2"


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
