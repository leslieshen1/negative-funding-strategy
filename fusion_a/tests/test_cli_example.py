"""The committed example is synthetic and the CLI stays submission_allowed false."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "fusion_a" / "examples" / "synthetic"


class ExampleTests(unittest.TestCase):
    def test_cli_configure_and_cycle_on_the_labeled_fixture(self):
        for name in ("config.json", "snapshot.json", "partial_fill.receipt.json"):
            document = json.loads((EXAMPLE / name).read_text())
            self.assertTrue(document["synthetic"])
            self.assertIn("SYNTHETIC", document["fixture_label"])
        configure = subprocess.run(
            [sys.executable, "-B", "-m", "fusion_a.cli", "configure", "--config", str(EXAMPLE / "config.json")],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        configured = json.loads(configure.stdout)
        self.assertFalse(configured["submission_allowed"])
        self.assertEqual(configured["capital_identity"]["cash_budget_usdt"], "27500000")
        self.assertEqual(configured["capital_identity"]["borrowed_market_value_usdt"], "22500000")
        cycle = subprocess.run(
            [sys.executable, "-B", "-m", "fusion_a.cli", "cycle", "--config", str(EXAMPLE / "config.json"), "--snapshot", str(EXAMPLE / "snapshot.json")],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        result = json.loads(cycle.stdout)
        batch = result["batch"]
        self.assertFalse(batch["submission_allowed"])
        self.assertEqual(batch["execution_actions"], [])
        self.assertTrue(batch["pnl_is_not_evidence"])
        self.assertEqual(batch["targets"][0]["desired_spot_qty_token"], "50000")
        self.assertEqual(batch["targets"][0]["desired_perp_qty_token"], "-50000")
        self.assertFalse(batch["targets"][0]["is_fill"])
        ledger_path = EXAMPLE / "ignored-ledger.json"
        self.assertFalse(ledger_path.exists())
