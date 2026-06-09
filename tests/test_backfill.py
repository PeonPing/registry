#!/usr/bin/env python3
"""Unit tests for backfill-quality.py selection and apply logic (no ffmpeg).

The grading itself needs network + ffmpeg and is smoke-tested in CI; these
cover the resume/force-regrade selection and the non-destructive batch apply.
"""

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "backfill-quality.py"
_spec = importlib.util.spec_from_file_location("backfill_quality", MODULE_PATH)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def packs():
    return [
        {"name": "alpha"},                          # no quality -> unreviewed
        {"name": "beta", "quality": "unreviewed"},
        {"name": "gamma", "quality": "gold"},       # already graded
        {"name": "delta", "quality": "flagged"},    # already graded
    ]


class SelectTargetsTest(unittest.TestCase):
    def test_default_skips_already_graded(self):
        names = [p["name"] for p in bf.select_targets(packs())]
        self.assertEqual(names, ["alpha", "beta"])

    def test_force_regrade_includes_all(self):
        names = [p["name"] for p in bf.select_targets(packs(), force_regrade=True)]
        self.assertEqual(names, ["alpha", "beta", "gamma", "delta"])

    def test_limit_then_resume_skip(self):
        # limit picks the first 3, then the resume skip drops graded gamma.
        names = [p["name"] for p in bf.select_targets(packs(), limit=3)]
        self.assertEqual(names, ["alpha", "beta"])

    def test_names_filter_with_force(self):
        names = [p["name"] for p in
                 bf.select_targets(packs(), force_regrade=True, names=["delta", "alpha"])]
        self.assertEqual(sorted(names), ["alpha", "delta"])

    def test_names_filter_without_force_applies_resume_skip(self):
        # An operator targeting an already-graded pack without --force-regrade
        # gets a no-op (resume skip runs after the names filter), not a regrade.
        self.assertEqual(bf.select_targets(packs(), names=["gamma"]), [])
        names = [p["name"] for p in bf.select_targets(packs(), names=["gamma", "alpha"])]
        self.assertEqual(names, ["alpha"])


class ApplyGradesTest(unittest.TestCase):
    def setUp(self):
        self.enum = ("gold", "silver", "flagged", "unreviewed")
        self.index = {"version": 1, "packs": packs(), "total_packs": 4}

    def _by_name(self):
        return {p["name"]: p for p in self.index["packs"]}

    def test_applies_counts_and_is_non_destructive(self):
        results = [
            {"name": "alpha", "verdict": "GOLD", "quality": "gold"},
            {"name": "beta", "verdict": "REJECTED", "quality": "flagged"},
        ]
        summary = bf.apply_grades(self.index, results, self.enum)
        self.assertEqual(summary["applied"], 2)
        self.assertEqual(summary["flagged"], ["beta"])
        self.assertEqual(summary["tier_counts"], {"gold": 1, "flagged": 1})
        # AE3/R12: flagged pack stays in the index, nothing removed.
        self.assertEqual(len(self.index["packs"]), 4)
        self.assertEqual(self._by_name()["alpha"]["quality"], "gold")
        self.assertEqual(self._by_name()["beta"]["quality"], "flagged")

    def test_error_entries_recorded_not_applied(self):
        results = [
            {"name": "alpha", "error": "download failed"},
            {"name": "beta", "verdict": "SILVER", "quality": "silver"},
        ]
        summary = bf.apply_grades(self.index, results, self.enum)
        self.assertEqual(summary["errored"], ["alpha"])
        self.assertEqual(summary["applied"], 1)
        self.assertNotIn("quality", self._by_name()["alpha"])  # left untouched

    def test_out_of_enum_recorded_as_errored(self):
        results = [{"name": "alpha", "verdict": "?", "quality": "platinum"}]
        summary = bf.apply_grades(self.index, results, self.enum)
        self.assertEqual(summary["errored"], ["alpha"])
        self.assertEqual(summary["applied"], 0)
        self.assertNotIn("quality", self._by_name()["alpha"])

    def test_idempotent_reapply_counts_but_does_not_rewrite(self):
        results = [{"name": "gamma", "verdict": "GOLD", "quality": "gold"}]  # already gold
        summary = bf.apply_grades(self.index, results, self.enum)
        self.assertEqual(summary["applied"], 0)             # no change written
        self.assertEqual(summary["tier_counts"], {"gold": 1})  # still graded


if __name__ == "__main__":
    unittest.main()
