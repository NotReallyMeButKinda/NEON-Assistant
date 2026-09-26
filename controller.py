"""
controller.py -- bridges the backend (assistant.py) and the Qt UI.

All backend work happens on plain worker threads; the only way results reach
the UI is by emitting signals, which Qt delivers on the UI thread.
"""

from __future__ import annotations

import os
import random
import re
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

import app_paths
import assistant as backend
import bitwarden
import board
import browser_bridge
import calendar_feed
import clipboard
import dictation
import fun
import memory_store
import notifications
import persona
import plugins
import routines
import sounds
import sysinfo
import timers
import tts
import wakeword

NOTIFY_QUESTION_SECONDS = 45   # how long a spoken 'want a summary?' waits for a yes / no
NOTIFY_BATCH_SECONDS = 1.2    # notifications arriving within this window are announced together
STOP_PHRASES = {"stop", "stop talking", "shut up", "be quiet", "quiet"}
MIC_RESUME_DELAY = 0.45   # seconds of grace after speech ends before the mic un-mutes
CONVERSATION_PAUSE = 0.35  # settle time before the follow-up listen in conversation mode

# UI states: idle, listening, thinking, speaking, error


class Assistant(QObject):
    message = Signal(str, str)        # sender ("You" / the assistant's name / "system"), full text
    message_chunk = Signal(str, str)  # sender, next piece of a reply that is still streaming in
    message_end = Signal()            # the streamed reply is complete
    state = Signal(str)               # idle | listening | thinking | speaking | error
    status = Signal(str)              # short human-readable status line
    caption = Signal(str, str)        # sender, text -- for the overlay bar
    audio_level = Signal(float)       # 0..1 loudness of Nova's voice while speaking
    spectrum = Signal(list)           # 0..1 per frequency band, same cadence as audio_level
    location = Signal(str)
    ready = Signal()
    wake_changed = Signal(bool)
    name_changed = Signal(str)        # the assistant was renamed (settings or by voice)
    muted_changed = Signal(bool)      # microphone muted / unmuted
    quit_requested = Signal()         # the user said exactly "exit"
    settings_saved = Signal()         # Settings dialog saved -> UI pieces re-read the config
    onboarding_requested = Signal()   # "run the welcome tour again" (Settings > General)
    live_changed = Signal(list)       # setting keys that were changed and applied instantly (Settings)
    notification = Signal(dict)       # a Windows notification to show: latest's fields + "count" + "seconds"
    notification_settled = Signal()   # it was answered / dismissed: hide the card
    history_requested = Signal()      # show the recent-notifications panel
    timers_requested = Signal()       # show the Timers window
    board_requested = Signal()        # show the Board window
    board_changed = Signal()          # a card or column changed (from any thread): redraw the board
    effect = Signal(str)              # a bit of nonsense for the status bar to perform (see fun.EFFECTS)
    shade_preview = Signal()          # Settings > Status bar > "Show the shade"
    wiki_article = Signal(dict)       # an article a search found, for the Wikipedia pop-up
    persona_changed = Signal(str)     # the persona key changed (by voice or in Settings)
    dictation_changed = Signal(bool)  # dictation mode turned on / off
    highlight_windows = Signal(list, float)   # outline these windows (hwnds) for this many seconds
    highlight_clear = Signal()                # ...and take the outlines away
    bitwarden_unlock_requested = Signal()     # show the Bitwarden unlock box

    def __init__(self) -> None:
        super().__init__()
        self._agent = None
        self._speaker = None
        self._listener = None
        self._agent_lock = threading.Lock()
        self._speaker_lock = threading.Lock()
        self.wake_on = False
        self.is_ready = False
        self._ptt_active = False
        self._wake_signature = wakeword.signature(backend.CONFIG)
        self._hook_heard = ""                      # for the plugins' on_reply hook
        self._hook_reply = ""
        self.message.connect(self._hook_message)
        self.message_chunk.connect(self._hook_chunk)
        self.message_end.connect(self._hook_end)
        self.muted = False
        self._name = backend.assistant_name()

        # Windows notifications (see notifications.py)
        self._watcher: notifications.NotificationWatcher | None = None
        self._silence = notifications.SilenceState()
        self._notify_lock = threading.Lock()
        self._notif_batch: list[notifications.Notification] = []
        self._notif_timer: threading.Timer | None = None
        self._calendar = calendar_feed.CalendarAlerts(lambda: backend.CONFIG, self._on_calendar_alert)
        self._board_stop = threading.Event()            # ends the board-reminder loop
        self._board_checked = False                     # the first check comes a few seconds after start
        backend.TIMER_HOOKS["fire"] = self._on_timer_fired
        backend.TIMER_HOOKS["warn"] = self._on_timer_warning
        backend.ROUTINE_HOOKS["say"] = self._say_now
        backend.ROUTINE_HOOKS["keys"] = dictation.press_keys
        backend.SEARCH_HOOKS["article"] = self.wiki_article.emit     # from the search thread: queued to the UI
        backend.WINDOW_HOOKS["highlight"] = self.highlight_windows.emit
        backend.WINDOW_HOOKS["clear"] = self.highlight_clear.emit
        backend.STATUS_HOOKS["status"] = self.status.emit
        import filesearch
        filesearch.HOOKS["listing"] = lambda text: self.message.emit("system", text)   # found files, in full
        bitwarden.configure(backend.CONFIG)
        bitwarden.HOOKS["unlock"] = self.bitwarden_unlock_requested.emit

        # Dictation ("take dictation") and the clipboard history ("what did I copy?").
        self._dictation = dictation.Session()
        self._clipboard = clipboard.Watcher()
        self._persona = persona.valid(backend.CONFIG.get("persona"))
        self._conversation_until = 0.0     # keep listening without the wake word until this time

        # Drives the waveform/orb from the real audio level while Nova speaks.
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(33)
        self._level_timer.timeout.connect(self._emit_level)
        self.state.connect(self._on_state_changed)

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._startup, daemon=True).start()

    def bitwarden_unlocked(self, summary: str) -> None:
        """The unlock box succeeded: carry on with whatever was asked while the vault was locked."""
        self.status.emit(f"Bitwarden unlocked ({summary})")
        pending = bitwarden.take_pending()
        if pending:
            self.submit(pending, echo=False)
        else:
            self.message.emit("system", f"Bitwarden unlocked ({summary}).")

    def unlock_bitwarden(self) -> None:
        """Settings' Unlock button: open the vault without a password if bw allows it, else show the box."""
        def work() -> None:
            ok, summary = bitwarden.open_without_password()
            if ok:
                self.bitwarden_unlocked(summary)
            else:
                self.bitwarden_unlock_requested.emit()
        threading.Thread(target=work, name="Nova-BitwardenOpen", daemon=True).start()

    def lock_bitwarden(self) -> None:
        threading.Thread(target=bitwarden.lock, name="Nova-BitwardenLock", daemon=True).start()
        self.status.emit("Bitwarden locked")

    def apply_browser_setting(self) -> None:
        """Start or stop the browser bridge to match Settings > Browser (and pick up a new port)."""
        try:
            browser_bridge.stop()
            if backend.cfg_bool("browser_enabled"):
                if not browser_bridge.start(int(backend.cfg_num("browser_port"))):
                    self.message.emit("system", f"The browser link couldn't use port {int(backend.cfg_num('browser_port'))}; "
                                                "another program has it. Pick another in Settings > Browser.")
        except Exception as exc:  # noqa: BLE001 -- the rest of the assistant works without it
            self.message.emit("system", f"Couldn't start the browser link: {exc}")

    def shutdown(self) -> None:
        """Stop audio in both directions and give Windows its pop-ups back. Each part is isolated
        so one failing can't skip the others."""
        self._calendar.stop()
        try:
            browser_bridge.stop()
            bitwarden.lock()
        except Exception:  # noqa: BLE001
            pass
        self._board_stop.set()
        try:
            backend.MEDIA.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._clipboard.stop()
        except Exception:  # noqa: BLE001
            pass
        scheduler = getattr(self, "_routines", None)
        if scheduler is not None:
            scheduler.stop()
        try:
            if self._watcher is not None:
                self._watcher.stop()
            self._silence.release()
        except Exception:  # noqa: BLE001
            pass
        for part in (self._listener, self._speaker):
            if part is None:
                continue
            try:
                part.close() if hasattr(part, "close") else part.stop()
            except Exception:  # noqa: BLE001
                pass

    def _startup(self) -> None:
        # Load the language model into memory now, in parallel with everything below, so the
        # first question doesn't pay a multi-second cold start.
        threading.Thread(target=backend.warm_ollama, daemon=True).start()
        if backend.CONFIG_LOAD_ERROR:
            self.message.emit("system", backend.CONFIG_LOAD_ERROR)

        # Location is only needed for weather (which fetches it itself if this hasn't finished),
        # so readiness never waits for it.
        threading.Thread(target=self._locate, daemon=True).start()

        # The four real start-up jobs are independent, so run them side by side: the total is the
        # slowest one, not their sum. A failure is reported instead of silently killing start-up.
        self.message.emit("system", "Starting up...")
        results: dict[str, object] = {}
        failures: dict[str, Exception] = {}

        def job(name: str, fn) -> threading.Thread:
            def work() -> None:
                try:
                    results[name] = fn()
                except Exception as exc:  # noqa: BLE001
                    failures[name] = exc
            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            return thread

        workers = [
            job("apps", lambda: backend.build_app_catalogue(progress_cb=self.status.emit)),
            job("agent", backend.build_agent),
            job("speaker", self._build_speaker),
            job("listener", self._new_listener),
        ]
        for worker in workers:
            worker.join()

        for name, label in (("apps", "the app scan"), ("speaker", "the voice"), ("listener", "the microphone")):
            if name in failures:
                self.message.emit("system", f"Couldn't start {label}: {failures[name]}")
        if "agent" in failures:
            self.message.emit("system", f"Couldn't load the command model: {failures['agent']}. "
                                        "Run `needle fetch --generation 2` and restart.")
            self.status.emit("Startup failed")
            self.state.emit("error")
            return

        self._agent = results["agent"]
        self._listener = results.get("listener")
        if self._listener is not None:
            if self.muted:
                self._listener.mute()  # muted before startup finished
            if not self._listener.available:
                self.message.emit(
                    "system", "No microphone detected -- voice input disabled, typing still works.")

        self._start_extras()

        self.is_ready = True
        hello = f"Hi, {backend.user_name()}. " if backend.user_name() else ""
        self.message.emit(self.name, f"{hello}{persona.greeting(self._persona)} "
                                     f"Wake word is '{wakeword.label(backend.CONFIG)}'.")
        self.state.emit("idle")
        self.ready.emit()
        if self._silence.recover():
            self.message.emit("system", "Windows' notification pop-ups were left off by an earlier crash; "
                                        "I've turned them back on.")
        self.apply_notification_settings()
        self._restore_timers()
        self._calendar.start()
        threading.Thread(target=self._board_reminder_loop, name="Nova-BoardReminders", daemon=True).start()

    BOARD_CHECK_SECONDS = 30

    def _board_reminder_loop(self) -> None:
        """Once a day, at `board_remind_time` (or as soon as the assistant is up after it): the cards that
        are due or overdue, in one announcement. board.py remembers who was told, across restarts."""
        while not self._board_stop.wait(self.BOARD_CHECK_SECONDS if self._board_checked else 3):
            self._board_checked = True
            self.check_board_reminders()

    def check_board_reminders(self, now=None) -> None:
        if not (backend.cfg_bool("board_enabled") and backend.cfg_bool("board_reminders")):
            return
        from datetime import time as dtime
        at = notifications._parse_clock(backend.CONFIG.get("board_remind_time"), dtime(9, 0))
        try:
            due = board.take_due_reminders(now, at)
        except Exception as exc:  # noqa: BLE001 -- a damaged board must not kill the loop
            backend.log.warning("board reminders failed: %s", exc)
            return
        if due:
            self._announce("Board", board.reminder_text(due, (now.date() if now else None)), open_board=True)

    def _start_extras(self) -> None:
        """The pieces that only need the config: long-term memory, plugins, the clipboard history.
        Each is isolated -- a broken plugin folder must not stop the assistant from starting."""
        try:
            board.HOOKS["changed"] = self.board_changed.emit
            board.HOOKS["show"] = self.board_requested.emit
            board.enable_persistence(app_paths.data_path("board.json"))
        except Exception as exc:  # noqa: BLE001
            self.message.emit("system", f"Couldn't load your board: {exc}")
        try:
            memory_store.enable_persistence(app_paths.data_path("memories.json"))
            import lookup_cache
            lookup_cache.enable_persistence(app_paths.data_path("lookup_cache.json"))
            backend.configure_memory_recall()
            remembered = memory_store.count()
            if remembered:
                self.status.emit(f"{remembered} thing{'s' if remembered != 1 else ''} remembered")
        except Exception as exc:  # noqa: BLE001
            self.message.emit("system", f"Couldn't load what I'd remembered: {exc}")

        self.apply_browser_setting()

        if backend.cfg_bool("plugins_enabled"):
            try:
                plugins.set_api(say=self._say_now, run=self._run_plugin_command,
                                config=backend.CONFIG, data_dir=app_paths.DATA_DIR)
                count, problems = plugins.load()
                if count:
                    self.message.emit("system", f"Loaded {count} plugin{'s' if count != 1 else ''}.")
                for problem in problems:
                    self.message.emit("system", f"Plugin problem -- {problem}")
            except Exception as exc:  # noqa: BLE001
                self.message.emit("system", f"Couldn't load plugins: {exc}")

        self._apply_clipboard_setting()
        try:
            self._routines = routines.Scheduler(lambda: backend.CONFIG, self._run_scheduled_routine,
                                                sysinfo.process_names)
            self._routines.start()
        except Exception as exc:  # noqa: BLE001
            self.message.emit("system", f"Couldn't start scheduled routines: {exc}")

    # ---- plugin hooks ---------------------------------------------------------------------------
    def _hook_message(self, sender: str, text: str) -> None:
        if sender == "You":
            self._hook_heard = text
        elif sender not in ("system",) and self._hook_heard:
            plugins.emit("on_reply", self._hook_heard, text)
            self._hook_heard = ""

    def _hook_chunk(self, _sender: str, piece: str) -> None:
        self._hook_reply += piece

    def _hook_end(self) -> None:
        reply, self._hook_reply = self._hook_reply.strip(), ""
        if reply and self._hook_heard:
            plugins.emit("on_reply", self._hook_heard, reply)
            self._hook_heard = ""

    def _run_scheduled_routine(self, name: str) -> None:
        """A routine's "when" came round (called on the scheduler thread)."""
        def work() -> None:
            routine = routines.find(backend.CONFIG, name) or {}
            when = routines.describe_trigger(routines.parse_trigger(routine.get("when", "")))
            try:
                reply = backend.run_routine_by_name(name)
            except Exception as exc:  # noqa: BLE001
                reply = f"it failed ({exc})"
            if reply is not None:
                self.message.emit("system", f"Ran \"{name}\" ({when}): {reply}")
                plugins.emit("on_routine", name, reply)
        threading.Thread(target=work, name="Nova-Routine", daemon=True).start()

    def _apply_clipboard_setting(self) -> None:
        if backend.cfg_bool("clipboard_history"):
            self._clipboard.start()
        else:
            self._clipboard.stop()
            clipboard.clear()

    def _run_plugin_command(self, text: str) -> str:
        """What a plugin's `api.run(...)` calls: the assistant's own router, on this thread."""
        if self._agent is None:
            return ""
        with self._agent_lock:
            return backend.handle_utterance(self._agent, str(text))

    def _say_now(self, text: str) -> None:
        """Speak a line immediately (routine "say:" steps, plugins' api.say)."""
        speaker = self._speaker
        if speaker is not None and str(text).strip():
            speaker.say(str(text))

    def _restore_timers(self) -> None:
        """Timers set in an earlier run carry on; any that went off while closed are reported once."""
        timers.enable_persistence(app_paths.data_path("timers.json"))     # from here on, timers survive a restart
        for item in timers.restore_timers():
            note = timers.missed_announcement(item)
            self.message.emit("system", note)
            self.notification.emit({"id": 0, "app": "Reminder" if item["reminder"] else "Timer",
                                    "title": timers.timer_announcement(item["label"], item["seconds"], item["reminder"]).rstrip("."),
                                    "body": "",
                                    "count": 1, "seconds": 20.0, "ask": "none"})

    def _new_listener(self):
        """A microphone listener wired to the UI: state changes, live captions, and notes like
        "Loading the speech model..."; the offline speech model (if chosen) starts loading now."""
        listener = backend.Listener(on_status=self._on_mic_status, on_partial=self._on_partial_speech,
                                    on_message=self.status.emit)
        listener.on_listen = self._play_listen_sound
        listener.transcriber.prepare()
        return listener

    def _on_partial_speech(self, text: str) -> None:
        """What you've said so far (offline speech engine): shown live in the status bar."""
        self.caption.emit("You", text)

    def _location_settings(self) -> tuple:
        return str(backend.CONFIG.get("location_override", "")).strip(), backend.cfg_bool("location_from_ip")

    def _locate(self) -> None:
        self._located_with = self._location_settings()
        if backend.refresh_location():
            self.location.emit(backend._LOCATION["city"] or "")
        elif not backend.cfg_bool("location_from_ip"):
            self.location.emit("location not set")
        else:
            self.location.emit("location unavailable")

    def _build_speaker(self) -> None:
        with self._speaker_lock:
            old, self._speaker = self._speaker, None
            if old is not None:
                old.stop()
            self._speaker = backend.make_speaker(
                on_error=lambda exc: self.message.emit("system", f"TTS error: {exc}"),
                on_status=self.status.emit)
            if isinstance(self._speaker, backend.SapiSpeaker) and \
                    str(backend.CONFIG.get("tts_engine")).lower() == "piper":
                self.message.emit("system", "Using the Windows voice (Piper unavailable).")

    @property
    def name(self) -> str:
        return backend.assistant_name()

    def _sync_name(self) -> None:
        """Announce a rename (made in Settings or by asking) so the UI can update."""
        current = backend.assistant_name()
        if current != self._name:
            self._name = current
            self.name_changed.emit(current)

    # ---- inputs --------------------------------------------------------------
    def submit(self, text: str, echo: bool = True) -> None:
        """Send a typed / button-triggered command. echo=False keeps it out of the chat and the
        status bar (used for the notification card's buttons)."""
        threading.Thread(target=self._process, args=(text, echo), daemon=True).start()

    def set_muted(self, on: bool) -> None:
        """Mute = ignore the microphone completely, wake word included. Typing still works."""
        if on == self.muted:
            return
        self.muted = on
        if self._listener is not None:
            (self._listener.mute if on else self._listener.unmute)()
        self.muted_changed.emit(on)
        self.message.emit("system", "Microphone muted." if on else "Microphone unmuted.")
        self.status.emit("Microphone muted" if on else "Microphone on")

    def toggle_mute(self) -> None:
        self.set_muted(not self.muted)

    def push_to_talk(self) -> None:
        if self.muted:
            self.status.emit("Microphone is muted -- unmute to talk.")
            return
        if self._ptt_active:  # already listening (e.g. hotkey pressed twice)
            return
        self._ptt_active = True
        threading.Thread(target=self._push_to_talk_worker, daemon=True).start()

    def hotkey_talk(self) -> None:
        """Hotkey "talk" action: interrupt the assistant if it is speaking, then listen."""
        if self._speaker is not None and getattr(self._speaker, "is_speaking", False):
            self.stop_speaking()
        self.push_to_talk()

    def hold_start(self) -> None:
        """Hold-to-talk pressed: interrupt the assistant if it's talking and record for as long as the keys are held."""
        if self.muted:
            self.status.emit("Microphone is muted -- unmute to talk.")
            return
        if self._ptt_active:
            return
        if self._speaker is not None and getattr(self._speaker, "is_speaking", False):
            self.stop_speaking()
        listener = self._listener
        if listener is None or not listener.available:
            self.status.emit("Microphone unavailable -- type instead.")
            self.state.emit("error")
            return
        self._ptt_active = True
        self._hold_release = threading.Event()
        threading.Thread(target=self._hold_worker, args=(listener, self._hold_release), daemon=True).start()

    def hold_stop(self) -> None:
        """Hold-to-talk released: stop recording and process what was said."""
        release = getattr(self, "_hold_release", None)
        if release is not None:
            release.set()

    def _hold_worker(self, listener, release: threading.Event) -> None:
        try:
            text = listener.listen_hold(release)
        finally:
            self._ptt_active = False
        if not text:
            self.status.emit("Didn't catch that.")
            self.state.emit("error")
            return
        self._process(text)

    def stop_speaking(self) -> None:
        if self._speaker is not None:
            self._speaker.stop_speaking()

    def set_wake(self, on: bool) -> None:
        listener = self._listener
        if listener is None or not listener.available:
            self.status.emit("Microphone unavailable -- can't enable wake word.")
            self.state.emit("error")
            self.wake_changed.emit(False)
            return
        if on:
            self._wake_signature = wakeword.signature(backend.CONFIG)
            listener.start_wake_loop(lambda: backend.CONFIG["wake_word"], self._process)
        else:
            listener.stop_wake_loop()
        self.wake_on = on
        self.wake_changed.emit(on)

    def hold_microphone(self, on: bool) -> None:
        """Settings' wake-word trainer: stop reacting to the microphone while it records."""
        listener = self._listener
        if listener is not None:
            (listener.pause if on else listener.resume)()

    def reload_wake_word(self) -> None:
        """A wake phrase was (re)trained: pick the new model up if it's the one in use."""
        self._restart_wake_if_changed()

    def _restart_wake_if_changed(self) -> None:
        """A different wake-word engine or phrase was chosen in Settings: switch over without a restart."""
        listener = self._listener
        if not self.wake_on or listener is None or wakeword.signature(backend.CONFIG) == self._wake_signature:
            return

        def work() -> None:
            listener.stop_wake_loop(wait=True)
            self._wake_signature = wakeword.signature(backend.CONFIG)
            if self.wake_on and not self.muted:
                listener.start_wake_loop(lambda: backend.CONFIG["wake_word"], self._process)
            self.status.emit(f"Listening for \"{wakeword.label(backend.CONFIG)}\"")
        threading.Thread(target=work, daemon=True).start()

    def apply_settings(self, listener_changed: bool = False, engine_changed: bool = False,
                       ai_changed: bool = False) -> None:
        """Call after CONFIG has been updated and saved. Most settings are read live;
        only a new microphone / calibration or a different TTS engine needs a rebuild."""
        self._sync_name()
        self._sync_persona()
        self.apply_notification_settings()
        self._apply_clipboard_setting()
        backend.configure_memory_recall()
        self._restart_wake_if_changed()
        if self._location_settings() != getattr(self, "_located_with", None):
            backend._LOCATION.update(city=None, region=None, country_code=None, lat=None, lon=None)
            threading.Thread(target=self._locate, daemon=True).start()     # a new place, or IP lookup switched
        if self._listener is not None:
            self._listener.transcriber.prepare()      # a new speech engine / model starts loading now
        if ai_changed:
            threading.Thread(target=backend.warm_ollama, daemon=True).start()
        if listener_changed:
            self.status.emit("Switching microphone...")
            threading.Thread(target=self._rebuild_listener, daemon=True).start()
        if engine_changed:
            self.status.emit("Switching voice...")
            threading.Thread(target=self._build_speaker, daemon=True).start()

    def apply_live(self, keys: list) -> None:
        """Settings changed on a page that applies instantly. Most are read live by whoever uses
        them; a few need a nudge (a new voice engine is built, the bar repaints)."""
        if "tts_engine" in keys:
            self.status.emit("Switching voice...")
            threading.Thread(target=self._build_speaker, daemon=True).start()
        if "persona" in keys:
            self._sync_persona()
        if "clipboard_history" in keys:
            self._apply_clipboard_setting()
        self.live_changed.emit(list(keys))

    def reload_plugins(self) -> str:
        """Settings > Plugins > Reload, and the "reload plugins" command."""
        plugins.set_api(say=self._say_now, run=self._run_plugin_command, config=backend.CONFIG,
                        data_dir=app_paths.DATA_DIR)
        count, problems = plugins.load()
        for problem in problems:
            self.message.emit("system", f"Plugin problem -- {problem}")
        return (f"Loaded {count} plugin{'s' if count != 1 else ''}"
                + (f", {len(problems)} failed." if problems else "."))

    def preview_persona(self, key: str) -> None:
        """Settings > Persona: hear one without switching to it."""
        speaker = self._speaker
        if speaker is not None:
            speaker.say(persona.sample(key))

    def say_preview(self, text: str) -> None:
        """Speak `text` with the current voice (a preview from Settings)."""
        if self._speaker is not None and text.strip():
            self._speaker.say(text)

    def test_voice(self) -> None:
        if self._speaker is not None:
            self._speaker.say(f"Hi, I'm {self.name}. This is how I sound.")

    def rescan_apps(self) -> None:
        def work():
            self.message.emit("system", "Rescanning installed apps...")
            backend.build_app_catalogue(progress_cb=self.status.emit)
            self.message.emit("system", "App scan finished.")
            self.state.emit("idle")
        threading.Thread(target=work, daemon=True).start()

    # ---- internals -----------------------------------------------------------
    def _on_state_changed(self, state: str) -> None:  # runs on the UI thread
        if state == "speaking":
            self._level_timer.start()
        else:
            self._level_timer.stop()
            self.audio_level.emit(0.0)
            self.spectrum.emit([0.0] * tts.SPECTRUM_BANDS)

    def _emit_level(self) -> None:
        sp = self._speaker
        if sp is not None and getattr(sp, "has_level", False):
            self.audio_level.emit(float(sp.level))
            if getattr(sp, "has_bands", False):
                self.spectrum.emit(list(sp.bands))

    def _rebuild_listener(self) -> None:
        was_on = self.wake_on
        if self._listener is not None:
            self._listener.stop_wake_loop()
        self._listener = self._new_listener()
        if self.muted:
            self._listener.mute()
        if not self._listener.available:
            self.wake_on = False
            self.wake_changed.emit(False)
            self.status.emit("Microphone unavailable with that device.")
            self.state.emit("error")
            return
        if was_on:
            self._listener.start_wake_loop(lambda: backend.CONFIG["wake_word"], self._process)
        self.state.emit("idle")

    def _on_mic_status(self, kind: str) -> None:
        self.state.emit({"listening": "listening", "thinking": "thinking"}.get(kind, "idle"))

    def _push_to_talk_worker(self) -> None:
        try:
            listener = self._listener
            if listener is None or not listener.available:
                self.status.emit("Microphone unavailable -- type instead.")
                self.state.emit("error")
                return
            text = listener.listen_once()
        finally:
            self._ptt_active = False  # recording is over; replying below may take a while
        if not text:
            self.status.emit("Didn't catch that.")
            self.state.emit("error")
            return
        self._process(text)

    def _mute_mic(self) -> None:
        if self._listener is not None and backend.cfg_bool("mute_mic_while_speaking"):
            self._listener.pause()

    def _unmute_mic(self) -> None:
        if self._listener is not None:
            self._listener.resume(delay=MIC_RESUME_DELAY)

    # ---- Windows notifications --------------------------------------------------------
    def apply_notification_settings(self) -> None:
        """Start / stop the notification watcher and the optional pop-up silencing to match the
        settings. Safe to call any time (startup, after Settings or the onboarding saved)."""
        threading.Thread(target=self._apply_notifications, daemon=True).start()

    def _ignored_apps(self) -> list[str]:
        return [a.strip().lower() for a in str(backend.CONFIG.get("notify_ignore", "")).split(",") if a.strip()]

    def _apply_notifications(self) -> None:
        with self._notify_lock:
            if not backend.cfg_bool("notify_enabled"):
                if self._watcher is not None:
                    self._watcher.stop()
                self._silence.release()
                backend.NOTIF_HOOKS["remove"] = None
                return
            if self._watcher is None:
                self._watcher = notifications.NotificationWatcher(
                    self._on_new_notification, lambda m: self.message.emit("system", m), self._ignored_apps,
                    on_backlog=self._on_notification_backlog)
            self._watcher.start()
            backend.NOTIF_HOOKS["remove"] = self._watcher.remove
            if backend.cfg_bool("notify_silence"):
                if not self._silence.engaged:
                    ok, message = notifications.enable_silence(self._watcher, self._silence)
                    self.message.emit("system", message)
                    if not ok:                      # it didn't work: don't retry on every launch
                        backend.CONFIG["notify_silence"] = False
                        backend.save_config(backend.CONFIG)
            else:
                self._silence.release()

    def test_notification(self) -> str:
        """Settings > Notifications > "Show a test notification": a made-up notification handed
        straight to the same pipeline real ones go through (rules, card, sound, the spoken question)."""
        note = notifications.Notification(id=notifications.TEST_ID, app="Neon",
                                           title="Test notification",
                                           body="If you can see this in the status bar, notifications work.")
        self._on_new_notification(note)
        return "Shown in the status bar (a simulated notification: Windows isn't involved)."

    def _on_new_notification(self, note: notifications.Notification) -> None:
        """Reader-thread callback. Bursts are collected briefly so three toasts in a row become
        one announcement, not three interruptions."""
        with self._notify_lock:
            self._notif_batch.append(note)
            if self._notif_timer is None:
                self._notif_timer = threading.Timer(NOTIFY_BATCH_SECONDS, self._flush_notifications)
                self._notif_timer.daemon = True
                self._notif_timer.start()

    def _flush_notifications(self) -> None:
        with self._notify_lock:
            batch, self._notif_batch, self._notif_timer = self._notif_batch, [], None
        if not batch:
            return
        paused = backend.notifications_paused()
        decided = [(n, notifications.decide(n, backend.CONFIG, paused)) for n in batch]
        kept = [(n, d) for n, d in decided if d.action != "ignore"]
        for note, _d in kept:
            backend.note_notification(note.as_dict())        # remembered even when silent: "any notifications?" finds it
        for note, _d in kept:
            note.launch = note.launch or notifications.launch_uri(note.id)   # before it can leave the Action Center
            backend.set_notification_launch(note.id, note.launch)
        shown = [(n, d) for n, d in kept if d.action != "silent"]
        if not shown:
            return
        action = notifications.strongest([d for _n, d in shown])
        latest = shown[-1][0]
        if action in ("ask", "card"):
            backend.open_notification_question(NOTIFY_QUESTION_SECONDS)
        for note, _d in shown[-3:]:
            self.message.emit("system", f"Notification from {note.app}: {note.text}")
        for note, _d in shown:
            plugins.emit("on_notification", note.as_dict())
        self.notification.emit({**latest.as_dict(), "count": len(shown), "seconds": backend.cfg_num("notify_seconds"),
                                "ask": {"ask": "speech", "card": "card"}.get(action, "none"), "can_open": True})
        sound = str(backend.CONFIG.get("notify_sound", "ping"))
        if sound and sound != "off" and any(d.sound for _n, d in shown):
            sounds.play(sound, backend.cfg_num("ack_volume"), backend.CONFIG.get("output_device") or None,
                        str(backend.CONFIG.get("notify_sound_file", "")))
        if backend.cfg_bool("notify_clear") and self._watcher is not None:
            for note, _d in shown:
                if note.id >= 0:                 # a simulated one has no Action Center entry
                    self._watcher.remove(note.id)
        if action == "ask":
            self._ask_about_notifications([n for n, _d in shown])
        elif action == "summarize":
            self._read_notifications([n.as_dict() for n, d in shown if d.action == "summarize"], summarize=True)
        elif action in ("read", "message"):
            self._read_notifications([{**n.as_dict(), "message": d.action == "message"}
                                      for n, d in shown if d.action in ("read", "message")])

    def _read_notifications(self, batch: list, summarize: bool = False) -> None:
        """Read new notifications out loud as they arrive (notify_ask = "read" / "message", or such a rule).
        `batch` holds notification dicts; "message": True reads just 'Alex says <message>'. summarize=True
        says a short AI summary instead (notify_ask = "summarize": "want a summary?" without the question).
        If something else is being said, it waits for it to finish rather than talking over it."""
        speaker = self._speaker
        if speaker is None or not batch:
            return
        text = backend.summarize_notifications(batch) if summarize else backend.read_aloud_text(batch)
        deadline = time.monotonic() + 20
        while getattr(speaker, "is_speaking", False) and time.monotonic() < deadline:
            time.sleep(0.2)
        backend.dismiss_notifications()              # read = handled: "summarize my notifications" skips them
        self._mute_mic()
        try:
            speaker.say(text).wait(timeout=60)
        finally:
            self._unmute_mic()
        self.notification_settled.emit()
        if (backend.pending_confirmation() == "notif_card" and not self.muted and not self._ptt_active
                and self._listener is not None and self._listener.available):
            self._conversation_until = time.monotonic() + MIC_RESUME_DELAY + max(2.0, backend.cfg_num("conversation_seconds"))
            time.sleep(MIC_RESUME_DELAY)             # let the mic un-pause after our own voice
            threading.Thread(target=self._conversation_listen, daemon=True).start()

    def _on_notification_backlog(self, notes: list) -> None:
        """What was already waiting in the Action Center when the watcher started: mentioned once,
        quietly (a card and a chat line: no sound and no spoken question at startup)."""
        if not backend.cfg_bool("notify_catchup"):
            return
        cutoff = time.time() - 24 * 3600
        recent = [n for n in notes if n.created == 0 or n.created >= cutoff][-10:]
        paused = backend.notifications_paused()
        kept = [(n, notifications.decide(n, backend.CONFIG, paused)) for n in recent]
        kept = [(n, d) for n, d in kept if d.action != "ignore"]
        for note, _d in kept:
            note.launch = note.launch or notifications.launch_uri(note.id)
            backend.note_notification(note.as_dict())
        shown = [(n, d) for n, d in kept if d.action != "silent"]
        if not shown:
            return
        apps = sorted({n.app for n, _d in shown})
        listing = ", ".join(apps[:3]) + (f" and {len(apps) - 3} more" if len(apps) > 3 else "")
        self.message.emit("system", f"While I wasn't running you got {len(shown)} notification"
                                    f"{'s' if len(shown) != 1 else ''} ({listing}). Say \"summarize my notifications\".")
        backend.open_notification_question(NOTIFY_QUESTION_SECONDS)
        latest = shown[-1][0]
        self.notification.emit({**latest.as_dict(), "count": len(shown), "seconds": backend.cfg_num("notify_seconds"),
                                "ask": "card", "can_open": True})

    def _on_timer_fired(self, label: str, seconds: float, reminder: bool) -> None:
        """A timer / reminder went off (called from its timer thread)."""
        plugins.emit("on_timer", label, reminder)
        self._announce("Reminder" if reminder else "Timer", backend.timer_announcement(label, seconds, reminder))

    def _on_timer_warning(self, text: str) -> None:
        """A minute or so before a long timer ends: a quiet spoken heads-up, no card, no sound."""
        self.message.emit("system", text)
        self.status.emit(text)
        speaker = self._speaker
        if speaker is not None and not getattr(speaker, "is_speaking", False):
            self._mute_mic()
            try:
                speaker.say(text).wait(timeout=20)
            finally:
                self._unmute_mic()

    def _on_calendar_alert(self, event, text: str) -> None:
        """An event is about to start (called from the calendar watcher thread)."""
        self._announce("Calendar", text)

    def _announce(self, app: str, what: str, open_board: bool = False) -> None:
        """Something the user asked to be told about: a card, a sound, the chat and the voice.
        open_board: the card's Open button shows the board (a due-card reminder)."""
        self.message.emit("system", what)
        self.notification.emit({"id": 0, "app": app, "title": what.rstrip("."), "body": "", "count": 1,
                                "seconds": 20.0, "ask": "none", "can_open": open_board, "open_board": open_board})
        sound = str(backend.CONFIG.get("notify_sound", "ping")) or "chime"
        sounds.play("chime" if sound == "off" else sound, backend.cfg_num("ack_volume"),
                    backend.CONFIG.get("output_device") or None, str(backend.CONFIG.get("notify_sound_file", "")))
        if self._speaker is not None:
            self._speaker.say(what)

    def pause_notifications(self, seconds: float) -> None:
        backend.pause_notifications(seconds)
        self.message.emit("system", f"Notifications paused for {backend._spoken_duration(seconds)}.")

    def resume_notifications(self) -> None:
        backend.resume_notifications()
        self.message.emit("system", "Notifications resumed.")

    def _ask_about_notifications(self, batch: list) -> None:
        """Say "want a summary?" and then listen for the answer (no wake word needed)."""
        speaker = self._speaker
        if speaker is None or getattr(speaker, "is_speaking", False) or self._ptt_active:
            return                                   # busy talking: the card is still there
        question = (f"New notification from {batch[-1].app}. Want a summary?" if len(batch) == 1
                    else f"{len(batch)} new notifications. Want a summary?")
        self._mute_mic()
        try:
            speaker.say(question).wait(timeout=30)
        finally:
            self._unmute_mic()
        if self.muted or self._listener is None or not self._listener.available:
            return
        time.sleep(MIC_RESUME_DELAY + 0.15)         # let the mic un-pause after our own voice
        self.push_to_talk()

    def answer_notification(self, accept: bool) -> None:
        """The card's Summarize / Dismiss buttons."""
        if accept:
            self.submit("summarize that notification", echo=False)
        else:
            backend.dismiss_notifications()
        self.notification_settled.emit()

    def show_notification_history(self) -> None:
        self.history_requested.emit()

    def show_timers(self) -> None:
        self.timers_requested.emit()

    def show_board(self) -> None:
        self.board_requested.emit()

    def start_timer_from_ui(self, seconds: float, label: str = "") -> str:
        """The Timers window's buttons: the same timer "set a timer for ..." makes."""
        reply = backend.start_timer(seconds, label)
        self.message.emit("system", reply)
        return reply

    def summarize_one_notification(self, n: dict) -> None:
        """The history panel's Summarize: that notification alone, in the chat and out loud."""
        backend.mark_notification_handled(n.get("id"))

        def work() -> None:
            text = backend.summarize_notifications([n])
            self.message.emit(self.name, text)
            self._say_now(text)
        threading.Thread(target=work, name="Nova-NotifSummary", daemon=True).start()

    def remind_about_notification(self, n: dict, seconds: float) -> None:
        backend.mark_notification_handled(n.get("id"))
        self.message.emit("system", backend.remind_about_notification(n, seconds))

    def open_notification_app(self, n: dict) -> None:
        """The card's Open button: what the notification itself opens when clicked (a chat, a page)
        if it says; otherwise the app it came from. Its AppUserModelId launches it exactly (Store and
        desktop apps alike); without one, the app is found by name."""
        aumid, app = str(n.get("aumid") or "").strip(), str(n.get("app") or "").strip()
        backend.dismiss_notifications()
        self.notification_settled.emit()
        if n.get("open_board"):
            self.show_board()
            return
        link = notifications.safe_launch(str(n.get("launch") or ""))
        if link:
            try:
                os.startfile(link)  # noqa: S606 -- the link the toast opens itself (blocked schemes excluded)
                return
            except OSError as exc:
                backend.log.info("couldn't open the notification's link (%s); opening the app", exc)
        if aumid:
            try:
                os.startfile("shell:AppsFolder\\" + aumid)  # noqa: S606 -- the sender's own app id
                return
            except OSError as exc:
                backend.log.info("couldn't open %s by its app id (%s); trying by name", aumid, exc)
        if not app or app.lower() == "an app":
            self.message.emit("system", "I don't know which app sent that notification.")
            return
        threading.Thread(target=lambda: self.message.emit("system", backend.launch_app(app)),
                         name="Nova-OpenNotifApp", daemon=True).start()

    # ---- "I heard you" acknowledgement ------------------------------------------
    def _play_ack(self) -> None:
        """Plays the configured acknowledgement: a built-in sound, or a spoken message."""
        kind = str(backend.CONFIG.get("ack_type", "sound")).strip().lower()
        if kind == "sound":
            sounds.play(str(backend.CONFIG.get("ack_sound", sounds.DEFAULT_SOUND)),
                        backend.cfg_num("ack_volume"), backend.CONFIG.get("output_device") or None,
                        str(backend.CONFIG.get("ack_sound_file", "")))
        elif kind == "speech":
            text = str(backend.CONFIG.get("ack_text", "")).strip()
            if text and self._speaker is not None:
                self._speaker.say(text)  # queued, so the real reply follows it in order

    def _start_ack(self):
        """Fires the acknowledgement right away, or after `ack_delay` seconds if one is set
        (then it is a filler for *slow* answers; the returned timer is cancelled when the
        answer starts arriving). Returns that timer, or None."""
        if str(backend.CONFIG.get("ack_type", "sound")).strip().lower() not in ("sound", "speech"):
            return None
        delay = max(0.0, backend.cfg_num("ack_delay"))
        if delay <= 0:
            self._play_ack()
            return None
        timer = threading.Timer(delay, self._play_ack)
        timer.daemon = True
        timer.start()
        return timer

    def test_ack(self) -> None:
        """Preview from Settings."""
        self._play_ack()

    # ---- "I'm listening" sound, and something to say while a slow answer is worked out ------------
    def _play_listen_sound(self) -> None:
        """A short sound as the microphone starts listening for a command (after the wake word, the
        talk button or a hotkey). Settings > Voice & sounds."""
        sound = str(backend.CONFIG.get("listen_sound", "blip")).strip()
        if not sound or sound == "off":
            return
        sounds.play(sound, backend.cfg_num("listen_sound_volume"), backend.CONFIG.get("output_device") or None,
                    str(backend.CONFIG.get("listen_sound_file", "")))

    def test_listen_sound(self) -> None:
        self._play_listen_sound()

    def _thinking_line(self) -> str:
        """One of the user's "still thinking" lines, not the same one twice in a row."""
        lines = [str(x).strip() for x in backend.CONFIG.get("thinking_lines") or [] if str(x).strip()]
        if not lines:
            return ""
        last = getattr(self, "_last_thinking_line", "")
        choices = [x for x in lines if x != last] or lines
        line = random.choice(choices)
        self._last_thinking_line = line
        return line

    def _say_thinking_line(self, done: threading.Event) -> None:
        if done.is_set():
            return
        line = self._thinking_line()
        if not line or self._speaker is None or getattr(self._speaker, "is_speaking", False):
            return
        self.status.emit(line)
        self._speaker.say(line)            # queued: the real reply follows it in order

    def _start_thinking_filler(self, done: threading.Event):
        """If the answer takes longer than `thinking_after` seconds, say one of the "still thinking" lines
        (Settings > Voice & sounds). The returned timer is cancelled as soon as the answer starts."""
        if not backend.cfg_bool("thinking_lines_enabled"):
            return None
        timer = threading.Timer(max(0.5, backend.cfg_num("thinking_after")), self._say_thinking_line, args=(done,))
        timer.daemon = True
        timer.start()
        return timer

    def _report_timing(self, received_at: float, started: dict) -> None:
        """Optional diagnostic line: how long from receiving the message to the reply starting."""
        if not backend.cfg_bool("show_timings") or "at" not in started:
            return
        if backend._ROUTE.get("direct"):
            lane = "direct command"
        else:
            lane = {"chat": "fast chat lane"}.get(backend._ROUTE.get("lane"), "tool-picker")
        self.message.emit("system", f"timing: reply started {started['at'] - received_at:.2f} s "
                                    f"after the message arrived ({lane})")

    # ---- dictation ("take dictation": what you say is typed into the focused window) ----------
    @property
    def dictating(self) -> bool:
        return self._dictation.active

    def set_dictation(self, on: bool) -> None:
        note = self._dictation.start() if on else self._dictation.stop()
        self.message.emit("system", note)
        self.status.emit("Dictation on" if on else "Dictation off")
        self.dictation_changed.emit(bool(on))
        if on:
            speaker = self._speaker
            if speaker is not None:
                speaker.say("Dictation on.")

    def _handle_dictation(self, text: str, echo: bool) -> bool:
        """True if `text` was consumed by dictation (either a control phrase or typed out)."""
        lowered = re.sub(r"[.!?,]+$", "", text.lower()).strip()
        if self._dictation.active:
            if echo:
                self.caption.emit("You", text)
            was_on = True
            note = self._dictation.feed(text)
            if note:
                self.status.emit(note)
            if was_on and not self._dictation.active:
                self.message.emit("system", "Dictation off.")
                self.dictation_changed.emit(False)
            return True
        if dictation.START.match(lowered):
            self.set_dictation(True)
            return True
        match = dictation.ONE_SHOT.match(lowered)
        if match:
            typed = dictation.transform(match.group("what"), leading_space=False)
            ok = dictation.type_text(typed)
            if echo:
                self.message.emit("You", text)
            self.message.emit("system", f"Typed: {typed}" if ok
                              else "I couldn't type into the focused window.")
            return True
        return False

    def _after_reply(self, reply: str) -> None:
        """Everything that happens once an answer exists: remember it for "copy that", run any
        visual effect the answer asked for, and keep listening if conversation mode is on."""
        clipboard.note_reply(reply)
        effect = fun.pending_effect()
        if effect:
            self.effect.emit(effect)
        self._sync_persona()
        # Conversation mode, or a question of mine waiting on a yes / no ("add that to your board?"):
        # listen once more without the wake word.
        if ((backend.cfg_bool("conversation_mode") or backend.pending_confirmation())
                and not self.muted and not self._ptt_active):
            self._conversation_until = time.monotonic() + max(2.0, backend.cfg_num("conversation_seconds"))
            threading.Thread(target=self._conversation_listen, daemon=True).start()

    def _conversation_listen(self) -> None:
        """After a reply, listen once more without needing the wake word -- so "what about
        tomorrow?" works as a follow-up. One turn only; say nothing and it goes quiet again."""
        listener = self._listener
        if listener is None or not listener.available or self.muted:
            return
        time.sleep(CONVERSATION_PAUSE)
        if self._ptt_active or time.monotonic() > self._conversation_until:
            return
        self._ptt_active = True
        try:
            self.status.emit("Still listening...")
            text = listener.listen_once(timeout=max(2.0, backend.cfg_num("conversation_seconds")))
        except Exception:  # noqa: BLE001 -- a follow-up listen is a nicety, never a failure
            text = None
        finally:
            self._ptt_active = False
        if text:
            self._process(text)
        else:
            self.state.emit("idle")

    def _sync_persona(self) -> None:
        current = persona.valid(backend.CONFIG.get("persona"))
        if current != self._persona:
            self._persona = current
            self.persona_changed.emit(current)

    def _process(self, text: str, echo: bool = True) -> None:
        text = text.strip()
        if not text:
            return

        if self._handle_dictation(text, echo):
            self.state.emit("idle")
            return

        received_at = time.perf_counter()
        if echo:
            self.message.emit("You", text)
            self.caption.emit("You", text)
        if backend.spoken_notification_command(text) is not None:
            self.notification_settled.emit()     # answering a notification: its card is done

        if text.lower().strip(" .!") in STOP_PHRASES:
            self.stop_speaking()
            self.message.emit("system", "TTS stopped")
            self.state.emit("idle")
            return

        # Exactly "exit" (and nothing else) closes this assistant. "Close this app" and the
        # like are handled by the backend and close the focused app instead.
        if text.lower().strip(" .!?,") == "exit":
            self.message.emit(self.name, "Goodbye.")
            if self._speaker is not None:
                self._speaker.say("Goodbye.").wait(timeout=5)
            self.quit_requested.emit()
            return

        if self._agent is None:
            self.message.emit(self.name, "Still starting up -- one second.")
            return

        self.state.emit("thinking")
        ack_timer = self._start_ack()   # "I heard you": a sound or a short spoken message
        answered = threading.Event()
        filler = self._start_thinking_filler(answered)   # "Hmm, let me think..." if it's slow
        speaker = self._speaker
        name = self.name
        streamed = {"utt": None, "text": ""}
        started = {}    # when the reply began (for the optional timing readout)

        def sink(piece: str) -> None:
            """Receives Ollama output as it is generated: shows it and starts speaking
            the first sentence while the model is still writing the rest."""
            answered.set()
            if filler is not None:
                filler.cancel()
            if ack_timer is not None:
                ack_timer.cancel()      # the answer is already arriving; no need to stall-fill
            started.setdefault("at", time.perf_counter())
            if streamed["utt"] is None and speaker is not None:
                streamed["utt"] = speaker.begin_stream()
                self._mute_mic()
                self.state.emit("speaking")
            streamed["text"] += piece
            self.message_chunk.emit(name, piece)
            self.caption.emit(name, streamed["text"].strip())
            if streamed["utt"] is not None:
                streamed["utt"].feed(piece)

        backend._STREAM["sink"] = sink
        try:
            with self._agent_lock:
                reply = backend.handle_utterance(self._agent, text)
                self._agent.reset()
        except Exception as exc:  # noqa: BLE001
            reply = f"Something went wrong handling that: {exc}"
        finally:
            backend._STREAM["sink"] = None
            answered.set()
            if filler is not None:
                filler.cancel()
            if ack_timer is not None:
                ack_timer.cancel()
        self._sync_name()      # the request may have been "call yourself ..."
        name = self.name       # so the reply is labelled with the new name

        if streamed["text"]:
            self.message_end.emit()
            utt = streamed["utt"]
            if utt is not None:
                utt.finish()
                utt.done.wait(timeout=180)
                self._unmute_mic()
            self._report_timing(received_at, started)
            self.state.emit("idle")
            self._after_reply(streamed["text"])
            return

        self.message.emit(name, reply)
        self.caption.emit(name, reply)
        started.setdefault("at", time.perf_counter())
        if speaker is not None:
            self._mute_mic()
            self.state.emit("speaking")
            # Runs off the UI thread, so waiting out the playback is fine and
            # keeps the "speaking" state accurate for exactly as long as it lasts.
            speaker.say(reply).wait(timeout=180)
            self._unmute_mic()
        self._report_timing(received_at, started)
        self.state.emit("idle")
        self._after_reply(reply)
