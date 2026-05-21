# YouTube (com.google.android.youtube)

## Navigation
- Bottom tab bar: Home, Shorts, +, Subscriptions, You. The search icon
  (magnifying glass) is top-right on Home.
- Tap the search icon, then type the query. The autocomplete list appears
  below; tap a row to run the search.

## Watching
- A tapped video opens in the mini-player at the top. Tap once on the
  thumbnail to expand to full-screen.
- The like/dislike/share row is just below the title on the expanded view.

## Pitfalls
- Sign-in prompts may appear on first run; treat them as HITL.
- Don't tap "Sign out" or "Delete history" unless the user explicitly asked
  — emit `need_approval` first.
