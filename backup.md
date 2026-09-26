# Session backup — 2026-09-23

A record of everything changed in this session: what was done, where it lives, how it was checked,
and what is still open. The project isn't a git repository, so this file is the change log.

State at the end: **302 tests pass** (3 live-only skipped), pyflakes clean on every changed file,
and a hidden smoke start (temp data folder, no windows, no status bar) logged no errors.

---

## 1. Simulated test notification

- The button is now **"Show a test notification"** and injects a *simulated* notification
  (`notifications.TEST_ID = -1`) straight into NEON's own pipeline (`controller.test_notification`).
  Nothing is sent to Windows. `NotificationWatcher.remove()` and the flush code never dismiss negative ids.

## 2. Status bar

- **Notification card was see-through.** `NotificationStrip` is a plain `QWidget`, which ignores its style-sheet
  background unless it has `Qt.WA_StyledBackground`. It's a solid card now.
- **Clipped text:**
  - The state label was fixed at 78 px, so "Listening" was cut off at 20 px text. It now sizes to the
    longest state at the current text size (`_show_state_label`).
  - Spin boxes and combo boxes showed empty arrow squares, because the style sheet removed the native
    arrows. `ui/theme.py` now writes themed chevron SVGs (`_arrow()`, into `%TEMP%\neon-ui`) and styles
    the up/down buttons.
- **Media buttons:** `MediaButton` (vector previous / play / pause / next) beside now-playing. They use
  `backend.media_control`: Pear Desktop first, else the Windows media keys. The play icon flips at once.
  New setting `bar_show_media_buttons`.
- **Right-click menu:** a tick list of every bar element plus a "Buttons" submenu (`build_menu`, `set_element`).
  It writes via `persist_keys` and `controller.apply_live`. `ClickLabel` swallows the context-menu event so
  right-clicking the timer or stopwatch doesn't also open the menu.
- **Accessible names** on the orb, stopwatch, timer, now-playing, the bar buttons and the main window's
  icon buttons.

## 3. Wake words (new: `wake_training.py`, extended `wakeword.py`, `ui/wake_trainer.py`)

- **Detector without the openWakeWord package:** `wakeword.FeatureStream` runs openWakeWord's two shared
  feature models (`melspectrogram.onnx`, `embedding_model.onnx`) with onnxruntime, which Piper already
  installs. They're downloaded once, about 2.4 MB, from the openWakeWord v0.5.1 GitHub release into
  `<data>/wakeword/`.
- **Custom models:** `custom:<slug>` settings values; files at `<data>/wakeword/custom/<slug>.npz`. Each holds
  a 1536→32→1 network over 16 embeddings (1.28 s), plus a JSON `meta` (phrase, created, recording counts,
  held-back accuracy). A custom model must score over the threshold on **two frames in a row**.
- **Presets:** `PRESETS = {"hey_nova", "hey_dan"}` train themselves on first use (a few minutes, in the
  wake-loop thread).
- **Training data:**
  - Positive examples: every installed Piper voice saying the phrase at 4 speeds × 2 variations, plus
    pitch-shifted copies. The user's own recordings are weighted 3× with 10 augmentations each.
  - Negative examples: near misses built from the phrase (`confusables()`), everyday phrases, noise,
    back-to-back speech, cut-off halves of the phrase itself, and an optional room recording.
  - Hard negatives are counted 4× in a second training round. Training is full-batch Adam.
- **Multi-speaker voice:** `en_US-libritts_r-medium` (78 MB, ~900 speakers; 16 of them are used). The
  trainer offers it through "Get more voices".
- **Measured** (voice held out of training, a man's; 36 s of other speech):

  | Phrase | Training voices | Woke on the phrase | False wakes |
  | --- | --- | --- | --- |
  | "hey nova" | 3 standard voices | 2 of 3 | 0 |
  | "hey dan" | 3 standard voices | 1 of 3 | 1 |
  | "hey dan" | + the multi-speaker voice | 3 of 3 | 1 |

  Cost: about 1.9 ms per 80 ms frame. The user has 6 Piper voices installed (amy, cori, hfc_female, joe,
  lessac, ryan-high), so real training has more variety than these tests. **Not yet tested with the
  user's own voice or mic.**
- **Settings (Listening):** the engine is labelled "Wake-word detector". The phrase list shows trained /
  presets / openWakeWord built-ins, with "Train a wake word..." and "Delete this phrase" buttons.
  `settings_schema` has a new `("pattern", regex)` rule for `wake_model`. `wakeword.signature()` includes
  the model file's time, so retraining reloads it. `controller.hold_microphone()` pauses the listener while
  the trainer records; `controller.reload_wake_word()` picks up a new model.

## 4. Notifications read out loud

- `notify_ask` gained `"read"`, and per-app rules gained a "Read it out loud" mode.
  `notifications.RULE_MODES` / `_RANK` include `read`.
- `controller._read_notifications` waits up to 20 s if something is already being said, marks the
  notifications handled, and speaks `backend.read_aloud_text()`: "Discord says: Alex. Are you coming?".
  It reads the newest 3, then "And N more"; each notification is cut at about 300 characters.
- Quiet hours turn it into a silent card, as with the other modes.

## 5. Memory (`memory_store.py`, `ui/settings_widgets.MemoryList`)

- Every memory has a stable `id` and an `expires` time. New functions: `update()`, `forget_id()`, `_prune()`.
- **Expiry phrases:** `split_expiry()`, `expiry_time()`, `describe_expiry()`:
  - "remember until Friday that …", "remember for 2 hours that …", "… just for today", "… until tomorrow".
  - A trailing "for two hours" with no "just" or "only" stays part of the fact.
- **Settings browser:** an editable table (topic, fact, added, "forget it"), with search, forget selected,
  and forget everything.
- **Recall by meaning:** `set_embedder()` plus `assistant.ollama_embed()` / `configure_memory_recall()`,
  using the Ollama `/api/embed` endpoint and model `memory_embed_model` (default `nomic-embed-text`,
  **not pulled yet**: `ollama pull nomic-embed-text`). It's used only when word matching finds nothing.
  Vectors are cached in memories.json; failures back off for 120 s. Settings: `memory_semantic`,
  `memory_embed_model`.

## 6. Other suggestions implemented

- **Scheduled routines** (`routines.parse_trigger`, `describe_trigger`, `Scheduler`; `sysinfo.process_names`
  via Toolhelp32, about 7 ms). A routine's optional `"when"` runs it by itself: "weekdays at 9:00",
  "mon, wed and fri at 8am", "when discord starts", "at startup". There's a "when" box in the Routines
  editor; the controller starts the scheduler and runs routines on a thread
  (`backend.run_routine_by_name`).
- **Plugin hooks:** `plugins.emit()` with `on_reply(heard, reply)`, `on_notification(note)`,
  `on_timer(label, reminder)` and `on_routine(name, result)`. Each runs on its own thread and failures
  are contained. A plugin with only hooks now loads.
- **Per-persona voices:** `persona.VOICES` suggestions and `persona.voice_for()`, used by
  `PiperSpeaker._voice_choice`. Settings: `persona_auto_voice`, `persona_voices`. Only installed voices
  are ever used.
- **Noise-robust wake loop:** `stt.looks_like_steady_noise()` (spread of 20 ms frame loudness) skips steady
  noise before any transcription; Whisper segments are dropped by its own no-speech rule in wake mode.
  Setting `wake_skip_noise`.
- **High contrast:** themes `contrast` and `contrast_light` (7:1 or better). `theme.windows_high_contrast()`
  and `effective_theme()` follow Windows' Contrast themes while `follow_high_contrast` is on (checkbox on
  the Appearance page).
- **CI:** `.github/workflows/tests.yml` runs the tests on Windows with Python 3.13. `NEON_SKIP_SCREENSHOTS=1`
  skips pixel comparisons (the clipped-text audit still runs).
- **Split of `assistant.py`, first step:** the pure parsers (`spoken_rename`, `spoken_website`,
  `spoken_media_action`, `spoken_music_command`, `spoken_calendar_query`, `spoken_help_query` and their
  regexes) moved to **`commands.py`** and are re-exported from `assistant`.
- **Streaming replies into the quick box** already worked; nothing changed.

## 7. New settings keys (all in `DEFAULT_CONFIG`)

`bar_show_media_buttons`, `wake_skip_noise`, `follow_high_contrast`, `persona_auto_voice`,
`persona_voices`, `memory_semantic`, `memory_embed_model`; `notify_ask` gained `read`; `wake_model`
accepts `custom:<name>`; routines gained an optional `when`.

## 8. Tests and checks

- New file `tests/test_new_features.py` (32 tests) covers: wake-word models and the detector streak, the
  schema pattern, training helpers, the noise filter, memory expiry / editing / semantic recall / the
  browser, routine triggers and the scheduler, plugin hooks, persona voices, high-contrast contrast
  ratios, the opaque card, the state-label width, media buttons, the right-click menu, the simulated test
  notification, and read-aloud.
- Screenshot baselines were re-accepted for the settings Appearance, Persona, Memory and Routines pages
  after checking each by eye.
- Commands:
  - `python -m unittest discover -s tests -t . -v`
  - `NEON_UPDATE_BASELINES=1 python -m unittest tests.test_render`
- **Safe smoke run:** a temp `NEON_DATA_DIR` whose `assistant_config.json` has `start_minimized: true`,
  `show_status_bar: false` and `onboarding_done: true`. Run `python main.py --minimized` hidden, stop it
  after about 20 s, and read that folder's `neon.log`.

## 9. Still open (also in `suggestions.md`)

- Test a trained wake word with the user's real voice; add a "try it" live-score view to the trainer;
  cache synthesized training audio so retraining is faster.
- Now playing from any player (Windows' media session API).
- Private calendars (OAuth), replying from notifications, faster GPU recognition, typed settings.
- Finish splitting `assistant.py` (router, notification queue). A bar per monitor was left out on request.

## 10. Standing preferences from the user

- Make code changes without asking for confirmation.
- Still ask before anything that interrupts: windows on screen, speaking aloud, real toasts.
- Put improvement ideas found along the way in `suggestions.md`.
