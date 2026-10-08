#!/usr/bin/env python3
"""Rescore the lifecycle table from the cache, touching no network.

Two things change a part's score without anyone re-querying a distributor: a
person filling in what they found, and the scoring model itself being
corrected. Both happened on this project inside an hour, and re-running the
audit to pick them up would spend part lookups against an evaluation licence
that allows a hundred for the life of the key.

So this reads the answers already on disk and the table already written, and
rewrites the computed columns. It imports the same compute() the audit uses
rather than reimplementing it, because a second scoring path that drifts from
the first is worse than no second path.

    python3 lifecycle_recalc.py <project-dir> [--cache PATH] [--table PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lifecycle_audit import (  # noqa: E402
    _default_cache_path, _default_table_path, _normalize_status,
    status_capable_count,
)
from lifecycle_cache import compute, mpn_key  # noqa: E402
from lifecycle_table import (  # noqa: E402
    read_table, render_table, user_row, is_departed,
)

SEP = "\x1f"


def recorded_sources(cache_path: str) -> list[str] | None:
    """The source set the audit that wrote this cache asked, if it said."""
    try:
        with open(cache_path) as fh:
            sources = json.load(fh).get("sources")
    except (OSError, ValueError, AttributeError):
        return None
    return list(sources) if isinstance(sources, list) and sources else None


def statuses_from_cache(cache_path: str) -> dict[str, dict[str, str]]:
    """Every cached answer, as {mpn_key(mpn): {source: normalised status}}."""
    with open(cache_path) as fh:
        blob = json.load(fh)
    out: dict[str, dict[str, str]] = {}
    for key, row in (blob.get("entries") or {}).items():
        if SEP not in key:
            continue
        mpn, source = key.split(SEP, 1)
        data = (row or {}).get("data")
        if not isinstance(data, dict):
            continue
        status = _normalize_status(data.get("status"))
        if status != "unknown":
            out.setdefault(mpn, {})[source] = status
    return out


def default_cache_path(project_dir: str) -> str:
    """The cache the audit would have written for this project.

    Derived from the audit's own _default_cache_path rather than spelled out
    here, because a second copy is what drifted: recalc looked only under
    .pipeline/ while the audit writes <project>/analysis/. The audit is handed
    the schematic's directory, so where the schematic sits in .pipeline/ the
    cache is under that instead, and both places are tried.
    """
    root = os.path.abspath(project_dir)
    candidates = [_default_cache_path(root)]
    if os.path.basename(root) != ".pipeline":
        candidates.append(_default_cache_path(os.path.join(root, ".pipeline")))
    return next((c for c in candidates if os.path.exists(c)), candidates[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_dir")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--table", default=None)
    ap.add_argument("--sources", default=None,
                    help="Comma-separated source set the scores assume "
                         "(default: the set the audit recorded in the cache, "
                         "else the audit's default sources)")
    args = ap.parse_args()

    cache_path = args.cache or default_cache_path(args.project_dir)
    table_path = args.table or _default_table_path(args.project_dir)

    # An unreadable cache used to come back as an empty dict, and the loop
    # below would then rescore every row to zero and demand an acknowledgement
    # for the whole BOM. Scoring nothing is not the same as scoring badly:
    # without the cache there is nothing to recalculate from, so stop.
    try:
        cached = statuses_from_cache(cache_path)
    except (OSError, ValueError) as exc:
        print("cannot read %s: %s" % (cache_path, exc), file=sys.stderr)
        return 1
    if not cached:
        print("no cached statuses in %s - nothing to recalculate from"
              % cache_path, file=sys.stderr)
        return 1

    rows = read_table(table_path)
    if not rows:
        print("no table at %s" % table_path, file=sys.stderr)
        return 1

    # Counted by the audit's own rule, against the sources the audit actually
    # asked. Counting every status-capable source instead included Nexar,
    # which a default audit never queries, so one active DigiKey answer the
    # audit scored 80 came back from here as 57 and awaiting acknowledgement.
    sources = ([s.strip() for s in args.sources.split(",") if s.strip()]
               if args.sources else recorded_sources(cache_path))
    capable = status_capable_count(sources)

    changed = 0
    for mpn, row in rows.items():
        # Cache keys are normalised; the table keeps the part's own spelling.
        per_source = cached.get(mpn_key(mpn), {})
        scored = compute(per_source, capable, user_row(row))
        before = (row.get("Computed"), row.get("Ack?"))
        row["Status"] = scored.get("status", "unknown")
        row["Computed"] = "%.0f" % scored["computed"]
        row["Raw"] = "%.2f" % scored["raw"]
        row["Sources"] = "%d/%d" % (scored["responding"], scored["capable"])
        row["Ack?"] = ("YES" if (scored["needs_ack"]
                                 and not is_departed(row)
                                 and not (row.get("Acknowledged") or "").strip())
                       else "")
        if (row["Computed"], row["Ack?"]) != before:
            changed += 1

    with open(table_path, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))

    outstanding = [m for m, r in rows.items() if r.get("Ack?") == "YES"]
    print("recalculated %d rows from cache, %d changed, no network touched"
          % (len(rows), changed))
    print("outstanding acknowledgements: %d%s"
          % (len(outstanding),
             (" — " + ", ".join(sorted(outstanding))) if outstanding else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
