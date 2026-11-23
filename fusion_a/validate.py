"""Closed-field validators for the fusion A messages. The engine treats these as normative."""

from fusion_a.decimal_util import FusionError, money
from fusion_a.identity import GROSS, NET

CONFIG_SCHEMA = "nfb.fusion-a.config.v1"
SNAPSHOT_SCHEMA = "nfb.fusion-a.snapshot.v1"
RECEIPT_SCHEMA = "nfb.fusion-a.receipt.v1"
STRATEGY_ID = "nfb.fusion-a.research.20261123"
STRATEGY_VERSION = "0.1.0"

ON_FAULT = {"STOP_NEW", "REDUCE_TOWARD_HEDGE", "REDUCE_TO_FLAT_TARGET"}
OI_POLICY = {"reject_add", "adv_only_weaker"}
CASH_PURPOSES = {
    "IDLE",
    "SAFETY_RESERVE",
    "PERP_MARGIN",
    "OPEN_ORDER",
    "IN_TRANSIT",
    "BUYBACK_RESERVE",
}
BOOKS = {"POSITIVE_CARRY", "NEGATIVE_FUNDING"}


def _keys(value, required, allowed, path):
    if not isinstance(value, dict):
        raise FusionError("OBJECT_REQUIRED", path)
    missing = required - set(value)
    extra = set(value) - allowed
    if missing:
        raise FusionError("MISSING_FIELD", path + " " + ",".join(sorted(missing)))
    if extra:
        raise FusionError("UNKNOWN_FIELD", path + " " + ",".join(sorted(extra)))


def _bool(value, path):
    if type(value) is not bool:
        raise FusionError("BOOL_REQUIRED", path)
    return value


def _int(value, path, minimum=0):
    if type(value) is not int or isinstance(value, bool) or value < minimum:
        raise FusionError("INT_REQUIRED", path)
    return value


def _text(value, path):
    if not isinstance(value, str) or not value:
        raise FusionError("TEXT_REQUIRED", path)
    return value


def _optional_money(value, path):
    if value is None:
        return None
    return money(value)


def validate_config(config):
    required = {
        "schema_version",
        "strategy_id",
        "strategy_version",
        "mode",
        "submission_allowed",
        "source_mode",
        "synthetic",
        "fixture_label",
        "capital",
        "policy",
    }
    _keys(config, required, required, "config")
    if config["schema_version"] != CONFIG_SCHEMA:
        raise FusionError("UNSUPPORTED_SCHEMA", "config")
    if config["strategy_id"] != STRATEGY_ID or config["strategy_version"] != STRATEGY_VERSION:
        raise FusionError("UNEXPECTED_STRATEGY_IDENTITY")
    if config["mode"] not in {"dry_run", "research_replay"}:
        raise FusionError("FORBIDDEN_MODE", str(config["mode"]))
    if config["submission_allowed"] is not False:
        raise FusionError("SUBMISSION_NOT_ALLOWED")
    _source_pair(config, "config")
    _capital(config["capital"])
    _policy(config["policy"])
    return config


def _source_pair(document, path):
    source = document["source_mode"]
    synthetic = _bool(document["synthetic"], path + ".synthetic")
    label = _text(document["fixture_label"], path + ".fixture_label")
    if source not in {"synthetic", "observed"}:
        raise FusionError("UNKNOWN_SOURCE_MODE", path)
    if source == "synthetic":
        if not synthetic or "SYNTHETIC" not in label:
            raise FusionError("SYNTHETIC_FIXTURE_MUST_BE_LABELED", path)
    else:
        if synthetic or label != "OBSERVED_INPUT":
            raise FusionError("OBSERVED_INPUT_MISLABELED", path)


def _capital(capital):
    required = {
        "interpretation",
        "scale_usdt",
        "usdt_fraction",
        "identity_confirmed",
        "cash_has_no_extra_liabilities",
        "usdt_fraction_is_gross_asset_weight",
    }
    _keys(capital, required, required, "capital")
    if capital["interpretation"] not in {GROSS, NET}:
        raise FusionError("UNKNOWN_CAPITAL_INTERPRETATION")
    money(capital["scale_usdt"])
    money(capital["usdt_fraction"])
    _bool(capital["identity_confirmed"], "capital.identity_confirmed")
    _bool(capital["cash_has_no_extra_liabilities"], "capital.cash_has_no_extra_liabilities")
    _bool(capital["usdt_fraction_is_gross_asset_weight"], "capital.usdt_fraction_is_gross_asset_weight")


def _policy(policy):
    required = {
        "ab_transfer_allowed",
        "a_sale_proceeds_fund_b_margin",
        "safety_reserve_fraction_of_cash",
        "cash_floor_fraction_of_cash",
        "negative_budget_fraction_of_cash",
        "negative_budget_cap_usdt",
        "initial_margin_fraction",
        "max_planned_hold_hours",
        "min_completion_probability",
        "adv_participation_target",
        "adv_participation_delta",
        "oi_cap_fraction",
        "oi_missing_policy",
        "per_coin_gross_cap_fraction",
        "min_order_usdt",
        "max_retries",
        "max_leg_imbalance_ms",
        "on_fault",
        "depeg_haircut",
        "kill_switch",
        "target_ttl_ms",
        "snapshot_max_age_ms",
        "allow_new_negative_without_rc2_decision",
        "allow_usdt_fraction_outside_band",
        "year_hours",
        "enforce_wall_clock",
        "exchange_halt_stops_new",
        "settlement_book_skew_ms",
    }
    _keys(policy, required, required, "policy")
    _authorization(policy["ab_transfer_allowed"], "ab_transfer_allowed")
    _authorization(policy["a_sale_proceeds_fund_b_margin"], "a_sale_proceeds_fund_b_margin")
    for name in (
        "safety_reserve_fraction_of_cash",
        "cash_floor_fraction_of_cash",
        "negative_budget_fraction_of_cash",
        "adv_participation_target",
        "adv_participation_delta",
        "oi_cap_fraction",
        "per_coin_gross_cap_fraction",
        "depeg_haircut",
    ):
        number = money(policy[name])
        if number < 0 or number > 1:
            raise FusionError("FRACTION_OUT_OF_RANGE", name)
    margin = money(policy["initial_margin_fraction"])
    if margin <= 0:
        raise FusionError("MARGIN_FRACTION_MUST_BE_POSITIVE")
    if money(policy["min_order_usdt"]) < 0:
        raise FusionError("MIN_ORDER_NEGATIVE")
    if policy["negative_budget_cap_usdt"] is not None:
        if money(policy["negative_budget_cap_usdt"]) < 0:
            raise FusionError("NEGATIVE_CAP_INVALID")
    if policy["min_completion_probability"] is not None:
        probability = money(policy["min_completion_probability"])
        if probability < 0 or probability > 1:
            raise FusionError("FRACTION_OUT_OF_RANGE", "min_completion_probability")
    if policy["oi_missing_policy"] not in OI_POLICY:
        raise FusionError("UNKNOWN_OI_POLICY")
    if policy["on_fault"] not in ON_FAULT:
        raise FusionError("UNKNOWN_FAULT_POLICY")
    _int(policy["max_planned_hold_hours"], "max_planned_hold_hours", 1)
    _int(policy["max_retries"], "max_retries", 0)
    _int(policy["max_leg_imbalance_ms"], "max_leg_imbalance_ms", 0)
    _int(policy["target_ttl_ms"], "target_ttl_ms", 1)
    _int(policy["snapshot_max_age_ms"], "snapshot_max_age_ms", 0)
    _int(policy["year_hours"], "year_hours", 1)
    _int(policy["settlement_book_skew_ms"], "settlement_book_skew_ms", 0)
    _bool(policy["kill_switch"], "kill_switch")
    _bool(policy["allow_new_negative_without_rc2_decision"], "allow_new_negative_without_rc2_decision")
    _bool(policy["allow_usdt_fraction_outside_band"], "allow_usdt_fraction_outside_band")
    _bool(policy["enforce_wall_clock"], "enforce_wall_clock")
    _bool(policy["exchange_halt_stops_new"], "exchange_halt_stops_new")
    if policy["year_hours"] != 8760:
        raise FusionError("YEAR_HOURS_MUST_BE_EXPLICIT_365D")


def _authorization(value, path):
    if type(value) is bool:
        if value is True:
            raise FusionError("AUTHORIZATION_REQUIRES_EVIDENCE", path)
        return
    required = {"authorized", "evidence_id", "approver"}
    _keys(value, required, required, path)
    if value["authorized"] is not True:
        raise FusionError("AUTHORIZATION_FLAG_REQUIRED", path)
    _text(value["evidence_id"], path + ".evidence_id")
    _text(value["approver"], path + ".approver")


def authorized(value):
    return isinstance(value, dict) and value.get("authorized") is True


def validate_snapshot(snapshot):
    required = {
        "schema_version",
        "snapshot_id",
        "source_mode",
        "synthetic",
        "fixture_label",
        "event_time_ms",
        "received_at_ms",
        "valid_until_ms",
        "sequence",
        "decision_time_ms",
        "frozen",
        "opening_fact",
        "cash",
        "positions",
        "liabilities",
        "open_orders",
        "universe",
        "yield_products",
        "capital_flows",
        "execution_calibration",
        "risk_flags",
        "rc2_negative_decisions",
        "balances_are_facts",
        "targets_are_not_fills",
    }
    _keys(snapshot, required, required, "snapshot")
    if snapshot["schema_version"] != SNAPSHOT_SCHEMA:
        raise FusionError("UNSUPPORTED_SCHEMA", "snapshot")
    _text(snapshot["snapshot_id"], "snapshot_id")
    _source_pair(snapshot, "snapshot")
    event_time = _int(snapshot["event_time_ms"], "event_time_ms")
    received = _int(snapshot["received_at_ms"], "received_at_ms")
    valid_until = _int(snapshot["valid_until_ms"], "valid_until_ms")
    decision = _int(snapshot["decision_time_ms"], "decision_time_ms")
    if received < event_time or decision < received or valid_until < event_time:
        raise FusionError("SNAPSHOT_CLOCK_ORDER")
    _int(snapshot["sequence"], "sequence", 1)
    _bool(snapshot["frozen"], "frozen")
    _bool(snapshot["opening_fact"], "opening_fact")
    if snapshot["balances_are_facts"] is not True or snapshot["targets_are_not_fills"] is not True:
        raise FusionError("SNAPSHOT_FACT_FLAGS_REQUIRED")
    for row in snapshot["cash"]:
        _cash_row(row)
    for row in snapshot["positions"]:
        _position_row(row)
    for row in snapshot["liabilities"]:
        _liability_row(row)
    for row in snapshot["open_orders"]:
        _order_row(row)
    seen = set()
    for row in snapshot["universe"]:
        _universe_row(row)
        if row["asset"] in seen:
            raise FusionError("DUPLICATE_UNIVERSE_ASSET", row["asset"])
        seen.add(row["asset"])
    for row in snapshot["yield_products"]:
        _yield_row(row)
    for row in snapshot["capital_flows"]:
        _flow_row(row)
    if snapshot["execution_calibration"] is not None:
        _calibration(snapshot["execution_calibration"])
    if not isinstance(snapshot["risk_flags"], list) or not all(isinstance(x, str) for x in snapshot["risk_flags"]):
        raise FusionError("RISK_FLAGS_INVALID")
    for row in snapshot["rc2_negative_decisions"]:
        _rc2_row(row)
    return snapshot


def _cash_row(row):
    required = {"lot_id", "account", "purpose", "amount"}
    _keys(row, required, required, "cash")
    _text(row["lot_id"], "cash.lot_id")
    if row["account"] not in {"A", "B"}:
        raise FusionError("UNKNOWN_ACCOUNT")
    if row["purpose"] not in CASH_PURPOSES:
        raise FusionError("UNKNOWN_CASH_PURPOSE")
    if money(row["amount"]) < 0:
        raise FusionError("NEGATIVE_CASH")
    if row["purpose"] == "PERP_MARGIN" and row["account"] != "B":
        raise FusionError("A_CASH_CANNOT_BE_PERP_MARGIN")
    if row["purpose"] == "BUYBACK_RESERVE" and row["account"] != "A":
        raise FusionError("BUYBACK_RESERVE_IS_A_ONLY")


def _position_row(row):
    required = {
        "book",
        "asset",
        "spot_account",
        "perp_account",
        "spot_qty_token",
        "spot_avg_cost_usdt",
        "perp_qty_token",
        "perp_avg_entry_usdt",
        "margin_usdt",
        "spot_reserved_token",
        "perp_close_reserved_token",
    }
    _keys(row, required, required, "position")
    if row["book"] not in BOOKS:
        raise FusionError("UNKNOWN_BOOK")
    _text(row["asset"], "position.asset")
    if row["spot_account"] not in {"A", "B"} or row["perp_account"] != "B":
        raise FusionError("POSITION_ACCOUNT_INVALID")
    if row["book"] == "POSITIVE_CARRY" and row["spot_account"] != "B":
        raise FusionError("POSITIVE_CARRY_SPOT_IS_B")
    if row["book"] == "NEGATIVE_FUNDING" and row["spot_account"] != "A":
        raise FusionError("NEGATIVE_SPOT_IS_A")
    for name in (
        "spot_qty_token",
        "spot_avg_cost_usdt",
        "perp_avg_entry_usdt",
        "margin_usdt",
        "spot_reserved_token",
        "perp_close_reserved_token",
    ):
        if money(row[name]) < 0:
            raise FusionError("NEGATIVE_POSITION_FIELD", name)
    money(row["perp_qty_token"])


def _liability_row(row):
    required = {
        "lot_id",
        "asset",
        "qty_token",
        "inception_mark_usdt",
        "interest_rate",
        "owner",
        "recall_status",
        "recall_deadline_ms",
    }
    _keys(row, required, required, "liability")
    _text(row["lot_id"], "liability.lot_id")
    _text(row["asset"], "liability.asset")
    _text(row["owner"], "liability.owner")
    if money(row["qty_token"]) < 0 or money(row["inception_mark_usdt"]) <= 0:
        raise FusionError("INVALID_LIABILITY")
    if money(row["interest_rate"]) != 0:
        raise FusionError("BORROW_INTEREST_MUST_BE_ZERO")
    if row["recall_status"] not in {"NONE", "RECEIVED", "IN_PROGRESS", "DONE"}:
        raise FusionError("UNKNOWN_RECALL_STATUS")
    if row["recall_deadline_ms"] is not None:
        _int(row["recall_deadline_ms"], "recall_deadline_ms")


def _order_row(row):
    required = {
        "order_id",
        "client_order_id",
        "target_id",
        "book",
        "asset",
        "account",
        "leg",
        "side",
        "qty_token",
        "filled_qty_token",
        "limit_price",
        "state",
        "initial_margin_fraction",
        "reserves_margin",
    }
    _keys(row, required, required, "open_order")
    _text(row["order_id"], "order_id")
    _text(row["client_order_id"], "client_order_id")
    _text(row["target_id"], "target_id")
    _text(row["asset"], "order.asset")
    if row["book"] not in BOOKS or row["account"] not in {"A", "B"}:
        raise FusionError("ORDER_ACCOUNT_INVALID")
    if row["leg"] not in {"SPOT", "PERP"} or row["side"] not in {"BUY", "SELL"}:
        raise FusionError("ORDER_LEG_INVALID")
    if row["state"] not in {"NEW", "CANCEL_REQUESTED", "CANCELED", "FILLED"}:
        raise FusionError("ORDER_STATE_INVALID")
    qty = money(row["qty_token"])
    filled = money(row["filled_qty_token"])
    if qty <= 0 or filled < 0 or filled > qty or money(row["limit_price"]) <= 0:
        raise FusionError("ORDER_QTY_INVALID")
    if money(row["initial_margin_fraction"]) <= 0:
        raise FusionError("ORDER_MARGIN_INVALID")
    _bool(row["reserves_margin"], "reserves_margin")


def _universe_row(row):
    required = {
        "asset",
        "spot_symbol",
        "perp_symbol",
        "tokens_per_perp_quantity",
        "qty_step",
        "min_qty",
        "min_notional_usdt",
        "spot_usdt",
        "perp_usdt",
        "index_usdt",
        "funding_rate_observed",
        "funding_interval_hours",
        "funding_rate_asof_ms",
        "basis",
        "lagged_adv_quote_usdt",
        "adv_known",
        "lagged_oi_quote_usdt",
        "oi_known",
        "borrowable_qty_token",
        "borrow_available",
        "filters_ok",
        "trading_status",
        "mmr",
    }
    allowed = required | {"ignored_forecast_rate"}
    _keys(row, required, allowed, "universe")
    _text(row["asset"], "universe.asset")
    _text(row["spot_symbol"], "spot_symbol")
    _text(row["perp_symbol"], "perp_symbol")
    for name in (
        "tokens_per_perp_quantity",
        "qty_step",
        "min_qty",
        "min_notional_usdt",
        "spot_usdt",
        "perp_usdt",
        "index_usdt",
    ):
        if money(row[name]) <= 0:
            raise FusionError("UNIVERSE_PRICE_OR_STEP_INVALID", name)
    money(row["funding_rate_observed"])
    money(row["basis"])
    _int(row["funding_interval_hours"], "funding_interval_hours", 1)
    _int(row["funding_rate_asof_ms"], "funding_rate_asof_ms")
    _bool(row["adv_known"], "adv_known")
    _bool(row["oi_known"], "oi_known")
    _bool(row["borrow_available"], "borrow_available")
    _bool(row["filters_ok"], "filters_ok")
    if row["adv_known"]:
        if money(row["lagged_adv_quote_usdt"]) < 0:
            raise FusionError("ADV_INVALID")
    elif row["lagged_adv_quote_usdt"] is not None:
        raise FusionError("UNKNOWN_ADV_MUST_BE_NULL")
    if row["oi_known"]:
        if money(row["lagged_oi_quote_usdt"]) < 0:
            raise FusionError("OI_INVALID")
    elif row["lagged_oi_quote_usdt"] is not None:
        raise FusionError("UNKNOWN_OI_MUST_BE_NULL")
    if money(row["borrowable_qty_token"]) < 0:
        raise FusionError("BORROWABLE_INVALID")
    if row["trading_status"] not in {"TRADING", "HALT"}:
        raise FusionError("UNKNOWN_TRADING_STATUS")
    if row["mmr"] is not None and money(row["mmr"]) < 0:
        raise FusionError("MMR_INVALID")
    if "ignored_forecast_rate" in row and row["ignored_forecast_rate"] is not None:
        money(row["ignored_forecast_rate"])


def _yield_row(row):
    required = {
        "product_id",
        "account",
        "asset",
        "state",
        "quantity",
        "nav",
        "apr",
        "apr_known",
        "redeem_delay_ms",
        "accrued_unpaid_usdt",
    }
    _keys(row, required, required, "yield")
    _text(row["product_id"], "product_id")
    if row["account"] not in {"A", "B"}:
        raise FusionError("UNKNOWN_ACCOUNT")
    _text(row["asset"], "yield.asset")
    if row["state"] not in {"AVAILABLE", "SUBSCRIBED", "REDEEM_REQUESTED", "REDEEM_PENDING", "REDEEM_SETTLED"}:
        raise FusionError("UNKNOWN_YIELD_STATE")
    if money(row["quantity"]) < 0 or money(row["nav"]) <= 0 or money(row["accrued_unpaid_usdt"]) < 0:
        raise FusionError("YIELD_AMOUNT_INVALID")
    _bool(row["apr_known"], "apr_known")
    _int(row["redeem_delay_ms"], "redeem_delay_ms")
    if row["apr_known"]:
        money(row["apr"])
    elif row["apr"] is not None:
        raise FusionError("UNKNOWN_APR_MUST_BE_NULL")


def _flow_row(row):
    required = {"flow_id", "account", "asset", "amount", "kind", "already_in_balances", "status"}
    _keys(row, required, required, "capital_flow")
    _text(row["flow_id"], "flow_id")
    if row["account"] not in {"A", "B"} or row["asset"] != "USDT":
        raise FusionError("FLOW_ASSET_INVALID")
    if money(row["amount"]) <= 0:
        raise FusionError("FLOW_AMOUNT_INVALID")
    if row["kind"] not in {"SUBSCRIPTION", "REDEMPTION"}:
        raise FusionError("UNKNOWN_FLOW_KIND")
    if row["already_in_balances"] is not False:
        raise FusionError("FLOW_ALREADY_IN_BALANCES_WOULD_DOUBLE_COUNT")
    if row["status"] != "SETTLED":
        raise FusionError("ONLY_SETTLED_FLOWS_ARE_APPLIED")


def _calibration(row):
    required = {
        "source_mode",
        "fixture_label",
        "synthetic",
        "fee_schedule_known",
        "spot_fee_rate",
        "perp_fee_rate",
        "impact_known",
        "round_trip_impact_rate",
        "completion_known",
        "completion_probability",
        "queue_known",
        "evidence_id",
        "asof_ms",
    }
    _keys(row, required, required, "execution_calibration")
    _source_pair(row, "execution_calibration")
    _bool(row["fee_schedule_known"], "fee_schedule_known")
    _bool(row["impact_known"], "impact_known")
    _bool(row["completion_known"], "completion_known")
    _bool(row["queue_known"], "queue_known")
    _int(row["asof_ms"], "calibration.asof_ms")
    _pair_known(row["fee_schedule_known"], row["spot_fee_rate"], "spot_fee_rate")
    _pair_known(row["fee_schedule_known"], row["perp_fee_rate"], "perp_fee_rate")
    _pair_known(row["impact_known"], row["round_trip_impact_rate"], "round_trip_impact_rate")
    _pair_known(row["completion_known"], row["completion_probability"], "completion_probability")
    if row["fee_schedule_known"]:
        for name in ("spot_fee_rate", "perp_fee_rate"):
            if money(row[name]) < 0:
                raise FusionError("NEGATIVE_FEE")
    if row["impact_known"] and money(row["round_trip_impact_rate"]) < 0:
        raise FusionError("NEGATIVE_IMPACT")
    if row["completion_known"]:
        probability = money(row["completion_probability"])
        if probability < 0 or probability > 1:
            raise FusionError("COMPLETION_OUT_OF_RANGE")
    if row["source_mode"] == "observed" and not row["evidence_id"]:
        raise FusionError("OBSERVED_CALIBRATION_NEEDS_EVIDENCE")
    if row["evidence_id"] is not None:
        _text(row["evidence_id"], "evidence_id")


def _pair_known(known, value, path):
    if known and value is None:
        raise FusionError("KNOWN_VALUE_MISSING", path)
    if not known and value is not None:
        raise FusionError("UNKNOWN_VALUE_MUST_BE_NULL", path)


def _rc2_row(row):
    required = {"asset", "decision_id", "allow_new", "max_perp_qty_token"}
    _keys(row, required, required, "rc2_negative_decision")
    _text(row["asset"], "rc2.asset")
    _text(row["decision_id"], "rc2.decision_id")
    _bool(row["allow_new"], "rc2.allow_new")
    if money(row["max_perp_qty_token"]) < 0:
        raise FusionError("RC2_QTY_INVALID")


def validate_receipt(receipt):
    required = {
        "schema_version",
        "receipt_id",
        "source_mode",
        "synthetic",
        "fixture_label",
        "stream_id",
        "sequence",
        "target_id",
        "event_time_ms",
        "received_at_ms",
        "events",
    }
    _keys(receipt, required, required, "receipt")
    if receipt["schema_version"] != RECEIPT_SCHEMA:
        raise FusionError("UNSUPPORTED_SCHEMA", "receipt")
    _text(receipt["receipt_id"], "receipt_id")
    _source_pair(receipt, "receipt")
    _text(receipt["stream_id"], "stream_id")
    _int(receipt["sequence"], "sequence", 1)
    if receipt["target_id"] is not None:
        _text(receipt["target_id"], "target_id")
    event_time = _int(receipt["event_time_ms"], "event_time_ms")
    received = _int(receipt["received_at_ms"], "received_at_ms")
    if received < event_time:
        raise FusionError("RECEIPT_CLOCK_ORDER")
    if not isinstance(receipt["events"], list) or not receipt["events"]:
        raise FusionError("RECEIPT_EVENTS_REQUIRED")
    for event in receipt["events"]:
        _event(event)
    return receipt


def _event(event):
    kind = event.get("type")
    if kind == "FILL":
        required = {
            "type",
            "fill_id",
            "order_id",
            "book",
            "asset",
            "account",
            "leg",
            "side",
            "qty_token",
            "price",
            "fee_usdt",
            "fee_known",
            "fee_purpose",
            "initial_margin_fraction",
        }
        _keys(event, required, required, "FILL")
        _text(event["fill_id"], "fill_id")
        if event["order_id"] is not None:
            _text(event["order_id"], "order_id")
        if event["book"] not in BOOKS:
            raise FusionError("UNKNOWN_BOOK")
        _text(event["asset"], "fill.asset")
        if event["account"] not in {"A", "B"} or event["leg"] not in {"SPOT", "PERP"} or event["side"] not in {"BUY", "SELL"}:
            raise FusionError("FILL_LEG_INVALID")
        if money(event["qty_token"]) <= 0 or money(event["price"]) <= 0:
            raise FusionError("FILL_QTY_INVALID")
        _bool(event["fee_known"], "fee_known")
        _pair_known(event["fee_known"], event["fee_usdt"], "fee_usdt")
        if event["fee_known"] and money(event["fee_usdt"]) < 0:
            raise FusionError("NEGATIVE_FEE")
        if event["fee_purpose"] not in {"IDLE", "BUYBACK_RESERVE"}:
            raise FusionError("FEE_PURPOSE_INVALID")
        if money(event["initial_margin_fraction"]) <= 0:
            raise FusionError("FILL_MARGIN_INVALID")
    elif kind == "ORDER_ACK":
        required = {
            "type",
            "order_id",
            "client_order_id",
            "target_id",
            "book",
            "asset",
            "account",
            "leg",
            "side",
            "qty_token",
            "limit_price",
            "initial_margin_fraction",
        }
        _keys(event, required, required, "ORDER_ACK")
        for name in ("order_id", "client_order_id", "target_id", "asset"):
            _text(event[name], name)
        if event["book"] not in BOOKS or event["account"] not in {"A", "B"}:
            raise FusionError("ORDER_ACCOUNT_INVALID")
        if event["leg"] not in {"SPOT", "PERP"} or event["side"] not in {"BUY", "SELL"}:
            raise FusionError("ORDER_LEG_INVALID")
        if money(event["qty_token"]) <= 0 or money(event["limit_price"]) <= 0:
            raise FusionError("ORDER_QTY_INVALID")
        if money(event["initial_margin_fraction"]) <= 0:
            raise FusionError("ORDER_MARGIN_INVALID")
    elif kind in {"CANCEL_REQUESTED", "CANCEL_ACK"}:
        _keys(event, {"type", "order_id"}, {"type", "order_id"}, kind)
        _text(event["order_id"], "order_id")
    elif kind == "REJECT":
        required = {"type", "client_order_id", "asset", "book", "retryable"}
        _keys(event, required, required, "REJECT")
        _text(event["client_order_id"], "client_order_id")
        _text(event["asset"], "reject.asset")
        if event["book"] not in BOOKS:
            raise FusionError("UNKNOWN_BOOK")
        _bool(event["retryable"], "retryable")
    elif kind == "FUNDING":
        required = {
            "type",
            "funding_id",
            "book",
            "asset",
            "account",
            "qty_token",
            "amount_usdt",
            "settled",
            "window_start_ms",
            "window_end_ms",
        }
        _keys(event, required, required, "FUNDING")
        _text(event["funding_id"], "funding_id")
        if event["book"] not in BOOKS:
            raise FusionError("UNKNOWN_BOOK")
        _text(event["asset"], "funding.asset")
        if event["account"] != "B":
            raise FusionError("FUNDING_CASH_IS_B")
        if money(event["qty_token"]) < 0:
            raise FusionError("FUNDING_QTY_INVALID")
        money(event["amount_usdt"])
        if event["settled"] is not True:
            raise FusionError("UNSETTLED_FUNDING_NOT_INCOME")
        start = _int(event["window_start_ms"], "window_start_ms")
        end = _int(event["window_end_ms"], "window_end_ms")
        if end < start:
            raise FusionError("FUNDING_WINDOW_ORDER")
    elif kind == "BORROW":
        required = {"type", "lot_id", "asset", "qty_token", "mark_usdt", "owner", "recall_deadline_ms"}
        _keys(event, required, required, "BORROW")
        _text(event["lot_id"], "lot_id")
        _text(event["asset"], "borrow.asset")
        _text(event["owner"], "owner")
        if money(event["qty_token"]) <= 0 or money(event["mark_usdt"]) <= 0:
            raise FusionError("BORROW_QTY_INVALID")
        if event["recall_deadline_ms"] is not None:
            _int(event["recall_deadline_ms"], "recall_deadline_ms")
    elif kind == "RETURN_CONFIRMED":
        required = {"type", "lot_id", "qty_token"}
        _keys(event, required, required, "RETURN_CONFIRMED")
        _text(event["lot_id"], "lot_id")
        if money(event["qty_token"]) <= 0:
            raise FusionError("RETURN_QTY_INVALID")
    elif kind == "YIELD":
        required = {"type", "product_id", "action", "quantity", "nav", "amount_usdt", "account", "asset"}
        _keys(event, required, required, "YIELD")
        _text(event["product_id"], "product_id")
        if event["action"] not in {"SUBSCRIBE", "ACCRUE_UNPAID", "PAY", "REDEEM_REQUEST", "REDEEM_PENDING", "REDEEM_SETTLE"}:
            raise FusionError("UNKNOWN_YIELD_ACTION")
        if event["account"] not in {"A", "B"}:
            raise FusionError("UNKNOWN_ACCOUNT")
        _text(event["asset"], "yield.asset")
        if money(event["nav"]) <= 0:
            raise FusionError("YIELD_NAV_INVALID")
        for name in ("quantity", "amount_usdt"):
            if money(event[name]) < 0:
                raise FusionError("YIELD_AMOUNT_INVALID", name)
    elif kind == "CAPITAL_FLOW":
        required = {"type", "flow_id", "account", "amount", "kind"}
        _keys(event, required, required, "CAPITAL_FLOW")
        _text(event["flow_id"], "flow_id")
        if event["account"] not in {"A", "B"} or event["kind"] not in {"SUBSCRIPTION", "REDEMPTION"}:
            raise FusionError("FLOW_ASSET_INVALID")
        if money(event["amount"]) <= 0:
            raise FusionError("FLOW_AMOUNT_INVALID")
    elif kind == "TRANSFER":
        required = {"type", "transfer_id", "from_account", "to_account", "purpose_from", "purpose_to", "amount", "source_purpose_is_sale_proceeds"}
        _keys(event, required, required, "TRANSFER")
        _text(event["transfer_id"], "transfer_id")
        if event["from_account"] not in {"A", "B"} or event["to_account"] not in {"A", "B"}:
            raise FusionError("UNKNOWN_ACCOUNT")
        if event["purpose_from"] not in CASH_PURPOSES or event["purpose_to"] not in CASH_PURPOSES:
            raise FusionError("UNKNOWN_CASH_PURPOSE")
        if money(event["amount"]) <= 0:
            raise FusionError("TRANSFER_AMOUNT_INVALID")
        _bool(event["source_purpose_is_sale_proceeds"], "source_purpose_is_sale_proceeds")
    elif kind == "INTEREST":
        required = {"type", "lot_id", "amount_usdt"}
        _keys(event, required, required, "INTEREST")
        _text(event["lot_id"], "lot_id")
        money(event["amount_usdt"])
    else:
        raise FusionError("UNKNOWN_EVENT", str(kind))
