"""Snapshot to absolute target, then receipt application.

Targets are not fills. Expired, frozen, inconsistent and uncalibrated inputs do not
add exposure. Receipts that do not fit the lots are quarantined. Replaying a snapshot
id returns the original target bytes.
"""

import hashlib
import json
from copy import deepcopy

from fusion_a import BASELINE_COMMIT, BASELINE_STRATEGY_ID, PACKAGE_VERSION, STRATEGY_ID
from fusion_a.allocate import plan
from fusion_a.decimal_util import FusionError, dec_str, money
from fusion_a.gate import load_frozen_negative_gate
from fusion_a.identity import solve_identity
from fusion_a.state import (
    apply_borrow,
    apply_capital_flow,
    apply_fill,
    apply_return,
    apply_transfer,
    apply_yield,
    ack_cancel,
    ack_order,
    book_funding,
    echo_facts,
    explain,
    fact_diff,
    liability_qty,
    live_orders,
    note_interest,
    note_reject,
    position_qty,
    request_cancel,
    seed_from_snapshot,
    set_marks,
    signed_open_qty,
    structural_invariants,
)
from fusion_a.validate import STRATEGY_VERSION, validate_config, validate_receipt, validate_snapshot

TARGET_SCHEMA = "nfb.fusion-a.target-batch.v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def configure(config):
    validate_config(config)
    identity = solve_identity(config["capital"])
    return {
        "schema_version": "nfb.fusion-a.configure.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "package_version": PACKAGE_VERSION,
        "mode": config["mode"],
        "submission_allowed": False,
        "research_only": True,
        "production_accepted": False,
        "replaces_rc2": False,
        "baseline_commit": BASELINE_COMMIT,
        "capital_identity": identity,
        "new_exposure_permitted": False,
        "note": "STRUCTURAL_PARAMETER_NOT_ARRIVED_CASH_AND_NOT_A_TARGET",
    }


def cycle(config, snapshot, ledger=None, root=None, now_ms=None):
    validate_config(config)
    validate_snapshot(snapshot)
    gate = load_frozen_negative_gate(root)
    identity = solve_identity(config["capital"])
    if ledger is None:
        if snapshot["opening_fact"] is not True:
            raise FusionError("OPENING_FACT_REQUIRED")
        ledger = seed_from_snapshot(snapshot)
        _apply_flows(ledger, snapshot)
        set_marks(ledger, snapshot["universe"])
        _touch_imbalance(ledger, snapshot["decision_time_ms"], config["policy"]["max_leg_imbalance_ms"])
        faults = _faults(config, snapshot, ledger, now_ms, [])
        batch = _issue(config, snapshot, ledger, identity, gate, faults)
        _remember(ledger, snapshot, batch)
        return batch, ledger
    prior = ledger["issued_targets"].get(snapshot["snapshot_id"])
    snapshot_hash = digest(snapshot)
    if prior is not None:
        if prior["snapshot_sha256"] != snapshot_hash:
            raise FusionError("SNAPSHOT_ID_CONFLICT")
        return deepcopy(prior["batch"]), ledger
    faults = []
    if ledger["last_sequence"] is not None and snapshot["sequence"] != ledger["last_sequence"] + 1:
        faults.append("SEQUENCE_GAP")
        batch = _issue(config, snapshot, ledger, identity, gate, _faults(config, snapshot, ledger, now_ms, faults))
        _remember(ledger, snapshot, batch, advance_sequence=False)
        return batch, ledger
    mismatches = fact_diff(ledger, snapshot)
    if mismatches:
        faults.extend(mismatches)
        batch = _issue(config, snapshot, ledger, identity, gate, _faults(config, snapshot, ledger, now_ms, faults))
        _remember(ledger, snapshot, batch, advance_sequence=False)
        return batch, ledger
    _apply_flows(ledger, snapshot)
    set_marks(ledger, snapshot["universe"])
    _touch_imbalance(ledger, snapshot["decision_time_ms"], config["policy"]["max_leg_imbalance_ms"])
    faults = _faults(config, snapshot, ledger, now_ms, [])
    batch = _issue(config, snapshot, ledger, identity, gate, faults)
    _remember(ledger, snapshot, batch)
    return batch, ledger


def apply_receipt(ledger, receipt, config):
    validate_config(config)
    validate_receipt(receipt)
    if ledger is None:
        raise FusionError("LEDGER_REQUIRED")
    semantic = {key: value for key, value in receipt.items() if key != "received_at_ms"}
    receipt_hash = digest(semantic)
    receipt_id = receipt["receipt_id"]
    if receipt_id in ledger["receipt_hashes"]:
        if ledger["receipt_hashes"][receipt_id] != receipt_hash:
            _quarantine(ledger, receipt, "RECEIPT_ID_CONFLICT")
        return ledger
    if receipt_id in ledger["rejected_hashes"]:
        if ledger["rejected_hashes"][receipt_id] != receipt_hash:
            _quarantine(ledger, receipt, "RECEIPT_ID_CONFLICT")
        return ledger
    if any(item["receipt_id"] == receipt_id for item in ledger["pending_receipts"]):
        return ledger
    expected = ledger["next_sequence"].get(receipt["stream_id"], 1)
    if receipt["sequence"] < expected:
        _quarantine(ledger, receipt, "SEQUENCE_REGRESSION")
        return ledger
    if receipt["sequence"] > expected:
        ledger["pending_receipts"].append(receipt)
        return ledger
    _commit_or_quarantine(ledger, receipt, config, receipt_hash)
    _drain(ledger, config)
    return ledger


def fold(config, events, root=None, now_ms=None):
    ledger = None
    batches = []
    for event in events:
        kind = event.get("kind")
        if kind == "snapshot":
            batch, ledger = cycle(config, event["snapshot"], ledger, root, now_ms)
            batches.append(json.loads(canonical(batch)))
        elif kind == "receipts":
            for receipt in event["receipts"]:
                ledger = apply_receipt(ledger, receipt, config)
        else:
            raise FusionError("UNKNOWN_FOLD_EVENT", str(kind))
    return batches, ledger


def _apply_flows(ledger, snapshot):
    for flow in snapshot["capital_flows"]:
        apply_capital_flow(ledger, flow["flow_id"], flow["account"], flow["kind"], flow["amount"])


def _remember(ledger, snapshot, batch, advance_sequence=True):
    ledger["issued_targets"][snapshot["snapshot_id"]] = {
        "snapshot_sha256": digest(snapshot),
        "batch": deepcopy(batch),
    }
    ledger["last_snapshot_id"] = snapshot["snapshot_id"]
    if snapshot["snapshot_id"] not in ledger["applied_snapshot_ids"]:
        ledger["applied_snapshot_ids"].append(snapshot["snapshot_id"])
    if advance_sequence:
        ledger["last_sequence"] = snapshot["sequence"]
    ledger["ledger_version"] += 1
    batch["ledger_version"] = ledger["ledger_version"]
    stored = ledger["issued_targets"][snapshot["snapshot_id"]]
    stored["batch"] = deepcopy(batch)


def _faults(config, snapshot, ledger, now_ms, extra):
    policy = config["policy"]
    faults = list(extra)
    decision = snapshot["decision_time_ms"]
    if snapshot["frozen"]:
        faults.append("FROZEN_SNAPSHOT")
    if decision > snapshot["valid_until_ms"]:
        faults.append("EXPIRED_SNAPSHOT")
    if policy["enforce_wall_clock"]:
        if now_ms is None:
            raise FusionError("NOW_MS_REQUIRED")
        if now_ms < decision:
            faults.append("FUTURE_DECISION")
        if now_ms - snapshot["received_at_ms"] > policy["snapshot_max_age_ms"]:
            faults.append("STALE_SNAPSHOT")
    if policy["kill_switch"] or "KILL_SWITCH" in snapshot["risk_flags"]:
        faults.append("KILL_SWITCH")
    for flag in snapshot["risk_flags"]:
        if flag in {"RECONCILE_STALL", "EXCHANGE_HALT", "DEPEG", "MMR_BREACH", "CANCEL_STORM", "COIN_RECALL"}:
            faults.append(flag)
    if policy["exchange_halt_stops_new"] and any(row["trading_status"] == "HALT" for row in snapshot["universe"]):
        faults.append("EXCHANGE_HALT")
    for since in ledger["imbalance_since_ms"].values():
        if decision - since > policy["max_leg_imbalance_ms"]:
            faults.append("LEG_IMBALANCE_TIMEOUT")
            break
    if any(row["recall_status"] in {"RECEIVED", "IN_PROGRESS"} and money(row["qty_token"]) > 0 for row in ledger["liabilities"]):
        faults.append("COIN_RECALL")
    return sorted(set(faults))


def _touch_imbalance(ledger, decision_time, max_ms):
    active = set()
    for row in list(ledger["positions"]):
        key = row["book"] + ":" + row["asset"]
        if _healthy(ledger, row):
            ledger["imbalance_since_ms"].pop(key, None)
        else:
            active.add(key)
            ledger["imbalance_since_ms"].setdefault(key, decision_time)
    for row in ledger["liabilities"]:
        if money(row["qty_token"]) == 0:
            continue
        key = "NEGATIVE_FUNDING:" + row["asset"]
        spot = position_qty(ledger, "NEGATIVE_FUNDING", row["asset"], "spot_qty_token")
        perp = position_qty(ledger, "NEGATIVE_FUNDING", row["asset"], "perp_qty_token")
        if spot - money(row["qty_token"]) + perp == 0:
            continue
        active.add(key)
        ledger["imbalance_since_ms"].setdefault(key, decision_time)
    for key in list(ledger["imbalance_since_ms"]):
        if key not in active:
            ledger["imbalance_since_ms"].pop(key, None)
    return max_ms


def _healthy(ledger, row):
    spot = money(row["spot_qty_token"])
    perp = money(row["perp_qty_token"])
    if row["book"] == "POSITIVE_CARRY":
        return perp == -spot
    liability = liability_qty(ledger, row["asset"])
    return spot - liability + perp == 0


def _issue(config, snapshot, ledger, identity, gate, faults):
    planning = config
    hard_hold = {"CASH_MISMATCH", "POSITION_MISMATCH", "LIABILITY_MISMATCH", "OPEN_ORDER_MISMATCH", "YIELD_MISMATCH", "SEQUENCE_GAP"}
    if any(item in hard_hold for item in faults):
        planning = deepcopy(config)
        planning["policy"] = dict(config["policy"])
        planning["policy"]["on_fault"] = "STOP_NEW"
    allocation = plan(planning, identity, ledger, snapshot, gate, faults)
    config_sha = digest(config)
    issued_version = ledger["ledger_version"] + 1
    targets = []
    for proposal in allocation["proposals"]:
        targets.append(_target(config, snapshot, ledger, proposal, config_sha, issued_version))
    targets.sort(key=lambda row: (row["priority"], row["book"], row["asset"]))
    intents = sorted(allocation["intents"], key=lambda row: canonical(row))
    reasons = list(allocation["reason_codes"])
    if gate["baseline_strategy_id"] != BASELINE_STRATEGY_ID:
        reasons.append("BASELINE_IDENTITY_UNEXPECTED")
    batch = {
        "schema_version": TARGET_SCHEMA,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "package_version": PACKAGE_VERSION,
        "mode": config["mode"],
        "submission_allowed": False,
        "research_only": True,
        "production_accepted": False,
        "replaces_rc2": False,
        "this_output_is_rc2": False,
        "handoff": "EXECUTION_TEAM",
        "execution_actions": [],
        "targets_are_not_fills": True,
        "return_evidence": False,
        "pnl_is_not_evidence": True,
        "synthetic": snapshot["synthetic"],
        "source_mode": snapshot["source_mode"],
        "fixture_label": snapshot["fixture_label"],
        "parent_snapshot_id": snapshot["snapshot_id"],
        "as_of_ms": snapshot["decision_time_ms"],
        "valid_until_ms": min(
            snapshot["decision_time_ms"] + config["policy"]["target_ttl_ms"],
            snapshot["valid_until_ms"],
        ),
        "ledger_version": issued_version,
        "config_sha256": config_sha,
        "baseline": {
            "commit": BASELINE_COMMIT,
            "strategy_id": gate["baseline_strategy_id"],
            "protocol": gate["baseline_protocol"],
            "stage": gate["baseline_stage"],
            "submission_allowed": False,
            "negative_gate_source": gate["source_file"],
            "negative_gate_engine_invoked": False,
            "entry_rate_8h_strict_below": dec_str(gate["entry_rate_8h_strict_below"]),
            "entry_basis_max": dec_str(gate["entry_basis_max"]),
            "borrow_interest_rate": "0",
        },
        "capital_identity": identity,
        "allocation": {
            "status": allocation["status"],
            "new_exposure_permitted": allocation["new_exposure_permitted"],
            "new_exposure_proposed": allocation["new_exposure_proposed"],
            "reason_codes": sorted(set(reasons)),
            "budgets": allocation["budgets"],
            "breakeven": allocation["breakeven"],
            "capacity_claim": allocation["capacity_claim"],
            "queue_modeled": False,
        },
        "targets": targets,
        "intents": intents,
        "books": explain(ledger),
        "invariants": structural_invariants(ledger),
    }
    return json.loads(canonical(batch))


def _target(config, snapshot, ledger, proposal, config_sha, ledger_version):
    book = proposal["book"]
    asset = proposal["asset"]
    gap_spot, gap_perp, gap_liability = _gaps(ledger, proposal)
    orders = live_orders(ledger, book, asset)
    replace_state = "NONE"
    accept_new_orders = True
    if orders and (gap_spot != 0 or gap_perp != 0):
        replace_state = "WAIT_CANCEL_ACK"
        accept_new_orders = False
    universe = next((row for row in snapshot["universe"] if row["asset"] == asset), None)
    multiplier = money(universe["tokens_per_perp_quantity"]) if universe else money("1")
    desired_perp = money(proposal["desired_perp_qty_token"])
    body = {
        "strategy_version": STRATEGY_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "config_sha256": config_sha,
        "book": book,
        "asset": asset,
        "desired_spot_qty_token": proposal["desired_spot_qty_token"],
        "desired_perp_qty_token": proposal["desired_perp_qty_token"],
        "desired_liability_qty_token": proposal["desired_liability_qty_token"],
    }
    key = digest(body)
    return {
        "target_id": "fa-" + key[:32],
        "idempotency_key": key,
        "parent_snapshot_id": snapshot["snapshot_id"],
        "ledger_version": ledger_version,
        "strategy_version": STRATEGY_VERSION,
        "as_of_ms": snapshot["decision_time_ms"],
        "valid_until_ms": min(snapshot["decision_time_ms"] + config["policy"]["target_ttl_ms"], snapshot["valid_until_ms"]),
        "book": book,
        "asset": asset,
        "qty_unit": "TOKEN",
        "tokens_per_perp_quantity": dec_str(multiplier),
        "desired_spot_qty_token": proposal["desired_spot_qty_token"],
        "desired_perp_qty_token": proposal["desired_perp_qty_token"],
        "desired_perp_qty_exchange": dec_str(desired_perp / multiplier),
        "desired_liability_qty_token": proposal["desired_liability_qty_token"],
        "current_spot_qty_token": proposal["current_spot_qty_token"],
        "current_perp_qty_token": proposal["current_perp_qty_token"],
        "current_liability_qty_token": proposal["current_liability_qty_token"],
        "max_notional_delta_usdt": proposal["planned_new_cash_usdt"],
        "risk_budget_usdt": proposal["planned_new_cash_usdt"],
        "planned_new_cash_usdt": proposal["planned_new_cash_usdt"],
        "priority": proposal["priority"],
        "reason_codes": proposal["reason_codes"],
        "replace_state": replace_state,
        "accept_new_orders": accept_new_orders,
        "reservation_released": False,
        "execution_gap": {
            "spot_qty_token": dec_str(gap_spot),
            "perp_qty_token": dec_str(gap_perp),
            "liability_qty_token": dec_str(gap_liability),
        },
        "submission_allowed": False,
        "is_fill": False,
        "liability_remains_until_return_receipt": gap_liability != 0 or proposal["desired_liability_qty_token"] != proposal["current_liability_qty_token"],
        "capacity_mode": proposal["capacity_mode"],
        "capacity_claim": proposal["capacity_claim"],
    }


def _gaps(ledger, proposal):
    book = proposal["book"]
    asset = proposal["asset"]
    buy_spot, sell_spot = signed_open_qty(ledger, book, asset, "SPOT")
    buy_perp, sell_perp = signed_open_qty(ledger, book, asset, "PERP")
    gap_spot = money(proposal["desired_spot_qty_token"]) - position_qty(ledger, book, asset, "spot_qty_token") - buy_spot + sell_spot
    gap_perp = money(proposal["desired_perp_qty_token"]) - position_qty(ledger, book, asset, "perp_qty_token") - buy_perp + sell_perp
    current_liability = liability_qty(ledger, asset) if book == "NEGATIVE_FUNDING" else money("0")
    gap_liability = money(proposal["desired_liability_qty_token"]) - current_liability
    return gap_spot, gap_perp, gap_liability


def _commit_or_quarantine(ledger, receipt, config, receipt_hash):
    trial = deepcopy(ledger)
    try:
        margin = money(config["policy"]["initial_margin_fraction"])
        for event in receipt["events"]:
            _apply_event(trial, event, receipt, config, margin)
    except FusionError as exc:
        _quarantine(ledger, receipt, exc.code)
        return
    trial["receipt_hashes"][receipt["receipt_id"]] = receipt_hash
    trial["next_sequence"][receipt["stream_id"]] = receipt["sequence"] + 1
    trial["ledger_version"] += 1
    # Keep quarantines and pending that already existed; the trial was copied before this commit.
    ledger.clear()
    ledger.update(trial)


def _drain(ledger, config):
    while True:
        expected_by_stream = {}
        chosen = None
        rest = []
        for receipt in ledger["pending_receipts"]:
            expected = expected_by_stream.setdefault(receipt["stream_id"], ledger["next_sequence"].get(receipt["stream_id"], 1))
            if receipt["sequence"] == expected and chosen is None:
                chosen = receipt
            elif receipt["sequence"] < expected:
                _quarantine(ledger, receipt, "SEQUENCE_REGRESSION")
            else:
                rest.append(receipt)
        ledger["pending_receipts"] = rest
        if chosen is None:
            return
        semantic = {key: value for key, value in chosen.items() if key != "received_at_ms"}
        _commit_or_quarantine(ledger, chosen, config, digest(semantic))


def _apply_event(ledger, event, receipt, config, margin):
    kind = event["type"]
    if kind == "FILL":
        apply_fill(ledger, event, margin)
    elif kind == "ORDER_ACK":
        ack_order(ledger, event, margin)
    elif kind == "CANCEL_REQUESTED":
        request_cancel(ledger, event["order_id"])
    elif kind == "CANCEL_ACK":
        ack_cancel(ledger, event["order_id"])
    elif kind == "REJECT":
        note_reject(ledger, event, config["policy"]["max_retries"])
    elif kind == "FUNDING":
        book_funding(
            ledger,
            event["funding_id"],
            event["book"],
            event["asset"],
            money(event["qty_token"]),
            money(event["amount_usdt"]),
            event["window_end_ms"],
            receipt["event_time_ms"],
        )
    elif kind == "BORROW":
        apply_borrow(
            ledger,
            event["lot_id"],
            event["asset"],
            money(event["qty_token"]),
            money(event["mark_usdt"]),
            event["owner"],
            event["recall_deadline_ms"],
        )
    elif kind == "RETURN_CONFIRMED":
        apply_return(ledger, event["lot_id"], money(event["qty_token"]))
    elif kind == "YIELD":
        apply_yield(ledger, event)
    elif kind == "CAPITAL_FLOW":
        apply_capital_flow(ledger, event["flow_id"], event["account"], event["kind"], event["amount"])
    elif kind == "TRANSFER":
        apply_transfer(ledger, event, config["policy"])
    elif kind == "INTEREST":
        note_interest(ledger, event)
    else:
        raise FusionError("UNKNOWN_EVENT", kind)


def _quarantine(ledger, receipt, code):
    receipt_id = receipt.get("receipt_id")
    if receipt_id and receipt_id not in ledger["rejected_hashes"]:
        semantic = {key: value for key, value in receipt.items() if key != "received_at_ms"}
        try:
            ledger["rejected_hashes"][receipt_id] = digest(semantic)
        except (TypeError, ValueError):
            ledger["rejected_hashes"][receipt_id] = code
    ledger["quarantine"].append(
        {
            "receipt_id": receipt_id,
            "sequence": receipt.get("sequence"),
            "reason": code,
        }
    )
    if code not in ledger["flags"]:
        ledger["flags"].append(code)
    ledger["ledger_version"] += 1


def check_ledger(ledger):
    report = structural_invariants(ledger)
    report["echo"] = echo_facts(ledger)
    return report
