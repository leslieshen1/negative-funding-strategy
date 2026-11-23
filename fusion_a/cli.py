"""Local JSON cycle for the dry-run fusion configurator. No network and no order API."""

import argparse
import json
import sys

from fusion_a.decimal_util import FusionError
from fusion_a.engine import apply_receipt, canonical, check_ledger, configure, cycle, fold
from fusion_a.validate import validate_config


def _load(path):
    def reject_float(value):
        raise FusionError("JSON_FLOAT_NOT_ALLOWED", value)

    with open(path, encoding="utf-8") as handle:
        return json.load(handle, parse_float=reject_float)


def _emit(payload, code=0):
    print(canonical(payload) if not isinstance(payload, str) else payload)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run fusion A configurator")
    parser.add_argument("command", choices=("configure", "cycle", "apply", "check", "fold"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--snapshot")
    parser.add_argument("--ledger")
    parser.add_argument("--receipt")
    parser.add_argument("--events")
    parser.add_argument("--now-ms", type=int)
    args = parser.parse_args(argv)
    try:
        config = _load(args.config)
        validate_config(config)
        if args.command == "configure":
            return _emit(configure(config))
        if args.command == "cycle":
            snapshot = _load(args.snapshot)
            ledger = _load(args.ledger) if args.ledger else None
            batch, updated = cycle(config, snapshot, ledger, now_ms=args.now_ms)
            return _emit({"batch": batch, "ledger": updated})
        if args.command == "apply":
            ledger = _load(args.ledger)
            receipt = _load(args.receipt)
            updated = apply_receipt(ledger, receipt, config)
            return _emit(updated)
        if args.command == "check":
            return _emit(check_ledger(_load(args.ledger)))
        events = _load(args.events)
        batches, ledger = fold(config, events, now_ms=args.now_ms)
        return _emit({"batches": batches, "ledger": ledger})
    except (FusionError, KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
        code = getattr(exc, "code", "INVALID_INPUT")
        print(canonical({
            "schema_version": "nfb.fusion-a.error.v1",
            "status": "INVALID_INPUT",
            "submission_allowed": False,
            "error": code,
            "detail": str(exc),
        }))
        return 2


if __name__ == "__main__":
    sys.exit(main())
