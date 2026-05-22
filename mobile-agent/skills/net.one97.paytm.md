# Paytm (net.one97.paytm)

## Navigation
- Home: service tiles (Recharge, Wallet, UPI, Bank).
- Recharge: tile → number → operator → plan → "Proceed to Pay".

## Pitfalls
- **All payment / "Pay" / "Proceed" buttons are HITL-worthy.**
- UPI PIN screen is FLAG_SECURE — `screencap` blank; emit need_approval.
- Aggressive cross-sell sheets ("activate Paytm Postpaid", lottery offers) on launch — dismiss before main flow.
