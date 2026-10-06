"""Order quantity parsing behaviour."""

import unittest

from app import notes


class ParseQuantityTests(unittest.TestCase):
    def test_whole_number_is_parsed(self):
        self.assertEqual(notes.parse_quantity("12"), 12)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(notes.parse_quantity(" 7 "), 7)

    def test_text_is_rejected(self):
        with self.assertRaises(ValueError):
            notes.parse_quantity("many")

    def test_empty_field_is_rejected(self):
        with self.assertRaises(ValueError):
            notes.parse_quantity("")


class ParseLineTests(unittest.TestCase):
    def test_valid_line(self):
        self.assertEqual(notes.parse_line("widget, 3"), ("widget", 3))

    def test_invalid_line_raises(self):
        with self.assertRaises(ValueError):
            notes.parse_line("widget, none")


if __name__ == "__main__":
    unittest.main()
