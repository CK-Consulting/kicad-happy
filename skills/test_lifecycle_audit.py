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
