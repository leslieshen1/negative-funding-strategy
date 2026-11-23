"""SYNTHETIC state invariants. These checks are not return evidence and not a backtest."""

import copy
import unittest
from decimal import Decimal
from pathlib import Path

from fusion_a.decimal_util import money
from fusion_a.engine import apply_receipt, canonical, cycle, fold
from fusion_a.gate import load_frozen_negative_gate, negative_signal_eligible
from fusion_a.identity import solve_identity
from fusion_a.state import echo_facts, structural_invariants
from fusion_a.synthetic_fixture import asset, calibration, config, receipt, snapshot

ROOT = Path(__file__).resolve().parents[2]


def continue_snapshot(previous, ledger, **overrides):
    facts = echo_facts(ledger)
    decision = previous["decision_time_ms"] + 1000
    echo_ids = {row["product_id"] for row in facts["yield_products"]}
    yield_rows = [copy.deepcopy(row) for row in previous["yield_products"] if row["product_id"] not in echo_ids] + copy.deepcopy(facts["yield_products"])
    built = snapshot(
        sequence=previous["sequence"] + 1,
        decision_time_ms=decision,
        assets=copy.deepcopy(previous["universe"]),
        snapshot_id=f"synthetic-snapshot-{previous['sequence'] + 1}",
        opening_fact=False,
        yield_products=yield_rows,
        execution_calibration=previous["execution_calibration"],
        capital_flows=[],
        risk_flags=[],
        rc2_negative_decisions=previous["rc2_negative_decisions"],
    )
    built["cash"] = facts["cash"]
    built["positions"] = facts["positions"]
    built["liabilities"] = facts["liabilities"]
    built["open_orders"] = facts["open_orders"]
    built["event_time_ms"] = decision - 1
    built["received_at_ms"] = decision
    built["valid_until_ms"] = decision + 60_000
    built.update(overrides)
    return built


class IdentityTests(unittest.TestCase):
    def test_scale_and_fraction_grid_uses_exact_decimals(self):
        for scale in ("25000000", "50000000", "100000000"):
            for fraction in ("0.50", "0.55", "0.60"):
                gross = solve_identity({
                    "interpretation": "GROSS_AUM_INCLUDING_BORROW",
                    "scale_usdt": scale,
                    "usdt_fraction": fraction,
                    "identity_confirmed": True,
                    "cash_has_no_extra_liabilities": True,
                    "usdt_fraction_is_gross_asset_weight": True,
                })
                cash = money(scale) * money(fraction)
                self.assertEqual(money(gross["cash_budget_usdt"]), cash)
                self.assertEqual(money(gross["borrowed_market_value_usdt"]), money(scale) - cash)
                self.assertEqual(money(gross["gross_assets_usdt"]), money(scale))
                self.assertEqual(money(gross["net_economic_capital_usdt"]), cash)
                net = solve_identity({
                    "interpretation": "NET_ECONOMIC_CAPITAL",
                    "scale_usdt": scale,
                    "usdt_fraction": fraction,
                    "identity_confirmed": True,
                    "cash_has_no_extra_liabilities": True,
                    "usdt_fraction_is_gross_asset_weight": True,
                })
                gross_assets = money(scale) / money(fraction)
                self.assertEqual(money(net["cash_budget_usdt"]), money(scale))
                self.assertEqual(money(net["gross_assets_usdt"]), gross_assets)
                self.assertEqual(money(net["borrowed_market_value_usdt"]), gross_assets - money(scale))
                self.assertEqual(
                    money(net["cash_budget_usdt"]) + money(net["borrowed_market_value_usdt"]),
                    money(net["gross_assets_usdt"]),
                )

    def test_unconfirmed_identity_does_not_invent_amounts(self):
        result = solve_identity({
            "interpretation": "GROSS_AUM_INCLUDING_BORROW",
            "scale_usdt": "50000000",
            "usdt_fraction": "0.55",
            "identity_confirmed": False,
            "cash_has_no_extra_liabilities": True,
            "usdt_fraction_is_gross_asset_weight": True,
        })
        self.assertFalse(result["applicable"])
        self.assertIsNone(result["cash_budget_usdt"])
        self.assertIn("CAPITAL_IDENTITY_UNCONFIRMED", result["reason_codes"])

    def test_frozen_gate_matches_release_file(self):
        gate = load_frozen_negative_gate(ROOT)
        self.assertEqual(gate["entry_rate_8h_strict_below"], Decimal("-0.001"))
        self.assertEqual(gate["entry_basis_max"], Decimal("-0.003"))
        self.assertEqual(gate["borrow_interest_rate"], Decimal("0"))
        self.assertFalse(gate["engine_invoked"])
        self.assertFalse(gate["baseline_submission_allowed"])
        eligible, _ = negative_signal_eligible(Decimal("-0.001"), 8, Decimal("-0.003"), gate)
        self.assertFalse(eligible)
        eligible, _ = negative_signal_eligible(Decimal("-0.0010001"), 8, Decimal("-0.003"), gate)
        self.assertTrue(eligible)
        eligible, _ = negative_signal_eligible(Decimal("-0.0006"), 4, Decimal("-0.003"), gate)
        self.assertTrue(eligible)
        eligible, _ = negative_signal_eligible(Decimal("-0.002"), 8, Decimal("-0.0029"), gate)
        self.assertFalse(eligible)


class AllocationTests(unittest.TestCase):
    def test_positive_carry_respects_cash_multiple_and_concentration(self):
        cfg = config(per_coin_gross_cap_fraction="0.10", safety_reserve_fraction_of_cash="0.20", cash_floor_fraction_of_cash="0.05")
        snap = snapshot(cash="27500000")
        batch, ledger = cycle(cfg, snap, root=ROOT)
        self.assertFalse(batch["submission_allowed"])
        self.assertEqual(batch["execution_actions"], [])
        self.assertTrue(batch["pnl_is_not_evidence"])
        self.assertEqual(batch["baseline"]["commit"], "6b98320bfb707c903212e26888cef79a1c12342f")
        self.assertFalse(batch["replaces_rc2"])
        self.assertEqual(batch["allocation"]["status"], "SYNTHETIC_STATE_ONLY")
        target = batch["targets"][0]
        self.assertEqual(target["desired_spot_qty_token"], "50000")
        self.assertEqual(target["desired_perp_qty_token"], "-50000")
        self.assertEqual(target["planned_new_cash_usdt"], "10000000")
        self.assertFalse(target["is_fill"])
        self.assertEqual(ledger["positions"], [])
        self.assertEqual(batch["books"]["identity_status"], "HELD")
        self.assertTrue(any(row["reason_codes"] == ["YIELD_APR_UNKNOWN_NO_HARDCODE"] for row in batch["intents"]))

    def test_missing_calibration_does_not_invent_bps(self):
        snap = snapshot(execution_calibration=None)
        batch, _ = cycle(config(), snap, root=ROOT)
        self.assertEqual(batch["allocation"]["status"], "UNVERIFIED_NO_NEW")
        self.assertFalse(batch["allocation"]["new_exposure_permitted"])
        self.assertEqual(batch["targets"], [])
        encoded = canonical(batch)
        self.assertNotIn("0.0006", encoded)
        self.assertNotIn("0.00032", encoded)
        self.assertIsNone(batch["allocation"]["breakeven"][0]["round_trip_cost_fraction"])

    def test_partial_calibration_is_breakeven_only(self):
        snap = snapshot(execution_calibration=calibration(impact_known=False, round_trip_impact_rate=None, completion_known=False, completion_probability=None, spot_fee_rate="0.0001", perp_fee_rate="0.0002"))
        batch, _ = cycle(config(), snap, root=ROOT)
        self.assertEqual(batch["allocation"]["status"], "BREAKEVEN_ONLY")
        self.assertFalse(batch["allocation"]["new_exposure_proposed"])
        row = batch["allocation"]["breakeven"][0]
        self.assertEqual(row["fee_only_round_trip_rate"], "0.0006")
        self.assertIsNone(row["round_trip_cost_fraction"])
        self.assertIsNone(row["net_carry_apr"])

    def test_unknown_yield_keeps_cash_and_known_yield_can_win(self):
        dominated = snapshot()
        dominated["yield_products"][0]["apr_known"] = True
        dominated["yield_products"][0]["apr"] = "1"
        batch, _ = cycle(config(), dominated, root=ROOT)
        self.assertFalse(batch["allocation"]["new_exposure_proposed"])
        self.assertTrue(any(item["action"] == "SUBSCRIBE_INTENTION" for item in batch["intents"]))
        self.assertEqual(batch["books"]["yield_realized_usdt"], "0")

    def test_forecast_field_does_not_change_the_target(self):
        plain, _ = cycle(config(), snapshot(), root=ROOT)
        noisy = snapshot(snapshot_id="synthetic-snapshot-forecast")
        noisy["universe"][0]["ignored_forecast_rate"] = "0.5"
        other, _ = cycle(config(), noisy, root=ROOT)
        self.assertEqual(plain["targets"][0]["desired_spot_qty_token"], other["targets"][0]["desired_spot_qty_token"])
        self.assertIn("FORECAST_IGNORED_NOT_USED", other["allocation"]["reason_codes"])

    def test_oi_missing_rejects_adds_and_adv_only_is_labeled_weaker(self):
        missing = snapshot()
        missing["universe"][0]["oi_known"] = False
        missing["universe"][0]["lagged_oi_quote_usdt"] = None
        batch, _ = cycle(config(), missing, root=ROOT)
        self.assertFalse(batch["allocation"]["new_exposure_proposed"])
        self.assertIn("OI_MISSING_REJECT_ADD", batch["targets"][0]["reason_codes"])
        weaker = snapshot()
        weaker["universe"][0]["oi_known"] = False
        weaker["universe"][0]["lagged_oi_quote_usdt"] = None
        batch, _ = cycle(config(oi_missing_policy="adv_only_weaker"), weaker, root=ROOT)
        self.assertEqual(batch["allocation"]["capacity_claim"], "ADV_ONLY_WEAKER_NOT_VERIFIED")
        self.assertTrue(batch["allocation"]["new_exposure_proposed"])

    def test_negative_sleeve_is_capped_and_cannot_spend_a_cash(self):
        cfg = config(
            negative_budget_fraction_of_cash="0.05",
            allow_new_negative_without_rc2_decision=True,
            per_coin_gross_cap_fraction="1",
        )
        coin = asset("SYN", rate="-0.002", basis="-0.003", borrow_available=True, borrowable_qty_token="1000000")
        snap = snapshot(cash="27500000", assets=[coin])
        batch, ledger = cycle(cfg, snap, root=ROOT)
        target = next(row for row in batch["targets"] if row["book"] == "NEGATIVE_FUNDING")
        self.assertEqual(target["desired_perp_qty_token"], "13750")
        self.assertEqual(target["desired_spot_qty_token"], "0")
        self.assertEqual(target["planned_new_cash_usdt"], "1375000")
        self.assertEqual(batch["allocation"]["budgets"]["a_cash_used_for_b_margin_usdt"], "0")
        self.assertEqual(ledger["cash_lots"][0]["amount"], "27500000")
        self.assertIn("NEGATIVE_ADAPTER_WITHOUT_RC2_DECISION_SYNTHETIC_ONLY", target["reason_codes"])

    def test_negative_without_rc2_decision_is_not_sized(self):
        coin = asset("SYN", rate="-0.002", basis="-0.003", borrow_available=True, borrowable_qty_token="10")
        batch, _ = cycle(config(negative_budget_fraction_of_cash="0.05"), snapshot(assets=[coin]), root=ROOT)
        self.assertFalse(any(row["book"] == "NEGATIVE_FUNDING" and money(row["planned_new_cash_usdt"]) > 0 for row in batch["targets"]))

    def test_expired_frozen_and_kill_switch_do_not_flatten_the_ledger(self):
        cfg = config(on_fault="REDUCE_TO_FLAT_TARGET")
        first, ledger = cycle(cfg, snapshot(), root=ROOT)
        self.assertEqual(first["targets"][0]["desired_spot_qty_token"], "50000")
        opened = continue_snapshot(snapshot(), ledger, risk_flags=["KILL_SWITCH"])
        # The opening ledger has no position; seed a fact position and prove a fault does not erase it.
        held = snapshot(
            positions=[{
                "book": "POSITIVE_CARRY",
                "asset": "SYN",
                "spot_account": "B",
                "perp_account": "B",
                "spot_qty_token": "1",
                "spot_avg_cost_usdt": "100",
                "perp_qty_token": "-1",
                "perp_avg_entry_usdt": "100",
                "margin_usdt": "100",
                "spot_reserved_token": "0",
                "perp_close_reserved_token": "0",
            }],
            cash=None,
        )
        held["cash"] = [
            {"lot_id": "B-IDLE", "account": "B", "purpose": "IDLE", "amount": "800"},
            {"lot_id": "B-PERP_MARGIN", "account": "B", "purpose": "PERP_MARGIN", "amount": "100"},
        ]
        held["risk_flags"] = ["KILL_SWITCH"]
        batch, ledger = cycle(config(on_fault="STOP_NEW"), held, root=ROOT)
        self.assertEqual(ledger["positions"][0]["spot_qty_token"], "1")
        self.assertEqual(batch["targets"][0]["desired_spot_qty_token"], "1")
        self.assertIn("KILL_SWITCH", batch["allocation"]["reason_codes"])
        flat_policy, _ = cycle(config(on_fault="REDUCE_TO_FLAT_TARGET"), held, root=ROOT)
        self.assertEqual(flat_policy["targets"][0]["desired_spot_qty_token"], "0")
        self.assertEqual(flat_policy["targets"][0]["is_fill"], False)

    def test_expired_snapshot_adds_nothing(self):
        snap = snapshot()
        snap["valid_until_ms"] = snap["decision_time_ms"] - 1
        snap["event_time_ms"] = snap["valid_until_ms"] - 1
        batch, _ = cycle(config(), snap, root=ROOT)
        self.assertIn("EXPIRED_SNAPSHOT", batch["allocation"]["reason_codes"])
        self.assertFalse(batch["allocation"]["new_exposure_proposed"])

    def test_open_order_reserves_cash_until_cancel_ack(self):
        snap = snapshot(cash=None)
        snap["cash"] = [
            {"lot_id": "B-IDLE", "account": "B", "purpose": "IDLE", "amount": "600"},
            {"lot_id": "B-OPEN_ORDER", "account": "B", "purpose": "OPEN_ORDER", "amount": "400"},
        ]
        snap["open_orders"] = [{
            "order_id": "synthetic-order-1",
            "client_order_id": "synthetic-client-1",
            "target_id": "fa-seed",
            "book": "POSITIVE_CARRY",
            "asset": "SYN",
            "account": "B",
            "leg": "SPOT",
            "side": "BUY",
            "qty_token": "4",
            "filled_qty_token": "0",
            "limit_price": "100",
            "state": "NEW",
            "initial_margin_fraction": "1",
            "reserves_margin": False,
        }]
        snap["universe"][0]["spot_usdt"] = "100"
        cfg = config(per_coin_gross_cap_fraction="1", min_order_usdt="1")
        batch, ledger = cycle(cfg, snap, root=ROOT)
        self.assertEqual(batch["allocation"]["budgets"]["b_open_order_usdt"], "400")
        self.assertLessEqual(money(batch["allocation"]["budgets"]["planned_positive_cash_usdt"]), Decimal("600"))
        requested = receipt(1, [{"type": "CANCEL_REQUESTED", "order_id": "synthetic-order-1"}])
        ledger = apply_receipt(ledger, requested, cfg)
        self.assertEqual(next(row["state"] for row in ledger["open_orders"]), "CANCEL_REQUESTED")
        self.assertEqual(
            sum(money(row["amount"]) for row in ledger["cash_lots"] if row["purpose"] == "OPEN_ORDER"),
            Decimal("400"),
        )
        released = apply_receipt(ledger, receipt(2, [{"type": "CANCEL_ACK", "order_id": "synthetic-order-1"}]), cfg)
        self.assertEqual(sum(money(row["amount"]) for row in released["cash_lots"] if row["purpose"] == "OPEN_ORDER"), Decimal("0"))
        self.assertEqual(sum(money(row["amount"]) for row in released["cash_lots"] if row["account"] == "B" and row["purpose"] == "IDLE"), Decimal("1000"))


class ReceiptTests(unittest.TestCase):
    def test_partial_fill_duplicate_reorder_and_pnl_identity(self):
        cfg = config(per_coin_gross_cap_fraction="1", min_order_usdt="1")
        snap = snapshot(cash="1000", assets=[asset(rate="0.01")])
        batch, ledger = cycle(cfg, snap, root=ROOT)
        self.assertTrue(batch["allocation"]["new_exposure_proposed"])
        spot = receipt(1, [{
            "type": "FILL",
            "fill_id": "synthetic-fill-spot",
            "order_id": None,
            "book": "POSITIVE_CARRY",
            "asset": "SYN",
            "account": "B",
            "leg": "SPOT",
            "side": "BUY",
            "qty_token": "1",
            "price": "100",
            "fee_usdt": "0",
            "fee_known": True,
            "fee_purpose": "IDLE",
            "initial_margin_fraction": "1",
        }])
        ledger = apply_receipt(ledger, spot, cfg)
        self.assertEqual(ledger["positions"][0]["spot_qty_token"], "1")
        self.assertEqual(ledger["positions"][0]["perp_qty_token"], "0")
        again = apply_receipt(ledger, spot, cfg)
        self.assertEqual(again["positions"][0]["spot_qty_token"], "1")
        late = receipt(3, [{
            "type": "FILL",
            "fill_id": "synthetic-fill-perp",
            "order_id": None,
            "book": "POSITIVE_CARRY",
            "asset": "SYN",
            "account": "B",
            "leg": "PERP",
            "side": "SELL",
            "qty_token": "1",
            "price": "100",
            "fee_usdt": None,
            "fee_known": False,
            "fee_purpose": "IDLE",
            "initial_margin_fraction": "1",
        }])
        parked = apply_receipt(ledger, late, cfg)
        self.assertEqual(parked["positions"][0]["perp_qty_token"], "0")
        self.assertEqual(len(parked["pending_receipts"]), 1)
        perp = receipt(2, late["events"], receipt_id="synthetic-receipt-2")
        done = apply_receipt(parked, perp, cfg)
        self.assertEqual(done["positions"][0]["perp_qty_token"], "-1")
        self.assertEqual(done["pending_receipts"], [])
        self.assertFalse(done["pnl"]["fees_complete"])
        report = structural_invariants(done)
        self.assertEqual(report["book_residual_usdt"], "0")
        self.assertEqual(report["identity_status"], "UNVERIFIED_FEES")
        self.assertNotEqual(report["fees_usdt"], "0.0006")

    def test_known_fees_funding_mark_and_open_liability_identity(self):
        cfg = config()
        snap = snapshot(cash="1000", assets=[asset("SYN"), asset("ETH", rate="-0.0001")])
        _, ledger = cycle(cfg, snap, root=ROOT)
        ledger = apply_receipt(ledger, receipt(1, [
            _fill("SPOT", "BUY", "1", "100", "synthetic-fill-1"),
            _fill("PERP", "SELL", "1", "100", "synthetic-fill-2"),
        ]), cfg)
        ledger = apply_receipt(ledger, receipt(2, [{
            "type": "FUNDING",
            "funding_id": "synthetic-funding-1",
            "book": "POSITIVE_CARRY",
            "asset": "SYN",
            "account": "B",
            "qty_token": "1",
            "amount_usdt": "1",
            "settled": True,
            "window_start_ms": 1,
            "window_end_ms": 2,
        }], event_time_ms=10), cfg)
        marked = continue_snapshot(snap, ledger)
        marked["universe"][0]["spot_usdt"] = "110"
        marked["universe"][0]["perp_usdt"] = "110"
        marked["universe"][0]["index_usdt"] = "110"
        _, ledger = cycle(cfg, marked, ledger, root=ROOT)
        report = structural_invariants(ledger)
        self.assertTrue(report["ok"])
        self.assertEqual(report["identity_status"], "HELD")
        self.assertEqual(report["funding_usdt"], "1")
        self.assertEqual(report["price_pnl_usdt"], "0")
        self.assertEqual(report["equity_usdt"], "1001")
        self.assertIsNone(report["annualized_return"])
        borrowed = apply_receipt(ledger, receipt(3, [{
            "type": "BORROW",
            "lot_id": "synthetic-borrow-1",
            "asset": "ETH",
            "qty_token": "1",
            "mark_usdt": "100",
            "owner": "synthetic-provider",
            "recall_deadline_ms": None,
        }]), cfg)
        sold = apply_receipt(borrowed, receipt(4, [
            _fill("SPOT", "SELL", "1", "100", "synthetic-fill-3", book="NEGATIVE_FUNDING", asset="ETH", account="A", fee_purpose="BUYBACK_RESERVE"),
        ]), cfg)
        self.assertTrue(any(row["purpose"] == "BUYBACK_RESERVE" and row["account"] == "A" and row["amount"] == "100" for row in sold["cash_lots"]))
        self.assertFalse(any(row["account"] == "A" and row["purpose"] == "PERP_MARGIN" for row in sold["cash_lots"]))
        self.assertEqual(sold["pnl"]["borrow_interest_usdt"], "0")
        moved = continue_snapshot(marked, sold)
        moved["universe"][1]["spot_usdt"] = "120"
        moved["universe"][1]["perp_usdt"] = "120"
        moved["universe"][1]["index_usdt"] = "120"
        _, valued = cycle(cfg, moved, sold, root=ROOT)
        report = structural_invariants(valued)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["obligations_open"])
        self.assertIsNone(report["annualized_return"])
        self.assertEqual(report["borrow_interest_usdt"], "0")
        blocked = apply_receipt(valued, receipt(5, [{"type": "TRANSFER", "transfer_id": "synthetic-transfer", "from_account": "A", "to_account": "B", "purpose_from": "BUYBACK_RESERVE", "purpose_to": "PERP_MARGIN", "amount": "100", "source_purpose_is_sale_proceeds": True}]), cfg)
        self.assertEqual(blocked["quarantine"][-1]["reason"], "AB_TRANSFER_FORBIDDEN")
        buyback = next(row["amount"] for row in blocked["cash_lots"] if row["account"] == "A" and row["purpose"] == "BUYBACK_RESERVE")
        self.assertEqual(buyback, "100")
        approved = config()
        approved["policy"]["ab_transfer_allowed"] = {
            "authorized": True,
            "evidence_id": "synthetic-approval",
            "approver": "synthetic-approver",
        }
        still_blocked = apply_receipt(blocked, receipt(5, [{"type": "TRANSFER", "transfer_id": "synthetic-transfer-2", "from_account": "A", "to_account": "B", "purpose_from": "BUYBACK_RESERVE", "purpose_to": "PERP_MARGIN", "amount": "100", "source_purpose_is_sale_proceeds": True}], receipt_id="synthetic-transfer-2"), approved)
        self.assertEqual(still_blocked["quarantine"][-1]["reason"], "SALE_PROCEEDS_CANNOT_FUND_B_MARGIN")
        self.assertEqual(next(row["amount"] for row in still_blocked["cash_lots"] if row["account"] == "A" and row["purpose"] == "BUYBACK_RESERVE"), "100")
        interest = apply_receipt(blocked, receipt(5, [{"type": "INTEREST", "lot_id": "synthetic-borrow-1", "amount_usdt": "5"}], receipt_id="synthetic-interest"), cfg)
        self.assertEqual(interest["quarantine"][-1]["reason"], "INTEREST_FREE_BORROW_REJECTS_NONZERO_INTEREST")
        self.assertEqual(interest["pnl"]["borrow_interest_usdt"], "0")

    def test_deposit_and_withdrawal_are_not_pnl(self):
        cfg = config()
        snap = snapshot(cash="1000", assets=[asset(rate="-0.0001")])
        _, ledger = cycle(cfg, snap, root=ROOT)
        deposited = continue_snapshot(snap, ledger, capital_flows=[{
            "flow_id": "synthetic-in",
            "account": "B",
            "asset": "USDT",
            "amount": "100",
            "kind": "SUBSCRIPTION",
            "already_in_balances": False,
            "status": "SETTLED",
        }])
        _, ledger = cycle(cfg, deposited, ledger, root=ROOT)
        report = structural_invariants(ledger)
        self.assertEqual(report["external_flows_usdt"], "100")
        self.assertEqual(report["price_pnl_usdt"], "0")
        self.assertEqual(report["equity_usdt"], "1100")
        self.assertTrue(report["identity_holds"])
        withdrawn = continue_snapshot(deposited, ledger, capital_flows=[{
            "flow_id": "synthetic-out",
            "account": "B",
            "asset": "USDT",
            "amount": "40",
            "kind": "REDEMPTION",
            "already_in_balances": False,
            "status": "SETTLED",
        }])
        _, ledger = cycle(cfg, withdrawn, ledger, root=ROOT)
        report = structural_invariants(ledger)
        self.assertEqual(report["external_flows_usdt"], "60")
        self.assertEqual(report["equity_usdt"], "1060")
        self.assertEqual(report["funding_usdt"], "0")
        self.assertTrue(report["identity_holds"])

    def test_retry_budget_stops_new_exposure(self):
        cfg = config(max_retries=1, per_coin_gross_cap_fraction="1")
        snap = snapshot(cash="1000", assets=[asset(rate="0.01")])
        _, ledger = cycle(cfg, snap, root=ROOT)
        for index, name in ((1, "one"), (2, "two")):
            ledger = apply_receipt(ledger, receipt(index, [{
                "type": "REJECT",
                "client_order_id": "synthetic-client",
                "asset": "SYN",
                "book": "POSITIVE_CARRY",
                "retryable": True,
            }], receipt_id=f"synthetic-reject-{name}"), cfg)
        self.assertIn("POSITIVE_CARRY:SYN", ledger["blocked_assets"])
        replay = apply_receipt(ledger, receipt(2, [{
            "type": "REJECT",
            "client_order_id": "synthetic-client",
            "asset": "SYN",
            "book": "POSITIVE_CARRY",
            "retryable": True,
        }], receipt_id="synthetic-reject-two"), cfg)
        self.assertEqual(replay["retry_counts"]["synthetic-client"], 2)
        nxt = continue_snapshot(snap, ledger)
        batch, _ = cycle(cfg, nxt, ledger, root=ROOT)
        self.assertFalse(any(money(row["planned_new_cash_usdt"]) > 0 for row in batch["targets"]))

    def test_snapshot_claiming_flat_does_not_erase_the_ledger(self):
        cfg = config()
        snap = snapshot(cash="1000")
        _, ledger = cycle(cfg, snap, root=ROOT)
        ledger = apply_receipt(ledger, receipt(1, [_fill("SPOT", "BUY", "1", "100", "synthetic-fill-1")]), cfg)
        lying = continue_snapshot(snap, ledger, positions=[])
        batch, updated = cycle(cfg, lying, ledger, root=ROOT)
        self.assertIn("POSITION_MISMATCH", batch["allocation"]["reason_codes"])
        self.assertEqual(updated["positions"][0]["spot_qty_token"], "1")
        self.assertTrue(all(row["is_fill"] is False for row in batch["targets"]))

    def test_prefix_targets_ignore_a_future_suffix(self):
        cfg = config()
        first = snapshot()
        _, ledger = cycle(cfg, first, root=ROOT)
        second = continue_snapshot(first, ledger)
        second["universe"][0]["funding_rate_observed"] = "0.0002"
        events = [{"kind": "snapshot", "snapshot": first}, {"kind": "snapshot", "snapshot": second}]
        prefix, _ = fold(cfg, events, root=ROOT)
        suffix = continue_snapshot(second, fold(cfg, events, root=ROOT)[1])
        suffix["universe"][0]["funding_rate_observed"] = "0.9"
        suffix["universe"][0]["ignored_forecast_rate"] = "9"
        longer, _ = fold(cfg, events + [{"kind": "snapshot", "snapshot": suffix}], root=ROOT)
        self.assertEqual(canonical(prefix), canonical(longer[:2]))
        self.assertNotEqual(canonical(longer[1]), canonical(longer[2]))

    def test_same_snapshot_is_idempotent(self):
        cfg = config()
        snap = snapshot()
        first, ledger = cycle(cfg, snap, root=ROOT)
        second, again = cycle(cfg, snap, ledger, root=ROOT)
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(ledger["ledger_version"], again["ledger_version"])

    def test_source_has_no_default_fee_or_apr_constants(self):
        text = "\n".join(path.read_text() for path in (ROOT / "fusion_a").glob("*.py"))
        self.assertNotIn("0.00032", text)
        self.assertNotIn("0.0004", text)
        self.assertNotIn("0.00012", text)
        self.assertNotIn("hardcoded_apr", text)


def _fill(leg, side, qty, price, fill_id, book="POSITIVE_CARRY", asset="SYN", account="B", fee_purpose="IDLE"):
    return {
        "type": "FILL",
        "fill_id": fill_id,
        "order_id": None,
        "book": book,
        "asset": asset,
        "account": account,
        "leg": leg,
        "side": side,
        "qty_token": qty,
        "price": price,
        "fee_usdt": "0",
        "fee_known": True,
        "fee_purpose": fee_purpose,
        "initial_margin_fraction": "1",
    }


class RecallTests(unittest.TestCase):
    def test_recall_stops_new_exposure_and_does_not_erase_the_liability(self):
        cfg = config()
        snap = snapshot(cash="1000", assets=[asset("ETH", rate="-0.002", basis="-0.003")])
        snap["liabilities"] = [{
            "lot_id": "synthetic-borrow-1",
            "asset": "ETH",
            "qty_token": "1",
            "inception_mark_usdt": "100",
            "interest_rate": "0",
            "owner": "synthetic-provider",
            "recall_status": "RECEIVED",
            "recall_deadline_ms": snap["decision_time_ms"] + 1_000,
        }]
        snap["positions"] = [{
            "book": "NEGATIVE_FUNDING",
            "asset": "ETH",
            "spot_account": "A",
            "perp_account": "B",
            "spot_qty_token": "1",
            "spot_avg_cost_usdt": "100",
            "perp_qty_token": "0",
            "perp_avg_entry_usdt": "0",
            "margin_usdt": "0",
            "spot_reserved_token": "0",
            "perp_close_reserved_token": "0",
        }]
        batch, ledger = cycle(cfg, snap, root=ROOT)
        self.assertIn("COIN_RECALL", batch["allocation"]["reason_codes"])
        self.assertFalse(batch["allocation"]["new_exposure_proposed"])
        self.assertEqual(ledger["liabilities"][0]["qty_token"], "1")
        target = batch["targets"][0]
        self.assertEqual(target["desired_liability_qty_token"], "1")
        self.assertFalse(target["is_fill"])
        self.assertIn("RECALL_DEADLINE_INFEASIBLE", batch["allocation"]["reason_codes"] + target["reason_codes"])

    def test_redeem_pending_is_not_deployable_cash(self):
        cfg = config(per_coin_gross_cap_fraction="1")
        snap = snapshot(cash="1000", assets=[asset(rate="0.01")])
        batch, ledger = cycle(cfg, snap, root=ROOT)
        before = money(batch["targets"][0]["planned_new_cash_usdt"])
        ledger = apply_receipt(ledger, receipt(1, [{
            "type": "YIELD",
            "product_id": "synthetic-yield",
            "action": "SUBSCRIBE",
            "quantity": "0",
            "nav": "1",
            "amount_usdt": "400",
            "account": "B",
            "asset": "USDT",
        }]), cfg)
        ledger = apply_receipt(ledger, receipt(2, [{
            "type": "YIELD",
            "product_id": "synthetic-yield",
            "action": "REDEEM_PENDING",
            "quantity": "0",
            "nav": "1",
            "amount_usdt": "0",
            "account": "B",
            "asset": "USDT",
        }]), cfg)
        nxt = continue_snapshot(snap, ledger)
        after, updated = cycle(cfg, nxt, ledger, root=ROOT)
        self.assertEqual(updated["yield_positions"][0]["state"], "REDEEM_PENDING")
        self.assertLess(money(after["allocation"]["budgets"]["b_idle_usdt"]), Decimal("1000"))
        self.assertLess(money(after["targets"][0]["planned_new_cash_usdt"]), before)
        report = structural_invariants(updated)
        self.assertTrue(report["identity_holds"], report)
        self.assertTrue(report["obligations_open"])


class MatrixTests(unittest.TestCase):
    def test_each_scale_and_fraction_keeps_flows_retries_expiry_orders_and_pnl(self):
        for scale, cash in (("25000000", "12500000"), ("50000000", "27500000"), ("100000000", "60000000")):
            fraction = {"12500000": "0.50", "27500000": "0.55", "60000000": "0.60"}[cash]
            cfg = config(scale=scale, fraction=fraction, per_coin_gross_cap_fraction="1", max_retries=0, min_order_usdt="1")
            snap = snapshot(cash=cash, assets=[asset(rate="0.01")])
            batch, ledger = cycle(cfg, snap, root=ROOT)
            self.assertTrue(batch["synthetic"])
            self.assertEqual(batch["fixture_label"], "SYNTHETIC_NOT_AN_ACCOUNT_OR_TRADE")
            self.assertGreater(money(batch["targets"][0]["desired_spot_qty_token"]), 0)
            deposited = continue_snapshot(snap, ledger, capital_flows=[{
                "flow_id": f"synthetic-in-{scale}",
                "account": "B",
                "asset": "USDT",
                "amount": "100",
                "kind": "SUBSCRIPTION",
                "already_in_balances": False,
                "status": "SETTLED",
            }])
            _, ledger = cycle(cfg, deposited, ledger, root=ROOT)
            self.assertEqual(structural_invariants(ledger)["external_flows_usdt"], "100")
            expired = continue_snapshot(deposited, ledger)
            expired["valid_until_ms"] = expired["decision_time_ms"] - 1
            expired["event_time_ms"] = expired["valid_until_ms"] - 1
            expired_batch, ledger = cycle(cfg, expired, ledger, root=ROOT)
            self.assertIn("EXPIRED_SNAPSHOT", expired_batch["allocation"]["reason_codes"])
            self.assertFalse(expired_batch["allocation"]["new_exposure_proposed"])
            ledger = apply_receipt(ledger, receipt(1, [{
                "type": "ORDER_ACK",
                "order_id": f"synthetic-order-{scale}",
                "client_order_id": f"synthetic-client-{scale}",
                "target_id": "fa-seed",
                "book": "POSITIVE_CARRY",
                "asset": "SYN",
                "account": "B",
                "leg": "SPOT",
                "side": "BUY",
                "qty_token": "1",
                "limit_price": "100",
                "initial_margin_fraction": "1",
            }], receipt_id=f"synthetic-ack-{scale}"), cfg)
            reserved = continue_snapshot(expired, ledger)
            held, ledger = cycle(cfg, reserved, ledger, root=ROOT)
            self.assertEqual(held["allocation"]["budgets"]["b_open_order_usdt"], "100")
            self.assertEqual(next(row["state"] for row in ledger["open_orders"]), "NEW")
            ledger = apply_receipt(ledger, receipt(2, [{
                "type": "REJECT",
                "client_order_id": f"synthetic-client-{scale}",
                "asset": "SYN",
                "book": "POSITIVE_CARRY",
                "retryable": True,
            }], receipt_id=f"synthetic-reject-{scale}"), cfg)
            nxt = continue_snapshot(reserved, ledger)
            blocked, ledger = cycle(cfg, nxt, ledger, root=ROOT)
            self.assertFalse(blocked["allocation"]["new_exposure_proposed"])
            ledger = apply_receipt(ledger, receipt(3, [{
                "type": "BORROW",
                "lot_id": f"synthetic-borrow-{scale}",
                "asset": "SYN",
                "qty_token": "1",
                "mark_usdt": "100",
                "owner": "synthetic-provider",
                "recall_deadline_ms": None,
            }]), cfg)
            final = structural_invariants(ledger)
            self.assertTrue(final["obligations_open"])
            self.assertIsNone(final["annualized_return"])
            self.assertEqual(final["borrow_interest_usdt"], "0")
            self.assertTrue(final["identity_holds"], final)
            self.assertEqual(final["book_residual_usdt"], "0")


if __name__ == "__main__":
    unittest.main()
