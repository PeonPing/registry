# Quality Check Analysis

Methodology and calibration for the audio-quality gate in
`.github/scripts/quality-check.py`. This document records how the pack-level
loudness thresholds were fit from the catalog, so the values in the checker are
evidence-based rather than guessed.

## Per-file thresholds (existing)

The per-file checks (true peak, integrated LUFS, leading/trailing silence,
duration, sample rate, bitrate) were calibrated against 164 packs and ~4,500
files when the gate first landed. Their block and warn constants live at the top
of `quality-check.py`. This document does not re-derive them. It covers the
pack-level loudness work added on top.

## Pack-level loudness (new)

The per-file gate judges each clip against absolute thresholds in isolation. A
pack whose clips all sit inside the per-file band can still swing widely
relative to each other and sound uneven, and a pack whose clips are uniformly
quiet can pass every per-file check yet read as low quality. Two pack-level
metrics close that gap:

- **Loudness spread**: the range between a pack's quietest and loudest
  measurable clip (`max - min` of integrated LUFS), in loudness units (LU).
- **Pack median loudness**: the median integrated LUFS across a pack's
  measurable clips, in LUFS.

A clip is "measurable" when it produced a finite integrated-loudness reading and
is at least 0.5s long. Silent clips (`-inf`), measurement failures, and clips
under 0.5s carry no trustworthy loudness and are excluded from both metrics
(ITU-R BS.1770 needs ~400ms to integrate). `aggregate_pack_loudness` in
`quality-check.py` computes both. A pack with fewer than two measurable clips
reports no spread.

Both metrics are score-only. They can demote a pack from GOLD to SILVER but
never produce REJECTED. The existing block set is unchanged.

## Catalog distribution

`calibrate-loudness.py` graded the full catalog with the same pipeline the gate
uses (download the source tarball, run the per-file analysis, aggregate). 333 of
334 packs graded across 10,823 audio files, with one (`acolyte_es`) failing to
download.

Verdict mix under the per-file gate alone: 87 GOLD, 244 SILVER, 2 REJECTED. The
per-file checks already push most packs to SILVER, so the pack-level metrics
mostly act on the GOLD set and on future submissions.

Per-pack loudness spread (LU), n = 330 packs with >= 2 measurable clips:

| min | p5 | p10 | p25 | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| 0.0 | 2.3 | 3.1 | 3.9 | 6.8 | 10.0 | 14.8 | 19.3 | 28.9 | 32.3 |

Per-pack median loudness (LUFS), n = 332:

| min | p5 | p10 | p25 | p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| -45.2 | -30.2 | -23.7 | -21.6 | -16.8 | -14.2 | -12.5 | -11.2 | -9.0 | -7.4 |

## Chosen thresholds

```
LOUDNESS_SPREAD_WARN_LU   = 15.0   # spread >= 15 LU  -> "inconsistent loudness" warn
LOUDNESS_QUIET_PACK_LUFS  = -26.0  # median <= -26 LUFS -> "uniformly quiet" warn
```

### Spread threshold: 15.0 LU

15 LU sits at roughly the 90th percentile of the catalog, well clear of the
median (6.8) and p75 (10.0). The decisive check is how many currently-GOLD packs
it would demote, since a poorly-fit threshold mass-mislabels the catalog:

| spread threshold | GOLD packs demoted |
|---|---|
| >= 12 LU | 4 / 87 (4.6%) |
| >= 14 LU | 0 / 87 |
| >= 15 LU | 0 / 87 |

No current GOLD pack has a spread of 15 LU or more. The cleanest packs are also
the most internally consistent. So 15 LU demotes zero current GOLD packs while
still flagging the genuinely-uneven tail (and every future submission that swings
that wide). A 15 dB range between the quietest and loudest clip is audibly
uneven, and the threshold gives contributors a clear target.

The spread is `max - min`, which a single outlier clip can inflate. A robust
percentile spread (p90 - p10) was considered. `max - min` was kept because the
badge's job is to flag any audibly-jarring inconsistency, including a single
clip far off the rest, and because the chosen threshold produces zero false
demotions of current GOLD packs, so outlier sensitivity is not causing
mislabeling here.

### Quiet floor: -26.0 LUFS

The pack-median floor catches packs that are uniformly quiet without any single
clip tripping the per-file quiet warn (-35 LUFS). -26 LUFS is about 10 dB below
the catalog median (-16.8) and below common streaming-loudness norms. Demotion
impact on the GOLD set:

| quiet floor | GOLD packs demoted |
|---|---|
| <= -22 LUFS | 18 / 87 (20.7%) |
| <= -24 LUFS | 7 / 87 (8.0%) |
| <= -26 LUFS | 1 / 87 (1.1%) |
| <= -28 LUFS | 1 / 87 (1.1%) |

-24 LUFS (the catalog p10) would demote 8% of GOLD packs, which over-flags
borderline-normal packs. -26 LUFS flags the quietest tail while demoting a single
current GOLD pack, consistent with the no-mass-mislabel requirement. The
contributor hint points at normalizing toward roughly -16 LUFS.

## Spot-checks

- **arnold** (issue #97's motivating example): spread 29.6 LU, median -21.2
  LUFS, verdict SILVER. It hits no hard block, so it grades SILVER rather than
  REJECTED, with a wide spread. The spread warn (29.6 >= 15) now gives an
  explicit reason it is not GOLD, which is exactly the distinction the badge
  surfaces. The quiet warn does not fire (-21.2 > -26).
- **acolyte_es**: download failed during calibration, so it has no measurement.
  Transient download failures are expected across a 334-pack sweep and do not
  affect the chosen thresholds.

## Reproducing

```
python3 .github/scripts/calibrate-loudness.py --index index.json --workers 8 \
  --output-json cal.json
```

The full catalog grades in about 7.5 minutes (1.3s per pack, 0.04s per file
across 10,823 files with 8 workers), comfortably under the GitHub Actions 6-hour
job ceiling. `--limit N`, `--sample N`, and `--packs name1,name2` scope smaller
runs. The script needs ffmpeg/ffprobe and no repository write access.
