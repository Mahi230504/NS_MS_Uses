"""Static registry of supported apps and per-app task templates.

The menu UX in handlers reads from here. To add an app: add an App entry,
add ~2-3 TaskTemplate entries on it, and drop a skills/<package>.md file
matching the package. That's it — no handler changes.

Package names are pinned to the Indian Play Store variants of each app.
If a user has a different variant, the AdbController's grep-discovery
fallback will still find the right one by name.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskTemplate:
    """One row inside an app's task menu.

    `template` is what we send the agent. If `param_prompt` is non-empty, the
    bot asks the user for free text first and substitutes `{param}` into the
    template before handoff.
    """

    id: str
    label: str
    template: str
    param_prompt: str = ""

    @property
    def needs_param(self) -> bool:
        return bool(self.param_prompt)


@dataclass(frozen=True)
class App:
    id: str
    package: str
    name: str
    category: str  # "groceries" | "food" | "mobility" | "shopping" | "media" | "tools" | "payments"
    emoji: str
    tasks: tuple[TaskTemplate, ...]


# ---------------------------------------------------------------------------
# Category emojis (used to prefix buttons in the inline keyboard).
CATEGORY_EMOJI: dict[str, str] = {
    "groceries": "🛒",
    "food": "🍕",
    "mobility": "🚕",
    "shopping": "🛍️",
    "media": "🎬",
    "tools": "🛠️",
    "payments": "💳",
}


# A reusable "type your own" template that any app can offer alongside its
# canned tasks. Keeps menus discoverable without limiting power users.
def _free_form_task(app_name: str) -> TaskTemplate:
    return TaskTemplate(
        id="free",
        label="Type my own",
        template="{param}",
        param_prompt=f"What do you want to do in {app_name}? Describe in plain English.",
    )


APPS: tuple[App, ...] = (
    # ----------------------------------------------------- Groceries
    App(
        id="blinkit",
        package="com.grofers.customerapp",
        name="Blinkit",
        category="groceries",
        emoji="🛒",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order groceries",
                template="Add {param} to the cart and proceed to checkout.",
                param_prompt="What would you like to order? (e.g. 'milk, bread, eggs')",
            ),
            TaskTemplate(
                id="reorder",
                label="Reorder last",
                template="Open my orders and reorder the most recent order.",
            ),
            TaskTemplate(
                id="status",
                label="Check order status",
                template="Open my orders and show the status of the latest order.",
            ),
            _free_form_task("Blinkit"),
        ),
    ),
    App(
        id="zepto",
        package="com.zeptoconsumerapp",
        name="Zepto",
        category="groceries",
        emoji="🛒",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order groceries",
                template="Add {param} to the cart and proceed to checkout.",
                param_prompt="What would you like to order?",
            ),
            TaskTemplate(
                id="status",
                label="Check order status",
                template="Open my orders and show the status of the latest order.",
            ),
            _free_form_task("Zepto"),
        ),
    ),
    App(
        id="instamart",
        package="in.swiggy.android",
        name="Instamart",
        category="groceries",
        emoji="🛒",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order groceries",
                template="Open the Instamart section of Swiggy and add {param} to the cart.",
                param_prompt="What would you like to order from Instamart?",
            ),
            _free_form_task("Swiggy Instamart"),
        ),
    ),
    # ----------------------------------------------------- Food
    App(
        id="swiggy",
        package="in.swiggy.android",
        name="Swiggy",
        category="food",
        emoji="🍕",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order food",
                template="Search for {param} and place an order.",
                param_prompt="What do you want to order? (restaurant or dish)",
            ),
            TaskTemplate(
                id="reorder",
                label="Reorder last",
                template="Open my orders and reorder the most recent order.",
            ),
            _free_form_task("Swiggy"),
        ),
    ),
    App(
        id="zomato",
        package="com.application.zomato",
        name="Zomato",
        category="food",
        emoji="🍕",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order food",
                template="Search for {param} and place an order.",
                param_prompt="What do you want to order?",
            ),
            TaskTemplate(
                id="dining",
                label="Find restaurant nearby",
                template="Open Dining and find a {param} restaurant nearby.",
                param_prompt="What kind of restaurant?",
            ),
            _free_form_task("Zomato"),
        ),
    ),
    App(
        id="dominos",
        package="com.Dominos",
        name="Domino's",
        category="food",
        emoji="🍕",
        tasks=(
            TaskTemplate(
                id="order",
                label="Order pizza",
                template="Add a {param} pizza to the cart and proceed to checkout.",
                param_prompt="Which pizza? (e.g. 'Farmhouse medium')",
            ),
            _free_form_task("Domino's"),
        ),
    ),
    # ----------------------------------------------------- Mobility
    App(
        id="uber",
        package="com.ubercab",
        name="Uber",
        category="mobility",
        emoji="🚕",
        tasks=(
            TaskTemplate(
                id="book",
                label="Book a ride",
                template="Book a ride to {param}.",
                param_prompt="Where to? (destination address or landmark)",
            ),
            TaskTemplate(
                id="history",
                label="Open ride history",
                template="Open the trips/history section.",
            ),
            _free_form_task("Uber"),
        ),
    ),
    App(
        id="ola",
        package="com.olacabs.customer",
        name="Ola",
        category="mobility",
        emoji="🚕",
        tasks=(
            TaskTemplate(
                id="book",
                label="Book a ride",
                template="Book a ride to {param}.",
                param_prompt="Where to?",
            ),
            _free_form_task("Ola"),
        ),
    ),
    App(
        id="rapido",
        package="com.rapido.passenger",
        name="Rapido",
        category="mobility",
        emoji="🚕",
        tasks=(
            TaskTemplate(
                id="book",
                label="Book a ride",
                template="Book a Rapido bike ride to {param}.",
                param_prompt="Where to?",
            ),
            _free_form_task("Rapido"),
        ),
    ),
    App(
        id="nammayatri",
        package="in.juspay.nammayatri",
        name="Namma Yatri",
        category="mobility",
        emoji="🚕",
        tasks=(
            TaskTemplate(
                id="book",
                label="Book a ride",
                template="Book a ride to {param}.",
                param_prompt="Where to?",
            ),
            _free_form_task("Namma Yatri"),
        ),
    ),
    # ----------------------------------------------------- Shopping
    App(
        id="amazon",
        package="in.amazon.mShop.android.shopping",
        name="Amazon",
        category="shopping",
        emoji="🛍️",
        tasks=(
            TaskTemplate(
                id="search",
                label="Search and buy",
                template="Search for {param} and open the first relevant result.",
                param_prompt="What are you looking for?",
            ),
            TaskTemplate(
                id="orders",
                label="My orders",
                template="Open the orders section and show recent orders.",
            ),
            _free_form_task("Amazon"),
        ),
    ),
    App(
        id="flipkart",
        package="com.flipkart.android",
        name="Flipkart",
        category="shopping",
        emoji="🛍️",
        tasks=(
            TaskTemplate(
                id="search",
                label="Search and buy",
                template="Search for {param} and open the first relevant result.",
                param_prompt="What are you looking for?",
            ),
            TaskTemplate(
                id="orders",
                label="My orders",
                template="Open the orders section and show recent orders.",
            ),
            _free_form_task("Flipkart"),
        ),
    ),
    App(
        id="myntra",
        package="com.myntra.android",
        name="Myntra",
        category="shopping",
        emoji="🛍️",
        tasks=(
            TaskTemplate(
                id="search",
                label="Search and shop",
                template="Search for {param} and open the first relevant result.",
                param_prompt="What are you looking for?",
            ),
            _free_form_task("Myntra"),
        ),
    ),
    App(
        id="meesho",
        package="com.meesho.supply",
        name="Meesho",
        category="shopping",
        emoji="🛍️",
        tasks=(
            TaskTemplate(
                id="search",
                label="Search and shop",
                template="Search for {param} and open the first relevant result.",
                param_prompt="What are you looking for?",
            ),
            _free_form_task("Meesho"),
        ),
    ),
    # ----------------------------------------------------- Media
    App(
        id="youtube",
        package="com.google.android.youtube",
        name="YouTube",
        category="media",
        emoji="🎬",
        tasks=(
            TaskTemplate(
                id="search",
                label="Search and play",
                template="Search for {param} and play the first relevant result.",
                param_prompt="What do you want to watch?",
            ),
            TaskTemplate(
                id="subs",
                label="Open Subscriptions",
                template="Open the Subscriptions tab.",
            ),
            TaskTemplate(
                id="shorts",
                label="Open Shorts",
                template="Open the Shorts tab.",
            ),
            _free_form_task("YouTube"),
        ),
    ),
    App(
        id="spotify",
        package="com.spotify.music",
        name="Spotify",
        category="media",
        emoji="🎬",
        tasks=(
            TaskTemplate(
                id="play",
                label="Play song or artist",
                template="Search for {param} and play the first result.",
                param_prompt="What do you want to play?",
            ),
            TaskTemplate(
                id="liked",
                label="Open Liked Songs",
                template="Open my Liked Songs playlist.",
            ),
            _free_form_task("Spotify"),
        ),
    ),
    App(
        id="jiosaavn",
        package="com.jio.media.jiobeats",
        name="JioSaavn",
        category="media",
        emoji="🎬",
        tasks=(
            TaskTemplate(
                id="play",
                label="Play song or artist",
                template="Search for {param} and play the first result.",
                param_prompt="What do you want to play?",
            ),
            _free_form_task("JioSaavn"),
        ),
    ),
    # ----------------------------------------------------- Tools
    App(
        id="maps",
        package="com.google.android.apps.maps",
        name="Google Maps",
        category="tools",
        emoji="🛠️",
        tasks=(
            TaskTemplate(
                id="nav",
                label="Navigate to…",
                template="Search for {param} and start driving directions.",
                param_prompt="Where to?",
            ),
            TaskTemplate(
                id="nearby",
                label="Find nearby…",
                template="Search for {param} near my current location.",
                param_prompt="What kind of place? (e.g. 'cafe', 'ATM')",
            ),
            _free_form_task("Google Maps"),
        ),
    ),
    App(
        id="whatsapp",
        package="com.whatsapp",
        name="WhatsApp",
        category="tools",
        emoji="🛠️",
        tasks=(
            TaskTemplate(
                id="send",
                label="Send a message",
                template="Open the chat with {param} and prepare a message (do not send without approval).",
                param_prompt="Who do you want to message?",
            ),
            _free_form_task("WhatsApp"),
        ),
    ),
    App(
        id="gmail",
        package="com.google.android.gm",
        name="Gmail",
        category="tools",
        emoji="🛠️",
        tasks=(
            TaskTemplate(
                id="compose",
                label="Compose email",
                template="Compose a new email to {param} (do not send without approval).",
                param_prompt="Who is the recipient?",
            ),
            TaskTemplate(
                id="inbox",
                label="Open inbox",
                template="Open the inbox.",
            ),
            _free_form_task("Gmail"),
        ),
    ),
    # ----------------------------------------------------- Payments
    App(
        id="phonepe",
        package="com.phonepe.app",
        name="PhonePe",
        category="payments",
        emoji="💳",
        tasks=(
            TaskTemplate(
                id="recharge",
                label="Mobile recharge",
                template="Open mobile recharge for {param} (require approval before paying).",
                param_prompt="Which number?",
            ),
            TaskTemplate(
                id="balance",
                label="Check balance",
                template="Open my bank account section and show the balance.",
            ),
            _free_form_task("PhonePe"),
        ),
    ),
    App(
        id="gpay",
        package="com.google.android.apps.nbu.paisa.user",
        name="Google Pay",
        category="payments",
        emoji="💳",
        tasks=(
            TaskTemplate(
                id="send",
                label="Send money",
                template="Open the Pay/Send flow for {param} (require approval before paying).",
                param_prompt="To whom? (name or UPI ID)",
            ),
            _free_form_task("Google Pay"),
        ),
    ),
    App(
        id="paytm",
        package="net.one97.paytm",
        name="Paytm",
        category="payments",
        emoji="💳",
        tasks=(
            TaskTemplate(
                id="recharge",
                label="Mobile recharge",
                template="Open mobile recharge for {param} (require approval before paying).",
                param_prompt="Which number?",
            ),
            _free_form_task("Paytm"),
        ),
    ),
)


_BY_ID: dict[str, App] = {app.id: app for app in APPS}


def get_app(app_id: str) -> App | None:
    return _BY_ID.get(app_id)


def get_task(app: App, task_id: str) -> TaskTemplate | None:
    for t in app.tasks:
        if t.id == task_id:
            return t
    return None


def apps_in_category(category: str) -> tuple[App, ...]:
    """Every registered app in a category, in registry order.

    The comparison feature uses this to expand a category-only request
    ("which app is cheapest for milk") into concrete candidate apps, and to
    keep a comparison within one comparable family (groceries vs groceries,
    food vs food). Note Swiggy (food) and Instamart (groceries) are distinct
    App entries despite sharing a package, so they never cross categories.
    """
    return tuple(a for a in APPS if a.category == category)


def render_prompt(template: str, param: str | None) -> str:
    """Substitute {param} into a template safely."""
    if param is None:
        return template
    # `template` may or may not contain the placeholder. .format() would
    # raise on missing keys; use a manual swap that's tolerant.
    return template.replace("{param}", param.strip())
