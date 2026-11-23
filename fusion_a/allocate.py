"""Constraint-transparent budgets. Unknown costs stay null and block new exposure.

No fee, impact, completion or yield APR is invented here. A forecast field on the
universe is ignored so a future rate cannot change the target.
"""

from decimal import Decimal

from fusion_a.decimal_util import dec_str, floor_step, money
from fusion_a.gate import negative_signal_eligible
from fusion_a.state import cash_amount, liability_qty, live_orders, position_qty

ZERO = Decimal(0)


def calibration_view(calibration):
    if calibration is None:
        return {
            "status": "MISSING",
            "spot_fee": None,
            "perp_fee": None,
            "impact": None,
            "completion": None,
            "synthetic": None,
            "queue_known": False,
        }
    def known(flag, value):
        return money(value) if flag else None
    return {
        "status": "PRESENT",
        "spot_fee": known(calibration["fee_schedule_known"], calibration["spot_fee_rate"]),
        "perp_fee": known(calibration["fee_schedule_known"], calibration["perp_fee_rate"]),
        "impact": known(calibration["impact_known"], calibration["round_trip_impact_rate"]),
        "completion": known(calibration["completion_known"], calibration["completion_probability"]),
        "synthetic": calibration["synthetic"],
        "queue_known": calibration["queue_known"],
        "source_mode": calibration["source_mode"],
    }


def carry_bounds(rate, interval_hours, hold_hours, year_hours, margin_fraction, costs, yield_apr):
    """Gross and net boundaries on one unit of spot notional. Costs are optional."""
    funding_over_hold = rate * Decimal(hold_hours) / Decimal(interval_hours)
    year_frac = Decimal(hold_hours) / Decimal(year_hours)
    multiple = Decimal(1) + margin_fraction
    gross_yield = funding_over_hold / (multiple * year_frac)
    fees_known = costs["spot_fee"] is not None and costs["perp_fee"] is not None
    impact = costs["impact"]
    completion = costs["completion"]
    fee_round_trip = None
    if fees_known:
        fee_round_trip = Decimal(2) * (costs["spot_fee"] + costs["perp_fee"])
    full_cost = None
    if fee_round_trip is not None and impact is not None:
        full_cost = fee_round_trip + impact
    completion_for_bound = completion if completion is not None else Decimal(1)
    net_apr = None
    if full_cost is not None and completion is not None:
        net_apr = (funding_over_hold * completion - full_cost) / (multiple * year_frac)
    breakeven_impact = None
    if fee_round_trip is not None:
        yield_drag = ZERO if yield_apr is None else yield_apr * multiple * year_frac
        breakeven_impact = funding_over_hold * completion_for_bound - fee_round_trip - yield_drag
    return {
        "funding_fraction_over_policy_hold": dec_str(funding_over_hold),
        "cash_multiple": dec_str(multiple),
        "year_fraction": dec_str(year_frac),
        "gross_breakeven_yield_apr_before_costs": dec_str(gross_yield),
        "gross_label": "GROSS_BEFORE_UNKNOWN_COSTS",
        "fee_only_round_trip_rate": None if fee_round_trip is None else dec_str(fee_round_trip),
        "round_trip_cost_fraction": None if full_cost is None else dec_str(full_cost),
        "net_carry_apr": None if net_apr is None else dec_str(net_apr),
        "breakeven_round_trip_impact_rate": None if breakeven_impact is None else dec_str(breakeven_impact),
        "breakeven_impact_assumes_full_completion": completion is None,
        "yield_apr": None if yield_apr is None else dec_str(yield_apr),
        "carry_beats_known_yield": None if net_apr is None or yield_apr is None else net_apr > yield_apr,
        "hold_horizon_is_policy_not_estimate": True,
        "calendar": "365d_24h",
    }


def _costs_complete(costs):
    return all(costs[name] is not None for name in ("spot_fee", "perp_fee", "impact", "completion"))


def plan(config, identity, ledger, snapshot, gate, faults):
    policy = config["policy"]
    costs = calibration_view(snapshot["execution_calibration"])
    reasons = list(faults)
    if costs["queue_known"]:
        reasons.append("QUEUE_NOT_MODELED")
    if any("ignored_forecast_rate" in row for row in snapshot["universe"]):
        reasons.append("FORECAST_IGNORED_NOT_USED")
    reasons.append("MMR_UNKNOWN_NO_LIQUIDATION_CLAIM")
    if not policy["enforce_wall_clock"]:
        reasons.append("WALL_CLOCK_NOT_ENFORCED")
    deploy_new = not faults and identity["applicable"] and identity["net_economic_capital_usdt"] is not None
    if "USDT_FRACTION_OUTSIDE_TARGET_BAND" in identity["reason_codes"] and not policy["allow_usdt_fraction_outside_band"]:
        deploy_new = False
        reasons.append("USDT_FRACTION_OUTSIDE_TARGET_BAND")
    status = "STOPPED"
    if costs["status"] == "MISSING" or costs["spot_fee"] is None:
        deploy_new = False
        status = "UNVERIFIED_NO_NEW"
        reasons.append("EXECUTION_CALIBRATION_MISSING")
    elif not _costs_complete(costs):
        deploy_new = False
        status = "BREAKEVEN_ONLY"
        reasons.append("COST_OR_COMPLETION_UNIDENTIFIED")
    elif faults or not deploy_new:
        deploy_new = False
        status = "STOPPED"
    elif costs["synthetic"] or snapshot["synthetic"]:
        status = "SYNTHETIC_STATE_ONLY"
    else:
        status = "OBSERVED_DRY_RUN"
    budgets = _budgets(policy, identity, ledger)
    if not deploy_new:
        budgets["effective_deployable_usdt"] = dec_str(ZERO)
    yield_apr, yield_product = _known_yield(snapshot)
    recall = _recall_assets(snapshot, ledger)
    proposals = []
    intents = []
    remaining = money(budgets["effective_deployable_usdt"]) if deploy_new else ZERO
    negative_budget = _negative_budget(policy, identity, remaining, snapshot, gate, deploy_new, ledger)
    if not _negative_can_add(snapshot, gate, policy):
        negative_budget = ZERO
    if negative_budget > remaining:
        negative_budget = remaining
    positive_room = remaining - negative_budget
    positive_spent = ZERO
    weaker = False
    for row in _ranked(snapshot["universe"]):
        bounds = carry_bounds(
            money(row["funding_rate_observed"]),
            row["funding_interval_hours"],
            policy["max_planned_hold_hours"],
            policy["year_hours"],
            money(policy["initial_margin_fraction"]),
            costs,
            yield_apr,
        )
        bounds["asset"] = row["asset"]
        proposal, spent, used_weaker = _positive_proposal(
            row, bounds, policy, identity, ledger, positive_room - positive_spent, deploy_new, costs, yield_apr, faults, recall
        )
        positive_spent += spent
        weaker = weaker or used_weaker
        if proposal is not None:
            proposals.append(proposal)
        elif _has_activity(ledger, "POSITIVE_CARRY", row["asset"]):
            proposals.append(_hold("POSITIVE_CARRY", row["asset"], ledger, ["HOLD_EXISTING"]))
    negative_spent = ZERO
    for row in _negative_ranked(snapshot["universe"], gate):
        proposal, spent = _negative_proposal(
            row, policy, identity, ledger, snapshot, gate, negative_budget - negative_spent, deploy_new, faults, recall
        )
        negative_spent += spent
        if proposal is not None:
            proposals.append(proposal)
        elif _has_activity(ledger, "NEGATIVE_FUNDING", row["asset"]) or liability_qty(ledger, row["asset"]) > 0:
            proposals.append(_hold("NEGATIVE_FUNDING", row["asset"], ledger, ["HOLD_EXISTING"]))
    # Assets under recall or with positions that were not ranked still need an absolute target.
    covered = {(item["book"], item["asset"]) for item in proposals}
    for row in ledger["positions"]:
        key = (row["book"], row["asset"])
        if key in covered:
            continue
        if money(row["spot_qty_token"]) == 0 and money(row["perp_qty_token"]) == 0:
            continue
        proposals.append(_risk_target(row["book"], row["asset"], ledger, faults, recall, policy))
    for asset in recall:
        key = ("NEGATIVE_FUNDING", asset)
        if key not in covered and liability_qty(ledger, asset) > 0:
            proposals.append(_risk_target("NEGATIVE_FUNDING", asset, ledger, faults, recall, policy))
            covered.add(key)
    if yield_product is not None:
        intents.extend(_yield_intents(yield_product, yield_apr, proposals, budgets, deploy_new, recall, ledger, policy))
    else:
        intents.append({"action": "KEEP_CASH", "reason_codes": ["YIELD_APR_UNKNOWN_NO_HARDCODE"], "amount_usdt": "0"})
    if recall:
        reasons.append("COIN_RECALL")
        redeem_delay = max((row["redeem_delay_ms"] for row in snapshot["yield_products"]), default=0)
        skew = policy["settlement_book_skew_ms"]
        for row in list(ledger["liabilities"]) + list(snapshot["liabilities"]):
            deadline = row.get("recall_deadline_ms")
            if deadline is not None and row.get("recall_status") in {"RECEIVED", "IN_PROGRESS"}:
                if snapshot["decision_time_ms"] + redeem_delay + skew > deadline:
                    reasons.append("RECALL_DEADLINE_INFEASIBLE")
        for order in live_orders(ledger):
            intents.append({"action": "CANCEL_OPEN_ORDER", "order_id": order["order_id"], "reason_codes": ["COIN_RECALL"]})
    for item in proposals:
        intents.extend(_liability_intents(item, ledger))
    if weaker:
        reasons.append("CAPACITY_ADV_ONLY_WEAKER")
    proposed = any(money(item["planned_new_cash_usdt"]) > 0 for item in proposals) or any(
        money(item["desired_liability_qty_token"]) > money(item["current_liability_qty_token"]) for item in proposals
    )
    if deploy_new and not proposed:
        if status in {"SYNTHETIC_STATE_ONLY", "OBSERVED_DRY_RUN"}:
            status = "HOLD_NO_OPPORTUNITY"
            reasons.append("OPPORTUNITY_INSUFFICIENT_KEEP_CASH")
    idle_after = money(budgets["b_idle_usdt"]) - positive_spent - negative_spent
    budgets["planned_positive_cash_usdt"] = dec_str(positive_spent)
    budgets["planned_negative_margin_usdt"] = dec_str(negative_spent)
    budgets["planned_idle_after_usdt"] = dec_str(idle_after)
    budgets["negative_budget_reserved_usdt"] = dec_str(negative_budget)
    budgets["a_cash_used_for_b_margin_usdt"] = "0"
    return {
        "status": status,
        "new_exposure_permitted": deploy_new,
        "new_exposure_proposed": proposed,
        "reason_codes": sorted(set(reasons)),
        "budgets": budgets,
        "breakeven": _breakeven_table(snapshot, policy, costs, yield_apr),
        "proposals": proposals,
        "intents": intents,
        "capacity_claim": "NONE" if not weaker else "ADV_ONLY_WEAKER_NOT_VERIFIED",
        "queue_modeled": False,
        "targets_are_not_fills": True,
    }


def _breakeven_table(snapshot, policy, costs, yield_apr):
    rows = []
    for row in _ranked(snapshot["universe"]):
        bounds = carry_bounds(
            money(row["funding_rate_observed"]),
            row["funding_interval_hours"],
            policy["max_planned_hold_hours"],
            policy["year_hours"],
            money(policy["initial_margin_fraction"]),
            costs,
            yield_apr,
        )
        bounds["asset"] = row["asset"]
        bounds["book"] = "POSITIVE_CARRY"
        rows.append(bounds)
    return rows


def _budgets(policy, identity, ledger):
    idle = cash_amount(ledger, "B", "IDLE")
    safety = cash_amount(ledger, "B", "SAFETY_RESERVE")
    margin = cash_amount(ledger, "B", "PERP_MARGIN")
    parked = cash_amount(ledger, "B", "OPEN_ORDER")
    transit = cash_amount(ledger, "B", "IN_TRANSIT")
    base = idle + safety + margin + parked + transit
    required = max(base * money(policy["safety_reserve_fraction_of_cash"]), base * money(policy["cash_floor_fraction_of_cash"]))
    lock = max(ZERO, required - safety)
    bare = max(ZERO, idle - lock)
    haircut = money(policy["depeg_haircut"])
    effective = bare * (Decimal(1) - haircut)
    structural = money(identity["cash_budget_usdt"]) if identity["cash_budget_usdt"] is not None else ZERO
    return {
        "structural_cash_budget_usdt": dec_str(structural),
        "arrived_b_idle_usdt": dec_str(idle),
        "b_idle_usdt": dec_str(idle),
        "b_safety_usdt": dec_str(safety),
        "b_perp_margin_usdt": dec_str(margin),
        "b_open_order_usdt": dec_str(parked),
        "b_in_transit_usdt": dec_str(transit),
        "a_buyback_reserve_usdt": dec_str(cash_amount(ledger, "A", "BUYBACK_RESERVE")),
        "a_idle_usdt": dec_str(cash_amount(ledger, "A", "IDLE")),
        "virtual_safety_hold_usdt": dec_str(lock),
        "deployable_before_haircut_usdt": dec_str(bare),
        "depeg_haircut": dec_str(haircut),
        "effective_deployable_usdt": dec_str(effective),
        "safety_hold_is_virtual": True,
        "a_cash_is_not_b_deployable": True,
    }


def _known_yield(snapshot):
    known = []
    for row in snapshot["yield_products"]:
        if row["account"] == "B" and row["asset"] == "USDT" and row["apr_known"] is True:
            known.append(row)
    if not known:
        return None, None
    known.sort(key=lambda row: (-money(row["apr"]), row["product_id"]))
    best = known[0]
    return money(best["apr"]), best


def _recall_assets(snapshot, ledger):
    assets = set()
    if "COIN_RECALL" in snapshot["risk_flags"]:
        assets.update(row["asset"] for row in ledger["liabilities"] if money(row["qty_token"]) > 0)
        assets.update(row["asset"] for row in snapshot["liabilities"] if money(row["qty_token"]) > 0)
    for row in ledger["liabilities"]:
        if row["recall_status"] in {"RECEIVED", "IN_PROGRESS"} and money(row["qty_token"]) > 0:
            assets.add(row["asset"])
    for row in snapshot["liabilities"]:
        if row["recall_status"] in {"RECEIVED", "IN_PROGRESS"} and money(row["qty_token"]) > 0:
            assets.add(row["asset"])
    return assets


def _ranked(universe):
    rows = [row for row in universe if row["trading_status"] == "TRADING" and row["filters_ok"]]
    return sorted(rows, key=lambda row: (-money(row["funding_rate_observed"]), row["asset"]))


def _negative_ranked(universe, gate):
    rows = []
    for row in universe:
        if row["trading_status"] != "TRADING" or not row["filters_ok"]:
            continue
        eligible, _ = negative_signal_eligible(
            money(row["funding_rate_observed"]), row["funding_interval_hours"], money(row["basis"]), gate
        )
        if eligible:
            rows.append(row)
    return sorted(rows, key=lambda row: (money(row["funding_rate_observed"]), row["asset"]))


def _negative_budget(policy, identity, remaining, snapshot, gate, deploy_new, ledger):
    if not deploy_new or identity["cash_budget_usdt"] is None:
        return ZERO
    if not _negative_can_add(snapshot, gate, policy):
        return ZERO
    cap = money(identity["cash_budget_usdt"]) * money(policy["negative_budget_fraction_of_cash"])
    if policy["negative_budget_cap_usdt"] is not None:
        cap = min(cap, money(policy["negative_budget_cap_usdt"]))
    return min(cap, remaining)


def _negative_can_add(snapshot, gate, policy):
    for row in _negative_ranked(snapshot["universe"], gate):
        decision = next((item for item in snapshot["rc2_negative_decisions"] if item["asset"] == row["asset"]), None)
        if decision is not None and decision["allow_new"] is True:
            return True
        if decision is None and snapshot["synthetic"] and policy["allow_new_negative_without_rc2_decision"]:
            return True
    return False


def _positive_proposal(row, bounds, policy, identity, ledger, room, deploy_new, costs, yield_apr, faults, recall):
    asset = row["asset"]
    current_spot = position_qty(ledger, "POSITIVE_CARRY", asset, "spot_qty_token")
    current_perp = position_qty(ledger, "POSITIVE_CARRY", asset, "perp_qty_token")
    blocked = f"POSITIVE_CARRY:{asset}" in ledger["blocked_assets"]
    local_reasons = []
    stop_asset = blocked or asset in recall or row["trading_status"] != "TRADING" or not row["filters_ok"] or not deploy_new
    if faults or asset in recall:
        if current_spot == 0 and current_perp == 0 and asset not in recall:
            return None, ZERO, False
        return _risk_target("POSITIVE_CARRY", asset, ledger, faults, recall, policy), ZERO, False
    healthy = current_perp == -current_spot
    if not healthy:
        local_reasons.append("LEG_IMBALANCE_HOLD")
        proposal = _hold("POSITIVE_CARRY", asset, ledger, local_reasons)
        return proposal, ZERO, False
    if stop_asset or money(row["funding_rate_observed"]) <= 0:
        if money(row["funding_rate_observed"]) <= 0:
            local_reasons.append("FUNDING_NOT_POSITIVE")
        if blocked:
            local_reasons.append("RETRY_BUDGET_EXHAUSTED")
        if current_spot == 0 and current_perp == 0:
            return None, ZERO, False
        return _hold("POSITIVE_CARRY", asset, ledger, local_reasons or ["HOLD_EXISTING"]), ZERO, False
    if not _costs_complete(costs):
        return _hold("POSITIVE_CARRY", asset, ledger, ["UNVERIFIED_NO_NEW"]), ZERO, False
    net = None if bounds["net_carry_apr"] is None else money(bounds["net_carry_apr"])
    if net is None or net <= 0:
        return _hold("POSITIVE_CARRY", asset, ledger, ["COSTS_EXCEED_OBSERVED_FUNDING_OVER_POLICY_HOLD"]), ZERO, False
    if yield_apr is not None and bounds["carry_beats_known_yield"] is not True:
        return _hold("POSITIVE_CARRY", asset, ledger, ["YIELD_BEATS_CARRY_KEEP_CASH"]), ZERO, False
    minimum = policy["min_completion_probability"]
    if minimum is not None and costs["completion"] < money(minimum):
        return _hold("POSITIVE_CARRY", asset, ledger, ["COMPLETION_BELOW_POLICY"]), ZERO, False
    if not row["adv_known"] or row["lagged_adv_quote_usdt"] is None:
        return _hold("POSITIVE_CARRY", asset, ledger, ["ADV_UNKNOWN"]), ZERO, False
    weaker = False
    oi_cap = None
    if row["oi_known"]:
        oi_cap = money(row["lagged_oi_quote_usdt"]) * money(policy["oi_cap_fraction"])
    elif policy["oi_missing_policy"] == "reject_add":
        return _hold("POSITIVE_CARRY", asset, ledger, ["OI_MISSING_REJECT_ADD"]), ZERO, False
    else:
        weaker = True
        local_reasons.append("CAPACITY_ADV_ONLY_WEAKER")
    multiple = Decimal(1) + money(policy["initial_margin_fraction"])
    adv = money(row["lagged_adv_quote_usdt"])
    gross = money(identity["gross_assets_usdt"]) if identity["gross_assets_usdt"] is not None else ZERO
    conc = gross * money(policy["per_coin_gross_cap_fraction"])
    cash_cap = room / multiple if multiple > 0 else ZERO
    target_cap = adv * money(policy["adv_participation_target"])
    caps = [cash_cap, target_cap, conc]
    if oi_cap is not None:
        caps.append(oi_cap)
    desired_notional = min(caps)
    if desired_notional < 0:
        desired_notional = ZERO
    spot = money(row["spot_usdt"])
    perp = money(row["perp_usdt"])
    current_notional = current_spot * spot
    max_delta = adv * money(policy["adv_participation_delta"])
    if desired_notional > current_notional + max_delta:
        desired_notional = current_notional + max_delta
    if desired_notional < current_notional:
        desired_notional = current_notional
    qty = floor_step(desired_notional / spot, money(row["qty_step"]))
    if qty < current_spot:
        qty = current_spot
    if qty < money(row["min_qty"]) and qty > current_spot:
        qty = current_spot
    delta = qty - current_spot
    delta_notional = delta * spot
    if delta > 0 and delta_notional < money(policy["min_order_usdt"]):
        qty = current_spot
        delta = ZERO
    if delta > 0 and delta_notional < money(row["min_notional_usdt"]):
        qty = current_spot
        delta = ZERO
    spent = delta * spot + delta * perp * money(policy["initial_margin_fraction"])
    if spent > room and delta > 0:
        qty = current_spot
        delta = ZERO
        spent = ZERO
    if qty == 0 and current_spot == 0:
        return None, ZERO, weaker
    local_reasons.append("ABSOLUTE_TARGET")
    if delta == 0:
        local_reasons.append("NO_ADD")
    proposal = _absolute("POSITIVE_CARRY", asset, ledger, qty, -qty, ZERO, spent, local_reasons, 6)
    proposal["capacity_mode"] = "ADV_ONLY_WEAKER" if weaker else "LAGGED_INPUT"
    proposal["capacity_claim"] = "INPUT_LAGGED_NOT_VENUE_VERIFIED"
    return proposal, spent, weaker


def _negative_proposal(row, policy, identity, ledger, snapshot, gate, room, deploy_new, faults, recall):
    asset = row["asset"]
    if faults or asset in recall or not deploy_new:
        if _has_activity(ledger, "NEGATIVE_FUNDING", asset) or liability_qty(ledger, asset) > 0 or asset in recall:
            return _risk_target("NEGATIVE_FUNDING", asset, ledger, faults, recall, policy), ZERO
        return None, ZERO
    current_perp = position_qty(ledger, "NEGATIVE_FUNDING", asset, "perp_qty_token")
    current_spot = position_qty(ledger, "NEGATIVE_FUNDING", asset, "spot_qty_token")
    current_liab = liability_qty(ledger, asset)
    eligible, equivalent = negative_signal_eligible(
        money(row["funding_rate_observed"]), row["funding_interval_hours"], money(row["basis"]), gate
    )
    reasons = ["FROZEN_RC2_THRESHOLD_ADAPTED", "RC2_ENGINE_NOT_INVOKED"]
    if not eligible:
        if current_liab == 0 and current_perp == 0 and current_spot == 0:
            return None, ZERO
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons + ["NEGATIVE_SIGNAL_NOT_ELIGIBLE"]), ZERO
    decision = next((item for item in snapshot["rc2_negative_decisions"] if item["asset"] == asset), None)
    synthetic_ok = snapshot["synthetic"] and policy["allow_new_negative_without_rc2_decision"]
    if decision is None and not synthetic_ok:
        reasons.append("NEGATIVE_ELIGIBLE_BUT_UNSIZED_PENDING_RC2")
        if current_liab == 0 and current_perp == 0 and current_spot == 0:
            return None, ZERO
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons), ZERO
    if decision is not None and decision["allow_new"] is not True and current_perp >= 0:
        reasons.append("RC2_TIGHTENED_TO_EXISTING")
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons), ZERO
    if f"NEGATIVE_FUNDING:{asset}" in ledger["blocked_assets"]:
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons + ["RETRY_BUDGET_EXHAUSTED"]), ZERO
    if not row["borrow_available"] and current_liab == 0:
        return None, ZERO
    if not row["adv_known"]:
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons + ["ADV_UNKNOWN"]), ZERO
    weaker = False
    oi_cap = None
    if row["oi_known"]:
        oi_cap = money(row["lagged_oi_quote_usdt"]) * money(policy["oi_cap_fraction"])
    elif policy["oi_missing_policy"] == "reject_add":
        return _hold("NEGATIVE_FUNDING", asset, ledger, reasons + ["OI_MISSING_REJECT_ADD"]), ZERO
    else:
        weaker = True
        reasons.append("CAPACITY_ADV_ONLY_WEAKER")
    perp = money(row["perp_usdt"])
    margin_fraction = money(policy["initial_margin_fraction"])
    adv = money(row["lagged_adv_quote_usdt"])
    gross = money(identity["gross_assets_usdt"]) if identity["gross_assets_usdt"] is not None else ZERO
    conc = gross * money(policy["per_coin_gross_cap_fraction"])
    borrowable = money(row["borrowable_qty_token"]) if row["borrow_available"] else ZERO
    max_qty_cash = (room / (perp * margin_fraction)) if perp * margin_fraction > 0 else ZERO
    max_qty_adv = (adv * money(policy["adv_participation_target"])) / perp
    max_qty_conc = conc / perp if perp else ZERO
    max_qty = min(max_qty_cash, max_qty_adv, max_qty_conc, current_liab + borrowable)
    if oi_cap is not None:
        max_qty = min(max_qty, oi_cap / perp)
    if decision is not None:
        max_qty = min(max_qty, money(decision["max_perp_qty_token"]))
        reasons.append("RC2_DECISION_TIGHTENED")
    else:
        reasons.append("NEGATIVE_ADAPTER_WITHOUT_RC2_DECISION_SYNTHETIC_ONLY")
    current_notional = max(current_perp, ZERO) * perp
    max_delta = adv * money(policy["adv_participation_delta"])
    desired_notional = min(max_qty * perp, current_notional + max_delta)
    qty = floor_step(desired_notional / perp, money(row["qty_step"]))
    if qty < current_perp:
        qty = current_perp
    delta = qty - current_perp
    if delta > 0 and delta * perp < money(policy["min_order_usdt"]):
        qty = current_perp
        delta = ZERO
    spent = delta * perp * margin_fraction if delta > 0 else ZERO
    if spent > room:
        qty = current_perp
        delta = ZERO
        spent = ZERO
    # Active negative target: borrowed coin sold, perp long equals liability.
    desired_liability = qty
    desired_spot = ZERO
    desired_perp = qty
    if qty == 0 and current_liab == 0 and current_perp == 0 and current_spot == 0:
        return None, ZERO
    proposal = _absolute("NEGATIVE_FUNDING", asset, ledger, desired_spot, desired_perp, desired_liability, spent, reasons, 5)
    proposal["capacity_mode"] = "ADV_ONLY_WEAKER" if weaker else "LAGGED_INPUT"
    proposal["eight_hour_equivalent_rate"] = dec_str(equivalent)
    proposal["a_sale_proceeds_stay_in_a"] = True
    return proposal, spent


def _hold(book, asset, ledger, reasons):
    return _absolute(
        book,
        asset,
        ledger,
        position_qty(ledger, book, asset, "spot_qty_token"),
        position_qty(ledger, book, asset, "perp_qty_token"),
        liability_qty(ledger, asset) if book == "NEGATIVE_FUNDING" else ZERO,
        ZERO,
        reasons,
        8,
    )


def _risk_target(book, asset, ledger, faults, recall, policy):
    current_spot = position_qty(ledger, book, asset, "spot_qty_token")
    current_perp = position_qty(ledger, book, asset, "perp_qty_token")
    current_liab = liability_qty(ledger, asset) if book == "NEGATIVE_FUNDING" else ZERO
    mode = "RECALL_UNWIND" if asset in recall and book == "NEGATIVE_FUNDING" else policy["on_fault"]
    reasons = ["STOP_NEW" if mode == "STOP_NEW" else mode, "TARGET_IS_NOT_A_FILL"]
    if mode == "STOP_NEW":
        spot, perp, liab = current_spot, current_perp, current_liab
    elif mode == "REDUCE_TO_FLAT_TARGET":
        spot, perp, liab = ZERO, ZERO, ZERO
        reasons.append("LEDGER_UNCHANGED_UNTIL_RECEIPT")
    else:
        spot, perp, liab = _reduce_toward_hedge(book, current_spot, current_perp, current_liab)
        reasons.append("REDUCE_ONLY_NO_INVENTED_FLAT")
    if asset in recall:
        reasons.append("COIN_RECALL")
    return _absolute(book, asset, ledger, spot, perp, liab, ZERO, reasons + faults, 0)


def _reduce_toward_hedge(book, spot, perp, liability):
    if book == "POSITIVE_CARRY":
        matched = min(spot, abs(perp))
        return matched, -matched, ZERO
    if perp < 0:
        desired_perp = ZERO
    else:
        desired_perp = min(perp, liability)
    desired_spot = max(ZERO, liability - max(desired_perp, ZERO))
    return desired_spot, desired_perp, liability


def _absolute(book, asset, ledger, spot, perp, liability, spent, reasons, priority):
    return {
        "book": book,
        "asset": asset,
        "desired_spot_qty_token": dec_str(spot),
        "desired_perp_qty_token": dec_str(perp),
        "desired_liability_qty_token": dec_str(liability),
        "current_spot_qty_token": dec_str(position_qty(ledger, book, asset, "spot_qty_token")),
        "current_perp_qty_token": dec_str(position_qty(ledger, book, asset, "perp_qty_token")),
        "current_liability_qty_token": dec_str(liability_qty(ledger, asset) if book == "NEGATIVE_FUNDING" else ZERO),
        "planned_new_cash_usdt": dec_str(spent),
        "reason_codes": sorted(set(reasons)),
        "priority": priority,
        "capacity_mode": "NONE",
        "capacity_claim": "NONE",
    }


def _has_activity(ledger, book, asset):
    return (
        position_qty(ledger, book, asset, "spot_qty_token") != 0
        or position_qty(ledger, book, asset, "perp_qty_token") != 0
        or bool(live_orders(ledger, book, asset))
    )


def _yield_intents(product, yield_apr, proposals, budgets, deploy_new, recall, ledger, policy):
    if product is None:
        return [{"action": "KEEP_CASH", "reason_codes": ["YIELD_APR_UNKNOWN_NO_HARDCODE"], "amount_usdt": "0"}]
    if recall or not deploy_new:
        return [{"action": "REDEEM_TO_CASH_WAIT_SETTLEMENT", "product_id": product["product_id"], "reason_codes": ["RECALL_OR_STOP"], "amount_usdt": dec_str(money(product["quantity"]) * money(product["nav"]))}]
    carry_uses = sum((money(item["planned_new_cash_usdt"]) for item in proposals if item["book"] == "POSITIVE_CARRY"), ZERO)
    idle = money(budgets["b_idle_usdt"])
    leftover = idle - carry_uses - money(budgets["virtual_safety_hold_usdt"])
    if leftover < 0:
        leftover = ZERO
    # Subscribe only when a known APR beats new carry. Existing carry is not force-unwound.
    beats = any(
        item.get("book") == "POSITIVE_CARRY" and "YIELD_BEATS_CARRY_KEEP_CASH" in item["reason_codes"] for item in proposals
    )
    any_positive_add = any(money(item["planned_new_cash_usdt"]) > 0 and item["book"] == "POSITIVE_CARRY" for item in proposals)
    if yield_apr is None:
        return [{"action": "KEEP_CASH", "reason_codes": ["YIELD_APR_UNKNOWN_NO_HARDCODE"], "amount_usdt": "0"}]
    if any_positive_add and not beats:
        return [{"action": "KEEP_CASH", "product_id": product["product_id"], "reason_codes": ["CARRY_BEATS_YIELD_ON_MARGINAL_CASH"], "amount_usdt": "0", "observed_apr": dec_str(yield_apr)}]
    if leftover == 0:
        return [{"action": "KEEP_CASH", "reason_codes": ["NO_IDLE_AFTER_RESERVE"], "amount_usdt": "0"}]
    return [{
        "action": "SUBSCRIBE_INTENTION",
        "product_id": product["product_id"],
        "amount_usdt": dec_str(leftover),
        "observed_apr": dec_str(yield_apr),
        "reason_codes": ["KNOWN_APR_BEATS_OR_REPLACES_NEW_CARRY", "INTENTION_NOT_A_FILL"],
        "ledger_unchanged": True,
    }]


def _liability_intents(proposal, ledger):
    if proposal["book"] != "NEGATIVE_FUNDING":
        return []
    desired = money(proposal["desired_liability_qty_token"])
    current = liability_qty(ledger, proposal["asset"])
    if desired > current:
        return [{"action": "BORROW_INTENTION", "asset": proposal["asset"], "qty_token": dec_str(desired - current), "reason_codes": ["INTENTION_NOT_A_FILL"]}]
    if desired < current:
        return [{"action": "RETURN_INTENTION", "asset": proposal["asset"], "qty_token": dec_str(current - desired), "reason_codes": ["LIABILITY_REMAINS_UNTIL_RETURN_RECEIPT"]}]
    return []
