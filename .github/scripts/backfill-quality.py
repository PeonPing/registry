#!/usr/bin/env python3
"""backfill-quality.py — one-time grade of the whole catalog into index.json.

Grades every listed pack with the same pipeline the gate and the persist job
use, maps each verdict to the quality enum, and writes the tiers into index.json
in a single serialization-preserving pass (reuses the U4 write-back). It is
non-destructive: entries are only updated, never removed. A pack that grades
below the bar is recorded as 'flagged' and listed in the summary for maintainer
review, not delisted.

Intended to run as a workflow_dispatch job that writes to a branch and opens a
PR, so a maintainer reviews the catalog-wide change before it lands on main.

Resume: by default, packs already graded (quality is not 'unreviewed') are
skipped, so a re-run converges. --force-regrade re-touches every pack, for use
after a threshold change when the default skip would grade nothing.

Usage:
  python3 backfill-quality.py --index index.json [--workers 8] [--force-regrade]
                              [--limit N] [--packs a,b] [--summary-json FILE]

Dependencies: Python 3.8+, ffmpeg/ffprobe. No pip packages.
"""

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Load the checker module by path and reuse its grading + write-back helpers.
_CHECKER_PATH = Path(__file__).resolve().parent / "quality-check.py"
_spec = importlib.util.spec_from_file_location("quality_check", _CHECKER_PATH)
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)

DEFAULT_QUALITY = "unreviewed"


def select_targets(packs, force_regrade=False, limit=None, names=None):
    """Choose which entries to grade.

    Without --force-regrade, already-graded packs (quality not 'unreviewed') are
    skipped so a re-run converges. --packs and --limit scope smaller runs and are
    applied before the resume skip.
    """
    targets = packs
    if names:
        by_name = {p.get("name"): p for p in packs}
        targets = [by_name[n] for n in names if n in by_name]
    elif limit:
        targets = packs[:limit]

    if not force_regrade:
        targets = [p for p in targets if p.get("quality", DEFAULT_QUALITY) == DEFAULT_QUALITY]
    return targets


def grade_entry(entry):
    """Download and grade one pack. Returns a result dict, never raises."""
    name = entry.get("name", "?")
    dest = tempfile.mkdtemp(prefix=f"backfill-{name}-")
    try:
        ok = qc.download_pack(
            entry["source_repo"],
            entry.get("source_ref", "main"),
            entry.get("source_path", "."),
            dest,
        )
        if not ok:
            return {"name": name, "error": "download failed"}
        results = qc.check_pack(dest)
        verdict = results.get("verdict", "REJECTED")
        return {"name": name, "verdict": verdict, "quality": qc.verdict_to_quality(verdict)}
    except Exception as exc:  # noqa: BLE001 — one bad pack must not sink the batch
        return {"name": name, "error": str(exc)}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def apply_grades(index_data, results, enum):
    """Apply graded tiers into a loaded index, returning a maintainer summary.

    Non-destructive: only updates entries via set_entry_quality. An out-of-enum
    value or a grading error leaves the pack untouched (recorded under errored).
    """
    applied = 0
    tier_counts = {}
    flagged = []
    errored = []

    for r in results:
        name = r["name"]
        if "error" in r:
            errored.append(name)
            continue
        quality = r["quality"]
        if quality not in enum:
            errored.append(name)
            continue
        try:
            if qc.set_entry_quality(index_data, name, quality):
                applied += 1
        except KeyError:
            errored.append(name)
            continue
        tier_counts[quality] = tier_counts.get(quality, 0) + 1
        if quality == "flagged":
            flagged.append(name)

    return {
        "graded": len([r for r in results if "error" not in r]),
        "applied": applied,
        "tier_counts": tier_counts,
        "flagged": sorted(flagged),
        "errored": sorted(errored),
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill pack quality tiers into index.json")
    parser.add_argument("--index", default="index.json", help="Path to index.json")
    parser.add_argument("--schema", default="schema/registry-v1.schema.json",
                        help="Schema whose quality enum the written values are checked against")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent packs")
    parser.add_argument("--force-regrade", action="store_true",
                        help="Re-grade already-graded packs (e.g. after a threshold change)")
    parser.add_argument("--limit", type=int, help="Grade only the first N packs (smoke test)")
    parser.add_argument("--packs", help="Comma-separated pack names to grade")
    parser.add_argument("--summary-json", help="Write the maintainer summary here")
    args = parser.parse_args()

    with open(args.index, encoding="utf-8") as f:
        index_data = json.load(f)
    packs = qc._index_packs(index_data)

    names = [n.strip() for n in args.packs.split(",") if n.strip()] if args.packs else None
    targets = select_targets(packs, args.force_regrade, args.limit, names)

    print(f"Grading {len(targets)} of {len(packs)} packs "
          f"(force_regrade={args.force_regrade})...", file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(grade_entry, e): e for e in targets}
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            sys.stderr.write(f"\r  graded {done}/{len(targets)} packs")
            sys.stderr.flush()
    sys.stderr.write("\n")

    enum = qc.load_quality_enum(args.schema)
    summary = apply_grades(index_data, results, enum)

    # One serialization-preserving write (reuses the U4 byte-stable writer).
    text = qc.serialize_index(index_data)
    json.loads(text)  # guard: never write text that does not parse back
    with open(args.index, "w", encoding="utf-8") as f:
        f.write(text)

    print(json.dumps(summary, indent=2))

    sys.stderr.write(
        f"\nApplied {summary['applied']} tiers. "
        f"Flagged for maintainer review: {len(summary['flagged'])}\n"
    )
    for name in summary["flagged"]:
        sys.stderr.write(f"  flagged: {name}\n")
    if summary["errored"]:
        sys.stderr.write(f"\n{len(summary['errored'])} packs could not be graded (left unreviewed):\n")
        for name in summary["errored"]:
            sys.stderr.write(f"  errored: {name}\n")

    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
