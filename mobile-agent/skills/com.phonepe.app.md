# PhonePe (com.phonepe.app)

## Navigation
- Home screen has tiles for Recharge, Send Money, Pay Bills, Bank Balance, etc.
- Recharge: tile → enter number → pick operator/plan → "Pay".

## Pitfalls
- **Every payment / "Pay" / "Proceed" button is HITL-worthy** — emit need_approval before tapping.
- UPI PIN entry screen is secured (FLAG_SECURE) — `screencap` returns a blank/black image; emit need_approval and instruct the user to enter the PIN on the device.
- Login / mPIN screen on first launch — HITL.
