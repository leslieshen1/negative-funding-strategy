"""Read-only view of the frozen RC2 negative-funding entry thresholds.

The numbers are loaded from CURRENT_RELEASE.json. This module does not write that
file and does not call the frozen decision engine.
"""

import json
from decimal import Decimal
from pathlib import Path

from fusion_a.decimal_util import FusionError, money

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def load_frozen_negative_gate(root=None):
    path = (PACKAGE_ROOT if root is None else Path(root)) / "CURRENT_RELEASE.json"
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FusionError("FROZEN_PROFILE_UNREADABLE", str(exc)) from exc
    if document.get("submission_allowed") is not False:
        raise FusionError("BASELINE_SUBMISSION_FLAG_UNEXPECTED")
    profile = document.get("frozen_profile") or {}
    try:
        rate = money(profile["entry_rate_r8_strict_below"])
        basis = money(profile["entry_basis_max"])
        borrow_interest = money(profile["borrow_interest_rate"])
    except (KeyError, FusionError) as exc:
        raise FusionError("FROZEN_PROFILE_UNREADABLE", str(exc)) from exc
    if borrow_interest != 0:
        raise FusionError("FROZEN_BORROW_INTEREST_NOT_ZERO")
    return {
        "baseline_strategy_id": document["strategy_id"],
        "baseline_protocol": document["protocol_version"],
        "baseline_stage": document["stage"],
        "baseline_submission_allowed": False,
        "entry_rate_8h_strict_below": rate,
        "entry_basis_max": basis,
        "borrow_interest_rate": borrow_interest,
        "source_file": "CURRENT_RELEASE.json",
        "engine_invoked": False,
    }


def eight_hour_equivalent(rate, interval_hours):
    if not isinstance(interval_hours, int) or isinstance(interval_hours, bool) or interval_hours <= 0:
        raise FusionError("INVALID_FUNDING_INTERVAL")
    return rate * (Decimal(8) / Decimal(interval_hours))


def negative_signal_eligible(rate, interval_hours, basis, gate):
    """Frozen rule: 8h-equivalent rate strictly below the threshold and basis at or under the cap."""
    equivalent = eight_hour_equivalent(rate, interval_hours)
    eligible = equivalent < gate["entry_rate_8h_strict_below"] and basis <= gate["entry_basis_max"]
    return eligible, equivalent
