#!/usr/bin/env python3
"""calibrate-loudness.py — fit the pack-level loudness thresholds from the catalog.

The pack-level loudness checks (U3) demote a pack from GOLD to SILVER when its
clips are too inconsistent (wide loudness spread) or uniformly too quiet (low
pack median). Those two thresholds must come from the catalog's own
distribution, not a guess, so the backfill does not mass-mislabel reasonable
packs. This script gathers that distribution.

For each pack it downloads the source tarball, runs the existing audio analysis
(reusing quality-check.py's download_pack + check_pack), and records the pack's
loudness spread and median produced by aggregate_pack_loudness. It then prints
the distribution (percentiles) of per-pack spread and per-pack median, names the
outliers, and suggests threshold candidates at standard percentiles. The chosen
values and rationale get written up by hand in QUALITY-CHECK-ANALYSIS.md.

This is a read-only analysis tool. It needs no repository write access.

Usage:
  python3 calibrate-loudness.py --index index.json                  # full catalog
  python3 calibrate-loudness.py --index index.json --limit 10       # first 10 (timing)
  python3 calibrate-loudness.py --index index.json --sample 40      # random 40
  python3 calibrate-loudness.py --index index.json --packs arnold,abbot
  python3 calibrate-loudness.py --index index.json --workers 6 --output-json dist.json

Dependencies: Python 3.8+, ffmpeg/ffprobe. No pip packages.
"""

import argparse
import importlib.util
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Load the checker module by path (hyphen + dotted dir make it non-importable by name).
_CHECKER_PATH = Path(__file__).resolve().parent / "quality-check.py"
_spec = importlib.util.spec_from_file_location("quality_check", _CHECKER_PATH)
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)


def load_packs(index_path):
    """Return the list of pack entries from an index.json (list or {'packs': [...]})."""
    with open(index_path) as f:
        index = json.load(f)
    return index if isinstance(index, list) else index.get("packs", [])


def measure_pack(entry):
    """Download and grade one pack. Returns a result dict (never raises)."""
    name = entry.get("name", "?")
    dest = tempfile.mkdtemp(prefix=f"cal-{name}-")
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
        metrics = results.get("pack_metrics", {})
        return {
            "name": name,
            "verdict": results.get("verdict"),
            "total_files": results.get("total_files", 0),
            "loudness_spread": metrics.get("loudness_spread"),
            "loudness_median": metrics.get("loudness_median"),
            "measured_clips": metrics.get("measured_clips", 0),
        }
    except Exception as exc:  # noqa: BLE001 — calibration must survive one bad pack
        return {"name": name, "error": str(exc)}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def percentile(sorted_values, pct):
    """Linear-interpolation percentile (pct in 0..100) over a pre-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def describe(values):
    """Summary stats + standard percentiles for a list of numbers."""
    if not values:
        return None
    s = sorted(values)
    pcts = [5, 10, 25, 50, 75, 90, 95, 99]
    return {
        "count": len(s),
        "min": round(min(s), 1),
        "max": round(max(s), 1),
        "mean": round(statistics.mean(s), 1),
        "percentiles": {p: round(percentile(s, p), 1) for p in pcts},
    }


def run(entries, workers):
    """Measure all entries concurrently, returning (results, elapsed_seconds)."""
    results = []
    start = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(measure_pack, e): e for e in entries}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            sys.stderr.write(f"\r  graded {done}/{len(entries)} packs")
            sys.stderr.flush()
    sys.stderr.write("\n")
    return results, time.monotonic() - start


def main():
    parser = argparse.ArgumentParser(description="Calibrate pack-level loudness thresholds")
    parser.add_argument("--index", default="index.json", help="Path to index.json")
    parser.add_argument("--limit", type=int, help="Grade only the first N packs (timing)")
    parser.add_argument("--sample", type=int, help="Grade a random N-pack sample")
    parser.add_argument("--packs", help="Comma-separated pack names to grade")
    parser.add_argument("--seed", type=int, default=1770, help="Random seed for --sample")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent packs")
    parser.add_argument("--output-json", help="Write full distribution + per-pack data here")
    args = parser.parse_args()

    entries = load_packs(args.index)
    by_name = {e.get("name"): e for e in entries}

    if args.packs:
        wanted = [n.strip() for n in args.packs.split(",") if n.strip()]
        entries = [by_name[n] for n in wanted if n in by_name]
        missing = [n for n in wanted if n not in by_name]
        if missing:
            print(f"Not in index: {', '.join(missing)}", file=sys.stderr)
    elif args.sample:
        rng = random.Random(args.seed)
        entries = rng.sample(entries, min(args.sample, len(entries)))
    elif args.limit:
        entries = entries[: args.limit]

    print(f"Grading {len(entries)} packs with {args.workers} workers...", file=sys.stderr)
    results, elapsed = run(entries, args.workers)

    graded = [r for r in results if "error" not in r]
    errored = [r for r in results if "error" in r]
    spreads = [r["loudness_spread"] for r in graded if r["loudness_spread"] is not None]
    medians = [r["loudness_median"] for r in graded if r["loudness_median"] is not None]

    spread_dist = describe(spreads)
    median_dist = describe(medians)

    # Threshold candidates: flag the uneven tail (high spread) and the quiet tail
    # (low median). p90 spread and p10 median are starting points; the final cut
    # is chosen and justified by hand in QUALITY-CHECK-ANALYSIS.md.
    suggestions = {}
    if spread_dist:
        suggestions["spread_threshold_candidates"] = {
            "p90": spread_dist["percentiles"][90],
            "p95": spread_dist["percentiles"][95],
        }
    if median_dist:
        suggestions["quiet_floor_candidates"] = {
            "p10": median_dist["percentiles"][10],
            "p5": median_dist["percentiles"][5],
        }

    # Per-pack timing helps size the U5 backfill against the 6h CI ceiling.
    total_files = sum(r.get("total_files", 0) for r in graded)
    timing = {
        "elapsed_s": round(elapsed, 1),
        "packs": len(results),
        "files": total_files,
        "s_per_pack": round(elapsed / len(results), 2) if results else None,
        "s_per_file": round(elapsed / total_files, 3) if total_files else None,
    }

    report = {
        "graded": len(graded),
        "errored": [r["name"] for r in errored],
        "spread_distribution": spread_dist,
        "median_distribution": median_dist,
        "suggestions": suggestions,
        "timing": timing,
        "per_pack": sorted(graded, key=lambda r: (r["loudness_spread"] is None, -(r["loudness_spread"] or 0))),
    }

    print(json.dumps(report, indent=2, default=str))

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(report, f, indent=2, default=str)

    # Human-facing tail callouts on stderr so they survive a piped stdout.
    if spread_dist:
        p90 = spread_dist["percentiles"][90]
        loud_outliers = [r for r in graded if (r["loudness_spread"] or 0) >= p90]
        sys.stderr.write(f"\nWidest-spread packs (>= p90 = {p90} LU):\n")
        for r in sorted(loud_outliers, key=lambda r: -(r["loudness_spread"] or 0))[:15]:
            sys.stderr.write(f"  {r['name']:<24} spread {r['loudness_spread']} LU, median {r['loudness_median']} LUFS\n")
    if median_dist:
        p10 = median_dist["percentiles"][10]
        quiet_outliers = [r for r in graded if r["loudness_median"] is not None and r["loudness_median"] <= p10]
        sys.stderr.write(f"\nQuietest packs (<= p10 median = {p10} LUFS):\n")
        for r in sorted(quiet_outliers, key=lambda r: (r["loudness_median"] or 0))[:15]:
            sys.stderr.write(f"  {r['name']:<24} median {r['loudness_median']} LUFS, spread {r['loudness_spread']} LU\n")


if __name__ == "__main__":
    main()
