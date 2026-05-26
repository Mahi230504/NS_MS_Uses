# Blinkit (com.grofers.customerapp — rebranded from Grofers but kept the package)

## Navigation
- Home-screen search bar is a tap target (NOT a real input). Tapping it
  NAVIGATES to a separate search screen that has the real EditText. Tap the
  EditText on the new screen before typing.
- Search results appear LIVE as you type. The orchestrator auto-sends ENTER
  after each `type` action, which both closes the keyboard and commits the
  query to a full search-results page (not the autocomplete dropdown).
- Cart icon is bottom-right; the cart screen lists items + total before
  checkout.

## Home screen — location header vs search bar (THE #1 misclick trap)
On the Blinkit home screen the TOP of the screen has TWO stacked horizontal
strips that look alike at a glance:

  1. **Delivery-location header** (top-most, roughly y=100–280 on a 1080×2400
     screen). Shows your saved address and estimated delivery time
     ("Delivering to Home · 8 mins"). Tapping it opens an address picker,
     which is NOT what you want.
  2. **Search bar** (immediately below, roughly y=280–360). Shows hint text
     like "Search for atta, butter…". Tapping it navigates to the search
     screen.

The two are visually similar. To distinguish them in the UI elements list:
  - The location header is prefixed with `[LOCATION]` and typically has
    coords ABOVE y=280.
  - The search bar has class=EditText OR an id containing `search` OR a hint
    text starting with "Search". Coords are BELOW y=280.

**Rule**: NEVER tap a `[LOCATION]`-prefixed element when the task is to
search. Pick the `[ACTION]`-flagged or class=EditText / id=search element
below it.

**The orchestrator enforces this structurally**: if you emit a tap whose
`note` says "search bar" / "search box" / "search icon" but the coordinate
lands on a `[LOCATION]` element, the tap is rejected before execution and
you'll be asked to retry. Read coords from the search-bar entry in the UI
elements list — don't estimate from the screenshot.

## Typing — read carefully
THE MANDATORY SEQUENCE for "add <X> to cart":
  1. Tap the home-screen search bar (NAVIGATES; doesn't focus an input).
  2. On the new screen, FIND the search EditText in the UI elements list and
     tap its coords — even if it looks focused. Cost of an extra tap is small;
     cost of typing into an unfocused field is total failure.
  3. Emit the `type` action with the query.
  4. The orchestrator presses ENTER for you. Wait one cycle for the results
     to render.

## Search-results page — telling PRODUCTS apart from CATEGORIES
This is where most failures happen. After the search resolves, the screen
contains a mix of:

- **PRODUCT CARDS** (what you want):
  - Show a product image, a brand-prefixed title ("Maggi 2-Minute Masala
    Noodles"), a weight ("70g"), a price ("₹14"), and an **ADD button** on
    the right.
  - In the UI elements list they show up with an `[ACTION] "ADD"` or
    `[ACTION] desc=Add to cart` line at coords within ~250px vertically of
    the title row.

- **CATEGORY TILES / SUGGESTIONS** (do NOT tap these):
  - Banner-style cards or autocomplete rows like "Maggi N Maggi House",
    "Knorr Noodles & Soups Range", "Maggi Store", "Atta Categories", "Shop
    by Brand", "Browse Noodles". They contain the brand keyword but are
    navigation, not products.
  - In the UI elements list they appear WITHOUT a nearby `[ACTION]` line.
    The renderer marks them with the `[CATEGORY?]` prefix when it detects
    one of the patterns above.
  - Tapping them navigates AWAY from the search results to a category
    landing page (often gated by a "Confirm Location" sheet) and you'll
    lose your search query.

**Decision procedure** before tapping a result row:
  1. Look at the row's coords. Is there an `[ACTION]` line within ~250px
     vertically of it? If YES, it's a product card — tap the [ACTION] ADD.
  2. If NO `[ACTION]` is nearby, it's a category/suggestion — DO NOT TAP.
     Instead: swipe up to scroll the list. The real product cards are below
     the category tiles. After scrolling, repeat the check.
  3. If after one or two ACTUAL swipe actions there's still no Maggi
     product card with an ADD button, emit
     `{"action":"need_approval","reason":"no Maggi products found for '<query>'"}`.
     The orchestrator verifies you actually swiped by checking task
     history — claiming "after scrolling" in the reason text without an
     executed swipe gets the need_approval rejected. Also: never emit a
     "no products found" need_approval right after a tap whose note said
     "tap ADD on …" — those two statements contradict each other.

## Brand strictness (unchanged)
- "Maggi" → only cards whose title contains "Maggi". Yippee ≠ Maggi.
  Knorr ≠ Maggi. Top Ramen ≠ Maggi.
- "Amul milk" → only cards with "Amul" in the title.
- Don't substitute a different brand on the user's behalf.

## After ADD — IMMEDIATELY go to cart, do NOT touch filters
Once you've tapped the [ACTION] ADD on the right card, the product is in the
cart. Your next move is the cart icon (bottom-right, usually labelled "View
Cart" with a price). Do NOT:
  - Tap "Filters" or any sort / veg-toggle control
  - Tap a "(1)" filter indicator to "clear" anything
  - Re-search or browse other results
  - Tap into the product detail page
The "Filter (1)" indicator that may appear is Blinkit's *cosmetic* filter
hint — it does not block what's in your cart. Ignore it.

## Quantity stepper
After tapping ADD, the card's ADD button becomes a `−  1  +` stepper. To get
qty 2, tap the `+` (same X as the old ADD). The UI elements list shows it
with id containing `plus` / `inc`. If tapping the same coords AFTER ADD opens
the product detail instead, you tapped slightly off — re-find the `+` in the
elements list and use its exact coords.

## Cart and checkout — HITL mandatory
- On the Cart screen with items + a "Proceed to checkout" / "Place order" /
  "Pay" button: emit
  `{"action":"need_approval","reason":"Cart review: <items + total>"}`.
  The user must approve before any checkout flow starts. Do NOT tap the
  button autonomously.
- Payment selection and "Place order" / "Pay Now" buttons are also HITL.

## Pitfalls
- A **delivery-address sheet** may pop up on first cart visit ("Confirm
  Location" / "Add address"). Confirm/dismiss only if it BLOCKS the cart
  screen. If it appears mid-search-flow (i.e. after tapping a category
  tile), pressing back instead is usually correct — confirming sends you
  deeper into the wrong flow.
- Promotional bottom sheets occasionally cover the cart icon; dismiss with
  the small "x" or swipe down.
- If the payment screen shows no available method (no saved UPI/card),
  emit need_approval with reason "no payment method on this device" — don't
  try to add one.
