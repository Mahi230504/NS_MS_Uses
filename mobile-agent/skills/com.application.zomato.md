# Zomato (com.application.zomato)

## Navigation
- Bottom tabs: Delivery, Dining, Live, Profile.
- Search bar at top, scoped to the active tab. Tap, then tap the real EditText
  on the search screen.

## Search and add
- For a dish/restaurant query: tap the FIRST matching result. Tap a dish's ADD
  button (often opens a customisation sheet for size/add-ons — pick default).
- Don't add multiple dishes for one requested item.

## Cart and checkout — HITL mandatory
- "Pay now" / "Place order" — emit need_approval with cart contents in reason.
- Location prompt + "for you" preferences sheet on first launch are also HITL.
