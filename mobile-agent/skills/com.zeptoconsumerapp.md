# Zepto (com.zeptoconsumerapp)

## Navigation
- Home-screen search bar at top — tapping navigates to a search screen with the
  real EditText. Tap the EditText before typing.
- Search results render live as you type. Wait one cycle after typing.
- Cart icon bottom-right.

## Search and add
- For "X": tap ADD on the FIRST card matching X. Exactly ONE tap on ONE card.
- For "<brand> X": find the first card whose name contains the brand specifier;
  fall back to first card if no match.
- Never ADD multiple products for one requested item.

## Cart and checkout — HITL mandatory
- On the Cart screen, DO NOT tap "Proceed to checkout" / "Place order" / "Pay".
  Emit need_approval with reason listing items + total. User must approve.
- Payment selection and final pay buttons are also HITL.

## Pitfalls
- Address sheet may pop on first cart visit — confirm/dismiss before proceeding.
- If no payment method available, emit need_approval — don't try to add one.
