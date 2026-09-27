# Handoff: carrying NEON on

Everything you need to keep working on this without the person or tool that wrote it. For what NEON
does and how to use it, see `GUIDE.md`; for installing it and each optional feature, `INSTALL.md`. For ideas not done yet, see `suggestions.md`.

## Run, test, build

| | |
| --- | --- |
| Run from source | `python main.py` (or double-click it) |
| All tests (~3 min) | `python -m unittest discover -s tests -t . -v` |
| One file | `python -m unittest tests.test_finish_pass -v` |
| Accept new screenshots | `NEON_UPDATE_BASELINES=1 python -m unittest tests.test_render`, then **look at** `tests/baselines/*.png` |
| Package the app | `pip install pyinstaller`, then `python build_exe.py` (build to a short path like `C:\nb`: deep paths break PyInstaller) |
| Package the extension | `python build_extension.py` → `dist/neon-bridge.xpi` |
| Try it without touching your setup | set `NEON_DATA_DIR` to an empty folder first (settings, memories, logs go there) |

The log is `neon.log` next to your settings (Settings → General → Open the log file). Almost every
problem shows up there.

## How a sentence is handled (assistant._route_utterance)

1. `_route_local`: the exact-wording parsers, in order: yes/no to a pending question, rename,
   persona, **close window**, **browser**, **Bitwarden**, window size, media, notifications, board,
   websites, music, timers, PC control, routines, calendar, help, memory, clipboard, units, fun,
   selected text, follow-ups, arithmetic, plugins.
2. The same parsers on `intent.normalize_utterance(text)` (fillers and politeness removed).
3. `intent.looks_like_fact_question` → `answer_fact` → `web_search_and_answer` (never the model's memory).
4. Small talk → `ollama_chit_chat`.
5. `intent.classify`: the model picks an intent from `intent.CATALOGUE`. A *template* intent is turned
   into a sentence and sent back through `_route_local`; a *call* intent runs `INTENT_CALLS[name]`.
6. Ollama unreachable → the Needle tool-picker, then chat.

**To add a command:** write a parser (a pure function in the feature's module), call it from
`_route_local`, and add an `intent.template(...)` whose sentence that parser accepts.
`tests/test_finish_pass.IntentCatalogueTests.test_every_template_parses` fails until you list the
parser there, which is on purpose.

## Where the new pieces live

| Feature | Files | Setting keys |
| --- | --- | --- |
| Natural speech | `intent.py`, `assistant._route_utterance` / `_run_intent` | `smart_commands` |
| Grounded facts, fact-check | `assistant.web_search_and_answer` / `fact_check`, `websearch.py` | `ground_facts`, `fact_check` |
| Close by name + outline | `windows.spoken_close_command` / `find_windows` / `close_windows`, `assistant.handle_close_command`, `ui/window_highlight.py` | `confirm_window_close` |
| Browser | `browser_bridge.py` (local server + token), `browser_commands.py`, `browser_extension/`, `assistant.handle_browser` | `browser_enabled`, `browser_port` |
| Bitwarden | `bitwarden.py`, `ui/unlock_dialog.py`, `assistant.handle_bitwarden` | `bitwarden_*` |
| Numbers and symbols spoken as words | `speech_text.words_for_speech` (called by `tts.clean_for_speech` and `SapiSpeaker._clean`) | — |
| Custom title bar | `ui/frame.py` (`install(self, ...)` at the end of each window's `__init__`; native hit-testing in `_FrameFilter`) | `custom_titlebar` |
| Model picker (+ downloads) | `ui/model_picker.py` (`SUGGESTED` is the curated list) | `ollama_model` |
| Welcome tour | `ui/onboarding.py` (steps in `_pages`; values gathered in `self._get`) | `onboarding_done` |
| Faster model for simple questions | `assistant.light_answer` (sorts chat vs. expert with `_JUDGE_PROMPT`, then answers or hands over), `ollama_answer(simple=...)` | `ollama_light_enabled`, `ollama_light_model` |
| Due times on the board | `board.split_due` / `_split_time` / `parse_due_at`, `due` is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`; `take_due_reminders` announces timed cards `AT_TIME_LEAD` early | — |
| Notifications missed in fullscreen | `notifications.DatabaseWatch` (reads `wpndatabase.db`, delivers after a 3 s grace if the listener didn't) | — |
| File search | `filesearch.py` (Everything's IPC over ctypes: `query_bytes` / `parse_list2`, per everything_ipc.h in voidtools' SDK), `assistant.handle_files` | `files_enabled`, `files_include_system` |
| Recent lookups | `lookup_cache.py` (newest 50 per kind, JSON beside the settings), used by `assistant._find`, `web_search_and_answer`, `geocode` | `lookup_cache` |
| Notification text clean-up | `notifications.strip_rules` / `strip_text` / `COMMON_STRIP_RULES`, `assistant._for_reading`, `ui/settings_widgets.StripRulesEditor` | `notify_strip` |
| Title bar switching | `ui/frame._apply` / `_refresh_native_frame`; hit test uses the message's own point (`tests/test_frame_live.py`, NEON_LIVE=1) | `custom_titlebar` |
| Speech detection | `vad.py` (Silero VAD from faster-whisper's assets, run with onnxruntime; `normalize_pcm`), `listening.Listener._voice_test` / `_finish`, `record_utterance(voice=...)` | `vad_enabled`, `mic_auto_gain` |
| Beep maker | `sounds.synth_beep` / `clean_recipe` / `PRESETS`, `ui/beep_maker.py`, `ui/sound_picker.SoundPicker._make` | `custom_beeps` |
| Your name | `commands.spoken_user_name`, `assistant.handle_user_name` / `user_name()` (system prompt, greeting) | `user_name` |
| Summary -> board card | `assistant.summarize_notifications` asks, `LOCAL_CONFIRMED["notif_card"]` = `_add_notification_card` | `notify_offer_card` |
| Smart home | `homeassistant.py` (`home_request` decides, `ask` calls /api/conversation/process), `assistant.handle_home`, Settings → Smart home; token in Credential Manager | `ha_enabled`, `ha_url`, `ha_confirm`, `ha_language` |
| Listening sound, "still thinking" lines | `controller._play_listen_sound` / `_start_thinking_filler`, `listening.Listener.on_listen` | `listen_sound*`, `thinking_*` |

Settings live in `assistant.DEFAULT_CONFIG` (keys, defaults, comments) and
`settings_schema.CONSTRAINTS` (ranges and choices). When a setting changes shape, bump
`CONFIG_VERSION` and add a step to `settings_schema.MIGRATIONS`.

## Rules that keep tests (and your PC) safe

`tests/common.use_temp_config()` gives every test a throwaway config, board, memories and clipboard
history. It also turns off everything that reaches outside the process: Pear Desktop, media players,
the AI's command reading, fact lookups, the browser bridge, Bitwarden, the listening sound. A test that
needs one of those turns it on and **stubs it**. Never, in a test:

- let `system_control.perform`, `dictation.type_text` / `press_keys`, `PostMessageW` (closing windows) or
  a real `hotkeys.KeyCapture` run: they act on whatever window you're using;
- call the real `bw` (stub `bitwarden._run`) or write to Credential Manager (stub `browser_bridge.token`);
- speak aloud or open a window on screen (tests run with `QT_QPA_PLATFORM=offscreen`).

## Known limits

- Stock Firefox won't keep an unsigned extension installed. Zen will once
  `xpinstall.signatures.required` is false. Unlisted signing through addons.mozilla.org
  (`web-ext sign`) would work everywhere.
- Browser pages the browser protects (`about:`, addons.mozilla.org) can't be read.
- Weather answers are for now, even when "tomorrow" is asked (the forecast isn't wired to a day).
- Qwen3 8B needs ~6 GB of VRAM. With a game running, Ollama may fall back to the CPU and answers get
  slower (the "still thinking" lines cover the wait). A smaller model can be picked in Settings → AI & chat.
