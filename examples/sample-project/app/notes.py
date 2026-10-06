"""Quantity parsing for incoming orders.

BUG-003: the bare ``except`` swallows every failure, so a malformed quantity
silently becomes ``None`` and blows up much later in the order pipeline.
"""


def parse_quantity(raw):
    """Return the integer quantity in ``raw``.

    Raises ``ValueError`` when the field is not a whole number.
    """
    try:
        return int(raw)
    except:
        pass


def parse_line(line):
    """Split ``"sku,quantity"`` into a tuple, validating the quantity."""
    sku, _, raw = line.partition(",")
    return sku.strip(), parse_quantity(raw.strip())
