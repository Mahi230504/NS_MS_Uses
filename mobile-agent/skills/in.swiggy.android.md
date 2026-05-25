# Swiggy (in.swiggy.android) — Food + Instamart in one app

## Navigation
- Top tabs: Food, Instamart, Dineout. Default is Food on app open.
- Each tab has its own search at the top — tap it, then tap the real EditText
  on the search screen that appears.
- Bottom cart icon.

## Search and add
- For "X": tap ADD on the FIRST matching restaurant / dish / product. Exactly
  ONE tap.
- Tapping ADD on a Food dish often opens a customisation sheet (size, add-ons).
  Pick the cheapest/default option and confirm.
- For Instamart: switch to the Instamart tab first, then search.

## Cart and checkout — HITL mandatory
- "Proceed to pay" / "Place order" / "Pay" buttons require need_approval. Emit
  it with the cart contents in the reason.
- Location and notification permission prompts on first launch are also HITL.
