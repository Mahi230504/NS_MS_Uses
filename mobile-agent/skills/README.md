# skills/

Per-app hints injected into the vision prompt. The filename is the Android
package name; e.g. `com.blinkit.markets.md` is loaded when Blinkit is the
foregrounded app.

## Authoring rules

- Keep skills short. Every iteration sends the file as input tokens — long
  files burn quota fast.
- Focus on **non-visible context**: stable navigation shortcuts, common
  pitfalls, screen identifiers the model can't infer from a screenshot
  alone. Don't describe what's already on screen.
- Mention HITL-worthy screens explicitly — they reinforce the keyword filter
  and the vision-based classifier.
- One file per package. Use the exact package name as reported by
  `adb shell dumpsys window | grep mCurrentFocus`.

## Loading

`SkillRegistry` (`agent/skills.py`) lazily loads `*.md` files on first lookup
and caches them in memory. Restart the bot to pick up edits.
