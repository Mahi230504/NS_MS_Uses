"""Unit tests for the per-app grounding profile registry."""
from __future__ import annotations

from agent.profiles import (
    COMMERCE,
    GENERIC,
    all_profiles,
    resolve_profile,
)


class TestResolveProfile:
    def test_none_resolves_generic(self) -> None:
        assert resolve_profile(None) is GENERIC

    def test_empty_string_resolves_generic(self) -> None:
        assert resolve_profile("") is GENERIC

    def test_unknown_package_resolves_generic(self) -> None:
        # A never-seen app must NOT inherit shopping heuristics.
        assert resolve_profile("com.whatsapp") is GENERIC
        assert resolve_profile("com.spotify.music") is GENERIC
        assert resolve_profile("com.google.android.apps.maps") is GENERIC

    def test_commerce_family_resolves_commerce(self) -> None:
        for pkg in (
            "com.grofers.customerapp",       # Blinkit
            "com.zeptoconsumerapp",          # Zepto
            "in.swiggy.android",             # Swiggy / Instamart
            "com.application.zomato",        # Zomato
            "com.Dominos",                   # Domino's
            "in.amazon.mShop.android.shopping",  # Amazon
            "com.flipkart.android",          # Flipkart
            "com.myntra.android",            # Myntra
            "com.meesho.supply",             # Meesho
        ):
            assert resolve_profile(pkg) is COMMERCE, pkg


class TestProfileShape:
    def test_generic_does_not_annotate(self) -> None:
        # The whole efficiency win hinges on this: GENERIC carries no
        # vocabulary, so the parser/renderer skip annotation work for it.
        assert GENERIC.annotates is False

    def test_commerce_annotates(self) -> None:
        assert COMMERCE.annotates is True

    def test_generic_vocab_is_empty(self) -> None:
        assert COMMERCE.action_text_tokens  # sanity: commerce has tokens
        assert not GENERIC.action_text_tokens
        assert not GENERIC.cart_id_tokens
        assert not GENERIC.category_text_patterns
        assert not GENERIC.location_tokens
        assert GENERIC.top_header_y_max == 0
        assert GENERIC.available_for_re is None

    def test_all_profiles_includes_generic_and_commerce(self) -> None:
        profs = all_profiles()
        assert GENERIC in profs
        assert COMMERCE in profs

    def test_no_duplicate_package_mappings(self) -> None:
        # Each package maps to exactly one profile.
        seen: set[str] = set()
        for prof in all_profiles():
            for pkg in prof.packages:
                assert pkg not in seen, f"{pkg} mapped twice"
                seen.add(pkg)
