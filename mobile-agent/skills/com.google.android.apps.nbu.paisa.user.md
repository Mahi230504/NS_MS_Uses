# Google Pay (com.google.android.apps.nbu.paisa.user)

## Navigation
- Home: contact list + service tiles (Recharge, Bills, Bank balance).
- "Pay" button on contact row → enter amount → next → "Pay" → UPI PIN.

## Pitfalls
- **Pay / Send Money / Confirm buttons are HITL-worthy** — never auto-approve.
- UPI PIN screen is FLAG_SECURE — `screencap` is blank/black; emit need_approval and let the user PIN it on-device.
- Multiple bank accounts may be linked; the bank shown above the PIN entry matters.
