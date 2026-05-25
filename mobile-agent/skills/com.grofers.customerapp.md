# Blinkit (com.grofers.customerapp — rebranded from Grofers but kept the package)

## Navigation
- Home-screen search bar is a tap target (NOT a real input). Tapping it navigates
  to a separate search screen that has the real EditText. Tap the EditText on the
  new screen before typing.
- Search results appear LIVE as you type — no need to press Enter or a magnifying
  glass. After typing, wait one cycle for the result list to render.
- Cart icon is bottom-right; the cart screen lists items + total before checkout.

## Search and add — read carefully
- First, READ the product name on the first result card. If it doesn't match
  what the user asked for (e.g. searching "cigarettes" returned beer because
  Blinkit doesn't sell cigarettes), emit need_approval with
  `"no matching product — results show <what>"`. Don't ADD a wrong-category item.
- For a generic request like "milk" with a matching first card: tap the **ADD**
  button on that card. Exactly ONE tap on ONE card.
- For a branded request like "Amul gold milk", scan the result cards and tap ADD
  on the FIRST card whose product name contains "Amul" AND "gold" (or as much of
  the brand specifier as you can match).
- **TAP THE ADD BUTTON, NOT THE CARD BODY.** Each product card has the product
  image and name on the LEFT and a small green "ADD" button on the RIGHT side
  (roughly the right ~20% of the card's width). Tapping the image or product
  name opens a detail page — that's a wasted step. The ADD button's coords are
  in the UI elements list under the id `add` / `add_to_cart` / similar, or as
  text "ADD". Use those coords exactly.
- **Never tap ADD on multiple product cards for a single requested item.** That
  adds multiple products to the cart.
- Tap ADD again to increment quantity — only if the user asked for more than 1.

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
