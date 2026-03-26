"""Tests for keystore.categories — secret classification."""

import pytest

from keystore.categories import SecretCategory, default_category, DEFAULT_CATEGORIES


class TestSecretCategory:
    def test_enum_values(self):
        assert SecretCategory.INJECTABLE.value == "injectable"
        assert SecretCategory.GATED.value == "gated"
        assert SecretCategory.SEALED.value == "sealed"
        assert SecretCategory.USER_ONLY.value == "user_only"

    def test_string_enum(self):
        """SecretCategory is a str enum — can be compared as string."""
        assert SecretCategory.INJECTABLE == "injectable"
        assert SecretCategory.SEALED.value == "sealed"


class TestDefaultCategory:
    def test_known_injectable(self):
        assert default_category("OPENROUTER_API_KEY") == SecretCategory.INJECTABLE
        assert default_category("FAL_KEY") == SecretCategory.INJECTABLE
        assert default_category("TELEGRAM_BOT_TOKEN") == SecretCategory.INJECTABLE

    def test_known_gated(self):
        assert default_category("GITHUB_TOKEN") == SecretCategory.GATED

    def test_known_user_only(self):
        assert default_category("SUDO_PASSWORD") == SecretCategory.USER_ONLY

    def test_wallet_keys_always_sealed(self):
        assert default_category("wallet:eth:0xABC") == SecretCategory.SEALED
        assert default_category("wallet:sol:7xKL") == SecretCategory.SEALED
        assert default_category("wallet:meta:0xABC") == SecretCategory.SEALED

    def test_unknown_defaults_to_injectable(self):
        """Unknown keys default to injectable for backward compatibility."""
        assert default_category("SOME_RANDOM_KEY") == SecretCategory.INJECTABLE
        assert default_category("MY_CUSTOM_TOKEN") == SecretCategory.INJECTABLE

    def test_default_categories_dict_complete(self):
        """All entries in DEFAULT_CATEGORIES should be valid SecretCategory values."""
        for name, cat in DEFAULT_CATEGORIES.items():
            assert isinstance(cat, SecretCategory), f"{name} has invalid category: {cat}"
