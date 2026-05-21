# Blinkit (com.blinkit.markets)

## Navigation
- The search bar lives near the top of the home screen. Tap it before typing.
- The cart icon is bottom-right; "Proceed to pay" and "Place order" buttons
  on the checkout screen are HITL-worthy — emit `need_approval` before
  tapping them.

## Search and add to cart
- Typing in the search bar updates the result list live; wait one cycle for
  it to settle before tapping a product.
- Each product card has an "ADD" button. Tapping it again increments quantity.

## Pitfalls
- A delivery-address sheet may pop up the first time you open the cart — it
  needs a tap on "Confirm" or a dismiss before checkout is reachable.
- Promotional bottom sheets occasionally cover the cart icon; look for a
  small "x" or swipe down to dismiss.
