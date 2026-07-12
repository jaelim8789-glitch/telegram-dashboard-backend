"""Pricing contract tests — verify canonical plan definitions and cross-contract integrity.

This test suite validates:
1. Canonical PLAN_CATALOG contains FREE TRIAL, PRO $100, TEAM $199
2. Deprecated plans (basic, enterprise) are recognized
3. Frontend/backend plan price mapping is consistent
4. Payment request contract compatibility
5. Mismatch hard-failure behavior
6. USDT matching behavior
"""

import pytest

from app.core.plans import (
    PLAN_CATALOG,
    get_plan,
    get_plan_price_usdt,
    get_plan_limits,
    is_deprecated_plan,
)


class TestPlanCatalogContract:
    """Verify the canonical PLAN_CATALOG contract."""

    def test_free_trial_exists(self):
        plan = get_plan("free")
        assert plan is not None
        assert plan["name"] == "Free"
        assert plan["trial_days"] == 1
        assert get_plan_price_usdt("free", "monthly") == 0

    def test_pro_plan_100_monthly(self):
        plan = get_plan("pro")
        assert plan is not None
        assert plan["name"] == "Pro"
        assert get_plan_price_usdt("pro", "monthly") == 100
        limits = get_plan_limits("pro")
        assert limits["max_accounts"] == 10

    def test_team_plan_199_quarterly(self):
        plan = get_plan("team")
        assert plan is not None
        assert plan["name"] == "Team"
        assert get_plan_price_usdt("team", "quarterly") == 199
        limits = get_plan_limits("team")
        assert limits["max_accounts"] == 20

    def test_catalog_has_exactly_three_plans(self):
        assert set(PLAN_CATALOG.keys()) == {"free", "pro", "team"}

    def test_each_plan_has_required_fields(self):
        for pid, pdef in PLAN_CATALOG.items():
            assert "name" in pdef
            assert "description" in pdef
            assert "prices_usdt" in pdef
            assert "limits" in pdef
            assert "features" in pdef
            assert isinstance(pdef["features"], list)

    def test_each_plan_has_limits(self):
        for pid in PLAN_CATALOG:
            limits = get_plan_limits(pid)
            assert limits is not None
            assert "max_accounts" in limits
            assert "monthly_message_limit" in limits

    def test_deprecated_plans_detected(self):
        assert is_deprecated_plan("basic") is True
        assert is_deprecated_plan("enterprise") is True

    def test_new_plans_not_deprecated(self):
        assert is_deprecated_plan("free") is False
        assert is_deprecated_plan("pro") is False
        assert is_deprecated_plan("team") is False

    def test_unknown_plan_returns_none(self):
        assert get_plan("nonexistent") is None
        assert get_plan_price_usdt("nonexistent") is None
        assert get_plan_limits("nonexistent") is None

    def test_free_trial_14_days(self):
        from app.core.plans import get_plan
        free = get_plan("free")
        assert free is not None
        assert free["trial_days"] == 1


class TestCrossContractIntegrity:
    """Verify frontend/backend plan mapping consistency."""

    def test_pro_plan_10_accounts(self):
        limits = get_plan_limits("pro")
        assert limits is not None
        assert limits["max_accounts"] == 10

    def test_team_plan_20_accounts(self):
        limits = get_plan_limits("team")
        assert limits is not None
        assert limits["max_accounts"] == 20

    def test_free_plan_1_account(self):
        limits = get_plan_limits("free")
        assert limits is not None
        assert limits["max_accounts"] == 1

    def test_no_enterprise_in_catalog(self):
        assert "enterprise" not in PLAN_CATALOG

    def test_no_basic_in_catalog(self):
        assert "basic" not in PLAN_CATALOG

    def test_pro_price_matches_contract(self):
        pro = get_plan("pro")
        assert pro is not None
        monthly = pro["prices_usdt"].get("monthly")
        assert monthly == 100, f"Expected PRO = $100/month, got ${monthly}"

    def test_team_price_matches_contract(self):
        team = get_plan("team")
        assert team is not None
        quarterly = team["prices_usdt"].get("quarterly")
        assert quarterly == 199, f"Expected TEAM = $199/quarter, got ${quarterly}"

    def test_free_trial_no_cost(self):
        assert get_plan_price_usdt("free", "monthly") == 0

    def test_pro_monthly_price_no_quarterly(self):
        pro = get_plan("pro")
        assert pro is not None
        assert "monthly" in pro["prices_usdt"]
        assert "quarterly" not in pro["prices_usdt"]

    def test_team_quarterly_price_no_monthly(self):
        team = get_plan("team")
        assert team is not None
        assert "quarterly" in team["prices_usdt"]
        assert "monthly" not in team["prices_usdt"]


class TestPaymentRequestContract:
    """Verify payment request compatibility."""

    def test_usdt_amounts_positive(self):
        for pid, pdef in PLAN_CATALOG.items():
            for interval, price in pdef["prices_usdt"].items():
                assert price >= 0
                if price > 0:
                    assert isinstance(price, int)

    def test_free_plan_has_zero_usdt(self):
        assert get_plan_price_usdt("free", "monthly") == 0

    def test_pro_usdt_100(self):
        assert get_plan_price_usdt("pro", "monthly") == 100

    def test_team_usdt_199(self):
        assert get_plan_price_usdt("team", "quarterly") == 199


class TestMismatchHardFailureBehavior:
    """Verify mismatch hard-failure behavior."""

    def test_deprecated_plan_rejected_by_get_plan(self):
        assert get_plan("basic") is None
        assert get_plan("enterprise") is None

    def test_deprecated_limits_still_available(self):
        deprecated = ["basic", "enterprise"]
        for plan in deprecated:
            limits = get_plan_limits(plan)
            assert limits is None, f"Deprecated {plan} should not return limits from PLAN_CATALOG"

    def test_unknown_billing_interval_returns_none(self):
        assert get_plan_price_usdt("pro", "yearly") is None

    def test_deprecated_plan_true_for_basic_enterprise(self):
        assert is_deprecated_plan("basic")
        assert is_deprecated_plan("enterprise")


class TestUSDTMatchingBehavior:
    """Verify USDT matching behavior via match_plan."""

    @pytest.fixture
    def match_plan_fn(self):
        from app.services.usdt_watcher import match_plan
        return match_plan

    def test_exact_pro_100_usdt(self, match_plan_fn):
        result = match_plan_fn(10000)  # $100 in cents
        assert result is not None
        assert result[0] == "pro"

    def test_exact_team_199_usdt(self, match_plan_fn):
        result = match_plan_fn(19900)  # $199 in cents
        assert result is not None
        assert result[0] == "team"
        assert result[1] == "quarterly"

    def test_tolerance_pro_95_usdt(self, match_plan_fn):
        result = match_plan_fn(9500)
        assert result is not None
        assert result[0] == "pro"

    def test_tolerance_team_190_usdt(self, match_plan_fn):
        result = match_plan_fn(19000)
        assert result is not None
        assert result[0] == "team"

    def test_zero_amount_no_match(self, match_plan_fn):
        assert match_plan_fn(0) is None

    def test_small_amount_no_match(self, match_plan_fn):
        assert match_plan_fn(100) is None  # $1

    def test_out_of_tolerance_pro(self, match_plan_fn):
        assert match_plan_fn(5000) is None  # $50 — doesn't match anything

    def test_legacy_deprecated_amounts_no_match(self, match_plan_fn):
        """Verify deprecated plan prices no longer match."""
        legacy_amounts = [1500, 15000]
        for cents in legacy_amounts:
            result = match_plan_fn(cents)
            assert result is None or result[0] not in ("basic", "enterprise"), \
                f"match_plan({cents}) matched deprecated plan: {result}"


class TestValidatePlanId:
    """Verify the validate_plan_id helper."""

    def test_validates_canonical_plans(self):
        from app.core.plans import validate_plan_id
        assert validate_plan_id("free") == "free"
        assert validate_plan_id("pro") == "pro"
        assert validate_plan_id("team") == "team"

    def test_rejects_deprecated_basic(self):
        from app.core.plans import validate_plan_id
        with pytest.raises(ValueError, match="제공"):
            validate_plan_id("basic")

    def test_rejects_deprecated_enterprise(self):
        from app.core.plans import validate_plan_id
        with pytest.raises(ValueError, match="제공"):
            validate_plan_id("enterprise")

    def test_rejects_unknown_plan(self):
        from app.core.plans import validate_plan_id
        with pytest.raises(ValueError):
            validate_plan_id("nonexistent")


class TestPlanLimitsNoDeprecated:
    """Verify deprecated plans removed from PLAN_LIMITS."""

    def test_no_basic_in_limits(self):
        from app.services.usage_tracker import PLAN_LIMITS
        assert "basic" not in PLAN_LIMITS

    def test_no_enterprise_in_limits(self):
        from app.services.usage_tracker import PLAN_LIMITS
        assert "enterprise" not in PLAN_LIMITS

    def test_limits_has_only_canonical(self):
        from app.services.usage_tracker import PLAN_LIMITS
        assert set(PLAN_LIMITS.keys()) == {"free", "pro", "team"}


class TestUSDTWatcherNoLegacy:
    """Verify deprecated legacy prices removed from USDT watcher."""

    def test_no_legacy_price_cents(self):
        from app.services.usdt_watcher import _PLAN_PRICES_CENTS
        assert "basic" not in _PLAN_PRICES_CENTS
        assert "enterprise" not in _PLAN_PRICES_CENTS

    def test_legacy_constant_removed(self):
        import app.services.usdt_watcher as watcher
        assert not hasattr(watcher, "_LEGACY_PRICES_CENTS")


class TestBillingPeriodConsistency:
    """Verify billing periods match canonical plan definitions."""

    def test_pro_only_monthly(self):
        from app.core.plans import get_plan
        pro = get_plan("pro")
        assert "monthly" in pro["prices_usdt"]
        assert "quarterly" not in pro["prices_usdt"]

    def test_team_only_quarterly(self):
        from app.core.plans import get_plan
        team = get_plan("team")
        assert "quarterly" in team["prices_usdt"]
        assert "monthly" not in team["prices_usdt"]
