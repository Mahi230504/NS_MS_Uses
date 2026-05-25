# Blinkit (com.grofers.customerapp — rebranded from Grofers but kept the package)

## Navigation
- Home-screen search bar is a tap target (NOT a real input). Tapping it navigates
  to a separate search screen that has the real EditText. Tap the EditText on the
  new screen before typing.
- Search results appear LIVE as you type — no need to press Enter or a magnifying
  glass. After typing, wait one cycle for the result list to render.
- Cart icon is bottom-right; the cart screen lists items + total before checkout.

## Search and add — read carefully
- For a generic request like "milk", tap the **ADD** button on the **FIRST**
  product card in the result list. Exactly ONE tap on ONE card.
- For a branded request like "Amul gold milk", scan the result cards and tap ADD
  on the FIRST card whose product name contains "Amul" AND "gold" (or as much of
  the brand specifier as you can match). If no card matches, fall back to the
  first card.
- **Never tap ADD on multiple product cards for a single requested item.** That
  adds multiple products to the cart, which is almost never what the user wants.
- Each product card's ADD button is at the bottom-right corner of the card. Tap
  again to increment quantity — only do this if the user explicitly asked for
  more than 1.

## Cart and checkout — HITL is mandatory
- When you reach the **Cart** screen (showing the items added, with a
  "Proceed to checkout" / "Place order" / "Pay" button at the bottom), DO NOT
  tap that button directly. Emit:
    {"action":"need_approval",
     "reason":"Cart review: <list each item: name, qty, price> · Total: ₹<amount>"}
  The user must explicitly approve before any checkout flow starts.
- Payment selection and "Place order" / "Pay Now" buttons are also HITL — never
  tap them autonomously.

## Pitfalls
- A delivery-address sheet may pop up on first cart visit. Confirm the saved
  address or dismiss before proceeding.
- Promotional bottom sheets occasionally cover the cart icon; dismiss with the
  small "x" or swipe down.
- If the payment screen shows no available method (e.g. no saved UPI/card),
  emit need_approval with reason "no payment method on this device" — don't try
  to add one.
