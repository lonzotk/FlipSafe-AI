import unittest

from backend.tools import WorkflowStop, comparison_tool, pricing_tool


class PricingToolTests(unittest.TestCase):
    def test_supported_category_uses_relevant_comparisons(self):
        result = pricing_tool("Monitor", "Good", "Viewline 27 QHD", "1440p 144Hz")
        self.assertGreater(result["evidence_count"], 0)
        self.assertLess(result["quick_sale"], result["fair_market"])
        self.assertLess(result["fair_market"], result["listing_price"])
        self.assertLess(result["minimum_acceptable_offer"], result["fair_market"])

    def test_unknown_condition_stops(self):
        with self.assertRaises(WorkflowStop):
            pricing_tool("SSD", "Unknown / unverified", "Northstar NVMe 1TB", "1TB")

    def test_unsupported_category_stops(self):
        with self.assertRaises(WorkflowStop):
            comparison_tool("Camera")


if __name__ == "__main__":
    unittest.main()
