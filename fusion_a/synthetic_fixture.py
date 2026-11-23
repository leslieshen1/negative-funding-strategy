"""SYNTHETIC fixtures only. These are not inventories, accounts, or trades."""

FIXTURE_LABEL = "SYNTHETIC_NOT_AN_ACCOUNT_OR_TRADE"


def policy(**overrides):
    base = {
        "ab_transfer_allowed": False,
        "a_sale_proceeds_fund_b_margin": False,
        "safety_reserve_fraction_of_cash": "0",
        "cash_floor_fraction_of_cash": "0",
        "negative_budget_fraction_of_cash": "0",
        "negative_budget_cap_usdt": None,
        "initial_margin_fraction": "1",
        "max_planned_hold_hours": 24,
        "min_completion_probability": None,
        "adv_participation_target": "0.05",
        "adv_participation_delta": "0.05",
        "oi_cap_fraction": "0.025",
        "oi_missing_policy": "reject_add",
        "per_coin_gross_cap_fraction": "0.10",
        "min_order_usdt": "10",
        "max_retries": 3,
        "max_leg_imbalance_ms": 3600000,
        "on_fault": "STOP_NEW",
        "depeg_haircut": "0",
        "kill_switch": False,
        "target_ttl_ms": 60000,
        "snapshot_max_age_ms": 10000,
        "allow_new_negative_without_rc2_decision": False,
        "allow_usdt_fraction_outside_band": False,
        "year_hours": 8760,
        "enforce_wall_clock": False,
        "exchange_halt_stops_new": True,
        "settlement_book_skew_ms": 15000,
    }
    base.update(overrides)
    return base


def config(scale="50000000", fraction="0.55", interpretation="GROSS_AUM_INCLUDING_BORROW", **policy_overrides):
    return {
        "schema_version": "nfb.fusion-a.config.v1",
        "strategy_id": "nfb.fusion-a.research.20261123",
        "strategy_version": "0.1.0",
        "mode": "dry_run",
        "submission_allowed": False,
        "source_mode": "synthetic",
        "synthetic": True,
        "fixture_label": FIXTURE_LABEL,
        "capital": {
            "interpretation": interpretation,
            "scale_usdt": scale,
            "usdt_fraction": fraction,
            "identity_confirmed": True,
            "cash_has_no_extra_liabilities": True,
            "usdt_fraction_is_gross_asset_weight": True,
        },
        "policy": policy(**policy_overrides),
    }


def calibration(**overrides):
    base = {
        "source_mode": "synthetic",
        "synthetic": True,
        "fixture_label": FIXTURE_LABEL,
        "fee_schedule_known": True,
        "spot_fee_rate": "0",
        "perp_fee_rate": "0",
        "impact_known": True,
        "round_trip_impact_rate": "0",
        "completion_known": True,
        "completion_probability": "1",
        "queue_known": False,
        "evidence_id": None,
        "asof_ms": 0,
    }
    base.update(overrides)
    return base


def asset(symbol="SYN", rate="0.0001", basis="0", **overrides):
    base = {
        "asset": symbol,
        "spot_symbol": symbol + "USDT",
        "perp_symbol": symbol + "USDT",
        "tokens_per_perp_quantity": "1",
        "qty_step": "0.00001",
        "min_qty": "0.00001",
        "min_notional_usdt": "5",
        "spot_usdt": "100",
        "perp_usdt": "100",
        "index_usdt": "100",
        "funding_rate_observed": rate,
        "funding_interval_hours": 8,
        "funding_rate_asof_ms": 0,
        "basis": basis,
        "lagged_adv_quote_usdt": "1000000000000",
        "adv_known": True,
        "lagged_oi_quote_usdt": "1000000000000",
        "oi_known": True,
        "borrowable_qty_token": "0",
        "borrow_available": False,
        "filters_ok": True,
        "trading_status": "TRADING",
        "mmr": None,
    }
    base.update(overrides)
    return base


def snapshot(cash="27500000", sequence=1, decision_time_ms=1_700_000_000_000, assets=None, **overrides):
    base = {
        "schema_version": "nfb.fusion-a.snapshot.v1",
        "snapshot_id": f"synthetic-snapshot-{sequence}",
        "source_mode": "synthetic",
        "synthetic": True,
        "fixture_label": FIXTURE_LABEL,
        "event_time_ms": decision_time_ms - 1,
        "received_at_ms": decision_time_ms,
        "valid_until_ms": decision_time_ms + 60_000,
        "sequence": sequence,
        "decision_time_ms": decision_time_ms,
        "frozen": False,
        "opening_fact": sequence == 1,
        "cash": [{"lot_id": "B-IDLE", "account": "B", "purpose": "IDLE", "amount": cash}],
        "positions": [],
        "liabilities": [],
        "open_orders": [],
        "universe": assets if assets is not None else [asset()],
        "yield_products": [
            {
                "product_id": "synthetic-yield",
                "account": "B",
                "asset": "USDT",
                "state": "AVAILABLE",
                "quantity": "0",
                "nav": "1",
                "apr": None,
                "apr_known": False,
                "redeem_delay_ms": 86400000,
                "accrued_unpaid_usdt": "0",
            }
        ],
        "capital_flows": [],
        "execution_calibration": calibration(),
        "risk_flags": [],
        "rc2_negative_decisions": [],
        "balances_are_facts": True,
        "targets_are_not_fills": True,
    }
    base.update(overrides)
    return base


def receipt(sequence, events, receipt_id=None, event_time_ms=1_700_000_000_100):
    return {
        "schema_version": "nfb.fusion-a.receipt.v1",
        "receipt_id": receipt_id or f"synthetic-receipt-{sequence}",
        "source_mode": "synthetic",
        "synthetic": True,
        "fixture_label": FIXTURE_LABEL,
        "stream_id": "synthetic-stream",
        "sequence": sequence,
        "target_id": None,
        "event_time_ms": event_time_ms,
        "received_at_ms": event_time_ms,
        "events": events,
    }
