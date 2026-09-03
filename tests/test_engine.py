from decimal import Decimal
import unittest

from engine import analyze, floor_step, funding_annual_pct, plan_reconcile, walk_book


def snapshot(**updates):
    value = {
        "spotBid": "99.9",
        "spotAsk": "100.1",
        "futuresBid": "99.95",
        "futuresAsk": "100.05",
        "futuresBids": [["99.95", "10"], ["99.90", "20"]],
        "futuresAsks": [["100.05", "10"]],
        "markPrice": "100",
        "lastFundingRate": "0.0001",
        "fundingIntervalHours": 8,
        "nextFundingTime": 1,
        "spotQuoteVolume24h": "100000000",
        "futuresQuoteVolume24h": "200000000",
        "rules": {"stepSize": "0.001", "minQty": "0.001", "minNotional": "5"},
        "source": {"provider": "Binance Agent OS"},
        "fetchedAt": "2026-09-03T00:00:00Z",
    }
    value.update(updates)
    return value


def intent(**updates):
    value = {
        "symbol": "BTCUSDT",
        "exposureQuantity": "1",
        "hedgeRatioPct": "100",
        "maxNotionalUsdt": "1000",
        "maxSlippageBps": "10",
        "maxBasisBps": "100",
        "maxFundingCostAnnualPct": "20",
        "minQuoteVolumeUsdt": "10000000",
    }
    value.update(updates)
    return value


class EngineTests(unittest.TestCase):
    def test_funding_rate_is_annualized_for_interval(self):
        self.assertEqual(funding_annual_pct("0.0001", 8), Decimal("10.9500"))

    def test_quantity_is_floored_to_exchange_step(self):
        self.assertEqual(floor_step(Decimal("1.2349"), Decimal("0.001")), Decimal("1.234"))

    def test_sell_walks_multiple_levels(self):
        result = walk_book([["100", "1"], ["99", "2"]], "2", "SELL")
        self.assertTrue(result["filled"])
        self.assertEqual(result["averagePrice"], "99.5")
        self.assertEqual(result["slippageBps"], "50")

    def test_liquid_market_with_positive_funding_passes(self):
        report = analyze(snapshot(), intent())
        self.assertEqual(report["decision"], "PASS")
        self.assertEqual(report["exposure"]["targetShortQuantity"], "1")
        self.assertEqual(report["source"]["provider"], "Binance Agent OS")

    def test_expensive_negative_funding_warns(self):
        report = analyze(snapshot(lastFundingRate="-0.001"), intent())
        self.assertEqual(report["decision"], "WARN")
        self.assertIn("fundingCost", [row["id"] for row in report["failedControls"]])

    def test_notional_limit_blocks(self):
        report = analyze(snapshot(), intent(exposureQuantity="20"))
        self.assertEqual(report["decision"], "BLOCK")
        self.assertIn("positionLimit", [row["id"] for row in report["failedControls"]])

    def test_insufficient_orderbook_blocks(self):
        report = analyze(snapshot(futuresBids=[["99.95", "0.1"]]), intent())
        self.assertEqual(report["decision"], "BLOCK")
        self.assertIn("orderbookFill", [row["id"] for row in report["failedControls"]])

    def test_plan_opens_and_increases_short(self):
        plan = plan_reconcile("-0.2", "1", "0.001", "0.001")
        self.assertEqual((plan["side"], plan["quantity"], plan["reduceOnly"]), ("SELL", "0.8", False))

    def test_plan_reduces_short_without_crossing_zero(self):
        plan = plan_reconcile("-1.5", "1", "0.001", "0.001")
        self.assertEqual((plan["side"], plan["quantity"], plan["reduceOnly"]), ("BUY", "0.5", True))

    def test_existing_long_is_blocked(self):
        self.assertEqual(plan_reconcile("0.1", "1", "0.001", "0.001")["action"], "BLOCK")

    def test_exact_target_requires_no_order(self):
        self.assertEqual(plan_reconcile("-1", "1", "0.001", "0.001")["action"], "NONE")

    def test_zero_spot_exposure_can_close_existing_short(self):
        report = analyze(snapshot(), intent(exposureQuantity="0"))
        self.assertEqual(report["decision"], "PASS")
        self.assertEqual(report["exposure"]["targetShortQuantity"], "0")
        plan = plan_reconcile("-1", report["exposure"]["targetShortQuantity"], "0.001", "0.001")
        self.assertEqual((plan["side"], plan["quantity"], plan["reduceOnly"]), ("BUY", "1", True))


if __name__ == "__main__":
    unittest.main()
