import unittest

from protocol import FrameDecoder, encode_joystick, mode_command, pointer_to_stick


class ProtocolTests(unittest.TestCase):
    def test_center_frame(self):
        self.assertEqual(encode_joystick(0.0, 0.0, 3), "Y2C5005003S")

    def test_cardinal_directions(self):
        self.assertEqual(encode_joystick(0.0, -1.0, 1), "Y2C6005001S")
        self.assertEqual(encode_joystick(1.0, 0.0, 6), "Y2C5004006S")

    def test_joystick_uses_dart_half_away_from_zero_rounding(self):
        self.assertEqual(encode_joystick(0.0, -0.005, 3), "Y2C5015003S")

    def test_outside_circle_is_clamped(self):
        self.assertEqual(encode_joystick(2.0, 0.0, 3), "Y2C5004003S")

    def test_mode_validation(self):
        for mode in range(1, 7):
            with self.subTest(mode=mode):
                self.assertEqual(
                    mode_command(mode),
                    (f"Y2O{mode}S", f"Y1O{mode}S"),
                )
                self.assertEqual(
                    encode_joystick(0.0, 0.0, mode),
                    f"Y2C500500{mode}S",
                )
        with self.assertRaises(ValueError):
            mode_command(0)

    def test_decoder_handles_fragmentation_and_concatenation(self):
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"noiseY1"), [])
        self.assertEqual(decoder.feed(b"ASY1BS"), ["Y1AS", "Y1BS"])

    def test_decoder_discards_unbounded_garbage(self):
        decoder = FrameDecoder(max_buffer=32)
        self.assertEqual(decoder.feed(b"x" * 100), [])
        self.assertLessEqual(decoder.buffer_size, 32)

    def test_pointer_coordinates_map_to_normalized_stick(self):
        self.assertEqual(pointer_to_stick(50, 50, 50, 50, 40), (0.0, 0.0))
        self.assertEqual(pointer_to_stick(50, 10, 50, 50, 40), (0.0, -1.0))

    def test_pointer_outside_circle_is_clamped(self):
        self.assertEqual(pointer_to_stick(130, 50, 50, 50, 40), (1.0, 0.0))

    def test_pointer_rejects_nonpositive_radius(self):
        with self.assertRaises(ValueError):
            pointer_to_stick(0, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
