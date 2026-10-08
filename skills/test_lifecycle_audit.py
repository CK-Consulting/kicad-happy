#!/usr/bin/env python3
"""The audit's own control flow: what it caches, what it reports, what it writes.

The cache and the confidence model are tested on their own beside this file.
What is tested here is the code between them and the network — the part that
decides whether an answer was an answer, and what the run does with it. None
of it touches a distributor: the transport is replaced, so a test that passes
offline means the same thing as one that passes on a good connection.

Run directly (``python3 skills/test_lifecycle_audit.py``) or under pytest.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

import lifecycle_audit  # noqa: E402
from lifecycle_cache import LifecycleCache  # noqa: E402

_CREDENTIAL_VARS = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
                    "MOUSER_SEARCH_API_KEY", "MOUSER_PART_API_KEY",
                    "ELEMENT14_API_KEY", "NEXAR_CLIENT_ID",
                    "NEXAR_CLIENT_SECRET", "NEXAR_ACCESS_TOKEN")


def _cache():
    return LifecycleCache(os.path.join(tempfile.mkdtemp(), "c.json"))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _transport(answer):
    """Replace urlopen. ``answer`` is a dict to return as JSON, or an exception."""
    real = urllib.request.urlopen

    def fake(req, timeout=None):
        if isinstance(answer, BaseException):
            raise answer
        return _Response(json.dumps(answer).encode())

    urllib.request.urlopen = fake
    try:
        yield
    finally:
        urllib.request.urlopen = real


@contextmanager
def _no_credentials():
    saved = {k: os.environ.pop(k) for k in _CREDENTIAL_VARS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


@contextmanager
def _sources(**fns):
    """Stand-in query functions, by source name, for the length of a block."""
    saved = dict(lifecycle_audit._API_FNS)
    lifecycle_audit._API_FNS.clear()
    lifecycle_audit._API_FNS.update(fns)
    try:
        yield
    finally:
        lifecycle_audit._API_FNS.clear()
        lifecycle_audit._API_FNS.update(saved)


def _answers(status, after=0.0):
    def query(mpn, timeout=10.0):
        time.sleep(after)
        return {"status": status}
    return query


# -- what gets cached -----------------------------------------------------

def test_a_failed_request_is_not_cached_as_a_negative_answer():
    """A timeout says nothing about whether LCSC carries the part. Caching it
    as "not carried" would hide the part from LCSC for the whole TTL."""
    c = _cache()
    with _transport(urllib.error.URLError("timed out")):
        lifecycle_audit.audit_component("TPS62840DLCR", ["lcsc"], cache=c)
    assert c.covered("TPS62840DLCR", ["lcsc"], count=False) == {}


def test_missing_credentials_are_not_cached_as_a_negative_answer():
    """Adding the key later has to be enough to start getting answers."""
    c = _cache()
    with _no_credentials():
        lifecycle_audit.audit_component("TPS62840DLCR", ["digikey", "mouser"],
                                        cache=c)
    assert c.covered("TPS62840DLCR", ["digikey", "mouser"], count=False) == {}


def test_a_confirmed_miss_is_still_cached():
    """The source answered and does not carry the part. That one is worth
    remembering; re-asking is the expensive half of the audit."""
    c = _cache()
    with _transport({"components": []}):
        lifecycle_audit.audit_component("TPS62840DLCR", ["lcsc"], cache=c)
    assert c.covered("TPS62840DLCR", ["lcsc"], count=False) == {"lcsc": None}


# -- which status is reported ---------------------------------------------

def test_disagreeing_sources_report_the_worst_status_whichever_answers_last():
    """The reported status used to be whichever request finished last, so the
    same part could read active on one run and obsolete on the next."""
    for obsolete_after, active_after in ((0.0, 0.05), (0.05, 0.0)):
        with _sources(digikey=_answers("Obsolete", obsolete_after),
                      nexar=_answers("Active", active_after)):
            r = lifecycle_audit.audit_component("LM1117IMP-3.3",
                                                ["digikey", "nexar"])
        assert r["per_source_status"] == {"digikey": "obsolete",
                                          "nexar": "active"}
        assert r["status"] == "obsolete", (active_after, r["status"])


# -- which temperature range is reported ----------------------------------

_DIGIKEY_RANGE = {"status": "Active", "temp_min_c": -40.0, "temp_max_c": 85.0}
_MOUSER_RANGE = {"temp_min_c": -20.0, "temp_max_c": 105.0,
                 "provides_status": False}


def _ranged(data, after=0.0):
    def query(mpn, timeout=10.0):
        time.sleep(after)
        return dict(data)
    return query


def _reported_range(r):
    t = r["temperature"]
    return t["temp_min_c"], t["temp_max_c"]


def test_disagreeing_temperature_ranges_settle_the_same_way_in_any_order():
    """The first response carrying a range used to win outright, so whether a
    part passed a -40..105 design depended on which distributor was faster."""
    for digikey_after, mouser_after in ((0.0, 0.05), (0.05, 0.0)):
        with _sources(digikey=_ranged(_DIGIKEY_RANGE, digikey_after),
                      mouser=_ranged(_MOUSER_RANGE, mouser_after)):
            r = lifecycle_audit.audit_component("TPS62840DLCR",
                                                ["digikey", "mouser"])
        assert _reported_range(r) == (-20.0, 85.0), (digikey_after, r["temperature"])


def test_a_cached_run_reports_the_range_a_fetched_one_does():
    """Cached answers were consumed in source order, so a re-run from the
    cache could pass a part the fetching run had failed, or the reverse."""
    c = _cache()
    c.put("TPS62840DLCR", "digikey", _DIGIKEY_RANGE)
    c.put("TPS62840DLCR", "mouser", _MOUSER_RANGE)
    with _sources(digikey=_ranged(_DIGIKEY_RANGE), mouser=_ranged(_MOUSER_RANGE)):
        r = lifecycle_audit.audit_component("TPS62840DLCR",
                                            ["digikey", "mouser"], cache=c)
    assert r["cache_hits"] == 2
    assert _reported_range(r) == (-20.0, 85.0)
    assert r["temperature"]["ranges"] == {"digikey": [-40.0, 85.0],
                                          "mouser": [-20.0, 105.0]}


# -- lead time ------------------------------------------------------------

def test_a_long_nexar_lead_time_raises_lc006():
    """Nexar reports lead time in days, under its own key. LC-006 read only
    Mouser's week count, so 200 days from Nexar produced no finding."""
    bom = {"bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}
    with _sources(nexar=_ranged({"status": "Production", "lead_time_days": 200})):
        r = lifecycle_audit.audit_bom(
            bom, sources=["nexar"], use_cache=False,
            table_path=os.path.join(tempfile.mkdtemp(), "lifecycle.md"))
    lc006 = [f for f in r["findings"] if f.get("rule_id") == "LC-006"]
    assert len(lc006) == 1
    assert lc006[0]["lead_weeks"] == 28
    assert lc006[0]["lead_source"] == "nexar"
    assert lc006[0]["severity"] == "warning"


# -- --no-cache -----------------------------------------------------------

def _run_cli(*argv):
    """The audit's CLI against a one-part BOM in a fresh project directory."""
    proj = tempfile.mkdtemp()
    analysis = os.path.join(proj, "schematic.json")
    with open(analysis, "w") as fh:
        json.dump({"file": os.path.join(proj, "board.kicad_sch"),
                   "bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}, fh)
    out = os.path.join(proj, "lifecycle.json")
    saved = sys.argv
    sys.argv = ["lifecycle_audit.py", analysis, "--only", "digikey",
                "-o", out, *argv]
    try:
        with _sources(digikey=_answers("Active")):
            lifecycle_audit.main()
    finally:
        sys.argv = saved
    with open(out) as fh:
        return proj, json.load(fh)


def test_no_cache_writes_no_cache():
    """The help says "ignore and do not write". It used to write one anyway,
    at the default path, with every entry it had just fetched."""
    proj, _ = _run_cli("--no-cache")
    assert not os.path.exists(os.path.join(proj, "analysis",
                                           "lifecycle_cache.json"))


def test_no_cache_writes_one_without_the_flag():
    """The control: the same run without the flag does leave a cache."""
    proj, _ = _run_cli()
    assert os.path.exists(os.path.join(proj, "analysis", "lifecycle_cache.json"))


def test_no_cache_reads_no_cache():
    """A cached answer must not be served, however fresh, and the file must
    be left exactly as it was."""
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    c = LifecycleCache(path)
    c.put("TPS62840DLCR", "digikey", {"status": "Obsolete"})
    c.save()
    with open(path) as fh:
        before = fh.read()
    _, result = _run_cli("--no-cache", "--cache", path)
    assert result["lifecycle_summary"]["active"] == 1
    with open(path) as fh:
        assert fh.read() == before


# -- alternatives ---------------------------------------------------------

def test_alternatives_search_runs_without_credentials():
    """LCSC needs no key, so this branch is reached on every machine. It used
    to raise NameError on an undefined timeout and take the audit with it."""
    lcsc = {"components": [{"stock": 120, "extra": {"mpn": "AP2112K-3.3TRG2",
                                                    "manufacturer": "Diodes"}}]}
    with _no_credentials(), _transport(lcsc):
        alts = lifecycle_audit.find_alternatives("AP2112K-3.3TRG1", ["lcsc"],
                                                 delay=0)
    assert [a["mpn"] for a in alts] == ["AP2112K-3.3TRG2"]


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
