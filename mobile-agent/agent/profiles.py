"""Per-app grounding profiles — the structured half of an app "skill".

A `skills/<package>.md` file carries the *prompt* guidance for an app (loaded
by `SkillRegistry`). An `AppProfile` carries the *structural* half: the
vocabulary the UI-tree renderer uses to tag elements ([ACTION] / [CART] /
[CATEGORY?] / [LOCATION]) and the toggles that arm the orchestrator's
shopping-flow validators. The two are paired by package name.

Why this exists
---------------
The shopping-flow grounding (add-to-cart buttons, category tiles, mini-cart
bars, delivery-location headers, "Cart review" handoffs) was originally
hardcoded as module globals in `ui_tree.py` / `orchestrator.py` and ran on
EVERY step of EVERY app — even WhatsApp, Maps, or Spotify, where it produces
no signal and can only mis-fire. But it isn't grocery-specific: the
search -> add -> cart -> checkout flow is identical across every commerce app
(Blinkit, Zepto, Swiggy/Instamart, Zomato, Domino's, Amazon, Flipkart,
Myntra, Meesho). So it's promoted to a single `COMMERCE` profile shared by
that family, while the ~dozen non-commerce apps resolve to `GENERIC` (empty
vocabulary, all shopping validators off) and pay nothing for it.

The universal grounding — coord-grounding, type-without-focus, loop and
stale-tree detection — is NOT here. It keys off structural signals (class,
focused, bounds, action repetition) that mean the same thing on any app, so
it stays always-on in the core.

Efficiency
----------
`GENERIC.annotates` is False, so the renderer and parser skip the annotation
work entirely on non-commerce apps. For commerce apps the vocabulary is
pre-built into frozensets / compiled regex once at import, and each element's
flags are computed a single time during `parse()` instead of being re-derived
on every property access by every reader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern

from config import prompts


@dataclass(frozen=True)
class AppProfile:
    """Vocabulary + toggles for grounding one family of apps.

    All collection fields default empty so `GENERIC` is just `AppProfile(
    name="generic")` — an explicitly inert profile. Frozen + only-immutable
    fields (str / int / tuple / frozenset / compiled-regex) so a single
    instance is safely shared across tasks and threads.
    """

    name: str
    packages: tuple[str, ...] = ()

    # Prompt guidance injected (via the same per-app channel as skills/<pkg>.md)
    # only when this profile is active. Empty for GENERIC → non-commerce apps
    # never see the shopping rules. COMMERCE points at prompts.COMMERCE_ADDENDUM.
    prompt_addendum: str = ""

    # Arms the orchestrator's shopping-flow validators (ADD-intent, category-
    # tap, product-name, repeated-ADD, premature-cart-review, giveup). False
    # under GENERIC, so non-commerce apps skip those validators entirely —
    # both for efficiency AND correctness: those checks read the annotation
    # flags (is_action, is_location_header, ...) which are all False under
    # GENERIC, so running them on a non-commerce screen would mis-reject.
    enforce_shopping_guards: bool = False

    # --- [ACTION] vocabulary: primary buttons (ADD / +/- / checkout / pay) ---
    action_text_tokens: frozenset[str] = frozenset()
    action_id_tokens: tuple[str, ...] = ()
    action_desc_tokens: tuple[str, ...] = ()
    action_desc_exact: frozenset[str] = frozenset()

    # --- [CART] vocabulary: View-cart / mini-cart bar ---
    cart_id_tokens: tuple[str, ...] = ()
    cart_text_tokens: tuple[str, ...] = ()
    cart_desc_tokens: tuple[str, ...] = ()

    # --- [CATEGORY?] vocabulary: navigation tiles / banners / suggestions ---
    category_id_tokens: tuple[str, ...] = ()
    category_text_patterns: tuple[str, ...] = ()

    # --- [LOCATION] vocabulary: delivery/address header (search-bar look-alike) ---
    location_tokens: tuple[str, ...] = ()
    location_id_tokens: tuple[str, ...] = ()
    # A clickable element above this y is eligible to be the location header.
    # 0 disables location detection. Absolute px today; the orchestrator can
    # later derive it from the live screen height (see top_header_fraction).
    top_header_y_max: int = 0
    top_header_fraction: float = 0.0  # if >0, overrides top_header_y_max as h*fraction

    # Vertical band for "same row" (a card has a nearby [ACTION] -> it's a
    # product, not a category). Tuned for ~600px grocery cards.
    same_row_tolerance_px: int = 250

    # --- container-label extraction for [ACTION] grounding ---
    # Regex stripping an app's redundant card-desc suffix (Blinkit appends
    # "... is available for ₹130"). None -> no stripping.
    available_for_re: Pattern[str] | None = None
    min_container_label_len: int = 5
    max_container_w: int = 0  # 0 -> no width ceiling on a container label ancestor
    max_container_h: int = 0  # 0 -> no height ceiling

    @property
    def annotates(self) -> bool:
        """True if this profile carries any annotation vocabulary at all.

        The renderer/parser fast-path on this: a `GENERIC` element gets no
        [ACTION]/[CART]/[CATEGORY?]/[LOCATION] flags and the per-element
        annotation work is skipped wholesale.
        """
        return bool(
            self.action_text_tokens
            or self.action_id_tokens
            or self.action_desc_tokens
            or self.action_desc_exact
            or self.cart_id_tokens
            or self.cart_text_tokens
            or self.category_id_tokens
            or self.category_text_patterns
            or self.location_tokens
            or self.location_id_tokens
        )


# --------------------------------------------------------------------------
# GENERIC — the always-safe default. No vocabulary, no shopping validators.
# Apps that aren't commerce (rides, media, maps, messaging, payments,
# settings, ...) resolve here and pay nothing for shopping grounding.
# --------------------------------------------------------------------------
GENERIC = AppProfile(name="generic")


# --------------------------------------------------------------------------
# COMMERCE — the shopping-flow grounding promoted out of the old grocery
# hardcoding. Shared by every search -> add -> cart -> checkout app. The token
# sets are exactly those that were module globals in ui_tree.py / orchestrator,
# so commerce-app behaviour is byte-identical to before this refactor.
# --------------------------------------------------------------------------
_COMMERCE_PACKAGES = (
    # quick-commerce / food delivery
    "com.grofers.customerapp",   # Blinkit
    "com.zeptoconsumerapp",      # Zepto
    "in.swiggy.android",         # Swiggy / Instamart
    "com.application.zomato",    # Zomato
    "com.Dominos",               # Domino's
    # marketplaces
    "in.amazon.mShop.android.shopping",  # Amazon
    "com.flipkart.android",      # Flipkart
    "com.myntra.android",        # Myntra
    "com.meesho.supply",         # Meesho
)

# Blinkit appends "... is available for ₹130" to product-card content-descs;
# strip it so the container label keeps brand+SKU only.
_AVAILABLE_FOR_RE = re.compile(
    r"\s+is\s+available\s+for\s+₹?[\d,.]+.*$", re.IGNORECASE
)

COMMERCE = AppProfile(
    name="commerce",
    packages=_COMMERCE_PACKAGES,
    prompt_addendum=prompts.COMMERCE_ADDENDUM,
    enforce_shopping_guards=True,
    action_text_tokens=frozenset(
        {"add", "+", "−", "-", "buy", "place order", "pay",
         "checkout", "proceed", "place"}
    ),
    action_id_tokens=(
        "add_to_cart", "add_btn", "add_button", "btn_add", "plus", "minus",
        "increment", "decrement", "qty_inc", "qty_dec", "stepper", "checkout",
        "place_order", "proceed", "pay_button",
    ),
    action_desc_tokens=(
        "add to cart", "increase quantity", "decrease quantity",
        "checkout", "place order",
    ),
    action_desc_exact=frozenset(
        {"add", "+", "−", "-", "buy", "buy now", "place order", "pay",
         "pay now", "checkout", "proceed", "increase", "decrease"}
    ),
    cart_id_tokens=(
        "view_cart", "mini_cart", "cart_bottom", "cart_widget",
        "floating_cart", "cart_bar", "go_to_cart",
    ),
    cart_text_tokens=("view cart", "go to cart", "open cart", "view your cart"),
    cart_desc_tokens=(
        "view cart", "go to cart", "open cart", "view your cart",
        "cart with", "items in cart", "item in cart",
    ),
    category_id_tokens=(
        "category", "banner", "tile", "collection", "browse", "showcase",
        "promo", "carousel", "merchandise", "rail",
    ),
    category_text_patterns=(
        " n ", " range", " store", " house", " shop", "shop by", "explore",
        "browse", "view all", "see all", "categories",
    ),
    location_tokens=(
        "deliver to", "deliver in", "delivery in", "delivery to",
        "delivering to", "delivery address", "delivery location",
        "your location", "set location", "change location", "change address",
        "select address", "select location", "home address",
        "current location",
    ),
    location_id_tokens=(
        "location_header", "address_header", "delivery_header",
        "deliver_header", "location_bar", "address_bar", "header_address",
        "header_location",
    ),
    top_header_y_max=300,
    same_row_tolerance_px=250,
    available_for_re=_AVAILABLE_FOR_RE,
    min_container_label_len=5,
    max_container_w=int(0.85 * 1080),
    max_container_h=int(0.65 * 2400),
)


# All non-generic profiles, in resolution order.
_PROFILES: tuple[AppProfile, ...] = (COMMERCE,)

# package -> profile, built once at import. Falls back to GENERIC.
_BY_PACKAGE: dict[str, AppProfile] = {
    pkg: prof for prof in _PROFILES for pkg in prof.packages
}


def resolve_profile(package: str | None) -> AppProfile:
    """Return the grounding profile for a foreground package.

    Unknown / None package -> GENERIC, so a never-before-seen app runs the
    lean structural-only path rather than inheriting shopping heuristics.
    """
    if not package:
        return GENERIC
    return _BY_PACKAGE.get(package, GENERIC)


def all_profiles() -> tuple[AppProfile, ...]:
    """Every registered profile including GENERIC — for tooling/tests."""
    return (GENERIC,) + _PROFILES
