**Anki Ambush** shows a small always-on-top window with a few due cards at random times. Cards come from the currently selected deck.

Everything below can be set from **Tools → Ambush → Settings…** (or the Config button on the Add-ons screen).

- `enabled`: master switch (also under Tools → Ambush → Enabled).
- `cards_per_popup`: cards per popup: a number (`3`) or a range (`"2-5"`, picked at random each time).
- `reviews_only`: if true, never show new cards (only learning/review cards).
- `min_interval_minutes` / `max_interval_minutes`: random wait between popups.
- `quiet_hours_start` / `quiet_hours_end`: "HH:MM"; no popups in this window (may wrap past midnight). Set both to "" to disable.
- `daily_cap`: max popups per day (0 = no cap). Resets when Anki restarts.
- `skip_when_idle_minutes`: macOS only; hold the popup while you've been away this long (0 = off).
- `backoff_after_dismissals`: after this many popups closed in a row without answering, start lengthening the interval (0 = off).
- `backoff_factor` / `backoff_max_multiplier`: how fast and how far the interval grows. Answering any card resets it.
- `steal_focus`: give the popup keyboard focus when it appears.
- The popup always snaps to the bottom-right corner of the screen your mouse is on, and shrinks to fit if `width`/`height` are too big.
- `width` / `height`: popup size in pixels.

The popup renders cards and the answer bar with Anki's own reviewer, so note styling, the "show next review time" preference and add-ons that restyle the reviewer apply to it too.

Keys: Space/Enter = show answer, then Good. 1–4 (or your custom answer keys) = Again/Hard/Good/Easy. E = edit (opens Browser), M = more, R = replay audio, - = bury, @ = suspend, Esc = skip.
Popups also wait while you're reviewing in the main window or a dialog is open.
