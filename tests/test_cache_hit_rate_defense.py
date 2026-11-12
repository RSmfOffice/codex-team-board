"""cache_hit_rate must not silently report 100% for inconsistent input.

When cached_input_tokens exceeds input_tokens the rate is truncated at the
normalization boundary; data that remains inconsistent is reported as unknown
(None) rather than a misleading 100%.
"""

from __future__ import annotations

import unittest

from codex_team.usage import cache_hit_rate


class CacheHitRateDefenseTests(unittest.TestCase):
    def test_cached_over_input_is_truncated_not_silent_full(self) -> None:
        # cached (150) > input (100): truncate to input -> 1.0, but the stored
        # cached figure is clamped rather than dividing 150/100 and clamping the
        # ratio after the fact. The explicit uncached figure stays consistent.
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


if __name__ == "__main__":
    unittest.main()