"""Dry-run general capital-scale A fusion configurator.

This package is a research integration candidate. It does not replace the frozen
RC2 runtime, does not submit orders, and does not certify returns.
"""

PACKAGE_VERSION = "0.1.0"
STRATEGY_ID = "nfb.fusion-a.research.20261123"
BASELINE_COMMIT = "6b98320bfb707c903212e26888cef79a1c12342f"
BASELINE_STRATEGY_ID = "nfb.strategy.20261108.rc2"

__all__ = [
    "PACKAGE_VERSION",
    "STRATEGY_ID",
    "BASELINE_COMMIT",
    "BASELINE_STRATEGY_ID",
]
