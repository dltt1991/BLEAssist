import json
import tempfile
import unittest
from pathlib import Path

from app import load_last_device, preferred_device_index, save_last_device
from ble_controller import ScanDevice


class LastDeviceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "settings.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_load_round_trip(self):
        save_last_device("device-uuid", "YS", self.path)

        self.assertEqual(
            load_last_device(self.path),
            {"key": "device-uuid", "name": "YS"},
        )

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(load_last_device(self.path))

    def test_load_malformed_json_returns_none(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")

        self.assertIsNone(load_last_device(self.path))

    def test_load_invalid_utf8_returns_none(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe")

        self.assertIsNone(load_last_device(self.path))

    def test_load_rejects_invalid_data(self):
        invalid_values = [
            [],
            {},
            {"last_device": []},
            {"last_device": {"key": "", "name": "YS"}},
            {"last_device": {"key": "uuid", "name": " "}},
            {"last_device": {"key": 123, "name": "YS"}},
        ]
        self.path.parent.mkdir(parents=True)

        for value in invalid_values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                self.assertIsNone(load_last_device(self.path))


class PreferredDeviceTests(unittest.TestCase):
    def setUp(self):
        self.devices = [
            ScanDevice("first", "YS", "first details", False),
            ScanDevice("remembered", "YS", "remembered details", True),
            ScanDevice("third", "Other", "third details", False),
        ]

    def test_key_match_has_priority_over_name_match(self):
        self.assertEqual(
            preferred_device_index(
                self.devices,
                {"key": "remembered", "name": "YS"},
            ),
            1,
        )

    def test_falls_back_to_exact_name(self):
        self.assertEqual(
            preferred_device_index(
                self.devices,
                {"key": "missing", "name": "Other"},
            ),
            2,
        )

    def test_unmatched_device_falls_back_to_first(self):
        self.assertEqual(
            preferred_device_index(
                self.devices,
                {"key": "missing", "name": "Missing"},
            ),
            0,
        )

    def test_no_saved_device_falls_back_to_first(self):
        self.assertEqual(preferred_device_index(self.devices, None), 0)

    def test_empty_scan_has_no_selection(self):
        self.assertIsNone(preferred_device_index([], None))


if __name__ == "__main__":
    unittest.main()
