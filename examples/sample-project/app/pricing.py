"""Basket maths.

BUG-002: ``average_price`` divides by the basket size without checking for an
empty basket, which raises ``ZeroDivisionError``.
"""



def average_price(prices):
    """Average price of a basket of prices; an empty basket averages to 0."""
    return sum(prices) / len(prices)


def basket_total(prices):
    """Sum of the basket."""
    return sum(prices)


def discounted(prices, percent):
    """Basket total with a percentage discount applied."""
    total = basket_total(prices)
    return round(total - total * (percent / 100), 2)
