# Google Maps (com.google.android.apps.maps)

## Navigation
- Search bar at top of the map. Type destination → first suggestion is usually the right one.
- "Directions" button after picking a destination; choose mode (driving/walking/transit) at top.
- "Start" button kicks off turn-by-turn navigation.

## Pitfalls
- "Start" navigation = the user is going somewhere — usually fine to proceed, but the live nav UI is hard to interrupt cleanly. Don't kick off Start without intent.
- Location permission prompt on first launch — HITL.
