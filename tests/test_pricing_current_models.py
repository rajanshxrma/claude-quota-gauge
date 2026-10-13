"""0.26.0: the four current models are priced by their own list prices, and a
cache read costs what the pricing page says for them, not a tenth of input."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import importlib.util
_spec = importlib.util.spec_from_file_location("tokens_since", os.path.join(os.path.dirname(__file__), "..", "bin", "tokens-since.py"))
tokens_since = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tokens_since)


class PricingTests(unittest.TestCase):
    def test_current_models_have_their_own_rows(self):
        for model, (i, o) in {"claude-fable-5-1": (10.0, 50.0), "claude-opus-5-5": (4.0, 20.0),
                              "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}.items():
            self.assertEqual(tokens_since.PRICING[model], (i, o), model)

    def test_cache_read_uses_the_listed_price(self):
        self.assertAlmostEqual(tokens_since.cache_read_price("claude-fable-5-1"), 0.25)
        self.assertAlmostEqual(tokens_since.cache_read_price("claude-opus-5-5"), 0.20)
        self.assertAlmostEqual(tokens_since.cache_read_price("claude-sonnet-5-5"), 0.10)
        # a model with no listed cache price keeps a tenth of its input price
        self.assertAlmostEqual(tokens_since.cache_read_price("claude-haiku-5-5"), 0.01)
        self.assertAlmostEqual(tokens_since.cache_read_price("claude-opus-4-8"), 0.50)

    def test_cost_of_one_million_cache_reads_on_fable(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1_000_000}
        self.assertAlmostEqual(tokens_since.cost("claude-fable-5-1", usage), 0.25)
        self.assertAlmostEqual(tokens_since.cost("claude-opus-4-8", usage), 0.50)

    def test_unknown_model_falls_back_to_sonnet_5(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        self.assertAlmostEqual(tokens_since.cost("claude-future-9", usage), 2.0)


if __name__ == "__main__":
    unittest.main()
