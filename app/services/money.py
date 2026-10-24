"""Indian money formatting: ₹12,34,567.00 and 'Rupees Twelve Lakh … Only'."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def paise(value) -> Decimal:
    """Rounds to 2 decimals, half up (how invoices round)."""
    return to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def inr(value, *, symbol: bool = True, decimals: bool = True) -> str:
    """12,34,567.50 — the last three digits, then groups of two (Indian numbering)."""
    amount = paise(value)
    negative = amount < 0
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    text = whole + (f".{frac}" if decimals else "")
    return ("-" if negative else "") + ("₹" if symbol else "") + text


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")


def _three(n: int) -> str:
    hundred, rest = divmod(n, 100)
    parts = []
    if hundred:
        parts.append(f"{_ONES[hundred]} Hundred")
    if rest:
        parts.append(_two(rest))
    return " ".join(parts)


def number_in_words(n: int) -> str:
    """Indian system: crore, lakh, thousand, hundred."""
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(f"{number_in_words(crore)} Crore")
    if lakh:
        parts.append(f"{_two(lakh)} Lakh")
    if thousand:
        parts.append(f"{_two(thousand)} Thousand")
    if n:
        parts.append(_three(n))
    return " ".join(parts)


def amount_in_words(value) -> str:
    amount = paise(value)
    rupees = int(amount)
    fraction = int((amount - rupees) * 100)
    text = f"Rupees {number_in_words(rupees)}"
    if fraction:
        text += f" and {number_in_words(fraction)} Paise"
    return text + " Only"
