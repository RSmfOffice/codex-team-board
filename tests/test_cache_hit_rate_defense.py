"""cache_hit_rate consistency defense.

Policy: cached_input_tokens is truncated to input_tokens at the normalization
boundary. After truncation, if an explicit uncached figure is present it must
reconcile with input - cached (cached + uncached == input); otherwise the
snapshot is inconsistent and the rate is unknown (None). A consistent snapshot
— including one where truncation leaves cached == input and uncached == 0 —
reports its true rate, which may legitimately be 1.0. The defense rejects
*inconsistent* data, not the value 1.0 itself.
"""

from __future__ import annotations

import unittest

from codex_team.usage import cache_hit_rate


class CacheHitRateDefenseTests(unittest.TestCase):
    def test_cached_over_input_truncated_consistent_reports_full(self) -> None:
        # cached (150) > input (100) is truncated to 100; explicit uncached (0)
        # reconciles (100 + 0 == 100), so the consistent snapshot reports 1.0.
        self.assertEqual(
            cache_hit_rate(
                {"input": 100, "cached": 150, "uncached": 0}
            ),
            1.0,
        )

    def test_cached_over_input_with_explicit_input_reports_full(self) -> None:
        self.assertEqual(
            cache_hit_rate({"input_tokens": 100, "cached_input_tokens": 150}),
            1.0,
        )

    def test_normal_rate_unchanged(self) -> None:
        self.assertAlmostEqual(
            cache_hit_rate({"input": 100, "cached": 60, "uncached": 40}),
            0.6,
        )

    def test_zero_cached_is_zero(self) -> None:
        self.assertEqual(
            cache_hit_rate({"input": 100, "cached": 0, "uncached": 100}),
            0.0,
        )

    def test_missing_cached_is_unknown(self) -> None:
        self.assertIsNone(cache_hit_rate({"input": 100, "uncached": 100}))
        self.assertIsNone(cache_hit_rate({}))
        self.assertIsNone(cache_hit_rate(None))

    def test_zero_input_is_unknown(self) -> None:
        self.assertIsNone(cache_hit_rate({"input": 0, "cached": 0}))

    def test_inconsistent_without_input_is_unknown(self) -> None:
        # No explicit input; cached and uncached cannot be reconciled into a
        # sensible denominator -> unknown rather than a fabricated rate.
        self.assertIsNone(cache_hit_rate({"cached": 5}))

    def test_derived_input_from_cached_and_uncached(self) -> None:
        # input absent but cached + uncached present -> derive denominator.
        self.assertAlmostEqual(
            cache_hit_rate({"cached": 2, "uncached": 3}),
            0.4,
        )

    def test_nested_last_over_input_truncated(self) -> None:
        self.assertEqual(
            cache_hit_rate(
                {
                    "total": {"input": 1000, "cached": 900},
                    "last": {"input": 100, "cached": 150},
                }
            ),
            1.0,
        )

    def test_truncation_then_uncached_mismatch_is_unknown(self) -> None:
        # cached (150) truncates to 100, but explicit uncached (50) does not
        # reconcile (100 + 50 != 100) -> unknown, not a misleading 1.0.
        self.assertIsNone(
            cache_hit_rate({"input": 100, "cached": 150, "uncached": 50})
        )

    def test_explicit_uncached_mismatch_without_truncation_is_unknown(self) -> None:
        # cached (60) <= input (100), no truncation, but explicit uncached (50)
        # != input - cached (40) -> unknown, not 0.6.
        self.assertIsNone(
            cache_hit_rate({"input": 100, "cached": 60, "uncached": 50})
        )

    def test_explicit_uncached_reconciles_after_truncation(self) -> None:
        # cached truncates to 100 and explicit uncached (0) reconciles -> 1.0.
        self.assertEqual(
            cache_hit_rate({"input": 100, "cached": 200, "uncached": 0}),
            1.0,
        )

    def test_consistent_partial_cache_unchanged(self) -> None:
        self.assertAlmostEqual(
            cache_hit_rate({"input": 100, "cached": 60, "uncached": 40}),
            0.6,
        )


if __name__ == "__main__":
    unittest.main()