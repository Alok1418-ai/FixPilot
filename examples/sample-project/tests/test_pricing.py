"""Basket pricing behaviour."""

import unittest

from app import pricing


class AveragePriceTests(unittest.TestCase):
    def test_average_of_priced_basket(self):
        self.assertEqual(pricing.average_price([100, 200, 300]), 200)

    def test_single_item_basket(self):
        self.assertEqual(pricing.average_price([250]), 250)

    def test_empty_basket_averages_to_zero(self):
        self.assertEqual(pricing.average_price([]), 0)


class DiscountTests(unittest.TestCase):
    def test_discount_is_applied(self):
        self.assertEqual(pricing.discounted([100, 100], 10), 180.0)

    def test_discount_on_empty_basket(self):
        self.assertEqual(pricing.discounted([], 25), 0)


if __name__ == "__main__":
    unittest.main()
