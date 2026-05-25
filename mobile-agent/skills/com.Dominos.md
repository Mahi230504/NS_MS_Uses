# Domino's (com.Dominos)

## Navigation
- Pizza menu is the home screen. Tap a pizza card → customisation sheet
  (size, crust) → ADD.
- Cart icon top-right or bottom; checkout flow asks delivery vs takeaway, then
  payment.

## Add and cart
- For "<pizza name>": tap the FIRST matching pizza card. On the customisation
  sheet, pick the size the user specified (default Medium) and proceed.
- Don't add multiple pizzas for one request unless user asks for more.

## Cart and checkout — HITL mandatory
- "Place order" / "Pay" — need_approval with cart contents.
- UPI PIN screen is secured (screencap goes blank). Emit need_approval and
  tell the user to enter PIN on-device.
