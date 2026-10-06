"""Stock lookup behaviour."""

import unittest

from app import inventory


class StockLevelTests(unittest.TestCase):
    def test_known_sku_reports_its_stock(self):
        self.assertEqual(inventory.stock_level("widget"), 4)

    def test_out_of_stock_sku_reports_zero(self):
        self.assertEqual(inventory.stock_level("gadget"), 0)

    def test_unknown_sku_has_no_stock(self):
        self.assertEqual(inventory.stock_level("not-a-real-sku"), 0)

    def test_has_stock_is_false_for_unknown_sku(self):
        self.assertFalse(inventory.has_stock("not-a-real-sku"))

    def test_low_stock_filters_skus(self):
        self.assertEqual(inventory.low_stock(["widget", "gadget"], threshold=4), ["widget", "gadget"])


if __name__ == "__main__":
    unittest.main()
