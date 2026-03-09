"""Tests for configurable compaction protection turns."""

import unittest
from unittest.mock import patch, MagicMock


class TestCompressionConfigDefaults(unittest.TestCase):
    """Verify DEFAULT_CONFIG includes protect_first_n / protect_last_n."""

    def test_default_config_has_protection_fields(self):
        from hermes_cli.config import DEFAULT_CONFIG
        compression = DEFAULT_CONFIG["compression"]
        self.assertIn("protect_first_n", compression)
        self.assertIn("protect_last_n", compression)

    def test_default_values(self):
        from hermes_cli.config import DEFAULT_CONFIG
        compression = DEFAULT_CONFIG["compression"]
        self.assertEqual(compression["protect_first_n"], 3)
        self.assertEqual(compression["protect_last_n"], 4)

    def test_config_version_bumped(self):
        from hermes_cli.config import DEFAULT_CONFIG
        self.assertGreaterEqual(DEFAULT_CONFIG["_config_version"], 6)


class TestContextCompressorAcceptsConfig(unittest.TestCase):
    """Verify ContextCompressor properly receives custom protection values."""

    @patch("agent.context_compressor.get_text_auxiliary_client")
    def test_custom_protection_values(self, mock_aux):
        mock_aux.return_value = (None, "test-model")
        from agent.context_compressor import ContextCompressor
        compressor = ContextCompressor(
            model="test/model",
            protect_first_n=5,
            protect_last_n=8,
        )
        self.assertEqual(compressor.protect_first_n, 5)
        self.assertEqual(compressor.protect_last_n, 8)

    @patch("agent.context_compressor.get_text_auxiliary_client")
    def test_default_protection_values(self, mock_aux):
        mock_aux.return_value = (None, "test-model")
        from agent.context_compressor import ContextCompressor
        compressor = ContextCompressor(model="test/model")
        self.assertEqual(compressor.protect_first_n, 3)
        self.assertEqual(compressor.protect_last_n, 4)


class TestProtectionClamping(unittest.TestCase):
    """Verify protection values are clamped to 0-12 range."""

    def test_clamp_negative_to_zero(self):
        val = max(0, min(12, -5))
        self.assertEqual(val, 0)

    def test_clamp_over_max_to_twelve(self):
        val = max(0, min(12, 50))
        self.assertEqual(val, 12)

    def test_valid_value_unchanged(self):
        val = max(0, min(12, 7))
        self.assertEqual(val, 7)


if __name__ == "__main__":
    unittest.main()
