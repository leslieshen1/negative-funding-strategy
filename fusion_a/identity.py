"""Two capital-identity formulas. They are not interchangeable and are not selected silently."""

from decimal import Decimal

from fusion_a.decimal_util import FusionError, dec_str, money

GROSS = "GROSS_AUM_INCLUDING_BORROW"
NET = "NET_ECONOMIC_CAPITAL"
BAND_LOW = Decimal("0.50")
BAND_HIGH = Decimal("0.60")

FORMULAS = {
    GROSS: "U=f*A; V=A-U; G=A; N=U only when cash has no extra liabilities",
    NET: "U=A; G=A/f; V=G-U; valid only when f is the USDT weight of gross assets and the other assets are matched borrows",
}


def solve_identity(capital):
    """Solve structural budgets from an explicit identity. Amounts are parameters, not arrived cash.

    Unconfirmed or inapplicable mappings return null amounts and status UNVERIFIED.
    """
    interpretation = capital["interpretation"]
    if interpretation not in (GROSS, NET):
        raise FusionError("UNKNOWN_CAPITAL_INTERPRETATION", interpretation)
    scale = money(capital["scale_usdt"])
    fraction = money(capital["usdt_fraction"])
    if scale <= 0:
        raise FusionError("SCALE_MUST_BE_POSITIVE")
    if fraction <= 0 or fraction >= 1:
        raise FusionError("USDT_FRACTION_MUST_BE_BETWEEN_0_AND_1")
    reasons = []
    if fraction < BAND_LOW or fraction > BAND_HIGH:
        reasons.append("USDT_FRACTION_OUTSIDE_TARGET_BAND")
    confirmed = capital["identity_confirmed"] is True
    mapping_ok = capital["usdt_fraction_is_gross_asset_weight"] is True
    if not confirmed:
        reasons.append("CAPITAL_IDENTITY_UNCONFIRMED")
    if not mapping_ok:
        reasons.append("USDT_FRACTION_MAPPING_NOT_CONFIRMED")
    applicable = confirmed and mapping_ok
    payload = {
        "interpretation": interpretation,
        "formula": FORMULAS[interpretation],
        "scale_usdt": dec_str(scale),
        "usdt_fraction": dec_str(fraction),
        "identity_confirmed": confirmed,
        "applicable": applicable,
        "cash_budget_usdt": None,
        "borrowed_market_value_usdt": None,
        "gross_assets_usdt": None,
        "net_economic_capital_usdt": None,
        "net_capital_status": "UNVERIFIED",
        "pnl_over_gross_is_not_pnl_over_cash": True,
        "reason_codes": reasons,
        "parameter_not_arrived_cash": True,
    }
    if not applicable:
        return payload
    if interpretation == GROSS:
        cash = scale * fraction
        gross = scale
        borrowed = gross - cash
        if capital["cash_has_no_extra_liabilities"] is True:
            net = cash
            net_status = "EQUALS_CASH_BUDGET_UNDER_STATED_ASSUMPTION"
        else:
            net = None
            net_status = "UNKNOWN_BECAUSE_EXTRA_CASH_LIABILITIES"
            reasons.append("NET_CAPITAL_UNKNOWN_EXTRA_LIABILITIES")
    else:
        cash = scale
        gross = scale / fraction
        borrowed = gross - cash
        net = scale
        net_status = "IDENTITY_IS_NET_CAPITAL"
        if capital["cash_has_no_extra_liabilities"] is not True:
            reasons.append("NET_INTERPRETATION_ASSUMES_NO_EXTRA_CASH_LIABILITIES")
            net = None
            net_status = "UNKNOWN_BECAUSE_EXTRA_CASH_LIABILITIES"
    payload.update(
        {
            "cash_budget_usdt": dec_str(cash),
            "borrowed_market_value_usdt": dec_str(borrowed),
            "gross_assets_usdt": dec_str(gross),
            "net_economic_capital_usdt": None if net is None else dec_str(net),
            "net_capital_status": net_status,
            "reason_codes": reasons,
        }
    )
    if money(payload["cash_budget_usdt"]) + money(payload["borrowed_market_value_usdt"]) != money(payload["gross_assets_usdt"]):
        raise FusionError("IDENTITY_DOES_NOT_ADD_UP")
    return payload
