#!/usr/bin/env python3
"""Unit tests for quality-check.py pack-level loudness aggregation.

Stdlib `unittest` only — the registry ships no pip dependencies. The checker
lives at .github/scripts/quality-check.py (hyphen + dotted path, not importable
by name), so it is loaded by path with importlib.

Run from the repo root:
  python3 -m unittest discover tests
  # or
  python3 tests/test_quality_check.py
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "quality-check.py"
_spec = importlib.util.spec_from_file_location("quality_check", MODULE_PATH)
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)


def file_result(lufs="absent", duration=1.0):
    """Build a classify_file-shaped result for the aggregation under test.

    lufs="absent" omits the key entirely (loudnorm failure); pass "-inf",
    float("-inf"), or a number to model the other cases. duration=None omits
    the duration (probe failure).
    """
    stats = {}
    if duration is not None:
        stats["duration"] = duration
    if lufs != "absent":
        stats["lufs"] = lufs
    return {"file": "clip.mp3", "blocks": [], "warns": [], "stats": stats}


class AggregatePackLoudnessTest(unittest.TestCase):
    def test_happy_path_spread_and_median(self):
        results = [file_result(-20.0), file_result(-23.0), file_result(-26.0)]
        m = qc.aggregate_pack_loudness(results)
        self.assertEqual(m["loudness_spread"], 6.0)   # -20 − (-26)
        self.assertEqual(m["loudness_median"], -23.0)
        self.assertEqual(m["measured_clips"], 3)

    def test_excludes_absent_inf_and_short_clips(self):
        results = [
            file_result(-20.0),                  # measurable
            file_result(-30.0),                  # measurable
            file_result("absent"),               # loudnorm failed -> excluded
            file_result("-inf"),                 # silent -> excluded
            file_result(-25.0, duration=0.3),    # too short -> excluded though finite
            file_result(-25.0, duration=None),   # probe failed -> excluded
        ]
        m = qc.aggregate_pack_loudness(results)
        self.assertEqual(m["measured_clips"], 2)
        self.assertEqual(m["loudness_spread"], 10.0)   # -20 − (-30)
        self.assertEqual(m["loudness_median"], -25.0)

    def test_inf_as_float_is_excluded(self):
        # Defensive: a raw float('-inf'), not just the "-inf" string, is excluded.
        results = [file_result(-20.0), file_result(float("-inf")), file_result(-24.0)]
        m = qc.aggregate_pack_loudness(results)
        self.assertEqual(m["measured_clips"], 2)
        self.assertEqual(m["loudness_spread"], 4.0)
        self.assertEqual(m["loudness_median"], -22.0)

    def test_fewer_than_two_measurable_yields_no_spread(self):
        results = [file_result(-20.0), file_result("-inf"), file_result("absent")]
        m = qc.aggregate_pack_loudness(results)
        self.assertIsNone(m["loudness_spread"])
        self.assertEqual(m["loudness_median"], -20.0)
        self.assertEqual(m["measured_clips"], 1)

    def test_single_clip_has_median_no_spread(self):
        m = qc.aggregate_pack_loudness([file_result(-18.0)])
        self.assertIsNone(m["loudness_spread"])
        self.assertEqual(m["loudness_median"], -18.0)
        self.assertEqual(m["measured_clips"], 1)

    def test_no_measurable_clips_does_not_error(self):
        m = qc.aggregate_pack_loudness([file_result("-inf"), file_result("absent")])
        self.assertIsNone(m["loudness_spread"])
        self.assertIsNone(m["loudness_median"])
        self.assertEqual(m["measured_clips"], 0)

    def test_empty_input(self):
        m = qc.aggregate_pack_loudness([])
        self.assertEqual(
            m, {"loudness_spread": None, "loudness_median": None, "measured_clips": 0}
        )


class FormatPackLoudnessSummaryTest(unittest.TestCase):
    def test_none_when_no_measurable_clips(self):
        self.assertIsNone(qc.format_pack_loudness_summary(None))
        self.assertIsNone(qc.format_pack_loudness_summary(
            {"loudness_spread": None, "loudness_median": None, "measured_clips": 0}))

    def test_spread_and_median_phrasing(self):
        s = qc.format_pack_loudness_summary(
            {"loudness_spread": 6.0, "loudness_median": -23.0, "measured_clips": 3})
        self.assertIn("spread 6.0 LU", s)
        self.assertIn("median -23.0 LUFS", s)
        self.assertIn("3 measurable clips", s)

    def test_single_clip_phrasing_omits_spread_figure(self):
        s = qc.format_pack_loudness_summary(
            {"loudness_spread": None, "loudness_median": -18.0, "measured_clips": 1})
        self.assertIn("median -18.0 LUFS", s)
        self.assertIn("1 measurable clip", s)
        self.assertIn("spread needs >= 2", s)


def metrics(spread, median, measured=5):
    return {"loudness_spread": spread, "loudness_median": median, "measured_clips": measured}


class PackLoudnessWarningsTest(unittest.TestCase):
    def test_wide_spread_warns(self):
        # AE1: in-band clips spanning a wide range add one pack warn.
        warns = qc.pack_loudness_warnings(metrics(spread=20.0, median=-16.0))
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["key"], "inconsistent_loudness")

    def test_spread_at_threshold_does_not_warn(self):
        # Threshold uses strict >, so exactly-at-threshold is not flagged.
        self.assertEqual(qc.pack_loudness_warnings(metrics(qc.LOUDNESS_SPREAD_WARN_LU, -16.0)), [])
        self.assertEqual(qc.pack_loudness_warnings(metrics(qc.LOUDNESS_SPREAD_WARN_LU - 0.1, -16.0)), [])

    def test_quiet_pack_warns(self):
        # R2: a pack whose median sits at/below the floor reads as too quiet.
        warns = qc.pack_loudness_warnings(metrics(spread=5.0, median=-27.0))
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["key"], "uniformly_quiet")

    def test_quiet_floor_is_inclusive(self):
        # Floor uses <=, so exactly-at-floor warns; just above does not.
        self.assertEqual(len(qc.pack_loudness_warnings(metrics(5.0, qc.LOUDNESS_QUIET_PACK_LUFS))), 1)
        self.assertEqual(qc.pack_loudness_warnings(metrics(5.0, qc.LOUDNESS_QUIET_PACK_LUFS + 0.1)), [])

    def test_clean_pack_has_no_warnings(self):
        # GOLD happy path: consistent and loud-enough pack adds no pack warn.
        self.assertEqual(qc.pack_loudness_warnings(metrics(spread=8.0, median=-16.0)), [])

    def test_both_conditions_warn(self):
        warns = qc.pack_loudness_warnings(metrics(spread=22.0, median=-30.0))
        self.assertEqual({w["key"] for w in warns}, {"inconsistent_loudness", "uniformly_quiet"})

    def test_no_spread_yields_no_spread_warn(self):
        # Single measurable clip: spread is None, only the quiet check can fire.
        self.assertEqual(qc.pack_loudness_warnings(metrics(spread=None, median=-16.0)), [])

    def test_empty_metrics(self):
        self.assertEqual(qc.pack_loudness_warnings(None), [])
        self.assertEqual(qc.pack_loudness_warnings({}), [])

    def test_demotion_message_names_measured_value_and_target(self):
        spread_warn = qc.pack_loudness_warnings(metrics(spread=22.5, median=-16.0))[0]
        self.assertIn("22.5", spread_warn["detail"])
        self.assertIn("15", spread_warn["detail"])  # the GOLD target
        quiet_warn = qc.pack_loudness_warnings(metrics(spread=5.0, median=-28.3))[0]
        self.assertIn("-28.3", quiet_warn["detail"])
        self.assertIn("-26", quiet_warn["detail"])  # the GOLD target


class DecideVerdictTest(unittest.TestCase):
    def test_clean_is_gold(self):
        self.assertEqual(qc.decide_verdict(0, 0), "GOLD")

    def test_any_warn_demotes_to_silver(self):
        self.assertEqual(qc.decide_verdict(0, 1), "SILVER")

    def test_block_forces_rejected_regardless_of_warns(self):
        # AE2 / R5: a hard block stays REJECTED no matter how many warns exist.
        self.assertEqual(qc.decide_verdict(1, 0), "REJECTED")
        self.assertEqual(qc.decide_verdict(1, 99), "REJECTED")

    def test_pack_warn_demotes_gold_to_silver_not_rejected(self):
        # AE1 end to end: a wide-spread, block-free pack lands SILVER.
        warns = len(qc.pack_loudness_warnings(metrics(spread=20.0, median=-16.0)))
        self.assertEqual(qc.decide_verdict(0, warns), "SILVER")
        # AE2: the same pack with a hard block is still REJECTED.
        self.assertEqual(qc.decide_verdict(1, warns), "REJECTED")


class VerdictToQualityTest(unittest.TestCase):
    def test_mapping_is_exact_lowercase(self):
        # Casing matters: a stray "GOLD" would pass the site's !== "flagged"
        # filter yet render no badge, undetected.
        self.assertEqual(qc.verdict_to_quality("GOLD"), "gold")
        self.assertEqual(qc.verdict_to_quality("SILVER"), "silver")
        self.assertEqual(qc.verdict_to_quality("REJECTED"), "flagged")

    def test_unknown_or_missing_maps_to_unreviewed(self):
        self.assertEqual(qc.verdict_to_quality(None), "unreviewed")
        self.assertEqual(qc.verdict_to_quality(""), "unreviewed")
        self.assertEqual(qc.verdict_to_quality("WHATEVER"), "unreviewed")


def _fixture_index():
    # Two entries; alpha carries a non-ASCII (u-umlaut + em-dash) description,
    # beta is the untouched control. alpha precedes beta so any change to alpha
    # leaves beta's bytes downstream unchanged.
    return {
        "version": 1,
        "packs": [
            {"name": "alpha", "display_name": "Alpha",
             "description": "Crüsader — scheming monastery lord"},
            {"name": "beta", "display_name": "Beta", "description": "beta plain desc",
             "quality": "silver"},
        ],
        "total_packs": 2,
    }


def _suffix_from_line(text, needle):
    i = text.index(needle)
    line_start = text.rfind("\n", 0, i) + 1
    return text[line_start:]


class WritePackQualityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="idx-test-")
        self.path = os.path.join(self.dir, "index.json")
        self.original = qc.serialize_index(_fixture_index())
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.original)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_target_entry(self):
        changed = qc.write_pack_quality(self.path, "alpha", "gold")
        self.assertTrue(changed)
        with open(self.path, encoding="utf-8") as f:
            written = f.read()
        self.assertEqual(written, qc.serialize_index(_set(_fixture_index(), "alpha", "gold")))

    def test_other_entries_stay_byte_identical(self):
        qc.write_pack_quality(self.path, "alpha", "gold")
        with open(self.path, encoding="utf-8") as f:
            written = f.read()
        # beta's entry (from its name line through EOF) is unchanged byte for byte.
        self.assertEqual(
            _suffix_from_line(self.original, '"name": "beta"'),
            _suffix_from_line(written, '"name": "beta"'),
        )

    def test_entry_before_target_also_stays_byte_identical(self):
        # Editing beta (the second entry) must leave alpha (before it) byte-
        # identical too, not just entries after the edit.
        qc.write_pack_quality(self.path, "beta", "gold")
        with open(self.path, encoding="utf-8") as f:
            written = f.read()
        # alpha's region runs from its name line up to beta's name line.
        def alpha_region(text):
            start = text.index('"name": "alpha"')
            start = text.rfind("\n", 0, start) + 1
            return text[start:text.index('"name": "beta"')]
        self.assertEqual(alpha_region(self.original), alpha_region(written))

    def test_non_ascii_survives_unescaped(self):
        qc.write_pack_quality(self.path, "alpha", "gold")
        with open(self.path, encoding="utf-8") as f:
            written = f.read()
        self.assertIn("Crüsader — scheming monastery lord", written)
        self.assertNotIn("\\u00fc", written)  # not escaped to ASCII

    def test_idempotent_rewrite_is_noop(self):
        qc.write_pack_quality(self.path, "beta", "silver")  # already silver
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.original)
        # A real change reports True, a repeat reports False.
        self.assertTrue(qc.write_pack_quality(self.path, "alpha", "gold"))
        self.assertFalse(qc.write_pack_quality(self.path, "alpha", "gold"))

    def test_out_of_enum_value_raises_before_writing(self):
        with self.assertRaises(ValueError):
            qc.write_pack_quality(self.path, "alpha", "platinum")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.original)  # file untouched

    def test_missing_pack_raises(self):
        with self.assertRaises(KeyError):
            qc.write_pack_quality(self.path, "nonexistent", "gold")


def _set(index_data, name, quality):
    qc.set_entry_quality(index_data, name, quality)
    return index_data


class TarballPathGuardTest(unittest.TestCase):
    def test_in_bounds_paths_allowed(self):
        base = tempfile.mkdtemp(prefix="tar-test-")
        try:
            self.assertTrue(qc._is_within_directory(base, os.path.join(base, "sounds/a.mp3")))
            self.assertTrue(qc._is_within_directory(base, base))
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_traversal_paths_rejected(self):
        base = tempfile.mkdtemp(prefix="tar-test-")
        try:
            self.assertFalse(qc._is_within_directory(base, os.path.join(base, "../evil")))
            self.assertFalse(qc._is_within_directory(base, os.path.join(base, "a/../../evil")))
            self.assertFalse(qc._is_within_directory(base, "/etc/passwd"))
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
