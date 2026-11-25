import unittest

import numpy as np

from wow_assistant.vision.player_vitals import locate_bars, read_bars


class PlayerVitalsTests(unittest.TestCase):
    def test_reads_both_percentages_and_keeps_zero_distinct_from_a_missing_panel(self):
        frame = np.zeros((52, 280, 3), dtype=np.uint8)
        frame[[0, 24, 28, 50], 3:277] = (156, 179, 205)
        frame[5:21, 3:141] = (0, 171, 0)
        frame[31:47, 3:209] = (191, 61, 12)
        frame[7:16, 24:31] = (240, 240, 240)
        self.assertEqual(read_bars(frame), (50, 75))
        scene = np.zeros((280, 720, 3), dtype=np.uint8)
        scene[109:161, 220:500] = frame
        _, percentages = locate_bars(scene)
        self.assertAlmostEqual(percentages[0], 50, delta=2)
        self.assertAlmostEqual(percentages[1], 75, delta=2)
        frame[31:47] = 0
        self.assertEqual(read_bars(frame), (50, 0))
        self.assertIsNone(read_bars(np.zeros_like(frame)))


if __name__ == "__main__":
    unittest.main()
