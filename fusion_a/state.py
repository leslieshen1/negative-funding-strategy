"""Lot ledger. Cash, spot, margin, yield, in-transit and open-order reserves are separate lots.

A sale-proceeds lot cannot be posted as B margin. Borrow interest is kept at zero.
Receipts that do not fit the lots are quarantined rather than inventing cash or a flat position.
"""

from copy import deepcopy
from decimal import Decimal

from fusion_a.decimal_util import FusionError, dec_str, money
from fusion_a.validate import authorized

ZERO = Decimal(0)


def empty_ledger(source_mode, synthetic, fixture_label):
    return {
        "schema_version": "nfb.fusion-a.ledger.v1",
        "ledger_version": 0,
        "source_mode": source_mode,
        "synthetic": synthetic,
        "fixture_label": fixture_label,
        "opening_snapshot_id": None,
        "last_snapshot_id": None,
        "last_sequence": None,
        "applied_snapshot_ids": [],
        "cash_lots": [],
        "positions": [],
        "liabilities": [],
        "open_orders": [],
        "yield_positions": [],
        "applied_flow_ids": [],
        "receipt_hashes": {},
        "rejected_hashes": {},
        "applied_fill_ids": [],
        "applied_funding_ids": [],
        "pending_receipts": [],
        "next_sequence": {},
        "quarantine": [],
        "issued_targets": {},
        "retry_counts": {},
        "blocked_assets": [],
        "imbalance_since_ms": {},
        "marks": {},
        "initial_margin_fraction": None,
        "realized_price_usdt": "0",
        "opening_price_level_usdt": "0",
        "start_net_usdt": "0",
        "pnl": {
            "funding_usdt": "0",
            "fees_usdt": "0",
            "yield_realized_usdt": "0",
            "yield_unpaid_usdt": "0",
            "borrow_interest_usdt": "0",
            "external_flows_usdt": "0",
            "fees_complete": True,
        },
        "flags": [],
    }


def bump(ledger):
    ledger["ledger_version"] += 1


def cash_amount(ledger, account, purpose):
    total = ZERO
    for lot in ledger["cash_lots"]:
        if lot["account"] == account and lot["purpose"] == purpose:
            total += money(lot["amount"])
    return total


def _lot(ledger, account, purpose):
    for lot in ledger["cash_lots"]:
        if lot["account"] == account and lot["purpose"] == purpose:
            return lot
    lot = {
        "lot_id": f"{account}-{purpose}",
        "account": account,
        "purpose": purpose,
        "amount": "0",
    }
    ledger["cash_lots"].append(lot)
    return lot


def add_cash(ledger, account, purpose, amount):
    if amount < 0:
        raise FusionError("NEGATIVE_CASH_MOVEMENT")
    if amount == 0:
        return
    if purpose == "PERP_MARGIN" and account != "B":
        raise FusionError("A_CASH_CANNOT_BE_PERP_MARGIN")
    if purpose == "BUYBACK_RESERVE" and account != "A":
        raise FusionError("BUYBACK_RESERVE_IS_A_ONLY")
    lot = _lot(ledger, account, purpose)
    lot["amount"] = dec_str(money(lot["amount"]) + amount)


def sub_cash(ledger, account, purpose, amount):
    if amount < 0:
        raise FusionError("NEGATIVE_CASH_MOVEMENT")
    if amount == 0:
        return
    lot = _lot(ledger, account, purpose)
    have = money(lot["amount"])
    if have < amount:
        raise FusionError("INSUFFICIENT_CASH", f"{account} {purpose}")
    lot["amount"] = dec_str(have - amount)


def position(ledger, book, asset):
    for row in ledger["positions"]:
        if row["book"] == book and row["asset"] == asset:
            return row
    spot_account = "B" if book == "POSITIVE_CARRY" else "A"
    row = {
        "book": book,
        "asset": asset,
        "spot_account": spot_account,
        "perp_account": "B",
        "spot_qty_token": "0",
        "spot_avg_cost_usdt": "0",
        "perp_qty_token": "0",
        "perp_avg_entry_usdt": "0",
        "margin_usdt": "0",
        "spot_reserved_token": "0",
        "perp_close_reserved_token": "0",
    }
    ledger["positions"].append(row)
    return row


def peek_position(ledger, book, asset):
    for row in ledger["positions"]:
        if row["book"] == book and row["asset"] == asset:
            return row
    return None


def position_qty(ledger, book, asset, field):
    row = peek_position(ledger, book, asset)
    if row is None:
        return ZERO
    return money(row[field])


def liability_qty(ledger, asset):
    return sum((money(row["qty_token"]) for row in ledger["liabilities"] if row["asset"] == asset), ZERO)


def mark_price(ledger, asset, kind):
    row = ledger["marks"].get(asset)
    if row is None:
        raise FusionError("MISSING_MARK", asset)
    return money(row[kind])


def set_marks(ledger, universe):
    marks = {}
    for row in universe:
        marks[row["asset"]] = {
            "spot": dec_str(money(row["spot_usdt"])),
            "perp": dec_str(money(row["perp_usdt"])),
            "index": dec_str(money(row["index_usdt"])),
        }
    for row in ledger["positions"]:
        if _open_position(row) and row["asset"] not in marks:
            raise FusionError("MISSING_MARK", row["asset"])
    for row in ledger["liabilities"]:
        if money(row["qty_token"]) > 0 and row["asset"] not in marks:
            raise FusionError("MISSING_MARK", row["asset"])
    ledger["marks"] = marks


def _open_position(row):
    return money(row["spot_qty_token"]) != 0 or money(row["perp_qty_token"]) != 0 or money(row["margin_usdt"]) != 0


def price_level(ledger):
    level = money(ledger["realized_price_usdt"])
    for row in ledger["positions"]:
        asset = row["asset"]
        if asset not in ledger["marks"]:
            if _open_position(row):
                raise FusionError("MISSING_MARK", asset)
            continue
        spot_qty = money(row["spot_qty_token"])
        if spot_qty:
            level += spot_qty * (mark_price(ledger, asset, "spot") - money(row["spot_avg_cost_usdt"]))
        perp_qty = money(row["perp_qty_token"])
        if perp_qty:
            level += perp_qty * (mark_price(ledger, asset, "perp") - money(row["perp_avg_entry_usdt"]))
    for row in ledger["liabilities"]:
        qty = money(row["qty_token"])
        if qty:
            level += -qty * (mark_price(ledger, row["asset"], "spot") - money(row["inception_mark_usdt"]))
    return level


def equity(ledger):
    cash = sum((money(lot["amount"]) for lot in ledger["cash_lots"]), ZERO)
    spot = ZERO
    perp = ZERO
    for row in ledger["positions"]:
        asset = row["asset"]
        spot_qty = money(row["spot_qty_token"])
        if spot_qty:
            spot += spot_qty * mark_price(ledger, asset, "spot")
        perp_qty = money(row["perp_qty_token"])
        if perp_qty:
            perp += perp_qty * (mark_price(ledger, asset, "perp") - money(row["perp_avg_entry_usdt"]))
    yielded = ZERO
    for row in ledger["yield_positions"]:
        yielded += money(row["quantity"]) * money(row["nav"]) + money(row["accrued_unpaid_usdt"])
    liability = ZERO
    for row in ledger["liabilities"]:
        qty = money(row["qty_token"])
        if qty:
            liability += qty * mark_price(ledger, row["asset"], "spot")
    return cash + spot + perp + yielded - liability


def price_pnl(ledger):
    return price_level(ledger) - money(ledger["opening_price_level_usdt"])


def explain(ledger):
    eq = equity(ledger)
    start = money(ledger["start_net_usdt"])
    external = money(ledger["pnl"]["external_flows_usdt"])
    funding = money(ledger["pnl"]["funding_usdt"])
    fees = money(ledger["pnl"]["fees_usdt"])
    paid = money(ledger["pnl"]["yield_realized_usdt"])
    unpaid = money(ledger["pnl"]["yield_unpaid_usdt"])
    price = price_pnl(ledger)
    residual = eq - start - external - funding + fees - paid - unpaid - price
    obligations = obligations_open(ledger)
    fees_complete = ledger["pnl"]["fees_complete"] is True
    if residual != 0:
        status = "BROKEN"
    elif not fees_complete:
        status = "UNVERIFIED_FEES"
    else:
        status = "HELD"
    return {
        "equity_usdt": dec_str(eq),
        "start_net_usdt": dec_str(start),
        "external_flows_usdt": dec_str(external),
        "funding_usdt": dec_str(funding),
        "fees_usdt": dec_str(fees),
        "yield_realized_usdt": dec_str(paid),
        "yield_unpaid_usdt": dec_str(unpaid),
        "borrow_interest_usdt": "0",
        "price_pnl_usdt": dec_str(price),
        "book_residual_usdt": dec_str(residual),
        "identity_status": status,
        "identity_holds": status == "HELD",
        "fees_complete": fees_complete,
        "obligations_open": obligations,
        "annualized_return": None,
        "return_evidence": False,
        "sale_proceeds_are_not_profit": True,
    }


def obligations_open(ledger):
    for row in ledger["liabilities"]:
        if money(row["qty_token"]) > 0:
            return True
    for row in ledger["positions"]:
        if money(row["spot_qty_token"]) != 0 or money(row["perp_qty_token"]) != 0:
            return True
    for row in ledger["open_orders"]:
        if row["state"] in {"NEW", "CANCEL_REQUESTED"}:
            return True
    for row in ledger["yield_positions"]:
        if row["state"] in {"SUBSCRIBED", "REDEEM_REQUESTED", "REDEEM_PENDING"} and (
            money(row["quantity"]) > 0 or money(row["accrued_unpaid_usdt"]) > 0
        ):
            return True
    if cash_amount(ledger, "A", "IN_TRANSIT") > 0 or cash_amount(ledger, "B", "IN_TRANSIT") > 0:
        return True
    return False


def freeze_opening_valuation(ledger):
    ledger["opening_price_level_usdt"] = dec_str(price_level(ledger))
    ledger["start_net_usdt"] = dec_str(equity(ledger))
    ledger["realized_price_usdt"] = "0"


def seed_from_snapshot(snapshot):
    ledger = empty_ledger(snapshot["source_mode"], snapshot["synthetic"], snapshot["fixture_label"])
    ledger["opening_snapshot_id"] = snapshot["snapshot_id"]
    for row in snapshot["cash"]:
        add_cash(ledger, row["account"], row["purpose"], money(row["amount"]))
    for row in snapshot["positions"]:
        current = position(ledger, row["book"], row["asset"])
        current.update(deepcopy(row))
    for row in snapshot["liabilities"]:
        ledger["liabilities"].append(deepcopy(row))
    for row in snapshot["open_orders"]:
        ledger["open_orders"].append(deepcopy(row))
    for row in snapshot["yield_products"]:
        if money(row["quantity"]) == 0 and money(row["accrued_unpaid_usdt"]) == 0 and row["state"] == "AVAILABLE":
            continue
        ledger["yield_positions"].append(
            {
                "product_id": row["product_id"],
                "account": row["account"],
                "asset": row["asset"],
                "state": row["state"],
                "quantity": dec_str(money(row["quantity"])),
                "nav": dec_str(money(row["nav"])),
                "apr": row["apr"],
                "apr_known": row["apr_known"],
                "redeem_delay_ms": row["redeem_delay_ms"],
                "accrued_unpaid_usdt": dec_str(money(row["accrued_unpaid_usdt"])),
            }
        )
    set_marks(ledger, snapshot["universe"])
    _check_order_reservations(ledger)
    freeze_opening_valuation(ledger)
    return ledger


def _check_order_reservations(ledger):
    implied = _implied_order_cash(ledger)
    posted = cash_amount(ledger, "A", "OPEN_ORDER") + cash_amount(ledger, "B", "OPEN_ORDER")
    if implied != posted:
        raise FusionError("OPEN_ORDER_RESERVATION_MISMATCH", f"implied {dec_str(implied)} posted {dec_str(posted)}")
    expected_spot = {}
    expected_perp = {}
    for order in ledger["open_orders"]:
        if order["state"] not in {"NEW", "CANCEL_REQUESTED"}:
            continue
        remaining = _order_remaining(order)
        key = (order["book"], order["asset"])
        if order["leg"] == "SPOT" and order["side"] == "SELL":
            expected_spot[key] = expected_spot.get(key, ZERO) + remaining
        if order["leg"] == "PERP" and not order["reserves_margin"]:
            expected_perp[key] = expected_perp.get(key, ZERO) + remaining
    for row in ledger["positions"]:
        key = (row["book"], row["asset"])
        if money(row["spot_reserved_token"]) != expected_spot.get(key, ZERO):
            raise FusionError("SPOT_RESERVATION_MISMATCH", row["asset"])
        if money(row["perp_close_reserved_token"]) != expected_perp.get(key, ZERO):
            raise FusionError("PERP_RESERVATION_MISMATCH", row["asset"])


def _implied_order_cash(ledger):
    total = ZERO
    for order in ledger["open_orders"]:
        if order["state"] not in {"NEW", "CANCEL_REQUESTED"}:
            continue
        total += _order_cash_reserve(order)
    return total


def _order_remaining(order):
    return money(order["qty_token"]) - money(order["filled_qty_token"])


def _order_cash_reserve(order):
    remaining = _order_remaining(order)
    if remaining == 0:
        return ZERO
    price = money(order["limit_price"])
    if order["leg"] == "SPOT" and order["side"] == "BUY":
        return remaining * price
    if order["leg"] == "PERP" and _perp_order_increases(order):
        return remaining * price * money(order["initial_margin_fraction"])
    return ZERO


def _perp_order_increases(order):
    return order.get("reserves_margin") is True


def apply_capital_flow(ledger, flow_id, account, kind, amount):
    if flow_id in ledger["applied_flow_ids"]:
        return False
    amount = money(amount) if not isinstance(amount, Decimal) else amount
    if kind == "SUBSCRIPTION":
        add_cash(ledger, account, "IDLE", amount)
        ledger["pnl"]["external_flows_usdt"] = dec_str(money(ledger["pnl"]["external_flows_usdt"]) + amount)
    elif kind == "REDEMPTION":
        sub_cash(ledger, account, "IDLE", amount)
        ledger["pnl"]["external_flows_usdt"] = dec_str(money(ledger["pnl"]["external_flows_usdt"]) - amount)
    else:
        raise FusionError("UNKNOWN_FLOW_KIND")
    ledger["applied_flow_ids"].append(flow_id)
    return True


def _add_realized(ledger, amount):
    ledger["realized_price_usdt"] = dec_str(money(ledger["realized_price_usdt"]) + amount)


def receive_spot(ledger, book, asset, qty, mark):
    row = position(ledger, book, asset)
    old = money(row["spot_qty_token"])
    avg = money(row["spot_avg_cost_usdt"])
    new = old + qty
    row["spot_avg_cost_usdt"] = dec_str((old * avg + qty * mark) / new)
    row["spot_qty_token"] = dec_str(new)


def buy_spot(ledger, book, asset, account, qty, price, cash_purpose):
    sub_cash(ledger, account, cash_purpose, qty * price)
    receive_spot(ledger, book, asset, qty, price)


def sell_spot(ledger, book, asset, account, qty, price, cash_purpose):
    row = position(ledger, book, asset)
    held = money(row["spot_qty_token"])
    reserved = money(row["spot_reserved_token"])
    if qty > held - reserved:
        raise FusionError("INSUFFICIENT_SPOT")
    avg = money(row["spot_avg_cost_usdt"])
    _add_realized(ledger, (price - avg) * qty)
    add_cash(ledger, account, cash_purpose, qty * price)
    left = held - qty
    row["spot_qty_token"] = dec_str(left)
    if left == 0:
        row["spot_avg_cost_usdt"] = "0"


def _apply_perp(ledger, book, asset, side, qty, price, margin_fraction, from_purpose):
    row = position(ledger, book, asset)
    signed = qty if side == "BUY" else -qty
    current = money(row["perp_qty_token"])
    if current == 0 or (current > 0 and signed > 0) or (current < 0 and signed < 0):
        _increase_perp(ledger, row, signed, price, margin_fraction, from_purpose)
        return
    closing = min(abs(current), abs(signed))
    _decrease_perp(ledger, row, closing, price)
    leftover = abs(signed) - closing
    if leftover:
        opened = leftover if signed > 0 else -leftover
        _increase_perp(ledger, row, opened, price, margin_fraction, from_purpose)


def _increase_perp(ledger, row, signed, price, margin_fraction, from_purpose):
    current = money(row["perp_qty_token"])
    avg = money(row["perp_avg_entry_usdt"])
    added = abs(signed)
    margin = added * price * margin_fraction
    sub_cash(ledger, "B", from_purpose, margin)
    add_cash(ledger, "B", "PERP_MARGIN", margin)
    new = current + signed
    if current == 0:
        new_avg = price
    elif (current > 0) == (new > 0):
        new_avg = (abs(current) * avg + added * price) / abs(new)
    else:
        new_avg = price
    row["perp_qty_token"] = dec_str(new)
    row["perp_avg_entry_usdt"] = "0" if new == 0 else dec_str(new_avg)
    row["margin_usdt"] = dec_str(money(row["margin_usdt"]) + margin)


def _decrease_perp(ledger, row, closing, price):
    current = money(row["perp_qty_token"])
    avg = money(row["perp_avg_entry_usdt"])
    sign = Decimal(1) if current > 0 else Decimal(-1)
    realized = closing * (price - avg) * sign
    _add_realized(ledger, realized)
    posted = money(row["margin_usdt"])
    full_close = closing == abs(current)
    release = posted if full_close else posted * closing / abs(current)
    sub_cash(ledger, "B", "PERP_MARGIN", release)
    net_cash = release + realized
    if net_cash >= 0:
        add_cash(ledger, "B", "IDLE", net_cash)
    else:
        sub_cash(ledger, "B", "IDLE", -net_cash)
    new = ZERO if full_close else current - sign * closing
    row["perp_qty_token"] = dec_str(new)
    row["margin_usdt"] = "0" if full_close else dec_str(posted - release)
    if new == 0:
        row["perp_avg_entry_usdt"] = "0"


def apply_borrow(ledger, lot_id, asset, qty, mark, owner, recall_deadline_ms):
    for row in ledger["liabilities"]:
        if row["lot_id"] == lot_id:
            raise FusionError("DUPLICATE_BORROW_LOT", lot_id)
    ledger["liabilities"].append(
        {
            "lot_id": lot_id,
            "asset": asset,
            "qty_token": dec_str(qty),
            "inception_mark_usdt": dec_str(mark),
            "interest_rate": "0",
            "owner": owner,
            "recall_status": "NONE",
            "recall_deadline_ms": recall_deadline_ms,
        }
    )
    receive_spot(ledger, "NEGATIVE_FUNDING", asset, qty, mark)


def apply_return(ledger, lot_id, qty):
    row = next((item for item in ledger["liabilities"] if item["lot_id"] == lot_id), None)
    if row is None:
        raise FusionError("UNKNOWN_LIABILITY", lot_id)
    held = money(row["qty_token"])
    if qty > held:
        raise FusionError("RETURN_EXCEEDS_LIABILITY")
    spot = position(ledger, "NEGATIVE_FUNDING", row["asset"])
    available = money(spot["spot_qty_token"]) - money(spot["spot_reserved_token"])
    if qty > available:
        raise FusionError("RETURN_WITHOUT_COIN")
    avg = money(spot["spot_avg_cost_usdt"])
    inception = money(row["inception_mark_usdt"])
    _add_realized(ledger, qty * (inception - avg))
    left_spot = money(spot["spot_qty_token"]) - qty
    spot["spot_qty_token"] = dec_str(left_spot)
    if left_spot == 0:
        spot["spot_avg_cost_usdt"] = "0"
    left = held - qty
    row["qty_token"] = dec_str(left)


def book_funding(ledger, funding_id, book, asset, qty, amount, window_end_ms, event_time_ms):
    if funding_id in ledger["applied_funding_ids"]:
        raise FusionError("DUPLICATE_FUNDING", funding_id)
    if window_end_ms > event_time_ms:
        raise FusionError("FUTURE_SETTLEMENT_WINDOW")
    row = position(ledger, book, asset)
    eligible = abs(money(row["perp_qty_token"]))
    if qty == 0 or eligible == 0:
        ledger["flags"].append("FUNDING_WITHOUT_POSITION")
        ledger["applied_funding_ids"].append(funding_id)
        return ZERO
    allowed = min(qty, eligible)
    if qty > eligible:
        ledger["flags"].append("INELIGIBLE_FUNDING_EXCESS")
    booked = amount * allowed / qty
    add_cash(ledger, "B", "IDLE", booked) if booked >= 0 else sub_cash(ledger, "B", "IDLE", -booked)
    ledger["pnl"]["funding_usdt"] = dec_str(money(ledger["pnl"]["funding_usdt"]) + booked)
    ledger["applied_funding_ids"].append(funding_id)
    return booked


def charge_fee(ledger, account, purpose, amount, known):
    if not known:
        ledger["pnl"]["fees_complete"] = False
        return
    sub_cash(ledger, account, purpose, amount)
    ledger["pnl"]["fees_usdt"] = dec_str(money(ledger["pnl"]["fees_usdt"]) + amount)


def _yield_row(ledger, product_id):
    for row in ledger["yield_positions"]:
        if row["product_id"] == product_id:
            return row
    return None


def apply_yield(ledger, event):
    action = event["action"]
    row = _yield_row(ledger, event["product_id"])
    nav = money(event["nav"])
    amount = money(event["amount_usdt"])
    quantity = money(event["quantity"])
    if action == "SUBSCRIBE":
        if amount <= 0:
            raise FusionError("YIELD_AMOUNT_INVALID")
        sub_cash(ledger, event["account"], "IDLE", amount)
        units = amount / nav
        if row is None:
            row = {
                "product_id": event["product_id"],
                "account": event["account"],
                "asset": event["asset"],
                "state": "SUBSCRIBED",
                "quantity": "0",
                "nav": dec_str(nav),
                "apr": None,
                "apr_known": False,
                "redeem_delay_ms": 0,
                "accrued_unpaid_usdt": "0",
            }
            ledger["yield_positions"].append(row)
        row["state"] = "SUBSCRIBED"
        row["nav"] = dec_str(nav)
        row["quantity"] = dec_str(money(row["quantity"]) + units)
        return
    if row is None:
        raise FusionError("UNKNOWN_YIELD_PRODUCT")
    row["nav"] = dec_str(nav)
    if action == "ACCRUE_UNPAID":
        row["accrued_unpaid_usdt"] = dec_str(money(row["accrued_unpaid_usdt"]) + amount)
        ledger["pnl"]["yield_unpaid_usdt"] = dec_str(money(ledger["pnl"]["yield_unpaid_usdt"]) + amount)
    elif action == "PAY":
        unpaid = money(row["accrued_unpaid_usdt"])
        if amount > unpaid:
            raise FusionError("YIELD_PAY_EXCEEDS_ACCRUAL")
        row["accrued_unpaid_usdt"] = dec_str(unpaid - amount)
        add_cash(ledger, row["account"], "IDLE", amount)
        ledger["pnl"]["yield_unpaid_usdt"] = dec_str(money(ledger["pnl"]["yield_unpaid_usdt"]) - amount)
        ledger["pnl"]["yield_realized_usdt"] = dec_str(money(ledger["pnl"]["yield_realized_usdt"]) + amount)
    elif action == "REDEEM_REQUEST":
        row["state"] = "REDEEM_REQUESTED"
    elif action == "REDEEM_PENDING":
        row["state"] = "REDEEM_PENDING"
    elif action == "REDEEM_SETTLE":
        held = money(row["quantity"])
        if quantity > held:
            raise FusionError("REDEEM_EXCEEDS_POSITION")
        row["quantity"] = dec_str(held - quantity)
        add_cash(ledger, row["account"], "IDLE", quantity * nav)
        if money(row["quantity"]) == 0 and money(row["accrued_unpaid_usdt"]) == 0:
            row["state"] = "REDEEM_SETTLED"
    else:
        raise FusionError("UNKNOWN_YIELD_ACTION")


def apply_transfer(ledger, event, policy):
    if event["from_account"] != event["to_account"] and not authorized(policy["ab_transfer_allowed"]):
        raise FusionError("AB_TRANSFER_FORBIDDEN")
    sale = event["source_purpose_is_sale_proceeds"] or event["purpose_from"] == "BUYBACK_RESERVE"
    funds_margin = event["to_account"] == "B" and event["purpose_to"] == "PERP_MARGIN"
    if sale and funds_margin and not authorized(policy["a_sale_proceeds_fund_b_margin"]):
        raise FusionError("SALE_PROCEEDS_CANNOT_FUND_B_MARGIN")
    if event["purpose_to"] == "PERP_MARGIN" and event["to_account"] != "B":
        raise FusionError("A_CASH_CANNOT_BE_PERP_MARGIN")
    sub_cash(ledger, event["from_account"], event["purpose_from"], money(event["amount"]))
    add_cash(ledger, event["to_account"], event["purpose_to"], money(event["amount"]))


def ack_order(ledger, event, margin_fraction):
    if any(order["order_id"] == event["order_id"] for order in ledger["open_orders"]):
        raise FusionError("DUPLICATE_ORDER", event["order_id"])
    if money(event["initial_margin_fraction"]) != margin_fraction:
        raise FusionError("MARGIN_FRACTION_MISMATCH")
    order = {
        "order_id": event["order_id"],
        "client_order_id": event["client_order_id"],
        "target_id": event["target_id"],
        "book": event["book"],
        "asset": event["asset"],
        "account": event["account"],
        "leg": event["leg"],
        "side": event["side"],
        "qty_token": dec_str(money(event["qty_token"])),
        "filled_qty_token": "0",
        "limit_price": dec_str(money(event["limit_price"])),
        "state": "NEW",
        "initial_margin_fraction": dec_str(margin_fraction),
    }
    reserves_margin = False
    if event["leg"] == "SPOT" and event["side"] == "SELL":
        row = position(ledger, event["book"], event["asset"])
        available = money(row["spot_qty_token"]) - money(row["spot_reserved_token"])
        if money(event["qty_token"]) > available:
            raise FusionError("INSUFFICIENT_SPOT")
        row["spot_reserved_token"] = dec_str(money(row["spot_reserved_token"]) + money(event["qty_token"]))
    elif event["leg"] == "PERP":
        row = position(ledger, event["book"], event["asset"])
        current = money(row["perp_qty_token"])
        signed = money(event["qty_token"]) if event["side"] == "BUY" else -money(event["qty_token"])
        opens = current == 0 or (current > 0 and signed > 0) or (current < 0 and signed < 0)
        if opens:
            reserves_margin = True
            reserve = money(event["qty_token"]) * money(event["limit_price"]) * margin_fraction
            sub_cash(ledger, "B", "IDLE", reserve)
            add_cash(ledger, "B", "OPEN_ORDER", reserve)
        else:
            reserved = money(row["perp_close_reserved_token"])
            if money(event["qty_token"]) > abs(current) - reserved:
                raise FusionError("INSUFFICIENT_PERP")
            row["perp_close_reserved_token"] = dec_str(reserved + money(event["qty_token"]))
    else:
        reserve = money(event["qty_token"]) * money(event["limit_price"])
        sub_cash(ledger, event["account"], "IDLE", reserve)
        add_cash(ledger, event["account"], "OPEN_ORDER", reserve)
    order["reserves_margin"] = reserves_margin
    ledger["open_orders"].append(order)


def request_cancel(ledger, order_id):
    order = _live_order(ledger, order_id)
    order["state"] = "CANCEL_REQUESTED"


def ack_cancel(ledger, order_id):
    order = _live_order(ledger, order_id)
    remaining = _order_remaining(order)
    if order["leg"] == "SPOT" and order["side"] == "SELL":
        row = position(ledger, order["book"], order["asset"])
        row["spot_reserved_token"] = dec_str(money(row["spot_reserved_token"]) - remaining)
    elif order["leg"] == "PERP" and not order["reserves_margin"]:
        row = position(ledger, order["book"], order["asset"])
        row["perp_close_reserved_token"] = dec_str(money(row["perp_close_reserved_token"]) - remaining)
    else:
        release = _order_cash_reserve(order)
        sub_cash(ledger, "B" if order["leg"] == "PERP" else order["account"], "OPEN_ORDER", release)
        add_cash(ledger, "B" if order["leg"] == "PERP" else order["account"], "IDLE", release)
    order["state"] = "CANCELED"


def _live_order(ledger, order_id):
    order = next((item for item in ledger["open_orders"] if item["order_id"] == order_id), None)
    if order is None or order["state"] not in {"NEW", "CANCEL_REQUESTED"}:
        raise FusionError("UNKNOWN_LIVE_ORDER", order_id)
    return order


def apply_fill(ledger, event, margin_fraction):
    if event["fill_id"] in ledger["applied_fill_ids"]:
        raise FusionError("DUPLICATE_FILL", event["fill_id"])
    if money(event["initial_margin_fraction"]) != margin_fraction:
        raise FusionError("MARGIN_FRACTION_MISMATCH")
    qty = money(event["qty_token"])
    price = money(event["price"])
    order = None
    if event["order_id"] is not None:
        order = _live_order(ledger, event["order_id"])
        if order["asset"] != event["asset"] or order["book"] != event["book"] or order["leg"] != event["leg"] or order["side"] != event["side"]:
            raise FusionError("FILL_ORDER_MISMATCH")
        if qty > _order_remaining(order):
            raise FusionError("FILL_EXCEEDS_ORDER")
    if event["leg"] == "SPOT":
        _fill_spot(ledger, event, order, qty, price)
    else:
        _fill_perp(ledger, event, order, qty, price, margin_fraction)
    if event["fee_known"]:
        charge_fee(ledger, event["account"], event["fee_purpose"], money(event["fee_usdt"]), True)
    else:
        charge_fee(ledger, event["account"], event["fee_purpose"], ZERO, False)
    ledger["applied_fill_ids"].append(event["fill_id"])
    if order is not None:
        order["filled_qty_token"] = dec_str(money(order["filled_qty_token"]) + qty)
        if _order_remaining(order) == 0:
            order["state"] = "FILLED"


def _fill_spot(ledger, event, order, qty, price):
    book = event["book"]
    asset = event["asset"]
    if event["side"] == "BUY":
        if book == "NEGATIVE_FUNDING" and event["account"] != "A":
            raise FusionError("NEGATIVE_SPOT_IS_A")
        if book == "POSITIVE_CARRY" and event["account"] != "B":
            raise FusionError("POSITIVE_CARRY_SPOT_IS_B")
        if order is None:
            buy_spot(ledger, book, asset, event["account"], qty, price, "IDLE")
        else:
            limit = money(order["limit_price"])
            reserved = qty * limit
            sub_cash(ledger, event["account"], "OPEN_ORDER", reserved)
            cost = qty * price
            if cost > reserved:
                sub_cash(ledger, event["account"], "IDLE", cost - reserved)
            elif reserved > cost:
                add_cash(ledger, event["account"], "IDLE", reserved - cost)
            receive_spot(ledger, book, asset, qty, price)
    else:
        purpose = "BUYBACK_RESERVE" if book == "NEGATIVE_FUNDING" else "IDLE"
        if book == "NEGATIVE_FUNDING":
            if event["account"] != "A" or purpose != "BUYBACK_RESERVE":
                raise FusionError("SALE_PROCEEDS_STAY_IN_A")
        if order is not None:
            row = position(ledger, book, asset)
            row["spot_reserved_token"] = dec_str(money(row["spot_reserved_token"]) - qty)
        sell_spot(ledger, book, asset, event["account"], qty, price, purpose)


def _fill_perp(ledger, event, order, qty, price, margin_fraction):
    if event["account"] != "B":
        raise FusionError("PERP_ACCOUNT_IS_B")
    if order is not None and order["reserves_margin"]:
        limit = money(order["limit_price"])
        reserved = qty * limit * margin_fraction
        sub_cash(ledger, "B", "OPEN_ORDER", reserved)
        add_cash(ledger, "B", "IDLE", reserved)
    elif order is not None:
        row = position(ledger, event["book"], event["asset"])
        row["perp_close_reserved_token"] = dec_str(money(row["perp_close_reserved_token"]) - qty)
    _apply_perp(ledger, event["book"], event["asset"], event["side"], qty, price, margin_fraction, "IDLE")


def note_reject(ledger, event, max_retries):
    if not event["retryable"]:
        key = event["book"] + ":" + event["asset"]
        if key not in ledger["blocked_assets"]:
            ledger["blocked_assets"].append(key)
        return
    count = ledger["retry_counts"].get(event["client_order_id"], 0) + 1
    ledger["retry_counts"][event["client_order_id"]] = count
    if count > max_retries:
        key = event["book"] + ":" + event["asset"]
        if key not in ledger["blocked_assets"]:
            ledger["blocked_assets"].append(key)


def note_interest(ledger, event):
    if money(event["amount_usdt"]) != 0:
        raise FusionError("INTEREST_FREE_BORROW_REJECTS_NONZERO_INTEREST")
    ledger["pnl"]["borrow_interest_usdt"] = "0"


def live_orders(ledger, book=None, asset=None):
    rows = []
    for order in ledger["open_orders"]:
        if order["state"] not in {"NEW", "CANCEL_REQUESTED"}:
            continue
        if book is not None and order["book"] != book:
            continue
        if asset is not None and order["asset"] != asset:
            continue
        rows.append(order)
    return rows


def signed_open_qty(ledger, book, asset, leg):
    buy = ZERO
    sell = ZERO
    for order in live_orders(ledger, book, asset):
        if order["leg"] != leg:
            continue
        remaining = _order_remaining(order)
        if order["side"] == "BUY":
            buy += remaining
        else:
            sell += remaining
    return buy, sell


def structural_invariants(ledger):
    errors = []
    seen = set()
    a_margin = ZERO
    for lot in ledger["cash_lots"]:
        if lot["lot_id"] in seen:
            errors.append("DUPLICATE_LOT")
        seen.add(lot["lot_id"])
        amount = money(lot["amount"])
        if amount < 0:
            errors.append("NEGATIVE_CASH_LOT")
        if lot["purpose"] == "PERP_MARGIN" and lot["account"] != "B":
            errors.append("A_PERP_MARGIN")
            a_margin += amount
        if lot["purpose"] == "BUYBACK_RESERVE" and lot["account"] != "A":
            errors.append("BUYBACK_NOT_ON_A")
    if money(ledger["pnl"]["borrow_interest_usdt"]) != 0:
        errors.append("NONEZERO_BORROW_INTEREST")
    for row in ledger["liabilities"]:
        if money(row["interest_rate"]) != 0:
            errors.append("LIABILITY_INTEREST_RATE")
        if money(row["qty_token"]) < 0:
            errors.append("NEGATIVE_LIABILITY")
    keys = set()
    for row in ledger["positions"]:
        key = (row["book"], row["asset"])
        if key in keys:
            errors.append("DUPLICATE_POSITION")
        keys.add(key)
        if money(row["spot_qty_token"]) < 0:
            errors.append("NEGATIVE_SPOT")
    report = explain(ledger)
    if report["book_residual_usdt"] != "0":
        errors.append("PNL_RESIDUAL")
    try:
        _check_order_reservations(ledger)
    except FusionError as exc:
        errors.append(exc.code)
    report["errors"] = errors
    report["a_perp_margin_usdt"] = dec_str(a_margin)
    report["ok"] = not errors
    return report


def _aggregate_cash(rows):
    totals = {}
    for row in rows:
        key = (row["account"], row["purpose"])
        totals[key] = totals.get(key, ZERO) + money(row["amount"])
    return {key: value for key, value in totals.items() if value != 0}


def fact_diff(ledger, snapshot):
    """Compare ledger facts to a snapshot. A flatter snapshot is a mismatch, not a close."""
    mismatches = []
    snap_cash = _aggregate_cash(snapshot["cash"])
    led_cash = _aggregate_cash(ledger["cash_lots"])
    if snap_cash != led_cash:
        mismatches.append("CASH_MISMATCH")
    snap_pos = {(row["book"], row["asset"]): row for row in snapshot["positions"]}
    led_pos = {(row["book"], row["asset"]): row for row in ledger["positions"] if _open_position(row) or money(row["spot_reserved_token"]) or money(row["perp_close_reserved_token"])}
    if set(snap_pos) != set(led_pos):
        mismatches.append("POSITION_MISMATCH")
    else:
        for key, row in led_pos.items():
            other = snap_pos[key]
            for field in (
                "spot_qty_token",
                "perp_qty_token",
                "margin_usdt",
                "spot_reserved_token",
                "perp_close_reserved_token",
                "spot_avg_cost_usdt",
                "perp_avg_entry_usdt",
            ):
                if money(row[field]) != money(other[field]):
                    mismatches.append("POSITION_MISMATCH")
                    break
    snap_liab = {row["lot_id"]: row for row in snapshot["liabilities"]}
    led_liab = {row["lot_id"]: row for row in ledger["liabilities"] if money(row["qty_token"]) != 0}
    snap_open = {key: row for key, row in snap_liab.items() if money(row["qty_token"]) != 0}
    if set(snap_open) != set(led_liab):
        mismatches.append("LIABILITY_MISMATCH")
    else:
        for key, row in led_liab.items():
            if money(row["qty_token"]) != money(snap_open[key]["qty_token"]):
                mismatches.append("LIABILITY_MISMATCH")
                break
    def order_key(row):
        return (
            row["order_id"],
            row["state"],
            dec_str(money(row["qty_token"]) - money(row["filled_qty_token"])),
            row["side"],
            row["leg"],
            row["book"],
            row["asset"],
            str(row["reserves_margin"]),
        )
    snap_orders = sorted(order_key(row) for row in snapshot["open_orders"] if row["state"] in {"NEW", "CANCEL_REQUESTED"})
    led_orders = sorted(order_key(row) for row in ledger["open_orders"] if row["state"] in {"NEW", "CANCEL_REQUESTED"})
    if snap_orders != led_orders:
        mismatches.append("OPEN_ORDER_MISMATCH")
    snap_yield = _yield_signature(snapshot["yield_products"])
    led_yield = _yield_signature(ledger["yield_positions"])
    if snap_yield != led_yield:
        mismatches.append("YIELD_MISMATCH")
    return sorted(set(mismatches))


def _yield_signature(rows):
    signature = {}
    for row in rows:
        quantity = money(row["quantity"])
        accrued = money(row["accrued_unpaid_usdt"])
        if quantity == 0 and accrued == 0 and row["state"] in {"AVAILABLE", "REDEEM_SETTLED"}:
            continue
        signature[row["product_id"]] = (row["state"], dec_str(quantity), dec_str(accrued))
    return signature


def echo_facts(ledger):
    """Fact rows a later snapshot must repeat. This does not create a target or a fill."""
    return {
        "cash": [dict(row) for row in ledger["cash_lots"] if money(row["amount"]) != 0],
        "positions": [
            dict(row)
            for row in ledger["positions"]
            if _open_position(row) or money(row["spot_reserved_token"]) != 0 or money(row["perp_close_reserved_token"]) != 0
        ],
        "liabilities": [dict(row) for row in ledger["liabilities"] if money(row["qty_token"]) != 0],
        "open_orders": [dict(row) for row in ledger["open_orders"] if row["state"] in {"NEW", "CANCEL_REQUESTED"}],
        "yield_products": [dict(row) for row in ledger["yield_positions"] if _yield_signature([row])],
    }
