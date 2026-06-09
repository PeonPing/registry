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


if __name__ == "__main__":
    unittest.main()
