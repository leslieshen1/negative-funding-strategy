"""Decimal strings only. Floats are rejected so money cannot pick up binary dust."""

from decimal import Decimal, InvalidOperation, ROUND_DOWN


class FusionError(ValueError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def money(value):
    if isinstance(value, bool) or not isinstance(value, str):
        raise FusionError("DECIMAL_STRING_REQUIRED", repr(value))
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise FusionError("INVALID_DECIMAL", value) from exc
    if not number.is_finite():
        raise FusionError("NON_FINITE_DECIMAL", value)
    return number


def dec_str(value):
    if not isinstance(value, Decimal):
        raise FusionError("INTERNAL_DECIMAL_REQUIRED")
    if not value.is_finite():
        raise FusionError("NON_FINITE_DECIMAL")
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if text == "-0":
        return "0"
    return text


def floor_step(quantity, step):
    if step <= 0:
        raise FusionError("INVALID_STEP")
    if quantity == 0:
        return Decimal(0)
    units = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def require_fraction(value, name):
    number = money(value)
    if number < 0 or number > 1:
        raise FusionError("FRACTION_OUT_OF_RANGE", name)
    return number
