# Swiggy (in.swiggy.android) — Food + Instamart in one app

## Navigation
- Top tabs let you switch between Food, Instamart, Dineout. The default is Food on app open.
- Search bar is at the top of every tab.
- Cart icon is bottom; "Proceed to pay" and "Place order" are HITL-worthy.

## Tasks
- "Order food" → stay on the Food tab.
- "Order from Instamart" → tap the Instamart tab first, then search.

## Pitfalls
- Location/permission prompt may appear on first launch — emit need_approval.
- Restaurant cards have an "ADD" button per dish; first tap may show a customization sheet.
