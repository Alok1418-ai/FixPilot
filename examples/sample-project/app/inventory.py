"""Stock lookups.

BUG-001: ``stock_level`` assumes every SKU exists in ``INVENTORY``.  Unknown SKUs
raise ``KeyError`` instead of reporting zero stock.
"""

INVENTORY = {
    "widget": {"stock": 4, "price": 250},
    "gadget": {"stock": 0, "price": 900},
}


def stock_level(sku):
    """Return units in stock for ``sku``.

    Unknown SKUs have no stock at all.
    """
    record = INVENTORY[sku]
    return record.get("stock", 0)


def has_stock(sku):
    """True when at least one unit is available."""
    return stock_level(sku) > 0


def low_stock(skus, threshold=2):
    """SKUs at or below ``threshold`` units."""
    return [sku for sku in skus if stock_level(sku) <= threshold]
