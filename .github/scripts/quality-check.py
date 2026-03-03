#!/usr/bin/env python3
"""quality-check.py — Audio quality gate for CESP sound pack submissions.

Downloads a pack from its source repo and runs automated quality checks on
every audio file. Produces a three-tier verdict:

  GOLD     — All checks pass. No issues found.
  SILVER   — No blocking issues, but warnings exist. Pack is accepted;
             author is asked to address warnings in a future release.
  REJECTED — Blocking issues found. Pack cannot be merged until fixed.

Usage:
  python3 quality-check.py <pack-dir>                    # Check a local pack
  python3 quality-check.py --from-index index.json pack1  # Download + check

Dependencies: Python 3.8+, ffmpeg/ffprobe (pre-installed on GitHub runners).
No pip packages required.

Exit codes:
  0 — GOLD or SILVER (pack accepted)
  1 — REJECTED (pack blocked)
  2 — Error (script failure, missing ffmpeg, etc.)
"""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import defaultdict

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Thresholds — calibrated against 164 registry packs (~4,500 audio files).
# See QUALITY-CHECK-ANALYSIS.md for methodology and justification.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# BLOCK thresholds — any file hitting these blocks the entire pack.
LEADING_SILENCE_BLOCK_MS = 2000   # > 2 seconds of leading dead air
TRAILING_SILENCE_BLOCK_MS = 2000  # > 2 seconds of trailing dead air
LUFS_BLOCK_FLOOR = -70.0          # Effectively silent / broken file (-inf parses as -inf)
SAMPLE_RATE_BLOCK_HZ = 8000      # Telephone quality
DURATION_MAX_BLOCK_S = 20.0      # Way too long for any CESP category
DURATION_MIN_BLOCK_S = 0.1       # Effectively empty

# WARN thresholds — file-level warnings, accumulated per pack.
CLIP_WARN_DBTP = -0.5            # Volume is very high
DURATION_LONG_WARN_S = 5.0       # Long for a notification sound
LEADING_SILENCE_WARN_MS = 500    # Noticeable pause before audio starts
TRAILING_SILENCE_WARN_MS = 500   # Noticeable dead air after audio ends
LUFS_QUIET_WARN = -35.0          # Very quiet (un-normalized movie dialogue)
LUFS_LOUD_WARN = -8.0            # Very loud
BITRATE_WARN_KBPS = 64           # Low-effort encoding (lossy formats only)
SAMPLE_RATE_WARN_HZ = 16000      # Low but functional

# Silence detection sensitivity
SILENCE_THRESHOLD_DB = -35


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Audio analysis helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ffprobe_info(filepath):
    """Return (duration_s, sample_rate_hz, bitrate_bps, codec_name) or None."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=sample_rate,bit_rate,codec_name",
            "-show_entries", "format=duration,bit_rate",
            "-of", "json", filepath
        ], stderr=subprocess.DEVNULL, text=True)
        d = json.loads(out)
        fmt = d.get("format", {})
        streams = d.get("streams", [])
        s = streams[0] if streams else {}
        return (
            float(fmt.get("duration", 0)),
            int(s.get("sample_rate", 0)),
            int(fmt.get("bit_rate", 0) or 0),
            s.get("codec_name", ""),
        )
    except Exception:
        return None


def silence_intervals(filepath):
    """Return list of (start, end_or_None) silence intervals."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", filepath,
             "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d=0.05",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )
        out = result.stderr
    except Exception:
        return []

    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([0-9.]+)", out)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([0-9.]+)", out)]

    intervals = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        intervals.append((s, e))
    return intervals


def loudnorm_stats(filepath):
    """Return (true_peak_dBTP, integrated_LUFS) or (None, None)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", filepath,
             "-af", "loudnorm=print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )
        out = result.stderr
    except Exception:
        return None, None

    brace_start = out.rfind("{")
    brace_end = out.rfind("}")
    if brace_start < 0 or brace_end < 0:
        return None, None
    try:
        d = json.loads(out[brace_start:brace_end + 1])
        tp_str = d.get("input_tp", "-99")
        lufs_str = d.get("input_i", "-99")
        tp = float("-inf") if tp_str == "-inf" else float(tp_str)
        lufs = float("-inf") if lufs_str == "-inf" else float(lufs_str)
        return tp, lufs
    except Exception:
        return None, None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Per-file classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_file(filepath):
    """Analyze one audio file. Returns dict with blocks, warns, and stats."""
    fname = os.path.basename(filepath)
    blocks = []
    warns = []
    stats = {}

    probe = ffprobe_info(filepath)
    if not probe:
        blocks.append("not a valid audio file")
        return {"file": fname, "blocks": blocks, "warns": warns, "stats": stats}

    duration, sample_rate, bitrate, codec = probe
    br_kbps = bitrate // 1000 if bitrate else 0
    stats = {
        "duration": round(duration, 3),
        "sample_rate": sample_rate,
        "bitrate_kbps": br_kbps,
        "codec": codec,
    }

    # Duration
    if duration > DURATION_MAX_BLOCK_S:
        blocks.append(f"too long ({duration:.1f}s, max {DURATION_MAX_BLOCK_S:.0f}s)")
    elif duration > DURATION_LONG_WARN_S:
        warns.append(f"long for a notification sound ({duration:.1f}s)")
    if duration < DURATION_MIN_BLOCK_S:
        blocks.append(f"too short ({duration:.2f}s, min {DURATION_MIN_BLOCK_S}s)")

    # Sample rate
    if sample_rate < SAMPLE_RATE_BLOCK_HZ:
        blocks.append(f"very low audio quality (sample rate {sample_rate} Hz, min {SAMPLE_RATE_BLOCK_HZ} Hz)")
    elif sample_rate < SAMPLE_RATE_WARN_HZ:
        warns.append(f"low audio quality (sample rate {sample_rate} Hz)")

    # Bitrate (lossy only)
    if codec in ("mp3", "vorbis", "opus", "aac") and 0 < br_kbps < BITRATE_WARN_KBPS:
        warns.append(f"low audio quality (bitrate {br_kbps} kbps)")

    # Silence analysis
    intervals = silence_intervals(filepath)

    # Leading silence: first interval starts at ~0
    if intervals and intervals[0][0] < 0.01 and intervals[0][1] is not None:
        lead_ms = int(intervals[0][1] * 1000)
        stats["leading_silence_ms"] = lead_ms
        if lead_ms > LEADING_SILENCE_BLOCK_MS:
            blocks.append(f"too much dead air at the start ({lead_ms} ms)")
        elif lead_ms > LEADING_SILENCE_WARN_MS:
            warns.append(f"dead air at the start ({lead_ms} ms)")

    # Trailing silence: last interval extends to EOF
    if intervals and duration > 0:
        last_start, last_end = intervals[-1]
        extends_to_eof = (
            last_end is None
            or last_start > last_end
            or abs(duration - last_end) < 0.05
        )
        if extends_to_eof:
            trail_ms = int((duration - last_start) * 1000)
            stats["trailing_silence_ms"] = trail_ms
            if trail_ms > TRAILING_SILENCE_BLOCK_MS:
                blocks.append(f"too much dead air at the end ({trail_ms} ms)")
            elif trail_ms > TRAILING_SILENCE_WARN_MS:
                warns.append(f"dead air at the end ({trail_ms} ms)")

    # Loudness + peak
    tp, lufs = loudnorm_stats(filepath)
    if tp is not None:
        stats["true_peak_dbtp"] = round(tp, 1) if tp != float("-inf") else "-inf"
        if tp >= CLIP_WARN_DBTP:
            warns.append(f"volume is very high, may sound distorted on some devices")

    if lufs is not None:
        stats["lufs"] = round(lufs, 1) if lufs != float("-inf") else "-inf"
        # loudnorm needs >= 400ms to compute integrated loudness (ITU-R BS.1770).
        # Files shorter than that report -inf, which is not a real silence reading.
        if lufs == float("-inf") and duration < 0.5:
            pass  # skip — too short for reliable loudness measurement
        elif lufs == float("-inf") or lufs < LUFS_BLOCK_FLOOR:
            blocks.append(f"file is silent or nearly silent")
        elif lufs < LUFS_QUIET_WARN:
            warns.append(f"very quiet compared to other sounds")
        elif lufs > LUFS_LOUD_WARN:
            warns.append(f"very loud compared to other sounds")

    return {"file": fname, "blocks": blocks, "warns": warns, "stats": stats}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pack-level analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_pack(pack_dir):
    """Run all quality checks on a local pack directory.

    Returns a results dict suitable for JSON serialization:
    {
        "pack_name": "...",
        "display_name": "...",
        "verdict": "GOLD" | "SILVER" | "REJECTED",
        "total_files": N,
        "total_blocks": N,
        "total_warns": N,
        "block_summary": {"clipping": N, "silence": N, ...},
        "warn_summary": {"quiet": N, "hot_signal": N, ...},
        "files": [ { "file": "...", "blocks": [...], "warns": [...], "stats": {...} }, ... ]
    }
    """
    pack_dir = pack_dir.rstrip("/")
    manifest_path = os.path.join(pack_dir, "openpeon.json")

    if not os.path.isfile(manifest_path):
        return {
            "error": f"No openpeon.json in {pack_dir}",
            "verdict": "REJECTED",
        }

    with open(manifest_path) as f:
        manifest = json.load(f)

    pack_name = manifest.get("name", os.path.basename(pack_dir))
    display_name = manifest.get("display_name", pack_name)

    sounds_dir = os.path.join(pack_dir, "sounds")
    if not os.path.isdir(sounds_dir):
        return {
            "pack_name": pack_name,
            "display_name": display_name,
            "error": "No sounds/ directory",
            "verdict": "REJECTED",
        }

    audio_files = sorted(
        os.path.relpath(os.path.join(root, f), sounds_dir)
        for root, _dirs, files in os.walk(sounds_dir)
        for f in files
        if f.lower().endswith((".mp3", ".wav", ".ogg"))
    )

    if not audio_files:
        return {
            "pack_name": pack_name,
            "display_name": display_name,
            "error": "No audio files in sounds/",
            "verdict": "REJECTED",
        }

    # Analyze each file
    file_results = []
    total_blocks = 0
    total_warns = 0
    block_summary = defaultdict(int)
    warn_summary = defaultdict(int)

    for i, fname in enumerate(audio_files):
        filepath = os.path.join(sounds_dir, fname)
        display = fname[:50]
        sys.stderr.write(f"\r  [{i+1}/{len(audio_files)}] {display:<50}")
        sys.stderr.flush()

        result = classify_file(filepath)
        file_results.append(result)
        total_blocks += len(result["blocks"])
        total_warns += len(result["warns"])

        for b in result["blocks"]:
            if "distorted" in b:
                block_summary["distorted"] += 1
            elif "dead air at the start" in b:
                block_summary["silence_at_start"] += 1
            elif "dead air at the end" in b:
                block_summary["silence_at_end"] += 1
            elif "silent or nearly silent" in b:
                block_summary["silent"] += 1
            elif "too long" in b or "too short" in b:
                block_summary["duration"] += 1
            elif "audio quality" in b:
                block_summary["low_quality"] += 1
            else:
                block_summary["other"] += 1

        for w in result["warns"]:
            if "very quiet" in w:
                warn_summary["very_quiet"] += 1
            elif "very loud" in w:
                warn_summary["very_loud"] += 1
            elif "dead air at the start" in w:
                warn_summary["silence_at_start"] += 1
            elif "dead air at the end" in w:
                warn_summary["silence_at_end"] += 1
            elif "volume is very high" in w:
                warn_summary["high_volume"] += 1
            elif "audio quality" in w:
                warn_summary["low_quality"] += 1
            else:
                warn_summary["other"] += 1

    sys.stderr.write("\r" + " " * 70 + "\r")

    # Determine verdict
    if total_blocks > 0:
        verdict = "REJECTED"
    elif total_warns > 0:
        verdict = "SILVER"
    else:
        verdict = "GOLD"

    return {
        "pack_name": pack_name,
        "display_name": display_name,
        "verdict": verdict,
        "total_files": len(audio_files),
        "total_blocks": total_blocks,
        "total_warns": total_warns,
        "block_summary": dict(block_summary),
        "warn_summary": dict(warn_summary),
        "files": file_results,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pack download (for CI use)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def download_pack(source_repo, source_ref, source_path, dest_dir):
    """Download a pack from GitHub to a local directory."""
    tarball_url = f"https://github.com/{source_repo}/archive/{source_ref}.tar.gz"
    path = source_path.strip("/") if source_path else "."

    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        urllib.request.urlretrieve(tarball_url, tmp.name)
        with tarfile.open(tmp.name, "r:gz") as tf:
            members = tf.getmembers()
            if not members:
                return False

            top = members[0].name.split("/")[0]
            prefix = f"{top}/{path}/" if path != "." else f"{top}/"

            os.makedirs(dest_dir, exist_ok=True)
            for m in members:
                if m.name.startswith(prefix) and m.name != prefix.rstrip("/"):
                    rel = m.name[len(prefix):]
                    if not rel:
                        continue
                    dest = os.path.join(dest_dir, rel)
                    if m.isdir():
                        os.makedirs(dest, exist_ok=True)
                    elif m.isfile():
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with tf.extractfile(m) as src:
                            with open(dest, "wb") as dst:
                                dst.write(src.read())

        return os.path.isfile(os.path.join(dest_dir, "openpeon.json"))
    except Exception as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return False
    finally:
        os.unlink(tmp.name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output formatting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def format_markdown(results):
    """Format results as a GitHub-flavored Markdown section."""
    lines = []
    verdict = results.get("verdict", "REJECTED")
    display = results.get("display_name", results.get("pack_name", "?"))
    total_files = results.get("total_files", 0)
    total_blocks = results.get("total_blocks", 0)
    total_warns = results.get("total_warns", 0)

    if verdict == "GOLD":
        icon = ":star:"
        label = "GOLD — all quality checks passed"
    elif verdict == "SILVER":
        icon = ":white_check_mark:"
        label = f"SILVER — accepted with {total_warns} {'warning' if total_warns == 1 else 'warnings'}"
    else:
        icon = ":x:"
        label = f"REJECTED — {total_blocks} blocking {'issue' if total_blocks == 1 else 'issues'} found"

    lines.append(f"### {icon} Audio Quality: {label}\n")
    lines.append(f"**{display}** — {total_files} audio files analyzed\n")

    # Error shortcut
    if "error" in results:
        lines.append(f"> Error: {results['error']}\n")
        return "\n".join(lines)

    # Block details
    if total_blocks > 0:
        block_summary = results.get("block_summary", {})
        lines.append("#### Blocking Issues\n")
        lines.append("| Issue | Count |")
        lines.append("|---|---|")
        for issue, count in sorted(block_summary.items(), key=lambda x: -x[1]):
            lines.append(f"| {issue.replace('_', ' ').title()} | {count} |")
        lines.append("")

        # List affected files (up to 20)
        blocked_files = [f for f in results.get("files", []) if f.get("blocks")]
        lines.append("<details><summary>Affected files</summary>\n")
        for f in blocked_files[:20]:
            for b in f["blocks"]:
                lines.append(f"- `{f['file']}`: {b}")
        if len(blocked_files) > 20:
            lines.append(f"- ... and {len(blocked_files) - 20} more")
        lines.append("\n</details>\n")

    # Warn details
    if total_warns > 0:
        warn_summary = results.get("warn_summary", {})
        lines.append("#### Warnings\n")
        lines.append("| Issue | Count |")
        lines.append("|---|---|")
        for issue, count in sorted(warn_summary.items(), key=lambda x: -x[1]):
            lines.append(f"| {issue.replace('_', ' ').title()} | {count} |")
        lines.append("")

    # Threshold reference
    lines.append("<details><summary>Threshold reference</summary>\n")
    lines.append("| Check | Block | Warn |")
    lines.append("|---|---|---|")
    lines.append(f"| Volume (true peak) | — | >= {CLIP_WARN_DBTP:+.1f} dBTP |")
    lines.append(f"| Dead air at start | > {LEADING_SILENCE_BLOCK_MS} ms | > {LEADING_SILENCE_WARN_MS} ms |")
    lines.append(f"| Dead air at end | > {TRAILING_SILENCE_BLOCK_MS} ms | > {TRAILING_SILENCE_WARN_MS} ms |")
    lines.append(f"| Loudness (LUFS) | < {LUFS_BLOCK_FLOOR} | < {LUFS_QUIET_WARN} or > {LUFS_LOUD_WARN} |")
    lines.append(f"| Bitrate | — | < {BITRATE_WARN_KBPS} kbps |")
    lines.append(f"| Sample rate | < {SAMPLE_RATE_BLOCK_HZ} Hz | < {SAMPLE_RATE_WARN_HZ} Hz |")
    lines.append(f"| Duration | > {DURATION_MAX_BLOCK_S}s or < {DURATION_MIN_BLOCK_S}s | > {DURATION_LONG_WARN_S}s |")
    lines.append("\n</details>")

    return "\n".join(lines)


def format_console(results):
    """Format results for terminal output."""
    verdict = results.get("verdict", "REJECTED")
    display = results.get("display_name", results.get("pack_name", "?"))

    print(f"\n{'━' * 60}")
    print(f"  {display}")
    print(f"{'━' * 60}")

    if "error" in results:
        print(f"  ERROR: {results['error']}")
        print(f"  Verdict: REJECTED")
        return

    total_files = results.get("total_files", 0)
    total_blocks = results.get("total_blocks", 0)
    total_warns = results.get("total_warns", 0)

    # File-level details
    for f in results.get("files", []):
        if f.get("blocks") or f.get("warns"):
            print(f"\n  {f['file']}")
            for b in f.get("blocks", []):
                print(f"    BLOCK  {b}")
            for w in f.get("warns", []):
                print(f"    WARN   {w}")

    print(f"\n{'─' * 60}")
    print(f"  Files: {total_files}  |  Blocks: {total_blocks}  |  Warnings: {total_warns}")
    print(f"  Verdict: {verdict}")
    print(f"{'━' * 60}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    import argparse

    parser = argparse.ArgumentParser(description="CESP sound pack audio quality checker")
    parser.add_argument("pack_dir", nargs="?", help="Local pack directory to check")
    parser.add_argument("--from-index", metavar="INDEX_JSON",
                        help="Read pack info from index.json and download")
    parser.add_argument("--pack-name", metavar="NAME",
                        help="Pack name to check (with --from-index)")
    parser.add_argument("--output-json", metavar="FILE",
                        help="Write results as JSON to this file")
    parser.add_argument("--output-markdown", metavar="FILE",
                        help="Write results as Markdown to this file")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress console output (use with --output-*)")

    args = parser.parse_args()

    # Verify ffmpeg is available
    try:
        subprocess.check_output(["ffprobe", "-version"], stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("Error: ffprobe not found. Install ffmpeg.", file=sys.stderr)
        sys.exit(2)

    pack_dir = args.pack_dir

    # Download mode
    if args.from_index and args.pack_name:
        with open(args.from_index) as f:
            index = json.load(f)
        packs = index if isinstance(index, list) else index.get("packs", [])
        entry = next((p for p in packs if p["name"] == args.pack_name), None)
        if not entry:
            print(f"Pack '{args.pack_name}' not found in {args.from_index}", file=sys.stderr)
            sys.exit(2)

        pack_dir = tempfile.mkdtemp(prefix=f"qc-{args.pack_name}-")
        print(f"Downloading {args.pack_name} from {entry['source_repo']}@{entry.get('source_ref', 'main')}...")
        ok = download_pack(
            entry["source_repo"],
            entry.get("source_ref", "main"),
            entry.get("source_path", "."),
            pack_dir,
        )
        if not ok:
            print(f"Failed to download pack", file=sys.stderr)
            sys.exit(2)

    if not pack_dir:
        parser.print_help()
        sys.exit(2)

    # Run checks
    results = check_pack(pack_dir)

    # Output
    if not args.quiet:
        format_console(results)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(results, f, indent=2, default=str)

    if args.output_markdown:
        md = format_markdown(results)
        with open(args.output_markdown, "w") as f:
            f.write(md)

    # Exit code
    verdict = results.get("verdict", "REJECTED")
    if verdict == "REJECTED":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
