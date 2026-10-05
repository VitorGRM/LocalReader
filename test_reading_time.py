import unittest

from reading_time import DEFAULT_CHARS_PER_SECOND, ReadingTimeEstimator, format_duration


class FormatDurationTests(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(format_duration(65_000), "1:05")
        self.assertEqual(format_duration(0), "0:00")

    def test_hours(self):
        self.assertEqual(format_duration(3_725_000), "1:02:05")

    def test_rounds_up_so_countdown_never_shows_zero_while_playing(self):
        self.assertEqual(format_duration(500), "0:01")

    def test_negative_is_zero(self):
        self.assertEqual(format_duration(-1000), "0:00")


class EstimatorTests(unittest.TestCase):
    def test_empty_document(self):
        estimator = ReadingTimeEstimator()
        self.assertEqual(estimator.remaining_ms(0), 0)
        self.assertEqual(estimator.total_ms(), 0)

    def test_estimate_from_characters_before_any_measurement(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([150, 150], 0)
        expected = 300 / DEFAULT_CHARS_PER_SECOND * 1000
        self.assertAlmostEqual(estimator.total_ms(), expected, delta=2)

    def test_faster_rate_shortens_estimate(self):
        normal, fast = ReadingTimeEstimator(), ReadingTimeEstimator()
        normal.reset([100] * 5, 0)
        fast.reset([100] * 5, 100)
        self.assertAlmostEqual(fast.total_ms() * 2, normal.total_ms(), delta=5)

    def test_known_durations_are_used_exactly(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([10, 10], 0)
        estimator.record_duration(0, 2000)
        estimator.record_duration(1, 3000)
        self.assertEqual(estimator.remaining_ms(0, 500), 1500 + 3000)
        self.assertEqual(estimator.remaining_ms(1, 1000), 2000)

    def test_gap_is_added_between_sentences(self):
        estimator = ReadingTimeEstimator(gap_ms=250)
        estimator.reset([10, 10, 10], 0)
        for i in range(3):
            estimator.record_duration(i, 1000)
        self.assertEqual(estimator.total_ms(), 3000 + 2 * 250)
        self.assertEqual(estimator.remaining_ms(2, 0), 1000)

    def test_learns_real_speed_from_measured_sentences(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([100] * 50, 0)
        before = estimator.total_ms()
        for i in range(10):  # a voz real é 2x mais lenta que o palpite inicial
            estimator.record_duration(i, int(100 / DEFAULT_CHARS_PER_SECOND * 1000 * 2))
        self.assertGreater(estimator.remaining_ms(10, 0), before * 0.8 * 1.5)

    def test_remaining_decreases_with_position(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([100, 100], 0)
        self.assertGreater(estimator.remaining_ms(0, 0), estimator.remaining_ms(0, 3000))

    def test_position_beyond_duration_does_not_go_negative(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([10], 0)
        estimator.record_duration(0, 1000)
        self.assertEqual(estimator.remaining_ms(0, 5000), 0)

    def test_ignores_invalid_durations(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([10], 0)
        before = estimator.total_ms()
        estimator.record_duration(5, 1000)
        estimator.record_duration(0, 0)
        self.assertEqual(estimator.total_ms(), before)

    def test_rerecording_a_sentence_replaces_its_duration(self):
        estimator = ReadingTimeEstimator()
        estimator.reset([10, 10], 0)
        estimator.record_duration(0, 1000)
        estimator.record_duration(0, 2000)
        estimator.record_duration(1, 1000)
        self.assertEqual(estimator.total_ms(), 3000)


if __name__ == "__main__":
    unittest.main()
