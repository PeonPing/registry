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
import shutil
import statistics
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

# Integrated loudness needs >= ~400ms of audio to be reliable (ITU-R BS.1770).
# Clips shorter than this report no trustworthy LUFS and are excluded from both
# the per-file loudness check and the pack-level loudness aggregation.
LOUDNESS_MIN_DURATION_S = 0.5

# Pack-level loudness WARN thresholds — calibrated against the full catalog's
# distribution. See QUALITY-CHECK-ANALYSIS.md. These are score-only: they demote
# GOLD to SILVER, never REJECTED.
LOUDNESS_SPREAD_WARN_LU = 15.0    # range between quietest and loudest clip (p90)
LOUDNESS_QUIET_PACK_LUFS = -26.0  # pack median at or below this reads as too quiet


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
        if lufs == float("-inf") and duration < LOUDNESS_MIN_DURATION_S:
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

def aggregate_pack_loudness(file_results):
    """Aggregate per-file loudness into pack-level spread and median.

    Pure function over the dicts ``classify_file`` returns, so it is testable
    without ffmpeg. Reads each file's ``stats["lufs"]`` and ``stats["duration"]``,
    which are heterogeneous: ``lufs`` is a float for a normal clip, the string
    ``"-inf"`` for a silent clip, and absent when loudnorm timed out or failed to
    parse. Three classes of clip carry no trustworthy integrated loudness and are
    excluded from both metrics:

      - ``lufs`` absent (measurement failed),
      - ``lufs`` is ``"-inf"`` / ``-inf`` (silent),
      - clip shorter than ``LOUDNESS_MIN_DURATION_S`` (too short to measure per
        ITU-R BS.1770, even when it returns a finite number).

    Returns a dict:
      - ``loudness_spread``: max - min LUFS across measurable clips (LU), rounded
        to 0.1, or ``None`` when fewer than two clips are measurable (a spread is
        undeterminable from one point).
      - ``loudness_median``: median LUFS across measurable clips, rounded to 0.1,
        or ``None`` when none are measurable.
      - ``measured_clips``: how many clips contributed to the metrics.
    """
    lufs_values = []
    for result in file_results:
        stats = result.get("stats", {})
        lufs = stats.get("lufs")
        duration = stats.get("duration")

        if lufs is None or duration is None:
            continue
        if lufs == "-inf" or (isinstance(lufs, float) and lufs == float("-inf")):
            continue
        if not isinstance(lufs, (int, float)):
            continue
        if duration < LOUDNESS_MIN_DURATION_S:
            continue

        lufs_values.append(float(lufs))

    spread = None
    if len(lufs_values) >= 2:
        spread = round(max(lufs_values) - min(lufs_values), 1)

    median = round(statistics.median(lufs_values), 1) if lufs_values else None

    return {
        "loudness_spread": spread,
        "loudness_median": median,
        "measured_clips": len(lufs_values),
    }


def pack_loudness_warnings(pack_metrics):
    """Pack-level loudness warnings from the calibrated thresholds.

    Pure function so the demotion logic is testable without ffmpeg. Returns a
    list of warning dicts, each carrying a summary key, a short message, and a
    contributor-facing detail naming the measured value and the GOLD target.
    These are WARNS: they flow through the existing "any warn -> SILVER" path and
    can only demote GOLD to SILVER, never produce REJECTED.
    """
    warnings = []
    if not pack_metrics:
        return warnings

    spread = pack_metrics.get("loudness_spread")
    median = pack_metrics.get("loudness_median")

    if spread is not None and spread > LOUDNESS_SPREAD_WARN_LU:
        warnings.append({
            "key": "inconsistent_loudness",
            "message": "inconsistent loudness across clips",
            "detail": (
                f"clips span {spread:.1f} LU, GOLD needs {LOUDNESS_SPREAD_WARN_LU:.0f} LU or less "
                f"(normalize the quietest and loudest clips closer together)"
            ),
        })

    if median is not None and median <= LOUDNESS_QUIET_PACK_LUFS:
        warnings.append({
            "key": "uniformly_quiet",
            "message": "pack is uniformly quiet",
            "detail": (
                f"pack median {median:.1f} LUFS, GOLD needs above {LOUDNESS_QUIET_PACK_LUFS:.0f} LUFS "
                f"(normalize toward about -16 LUFS)"
            ),
        })

    return warnings


def decide_verdict(total_blocks, total_warns):
    """Map block/warn counts to the three-tier verdict.

    Extracted as a pure function so the score-only demotion is testable: any
    block forces REJECTED, any warn (including the pack-level loudness warns)
    demotes to SILVER, and a clean pack is GOLD.
    """
    if total_blocks > 0:
        return "REJECTED"
    if total_warns > 0:
        return "SILVER"
    return "GOLD"


def verdict_to_quality(verdict):
    """Map a checker verdict to the stored index.json quality enum (KTD4).

    GOLD -> gold, SILVER -> silver, REJECTED -> flagged. Anything else (an
    ungraded or unexpected value) maps to the schema default, unreviewed. The
    lowercase enum lives only in index.json; the checker keeps GOLD/SILVER/
    REJECTED internally.
    """
    return {
        "GOLD": "gold",
        "SILVER": "silver",
        "REJECTED": "flagged",
    }.get(verdict, "unreviewed")


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

    # Pack-level loudness: measure, then apply the calibrated score-only warns.
    # These are warns, so they demote GOLD to SILVER but never REJECTED (KTD3).
    pack_metrics = aggregate_pack_loudness(file_results)
    pack_warnings = pack_loudness_warnings(pack_metrics)
    for w in pack_warnings:
        total_warns += 1
        warn_summary[w["key"]] += 1

    verdict = decide_verdict(total_blocks, total_warns)

    return {
        "pack_name": pack_name,
        "display_name": display_name,
        "verdict": verdict,
        "total_files": len(audio_files),
        "total_blocks": total_blocks,
        "total_warns": total_warns,
        "block_summary": dict(block_summary),
        "warn_summary": dict(warn_summary),
        "pack_metrics": pack_metrics,
        "pack_warnings": pack_warnings,
        "files": file_results,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pack download (for CI use)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_within_directory(base_dir, target_path):
    """True only if target_path resolves inside base_dir.

    Guards tarball extraction against path-traversal members (e.g. a member
    named `top/../../etc/...`) that would otherwise write outside the temp
    pack directory. The grading jobs extract contributor-controlled tarballs
    while holding write access, so this check is load-bearing (KTD11).
    """
    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(target_path)
    return target_real == base_real or target_real.startswith(base_real + os.sep)


def download_pack(source_repo, source_ref, source_path, dest_dir):
    """Download a pack from GitHub to a local directory."""
    tarball_url = f"https://github.com/{source_repo}/archive/{source_ref}.tar.gz"
    path = source_path.strip("/") if source_path else "."

    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        # Stream with a timeout so a stalled GitHub response cannot hang the
        # persist/backfill runner indefinitely (urlretrieve has no timeout).
        with urllib.request.urlopen(tarball_url, timeout=60) as resp:
            with open(tmp.name, "wb") as out:
                shutil.copyfileobj(resp, out)
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
                    if not _is_within_directory(dest_dir, dest):
                        print(f"Skipping unsafe tarball member: {m.name}", file=sys.stderr)
                        continue
                    # Only regular files and dirs are extracted; symlinks and
                    # hardlinks (m.issym/m.islnk) fall through and are ignored.
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
# Quality persistence (write-back into index.json)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

QUALITY_ENUM_FALLBACK = ("gold", "silver", "flagged", "unreviewed")


def load_quality_enum(schema_path):
    """Read the allowed quality values from the registry schema.

    Falls back to the known enum if the schema cannot be read, so a missing or
    moved schema file degrades to a still-valid guard rather than crashing.
    """
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
        return tuple(schema["$defs"]["pack"]["properties"]["quality"]["enum"])
    except Exception:
        return QUALITY_ENUM_FALLBACK


def _index_packs(index_data):
    """The pack list from an index that is either a bare list or {'packs': [...]}."""
    return index_data if isinstance(index_data, list) else index_data.get("packs", [])


def set_entry_quality(index_data, pack_name, quality):
    """Set one pack entry's quality in a loaded index. Returns True if it changed.

    Mutates only the target entry, leaving every other entry untouched. Raises
    KeyError if the pack is absent.
    """
    for entry in _index_packs(index_data):
        if entry.get("name") == pack_name:
            if entry.get("quality") == quality:
                return False
            entry["quality"] = quality
            return True
    raise KeyError(f"pack '{pack_name}' not found in index")


def serialize_index(index_data):
    """Serialize an index dict to index.json's exact on-disk shape.

    4-space indent, ensure_ascii=False (the catalog stores raw non-ASCII
    descriptions that a default json.dump would escape across hundreds of
    lines), trailing newline. A load -> serialize round-trip is byte-identical,
    so editing one entry's quality leaves every other entry byte-for-byte
    unchanged (KTD10).
    """
    return json.dumps(index_data, indent=4, ensure_ascii=False) + "\n"


def write_pack_quality(index_path, pack_name, quality, schema_path=None):
    """Persist one pack's quality into index.json, surgically and validated.

    Asserts the value is in the schema enum before touching the file, edits only
    that entry, and re-serializes preserving the file's shape. Returns True if
    the file changed (False is a clean no-op for idempotent re-runs). Raises
    ValueError on an out-of-enum value, KeyError if the pack is absent.
    """
    enum = load_quality_enum(schema_path) if schema_path else QUALITY_ENUM_FALLBACK
    if quality not in enum:
        raise ValueError(f"quality '{quality}' is not in the schema enum {enum}")

    with open(index_path, encoding="utf-8") as f:
        index_data = json.load(f)

    changed = set_entry_quality(index_data, pack_name, quality)
    if changed:
        text = serialize_index(index_data)
        json.loads(text)  # guard: never write text that does not parse back
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(text)
    return changed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output formatting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def format_pack_loudness_summary(pack_metrics):
    """One-line human summary of pack loudness, or None when nothing is measurable."""
    if not pack_metrics:
        return None
    measured = pack_metrics.get("measured_clips", 0)
    if not measured:
        return None

    median = pack_metrics.get("loudness_median")
    spread = pack_metrics.get("loudness_spread")
    clip_word = "clip" if measured == 1 else "clips"

    if spread is None:
        return f"median {median:.1f} LUFS ({measured} measurable {clip_word}; spread needs >= 2)"
    return f"spread {spread:.1f} LU, median {median:.1f} LUFS across {measured} measurable {clip_word}"


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

    # Pack-level loudness metrics and any score-only loudness warnings.
    loudness = format_pack_loudness_summary(results.get("pack_metrics"))
    if loudness:
        lines.append(f"**Pack loudness** — {loudness}\n")
    pack_warnings = results.get("pack_warnings", [])
    if pack_warnings:
        lines.append("These pack-level findings demote the tier to SILVER:\n")
        for pw in pack_warnings:
            lines.append(f"- **{pw['message']}** — {pw['detail']}")
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
    lines.append(f"| Pack loudness spread | — | > {LOUDNESS_SPREAD_WARN_LU:.0f} LU |")
    lines.append(f"| Pack median loudness | — | <= {LOUDNESS_QUIET_PACK_LUFS:.0f} LUFS |")
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
    loudness = format_pack_loudness_summary(results.get("pack_metrics"))
    if loudness:
        print(f"  Loudness: {loudness}")
    for pw in results.get("pack_warnings", []):
        print(f"  Pack warning: {pw['message']} — {pw['detail']}")
    print(f"  Verdict: {verdict}")
    print(f"{'━' * 60}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_apply_quality(args):
    """Grade one pack and persist its quality tier into index.json.

    Downloads the pack (pinning to --source-ref when given, so a post-review
    content swap cannot change what gets persisted, KTD11), grades it, maps the
    verdict to the quality enum, and writes that one entry. Exits non-zero on
    any failure so the persist job fails loudly rather than writing nothing.
    """
    if not args.index or not args.pack:
        print("--apply-quality requires --index and --pack", file=sys.stderr)
        sys.exit(2)

    with open(args.index, encoding="utf-8") as f:
        index_data = json.load(f)
    entry = next((p for p in _index_packs(index_data) if p.get("name") == args.pack), None)
    if not entry:
        print(f"Pack '{args.pack}' not found in {args.index}", file=sys.stderr)
        sys.exit(2)

    ref = args.source_ref or entry.get("source_ref", "main")
    dest = tempfile.mkdtemp(prefix=f"persist-{args.pack}-")
    try:
        print(f"Grading {args.pack} from {entry['source_repo']}@{ref}...")
        ok = download_pack(entry["source_repo"], ref, entry.get("source_path", "."), dest)
        if not ok:
            print(f"Failed to download pack '{args.pack}'", file=sys.stderr)
            sys.exit(2)

        results = check_pack(dest)
        verdict = results.get("verdict", "REJECTED")
        quality = verdict_to_quality(verdict)
        changed = write_pack_quality(args.index, args.pack, quality, args.schema)
        state = "updated" if changed else "unchanged"
        print(f"{args.pack}: verdict {verdict} -> quality '{quality}' ({state})")
    finally:
        shutil.rmtree(dest, ignore_errors=True)


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
    # Persist mode: grade one pack and write its quality tier into index.json.
    parser.add_argument("--apply-quality", action="store_true",
                        help="Grade a pack and persist its quality into --index (post-merge)")
    parser.add_argument("--index", metavar="INDEX_JSON",
                        help="index.json to write the quality tier into (with --apply-quality)")
    parser.add_argument("--pack", metavar="NAME",
                        help="Pack name to grade and persist (with --apply-quality)")
    parser.add_argument("--schema", metavar="SCHEMA_JSON",
                        default="schema/registry-v1.schema.json",
                        help="Schema whose quality enum the written value is checked against")
    parser.add_argument("--source-ref", metavar="REF",
                        help="Override the pack's source_ref (pin to the reviewed SHA, KTD11)")

    args = parser.parse_args()

    # Verify ffmpeg is available
    try:
        subprocess.check_output(["ffprobe", "-version"], stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("Error: ffprobe not found. Install ffmpeg.", file=sys.stderr)
        sys.exit(2)

    if args.apply_quality:
        run_apply_quality(args)
        return

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
