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


def test_missing_credentials_leave_no_timing_behind():
    """A credential check returns in microseconds. Recording that as a
    successful answer gave the source a near-zero latency, so once the key
    was added it was allowed only the two-second floor."""
    c = _cache()
    with _no_credentials():
        lifecycle_audit.audit_component("TPS62840DLCR", ["digikey", "mouser"],
                                        cache=c)
    for src in ("digikey", "mouser"):
        assert c.timing(src) == {"ewma_s": None, "ok": 0, "timeouts": 0}, src


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


# -- the human half of the table ------------------------------------------

def test_a_human_check_still_counts_after_the_bom_respells_the_mpn():
    """The table row was looked up by exact spelling, so a BOM revision that
    changed only the capitalisation dropped the person's status from the
    score."""
    from lifecycle_table import read_table, render_table, write_table
    table = os.path.join(tempfile.mkdtemp(), "lifecycle.md")
    write_table(table, {"NRF52840-QIAA": {"refs": ["U1"], "status": "unknown",
                                          "computed": 0.0, "raw": 0.0,
                                          "responding": 0, "capable": 1,
                                          "needs_ack": True}})
    rows = read_table(table)
    rows["NRF52840-QIAA"].update({"User Status": "active",
                                  "Reference": "https://nordicsemi.com/nrf52840",
                                  "Checked On": time.strftime("%Y-%m-%d")})
    with open(table, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))

    bom = {"bom": [{"mpn": "nRF52840-QIAA", "references": ["U1"]}]}
    with _sources(digikey=_answers("Active")):
        lifecycle_audit.audit_bom(bom, sources=["digikey"], use_cache=False,
                                  table_path=table)
    after = read_table(table)
    assert list(after) == ["nRF52840-QIAA"]
    # DigiKey alone is 80; the referenced human check agreeing lifts it to 90.
    assert after["nRF52840-QIAA"]["Computed"] == "90"


def _table_with_user(mpn, status, acknowledged=""):
    from lifecycle_table import read_table, render_table, write_table
    table = os.path.join(tempfile.mkdtemp(), "lifecycle.md")
    write_table(table, {mpn: {"refs": ["U1"], "status": "unknown", "computed": 0.0,
                              "raw": 0.0, "responding": 0, "capable": 1,
                              "needs_ack": True}})
    rows = read_table(table)
    rows[mpn].update({"User Status": status, "Reference": "https://vendor.example/pcn",
                      "Checked On": time.strftime("%Y-%m-%d"),
                      "Acknowledged": acknowledged})
    with open(table, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))
    return table


def test_the_emitted_finding_carries_the_status_the_table_shows():
    """A referenced human "obsolete" against DigiKey's "active" made the table
    read obsolete and awaiting acknowledgement, while the JSON finding that
    reports consume still said LC-ACT, active."""
    from lifecycle_table import read_table
    table = _table_with_user("TPS62840DLCR", "obsolete")
    bom = {"bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}
    with _sources(digikey=_answers("Active")):
        r = lifecycle_audit.audit_bom(bom, sources=["digikey"], use_cache=False,
                                      table_path=table)
    row = read_table(table)["TPS62840DLCR"]
    f = [f for f in r["findings"] if f.get("category") == "lifecycle"
         and f.get("mpn") == "TPS62840DLCR"][0]
    assert row["Status"] == f["status"] == "obsolete"
    assert f["rule_id"] == "LC-001"
    assert f["consensus_split"] is True
    assert f["per_source_status"] == {"digikey": "active", "user": "obsolete"}
    assert r["lifecycle_summary"]["obsolete"] == 1
    assert f["computed_confidence"] == float(row["Computed"])
    assert f["needs_ack"] is (row["Ack?"] == "YES") is True


def test_one_part_spelled_two_ways_in_the_bom_is_one_part():
    """Two BOM lines differing only in case were audited as two parts and
    written as two rows the table then reads back as one."""
    from lifecycle_table import read_table
    table = os.path.join(tempfile.mkdtemp(), "lifecycle.md")
    bom = {"bom": [{"mpn": "nRF52840-QIAA", "references": ["U1"]},
                   {"mpn": "NRF52840-QIAA", "references": ["U2"]}]}
    with _sources(digikey=_answers("Active")):
        r = lifecycle_audit.audit_bom(bom, sources=["digikey"], use_cache=False,
                                      table_path=table)
    assert r["components_checked"] == 1
    rows = read_table(table)
    assert list(rows) == ["nRF52840-QIAA"]
    assert rows["nRF52840-QIAA"]["Refs"] == "U1, U2"


# -- --no-cache -----------------------------------------------------------

def _run_cli(*argv, only=("--only", "digikey"), fns=None):
    """The audit's CLI against a one-part BOM in a fresh project directory."""
    proj = tempfile.mkdtemp()
    analysis = os.path.join(proj, "schematic.json")
    with open(analysis, "w") as fh:
        json.dump({"file": os.path.join(proj, "board.kicad_sch"),
                   "bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}, fh)
    out = os.path.join(proj, "lifecycle.json")
    saved = sys.argv
    sys.argv = ["lifecycle_audit.py", analysis, *only, "-o", out, *argv]
    try:
        with _sources(**(fns or {"digikey": _answers("Active")})):
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


# -- source selection -----------------------------------------------------

def _asked(*argv):
    """Which sources the CLI actually queried, and how often, for these flags."""
    calls: list[str] = []

    def spy(name):
        def query(mpn, timeout=10.0):
            calls.append(name)
            return {"provides_status": False}
        return query
    _run_cli(*argv, only=(), fns={s: spy(s) for s in lifecycle_audit._API_FNS})
    return sorted(calls)


def test_nexar_is_added_to_an_explicit_source_list():
    """--nexar says "also query Nexar". With --only it was silently dropped,
    because the explicit list won and the flag was never consulted."""
    assert _asked("--only", "digikey", "--nexar", "--no-cache") == ["digikey", "nexar"]


def test_nexar_named_twice_is_asked_once():
    assert _asked("--only", "digikey,nexar", "--nexar", "--no-cache") == ["digikey", "nexar"]


def test_nexar_flag_alone_extends_the_defaults():
    assert _asked("--nexar", "--no-cache") == sorted(
        lifecycle_audit.DEFAULT_SOURCES + ["nexar"])


def test_spaces_in_the_source_list_do_not_drop_a_source():
    """"digikey, mouser" used to ask DigiKey alone: " mouser" matched nothing
    and was ignored without a word."""
    assert _asked("--only", "digikey, mouser", "--no-cache") == ["digikey", "mouser"]


def test_an_unknown_source_name_is_an_error_not_an_empty_run():
    """A typo in --only used to run the audit against no sources at all and
    report every part unknown."""
    try:
        _asked("--only", "digikye", "--no-cache")
    except SystemExit as exc:
        assert exc.code not in (0, None)
    else:
        raise AssertionError("an unknown source was accepted")


# -- where the project is -------------------------------------------------

def _current_format_run(*argv):
    """The CLI on an analyzer JSON of the current shape: no legacy ``file``,
    the schematic under ``inputs.source_files``, the JSON in a timestamped
    run directory inside the project."""
    proj = tempfile.mkdtemp()
    run = os.path.join(proj, "analysis", "2026-10-08_120000")
    os.makedirs(run)
    analysis = os.path.join(run, "schematic.json")
    with open(analysis, "w") as fh:
        json.dump({"inputs": {"source_files": [os.path.join(proj, "board.kicad_sch")]},
                   "bom": [{"mpn": "TPS62840DLCR", "references": ["U1"]}]}, fh)
    saved = sys.argv
    sys.argv = ["lifecycle_audit.py", analysis, "--only", "digikey",
                "-o", os.path.join(tempfile.mkdtemp(), "out.json"), *argv]
    try:
        with _sources(digikey=_answers("Active")):
            lifecycle_audit.main()
    finally:
        sys.argv = saved
    return proj, run


def test_the_project_comes_from_the_schematic_not_the_analysis_run():
    """Without the legacy field the JSON's own directory was taken as the
    project, so every timestamped run got a fresh cache and its own table."""
    proj, run = _current_format_run()
    assert os.path.exists(os.path.join(proj, "analysis", "lifecycle_cache.json"))
    assert os.path.exists(os.path.join(proj, "lifecycle.md"))
    assert sorted(os.listdir(run)) == ["schematic.json"]


def test_project_and_table_can_be_given_outright():
    elsewhere = tempfile.mkdtemp()
    table = os.path.join(tempfile.mkdtemp(), "parts.md")
    proj, run = _current_format_run("--project", elsewhere, "--table", table)
    assert os.path.exists(os.path.join(elsewhere, "analysis", "lifecycle_cache.json"))
    assert os.path.exists(table)
    assert not os.path.exists(os.path.join(proj, "lifecycle.md"))


def _limiter_floor(*argv):
    """The minimum per-source interval the CLI handed the rate limiter."""
    seen = []
    real = lifecycle_audit._RateLimiter

    class Spy(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen.append(self.min_interval)
    lifecycle_audit._RateLimiter = Spy
    try:
        _run_cli("--no-cache", *argv)
    finally:
        lifecycle_audit._RateLimiter = real
    return seen


def test_delay_reaches_the_per_source_limiter():
    """--delay was parsed and then ignored by every lifecycle query."""
    assert _limiter_floor("--delay", "2.5") == [2.5]


def test_the_default_delay_is_none():
    assert _limiter_floor() == [0.0]


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



def test_alternatives_read_the_current_flat_lcsc_response():
    """jlcsearch now returns fields flat ("mfr" is the part number). The
    alternatives search read only extra.mpn, so against the live API every
    component's MPN was empty and LCSC never offered an alternative."""
    lcsc = {"components": [{"mfr": "AP2112K-3.3TRG2", "lcsc": "C51118",
                            "package": "SOT-23-5", "stock": 120}]}
    with _no_credentials(), _transport(lcsc):
        alts = lifecycle_audit.find_alternatives("AP2112K-3.3TRG1", ["lcsc"], delay=0)
    assert [a["mpn"] for a in alts] == ["AP2112K-3.3TRG2"]
    assert alts[0]["lcsc_stock"] == 120



def test_a_nested_manufacturer_object_yields_its_name():
    """The nested response carries the manufacturer as {"id", "name"}; it was
    being stringified whole into a Python repr."""
    lcsc = {"components": [{"stock": 5, "extra": {
        "mpn": "GRM188R71C104KA01D", "manufacturer": {"id": 4, "name": "Murata Electronics"}}}]}
    with _no_credentials(), _transport(lcsc):
        alts = lifecycle_audit.find_alternatives("GRM188R71C104KA01J", ["lcsc"], delay=0)
    assert alts[0]["manufacturer"] == "Murata Electronics"


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
