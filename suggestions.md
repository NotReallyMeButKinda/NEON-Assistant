# Suggestions

Ideas I ran into while working on the code, kept out of the way of the task at hand. Each one says
why it's worth doing and roughly how big it is (S = an hour or two, M = an afternoon, L = a project).
`[x]` = done (with a note on how), `[ ]` = still open.

## Done (from the first version of this file)

- [x] **One microphone stream, kept open (M).** `listening.MicStream`: a long-lived stream with a 400 ms
  pre-roll, so the first syllable after the wake phrase is no longer lost. It closes itself after 6 idle
  seconds and when you mute (the mic indicator goes off).
- [x] **Startup doesn't wait for app descriptions (S).** The catalogue is ready from names alone (0.8 s on an
  empty cache); Wikipedia descriptions fill in from a background thread.
- [x] **A log file (S).** `neon.log` (3 x 512 KB) with uncaught exceptions on any thread; Settings > General >
  "Open the log file".
- [x] **One place that writes the config (S).** `save_config` is atomic and locked; `persist_keys()` writes only
  the keys that changed (used by the instant-apply pages) so half-edited settings are never saved by accident.
- [x] **Adaptive microphone threshold (S).** Re-measures the room while idle; capped at 4x the calibrated level.
- [x] **Offline speech-to-text (L).** `stt.py` + `faster-whisper` (tiny / base / small English models), with live
  partial captions in the status bar while you speak. Google stays the default because it is faster (about
  0.5 s per command versus 0.8 to 1 s on CPU for `base.en`). It falls back to Google if the model can't load.
- [x] **Hold-to-talk hotkey (S).** `hotkeys.HoldKeyHook` (a low-level hook that sees key-down and key-up).
- [x] **Cache common spoken phrases (S).** Short Piper phrases keep their audio (106 ms to 2.5 ms to first audio).
- [x] **Per-app notification rules, quiet hours, VIP words (M).** `notifications.decide()`; Settings > Notifications.
- [x] **Tray toggle + "pause notifications for an hour" (S).** Tray menu items and voice commands.
- [x] **Catch up on missed notifications (M).** The watcher reports what was already waiting in the Action Center
  at startup; shown once, quietly.
- [x] **Now-playing widget (S).** From Pear Desktop, with a progress line; click to play / pause.
- [x] **Bottom-of-screen and per-monitor bar (M).** `bar_position`, `bar_monitor`; verified against real work areas
  on a two-monitor setup (including the second monitor's 125% scale).
- [x] **Respect Windows' "animation effects" (S).** `ui/motion.py`; setting `follow_os_animations`.
- [x] **Show the reply in the quick box (S).**
- [x] **Settings search box, export / import (M).** Also a new sidebar layout, and instant-apply pages.
- [x] **Widgets: CPU, RAM, battery, stopwatch, next calendar event (S each).** The calendar reads any iCalendar
  feed or `.ics` file, including recurring events and time zones.
- [x] **A real `tests/` folder (M).** 90 tests, `python -m unittest discover -s tests -t . -v` (no extra packages);
  `NEON_LIVE=1` adds the tests that use real hooks and toasts.
- [x] **Secrets out of the project folder (S).** The Pear Desktop token lives in Windows Credential Manager
  (`secrets_store.py`); the old `ytm_token.json` is migrated and deleted. `.gitignore` added.
- [x] **Settings schema with versioned migrations (M).** `settings_schema.py`: types, ranges and choices are
  validated on load, and `MIGRATIONS` converts old files. (A dataclass-based schema would be a further step.)
- [x] **Packaging (M).** `neon.spec` + `build_exe.py` (PyInstaller, one-folder). Data lives in
  `%APPDATA%\NeonAssistant` when frozen (`app_paths.py`), so upgrades never touch settings.
- [x] **The smaller things:** the window is looked up once per window command; `PiperSpeaker.say` cleans the text
  once; `sounds.play` reuses one output stream (first play 10 ms, then 0.3 ms).

## Adjusted along the way

- [~] **Offline wake-word detection (M).** With the offline speech engine the wake loop already keeps your audio on
  the PC, but it still transcribes everything it hears. A dedicated detector (openWakeWord / Porcupine) would be
  cheaper, but they only ship fixed phrases ("hey jarvis", "alexa"...), not a custom name like "Dan", so I
  didn't wire one in. Worth revisiting if you're happy to pick from the fixed phrases.
- [~] **Truly hiding Windows' own banner.** Unchanged: I could read notifications and dismiss them, but couldn't
  confirm any registry switch suppresses the pop-up (my test toasts never showed a banner here). Windows'
  Do Not Disturb is the reliable route, and the assistant keeps presenting notifications through it.

## Done in the big upgrade pass

- [x] **Long-term memory (M).** `memory_store.py`: "remember that my gate code is 4821", recall by topic or by
  content ("where did I park"), forget one or all. Facts you state about yourself ("I'm vegetarian") are handed
  to the language model on every question, so ordinary chat knows them.
- [x] **Personas (S).** `persona.py`: eleven ways of talking, switchable by voice or in Settings. The persona is
  appended to the system prompt and carries a speaking-rate hint; it never changes what the assistant can do.
- [x] **Unit conversion (M).** `units.py`: length, mass, volume, temperature, data, speed, time, area, pressure,
  energy, angle, and number bases. Entirely local, sub-millisecond.
- [x] **Controlling the PC (M).** `system_control.py`: the real mixer through COM (so "set the volume to 40
  percent" lands on 40), lock, sleep, sign out, restart, shut down, empty the recycle bin, show the desktop,
  brightness, uptime, free space, and a screenshot written straight to PNG from GDI with no image library.
  Everything irreversible asks for a yes first.
- [x] **Routines (M).** `routines.py` + a Settings editor: one phrase runs a list of steps, each of which is
  either an ordinary command or one of `open:`, `url:`, `run:`, `wait:`, `say:`.
- [x] **Plugins (M).** `plugins.py`: drop a `.py` file in `plugins/`, say "reload plugins". Contained rather than
  sandboxed -- an import error, an exception or a hang is reported and skipped. They run after the built-ins,
  so they can add commands but never shadow one.
- [x] **Dictation (M).** `dictation.py`: "take dictation" types what you say into the focused window through
  SendInput (so any character works, whatever the keyboard layout, and nothing touches the clipboard), with
  spoken punctuation and "scratch that".
- [x] **Clipboard history (S).** `clipboard.py`: "what did I copy", "copy that". In memory only; anything that
  looks like a password is kept but never read aloud.
- [x] **Conversation mode (S).** After a reply it can keep listening for one more turn without the wake word.
- [x] **Speak the timer countdown (S).** A spoken heads-up a minute before a long timer ends, and a countdown
  element in the status bar (red in the last minute, right-click to cancel). Timers already survived a restart.
- [x] **Alert before calendar events (S).** `calendar_feed.CalendarAlerts` -- a card, a sound and a spoken
  heads-up, `calendar_alert_minutes` before each event.
- [x] **Keyboard-friendly Settings (S).** Ctrl+F / Ctrl+K focuses the search, Ctrl+Tab steps pages, and every
  row has a "reset to default" button that appears only when it differs.
- [x] **Screenshot tests (M).** `tests/test_render.py` + `tests/render_utils.py`: every window rendered
  offscreen, audited for clipped text and compared to a stored image.
- [x] **A voice spectrum (S).** A real FFT of what Piper is playing, computed in the audio callback, drawn as
  log-spaced bars in the status bar.
- [x] **Nonsense (S).** Dice, coins, jokes, a magic 8-ball -- and "do a barrel roll", "party mode" and
  "the matrix", which the status bar actually performs (`ui/effects.py`).
- [x] **A README (S).** What it does, what to say, how to extend it, and what leaves the machine.
- [x] **A relocatable data folder (S).** `NEON_DATA_DIR` points settings, memories, caches and plugins anywhere
  -- a second profile, a portable copy, or a throwaway folder for trying things out.

## Fixed along the way

- [x] **The app matcher picked apps on one shared word.** "visual studio code" resolved to "Roblox Studio" and
  "the photo editor" to "Registry Editor", because one word in common plus weak character similarity cleared the
  bar. It now scores by *coverage* -- how much of what you said an app accounts for, weighting its own name far
  above a word that merely appears in its description -- and a multi-word query needs near-identical spelling
  before spelling alone counts. `tests/test_apps.py` pins the awkward cases down.
- [x] **`_TIMERS` was mutated from three threads without a lock.** A timer firing while another was cancelled
  could lose one or raise.
- [x] **"Set the volume to 40" was owned by the music handler**, which reached into the mixer itself when Pear
  Desktop was unreachable. There is now exactly one place that changes the volume.
- [x] **The un-pause timer in `listening.resume` was not a daemon**, so quitting could wait on it.
- [x] **Status-bar tests left their pollers running**, which made everything after them slower and the theme
  tests flaky. They stop their timers now, and `use_temp_config()` also isolates memories and the clipboard.
- [x] **`QColor.isValidColor` is deprecated in Qt 6.8** and warned on every theme change.

## Done in the September 23 pass

- [x] **Simulated test notification.** The button now hands a *simulated* notification (id -1) to NEON's own
  pipeline; nothing reaches Windows, and the watcher refuses to dismiss negative ids.
- [x] **Notification card drawn over the bar's contents.** A plain `QWidget` ignores its style sheet background
  without `WA_StyledBackground`; the card is solid now.
- [x] **Clipped text.** The state label was a fixed 78 px ("Listening" was cut off at 20 px text) and now sizes to
  its text; spin boxes and combo boxes had empty arrow squares (the style sheet removed the native arrows) and now
  draw themed chevrons.
- [x] **Media buttons, and switching bar widgets from the bar.** Previous / play-pause / next beside now-playing
  (Pear Desktop, else the Windows media keys); right-click the bar for a tick list of every element.
- [x] **Custom wake words (L).** `wakeword.FeatureStream` + `CustomModel` (openWakeWord's feature models run with
  onnxruntime, no openWakeWord package) and `wake_training.py`. "hey nova" / "hey dan" train themselves on first use;
  *Settings > Listening > Train a wake word* records your voice and trains any phrase. Held-out-voice test: "hey nova"
  2/3 with 0 false triggers in 36 s of speech; "hey dan" 3/3 with 1, once the multi-speaker voice was added.
- [x] **Noise-robust wake loop (S).** Steady noise (by loudness variation) is never transcribed; Whisper's own
  no-speech rule drops the rest in wake mode.
- [x] **Per-persona voices (S).** `persona.voice_for`: a voice you picked, else a suitable installed one.
- [x] **High contrast + screen-reader names (M).** Two high-contrast themes (7:1 or better), followed
  automatically while Windows' Contrast themes are on; accessible names on the icon-only controls.
- [x] **Memory browser (S), expiry (S), recall by meaning (L).** An editable table (topic, fact, added, expiry);
  "remember until Friday that ..."; Ollama embeddings (`nomic-embed-text`) when words don't match.
- [x] **Routines on a schedule or a trigger (M).** "weekdays at 9:00", "when discord starts", "at startup".
- [x] **Plugin hooks (S).** `on_reply`, `on_notification`, `on_timer`, `on_routine`.
- [x] **CI (S).** `.github/workflows/tests.yml` (screenshots skipped there with `NEON_SKIP_SCREENSHOTS=1`).
- [x] **Stream replies into the quick box (S).** Already worked (caption signal per chunk, with a caret).
- [~] **Split `assistant.py` (M).** First step: the pure parsers moved to `commands.py` (re-exported). The
  stateful ones (notifications, confirmations) and the router are still in `assistant.py`.

## Still open

### Done in the September 24 pass
- [x] **"Summarize it out loud straight away" notification mode, and an Open button on every card.**
- [x] **Offline, private defaults (M).** New installs: Whisper + the "hey nova" detector, and three new switches
  off -- `stt_online_fallback` (no silent Google fallback), `location_from_ip`, `app_descriptions_online`.
  Config v3 migration turns those three *on* for existing files, so an update changes nothing for them.
  Onboarding lists Whisper first, asks for a town, and "use my name as the wake word" switches to speech mode.
- [x] **Notification history panel (M).** `ui/notification_history.py` (bell button, bar menu, tray); voice
  "what did Discord say", "remind me about that in an hour" ("that" only while the notification is < 15 min old).
- [x] **Open the exact chat / email (M), partly.** The listener API has no launch args, but Windows' own
  `wpndatabase.db` has the toast XML under the same id (verified). Protocol toasts' `launch` links are read
  (read-only) on arrival and opened by Open; dangerous schemes are blocked. Most chat apps (Discord, Chrome) use
  in-app activation, so Open still just launches those apps.
- [x] **Now playing from any player (M).** `media.py` + `media_watcher.ps1` (GSMTC via PowerShell, no packages):
  bar widget, play / pause / next / previous, "what's playing". Setting `media_any_player`.
- [x] **Wake-word "try it" + cached synthesis (S-M).** A live score meter in the trainer; Piper takes are cached
  as int16 `.npy` under `wakeword/synth_cache` (trimmed to 400 MB), keyed by voice file size/date.

- [x] **Timers window (S).** `ui/timer_panel.py`: presets, typed lengths (`timers.parse_duration_text`), a live
  list with per-timer cancel (`timers.list_timers` / `cancel_timer`). Also fixed `timers_status` lowercasing labels.
- [x] **Board (M).** `board.py` + `ui/board.py`: Trello-style columns and cards with due dates, drag and drop,
  inline add, card editor, and voice commands routed right after notifications (`handle_board`, `board_enabled`).

### Settings clean-up (September 24, later)
- [x] **Settings regrouped (M).** 15 pages instead of 16: Location & units moved into General, Sounds & feedback
  into Voice & sounds, board + calendar got their own page, PC control into Apps & PC, fun into Persona, "use my
  name as the wake word" and "ignore the mic while speaking" into Listening. "Always ignore these apps" became
  ignore rules at the top of the per-app rules (merged on load; `notify_ignore` is saved empty).
- [x] **Plain wording.** Jargon in parentheses (SAPI5, tool-picker, model file names, CUDA libraries, API names)
  is gone from labels and help text.
- [x] **Rows that don't apply are faded (`_enable_when`).** E.g. recognition-on-this-PC rows with Google, Piper
  rows with the Windows voice, every bar row with the bar off, the voice spectrum without Piper.
- [x] **Removed from Settings:** "Add three examples", Pear Desktop client id, processor threads, "show how fast
  each reply started". Their config keys still work.
- [x] **`keys:` routine steps.** `keys: ctrl+shift+esc` or `keys: win+d, alt+tab` (`dictation.press_keys`), a key
  recorder in the routines editor, and a warning line for steps that can't work.
- [ ] **Ctrl+Alt+Del and Win+L can't be pressed by an app (outside our control).** Windows only accepts them from
  the keyboard (or `SendSAS` from a service with a policy switched on). The editor says so and suggests "lock the
  PC" / Task Manager instead. A tiny helper service could do it, but that's an install-as-admin feature.
- [x] **Emoji and styled text are spoken as words (`tts.speakable_symbols`).** Both voices; 😂😂😂 -> "laughing",
  𝓗𝓮𝓵𝓵𝓸 -> "Hello", skin tones / joiners dropped.
- [x] **Attachment names in notifications (`notifications.spoken_file_names`).** "IMG_2041.jpg" -> "an image",
  "beach_trip.jpg" -> "an image called beach trip"; used by read-aloud, "just the message" and summaries.
- [x] **The shade (`ui/shade.py`).** Accent-to-transparent band with the reply in large text while the bar is
  off or another app is fullscreen (on that app's monitor); notifications optional; click-through.
- [x] **Shade restyle.** A radial accent glow from the top centre, sized to the text and easing between sizes,
  pulsing lightly with the voice; text in the theme's text color with a soft shadow.
- [x] **Fullscreen detection** (`shade.fullscreen_monitor`): the foreground window's *client area* covering its
  monitor (borderless windowed, F11, real fullscreen) -- not maximized windows -- plus
  `SHQueryUserNotificationState` for exclusive Direct3D fullscreen.
- [x] **Quick box over fullscreen apps.** Opens on the fullscreen app's screen, re-asserts topmost, and gives
  focus back to the game when it closes.
- [x] **Better web answers (`websearch.py`).** The question is reduced to its topic before searching; the AI gets
  the article's opening section plus Wikidata facts (release dates per platform, developer, director...);
  providers: DuckDuckGo (default), Brave Search (key in Credential Manager), SearXNG, or Wikipedia only.
  A 429 from Wikimedia is retried once after its Retry-After.
- [x] **Wikipedia pop-up (`ui/wiki_popup.py`).** Off by default; picture (free image first, non-free cover/poster
  as a fallback), opening section, "Read more", close button in the corner on the chosen side.
- [x] **Quick box over exclusive-fullscreen games.** It no longer takes focus over a fullscreen app (the game would
  minimize): shown without activating, typed into through `hotkeys.KeyCapture` (modifiers, Alt/Win combos and
  Ctrl shortcuts pass through; released on close, and after 60 s idle).
- [x] **Any date in a card title becomes its due date** (`board._split_due`): "Complete project on september
  22nd" -> "Complete project", due Sep 22. Longest date at the end first, or at the start after on/by/due.
- [x] **"the 29th"** is this month's 29th, or next month's once it has passed (a month too short is skipped);
  "tuesday the 29th" only if it is a Tuesday, so "Watch Friday the 13th" stays a title. "Add X on <date> to my
  board" also tries splitting at the last "to / on / in", so the date stays with the title.
- [ ] **Past dates on cards (S).** A date without a year that has passed means next year (so "september 22nd"
  typed on Sep 24 is 2027). A card for a date a few days ago might rather mean "overdue" -- worth a setting?
- [ ] **Topic extraction by the AI (S).** `websearch.topic_of` is rules-based; odd phrasings ("that game where
  you jump to music") would need the model to name the topic first.
- [ ] **Shade over exclusive-fullscreen games (L).** Not possible for a normal window; would need a game overlay
  hook. Borderless fullscreen works.
- [ ] **Flag names (S).** 🇺🇸 is said as just "flag"; a small country-code table would make it "US flag".
- [ ] **An "Advanced" toggle per page (S).** Timing, Ollama address and keep-alive, match strictness could hide
  behind it so the default view is shorter.

### Ideas from this pass
- [x] **Board due-date reminders (S).** `board.take_due_reminders` + `controller._board_reminder_loop`: once a
  day from `board_remind_time` (or at startup if later), due-today and overdue cards, marked `reminded` in
  board.json so a restart doesn't repeat them. The card's Open button shows the board.
- [ ] **Time-of-day due dates (S).** Due dates are days only; "due at 3pm" would need a time field and a
  reminder at that time.
- [ ] **Card descriptions, labels / colours, checklists (M).** The card editor has room for them; the JSON
  schema would get a `version: 2`.
- [ ] **Undo on the board (S).** Deleting a card or column is immediate; a short "Undo" toast would help.
- [ ] **Reply / open chats for in-app-activated toasts (L).** Would need each app's own activation (COM
  `INotificationActivationCallback`) or its API; not reachable from outside.
- [ ] **Album art in the now-playing widget (S).** GSMTC exposes a thumbnail stream (needs an `IAsyncAction`-style
  read in the helper).
- [ ] **Persist notification history across restarts (S).** It's in memory only (20 items); a small JSON file
  would let the panel show yesterday's.

### Speech and voice
- [ ] **Faster offline recognition (M).** A GPU build of `ctranslate2`, or Vosk for the wake loop. Less pressing
  now that the wake-word detector avoids transcription entirely.
- [x] **Wake-word training in fewer steps (S).** Done on September 24: Piper takes are cached per phrase.
- [x] **Test a trained wake word from the trainer (S).** Done on September 24: the "try it" meter.

### Notifications and calendar
- [ ] **Private calendars (M).** Google Calendar / Microsoft Graph with OAuth -- needs registered client ids.
- [ ] **Reply from a notification (L).** Per-app integrations (Slack / Discord APIs).
- [x] **Now playing from any player (M).** Done on September 24 (`media.py` + `media_watcher.ps1`).

### Status bar and UI
- [ ] **A bar per monitor (M).** (Deliberately left out of this pass.)

### Done in the September 25 pass
- [x] **Qwen3 8B instead of llama3.2** (config v4 swaps only the old default). `think: false` keeps it quick.
- [x] **Natural speech (`intent.py`).** A clean-up pass (fillers, politeness, the wake name) before the parsers,
  then the AI picks one of ~80 intents with Ollama's JSON-schema output. Templates are re-routed through the
  parsers, so every command still has one implementation. Live: 27/28 free-form phrasings right, ~0.6 s each.
- [x] **Facts are looked up, not remembered.** `looks_like_fact_question` sends release dates, directors and
  the like straight to the web answer. From memory, Qwen3 said Silksong came out "September 28, 2023"; grounded:
  4 September 2025. Wikipedia and the web search now run side by side.
- [x] **Fact-check mode** (`fact_check`): answers with checkable claims are verified against sources before
  they're spoken (nothing streams meanwhile). Corrected or flagged "couldn't confirm".
- [x] **Close windows by name**, with a yes/no and an accent glow around the window (`ui/window_highlight.py`).
  "Close all chrome windows" too. Routines' close steps don't ask.
- [x] **Zen / Firefox extension + local bridge.** Summaries, page questions (JSON-LD prices / dates first, then
  the most relevant chunks), find on page, list / switch / close tabs. Token plus Origin check.
- [x] **Bitwarden** through `bw`: usernames spoken, passwords copied (30 s, never in history) or typed after a
  yes (and only if the same window still has focus), 2FA codes, "which one?" for ties, auto-lock.
- [x] **A sound when listening starts**, and **"still thinking" lines** after N seconds (both in Voice & sounds).
- [x] **Fixed:** "add a card X tomorrow" ignored the day it was given (a test failed on Fridays only).

### Ideas from the September 25 pass
- [ ] **Weather for tomorrow (S).** "Is it gonna be cold tomorrow" gets today's weather; Open-Meteo's daily
  forecast is already in the response, it just isn't picked by day.
- [ ] **Faster intent reading (S).** Put the intent catalogue in the *system* prompt and keep it fixed, so Ollama
  can reuse its cache between requests (the first request after a chat pays ~1-3 s extra).
- [ ] **Cache fact answers for a day (S).** The same question twice shouldn't search twice.
- [ ] **More browser commands (S).** "Open X in a new tab", "go back", "scroll down", "read my selection" (a
  fallback for the selection commands when the clipboard trick fails in the browser).
- [ ] **Signed extension for stock Firefox (S).** `web-ext sign --channel unlisted` with an AMO key.
- [ ] **`bw sync` after unlocking (S).** The item list is whatever `bw` last synced; a background sync would pick
  up items added on the phone.
- [ ] **Use the model's topic for searches (S).** `fact_lookup` already returns a clean `query` slot ("new
  zelda"); passing it to `websearch.topic_of` would help odd phrasings. (Overlaps the open "topic extraction"
  entry above.)

### September 25, later
- [x] **Spoken numbers (`speech_text.py`).** Years ("two thousand eight", "nineteen ninety-nine"), prices, times,
  dates, ordinals, ranges, phone numbers digit by digit, symbols as words, brackets dropped. Both voices.
- [x] **Custom title bar (`ui/frame.py`)**, switchable to native live. Keeps DWM's shadow, corners and snapping.
- [x] **Model dropdown with downloads (`ui/model_picker.py`).**
- [x] **New welcome tour.** Eight steps with a rail, option cards, featured voices with play buttons, theme tiles,
  live mic meter (only while testing), Ollama check and model picker, connections, shortcuts, summary.
- [ ] **Snap layouts flyout on the custom maximize button (S).** Windows 11 shows it only for HTMAXBUTTON, which
  means handling WM_NCLBUTTONDOWN/UP for that button ourselves.
- [ ] **Flaky test (S):** `SummarizeModeTests.test_a_new_notification_is_summarized_without_a_question` failed once
  in a full run and never alone -- a 5 s wait on a thread under load. Worth a longer wait or an event.
- [ ] **Resolutions spoken as years (S):** "1920x1080" -> "one thousand nine hundred twenty by one thousand eighty";
  people say "nineteen twenty by ten eighty".

### September 25, evening
- [x] **Notifications in fullscreen.** Toasts the listener misses (Windows' automatic Do Not Disturb while a game
  or video is fullscreen) are read from `wpndatabase.db` and delivered 3 s later (`notifications.DatabaseWatch`).
- [x] **The notification card's Dismiss is an ✕**, always shown, at the card's right end.
- [x] **Bitwarden without a master password** when `bw` is already unlocked (`bitwarden.open_without_password`).
- [x] **Faster model for simple questions** (`assistant.light_answer`). llama3.2 can't tell when it doesn't know
  (it said Eddy Merckx won the 1987 Tour), but sorting "chat" from "expert" with a few examples was right on
  18 of 18 test phrases, so it sorts first.
- [x] **Times on board cards**, spoken, typed, in the editor, and announced ten minutes before.
- [x] **Everything file search (`filesearch.py`)**, no DLL or es.exe: the IPC protocol in ctypes.
- [x] **Recent lookups cached (`lookup_cache.py`)**: fact answers, search results, places; newest 50 each.
- [x] **Title bar**: switching modes live left no native bar / an invisible custom one; the hit test now uses
  the message's own point (right on the 125% monitor).
- [x] **"Remove before reading"** rules for notification text, with an editor and live preview.
- [x] **Voice-activity detection (`vad.py`)** instead of loudness: a quiet mic (1/20 level) is heard, and a phrase
  ends even with music underneath (both measured on a synthesized sentence in `tests/test_vad.py`).
- [x] **Beep maker, your name, the Unsure persona, summary -> board card.**
- [ ] **Speaker-adapted wake word (M).** Retrain "hey nova" with a few recordings of your own voice mixed into the
  synthetic samples; openWakeWord's scores are much higher for the voice it was trained on.
- [ ] **Show the mic level live in Settings (S).** A meter next to the microphone choice would make a too-quiet
  or wrong device obvious at once (the welcome tour has one already).
- [ ] **Search file contents (M).** Everything 1.5 can index contents (`content:`); 1.4 can't. Windows Search
  (the `SystemIndex` over OLE DB) could answer "the document that mentions the lease" meanwhile.
- [ ] **"The file I downloaded yesterday" (S).** Everything supports `dm:yesterday` and `path:downloads`; a few
  date words in the parser would map onto them.
- [ ] **Cache the weather for 10 minutes (S).** Asking twice in a row calls Open-Meteo twice.
- [ ] **Verify the fullscreen fix on a real game (S).** Couldn't be tested without taking over the screen. The
  log says "came from Windows' database" whenever the fallback delivered one; if nothing arrives at all, the app
  itself isn't sending while you're fullscreen.
- [ ] **Speak notifications when the card can't be seen (S).** In exclusive fullscreen neither the bar nor the
  shade can draw, so modes that only show a card ("card", quiet hours) are silent; a "while fullscreen, read
  them out" switch would cover it.
- [ ] **Redraw the board when a timed card goes overdue (S).** The badge turns red on the next redraw, not at
  the time itself; a minute timer in the window would do.
- [ ] **Light model for more jobs (S).** Follow-ups ("tell me more") and selection summaries could try it too;
  the intent reader could, if a test set shows llama3.2 picks intents as well as qwen3.
- [ ] **Ask again after "bw" locks (S).** An ambient `BW_SESSION` that `bw` later rejects surfaces as an error
  instead of re-opening the password box.

### Code health
- [ ] **Typed settings (M).** `DEFAULT_CONFIG` + `CONSTRAINTS` as one dataclass. There are now ~40 more keys.
- [ ] **Finish splitting `assistant.py` (M).** Next: the router into `router.py`, the notification queue into its
  own module.

### Packaging (September 26)
- [ ] **Stray `browser_extension/compressed.zip` (S).** An old zip of the extension sits in the source folder. The
  build and `build_extension.py` now skip `.zip`/`.xpi` files, but it can probably be deleted.
- [ ] **Smaller build (S-M).** The exe folder is ~375 MB; PySide6 modules the app never imports (WebEngine, 3D,
  Multimedia...) and `av.libs` (~64 MB, pulled in by faster-whisper) are the biggest wins to try excluding.
- [ ] **Installer (M).** An Inno Setup script would give a Start-menu entry and an uninstaller instead of a loose folder.

### Linux port (September 26)
- [ ] **Try it on a real Arch session (S).** Everything is tested against fakes (and a Linux CI job that hasn't run
  yet); the first real Hyprland and Plasma runs will show what the fakes got wrong. Worth checking first: the
  Lua-config commands (`hl.window_rule`, `hl.monitor` reserved area, `set_prop` "unset"), and Plasma's
  shortcut-portal dialog.
- [ ] **KDE: reserve the bar's strip (M).** Mostly covered by the panel widget now. For NEON's own bar, KWin
  rules can't reserve space; a layer-shell surface (LayerShellQt, needs a small compiled helper) would.
- [ ] **KDE: the "close this?" outline (M).** A KWin script (loaded over D-Bus) could draw it, or recolour the
  window's frame like Hyprland does.
- [ ] **KDE: fullscreen detection for the shade (S).** kdotool doesn't report fullscreen; a KWin script or
  comparing the active window's geometry to the screen would.
- [ ] **A PKGBUILD (S-M).** Install into /opt with its own venv plus a `neon-assistant` launcher, so keybinds and
  autostart don't need the source path.
- [ ] **Hotkeys on KDE follow Settings (S).** Plasma keeps the first confirmed keys; NEON could open the
  portal's ConfigureShortcuts (portal v2) when you change one, instead of the note telling you to.
- [ ] **Panel widget extras (S).** It shows state and captions only; the bar's clock, weather, CPU and music
  widgets could join the feed, and its pop-up could show notification cards with Summarize / Open.
- [ ] **Panel widget on a real Plasma (S).** Tested in plain Qt with stand-ins for Plasma's modules; check the
  look in a light and a dark theme, a vertical panel, and the pop-up's size on a real desktop.
