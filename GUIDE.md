# NEON Assistant

A desktop voice assistant for Windows, and for Linux (Arch, on Hyprland or KDE Plasma). Say "hey nova" (or
press a hotkey, or type) and ask for something: open an app, set a timer, look up a fact, read your
notifications, find a file, add a card to your board, pause the music. It answers out loud in a natural
voice and shows the reply in a slim status bar.

Speech recognition, the voice and the AI all run on your own PC by default: nothing you say leaves it
unless you choose an online option.

**Installing:** see [INSTALL.md](INSTALL.md). It covers the program itself and every optional feature.

---

## Contents

- [Using it](#using-it)
- [What you can say](#what-you-can-say)
- [The interface](#the-interface)
- [Making it yours](#making-it-yours)
- [Privacy](#privacy)
- [How it works](#how-it-works)
- [For developers](#for-developers)

---

## Using it

**Start it** by running `python main.py` (or the packaged `NeonAssistant.exe`). The first time, a short
welcome tour sets up your names, microphone, voice, AI model, look, and shortcuts. Everything in it can
be changed later in *Settings*.

**Talk to it** in any of these ways:

- **The wake word.** Say "hey nova", then your request: "hey nova, what's the weather". A sound tells
  you it's listening.
- **A hotkey.** *Settings → Hotkeys* can set keys to start listening, hold to talk, open the quick box,
  mute the microphone, and more. The Copilot key can be one of them.
- **Typing.** The main window has a text box, and the quick box is a small command line that floats
  over anything, even a fullscreen game.
- **The tray icon and the status bar buttons.**

**It answers** out loud, in the status bar across the top of your screen, and in the main window's
transcript. When the status bar is hidden or something is fullscreen, the reply drops down from the top
of the screen instead ("the shade").

**Stop it** talking with the Stop button, the tray menu, or by starting a new request. Say "exit" or use
the tray menu to quit.

You don't need exact wording. When a sentence doesn't match a known command, the local AI works out
which command you meant ("shut the music up for a sec" is a pause, "give me ten on the clock" a timer).

---

## What you can say

**Apps, windows and websites**
`open Chrome` · `open github.com` · `close Discord` · `close all Chrome windows` · `close this app` ·
`minimize everything` · `maximize Chrome`. Closing asks first, and draws a glowing outline around the
window it means.

**Questions**
`when did Hollow Knight Silksong come out` · `who directed Dune Part Two` · `how are you` ·
`explain that in more detail`. Questions of fact are always looked up (Wikipedia, Wikidata and the web),
never answered from the AI's memory. Recent answers are reused for a while, so asking twice is instant.

**Files** (with [Everything](INSTALL.md#file-search-everything))
`find the file called resume` · `where did I save my budget spreadsheet` · `find my screenshots folder` ·
`search my PC for invoice` · `open the pdf called tax return` · then `open the second one` ·
`show it in the folder`. Programs and scripts are never run from a search; they're shown in their folder.

**The board** (a Trello-style to-do board)
`make a card for dinner with Mom at 4pm tomorrow` · `add pay rent to to do due friday` ·
`dentist is due monday at 9:30` · `move pay rent to done` · `mark pay rent as done` ·
`what's due this week` · `anything overdue` · `what's on my board` · `add a column called ideas` ·
`open my board`

**Timers and reminders**
`set a timer for ten minutes` · `remind me in an hour to call Sam` · `how much time is left` ·
`cancel my timers`. They survive a restart, warn you a minute before, and count down in the status bar.

**Notifications and calendar**
`any notifications?` · `summarize my notifications` · `summarize that notification` ·
`pause notifications for an hour` · `what's my next meeting`. After a summary, NEON offers to put it on
your board, with the day and time it mentioned as the due date.

**Music**
`what's playing` · `pause the music` · `next song` · `skip ahead 30 seconds` · `like this song` ·
`play Glass Beach on YouTube Music` · `play the album Nurture by Porter Robinson` · `play my chill playlist`
(Pear Desktop; other players through the media keys). An album or playlist plays in full, in order, right
after the current song. If Pear Desktop is closed, NEON offers to open it and then plays what you asked for.

**The browser** (Chrome, Edge, Brave, Firefox or Zen, with the [NEON extension](INSTALL.md#the-browser-extension))
`summarize this page` · `what does this page say about shipping` · `read this article` ·
`find refund policy on this page` · `what tabs do I have open` · `switch to the GitHub tab` ·
`close this tab`

**Passwords** (with [Bitwarden](INSTALL.md#passwords-bitwarden))
`what's my username for Proton Mail` · `copy my GitHub password` · `type my Proton password` (asks
first) · `what's my GitHub 2FA code` · `lock Bitwarden`. Passwords are never said out loud.

**Smart home** (with [Home Assistant](INSTALL.md#smart-home-home-assistant), optional)
`turn off the kitchen lights` · `set the thermostat to 70` · `close the blinds` · `is the front door locked` ·
`what's the temperature in the bedroom` · `start the vacuum`. Unlocking and opening the garage ask first.

**Your PC**
`set the volume to 40 percent` · `mute the system` · `take a screenshot` · `lock the PC` ·
`how much disk space is left` · `shut down the computer` (asks first)

**Time, weather, maths and conversions**
`what's the weather in Lisbon` · `what time is it in Tokyo` · `what's 12 times 34` ·
`convert 5 miles to kilometres` · `what's 180 F in C` · `convert 255 to hex`

**Things to remember**
`remember that my gate code is 4821` then `what's my gate code` · `remember I'm vegetarian` (every later
answer knows) · `remember until Friday that my locker is 12` · `what do you remember` ·
`forget my gate code`

**Selected text** (highlight it in any app first)
`summarize this` · `read this aloud` · `explain the selected text`

**The clipboard**
`what did I copy` · `what did I copy before that` · `copy that` · `clear my clipboard history`

**Dictation**
`take dictation`: everything you say is typed into the focused window, with spoken punctuation
("comma", "new paragraph") and "scratch that" to take back the last phrase. `type this: hello world`
types one line.

**Names and personas**
`call me Alex` · `what's my name` · `call yourself Jarvis` · `be a pirate` · `talk like Shakespeare` ·
`be unsure` · `act normal` · `what personas do you have`

**Routines**
`run work mode`, or just `work mode`: one phrase, several steps (see [Routines](#routines)).

**For fun**
`flip a coin` · `roll a d20` · `pick pizza or curry` · `tell me a joke` · `do a barrel roll` ·
`party mode` · `the matrix`

**Help and housekeeping**
`what can you do` · `reload plugins` · `exit`

---

## The interface

**Status bar.** A slim bar across the top (or bottom) of a monitor, reserved like the taskbar so
maximized windows sit below it. It shows what it heard and the reply, and optionally a clock, the
weather, CPU / memory / battery, a timer countdown, the next calendar event, what's playing with media
buttons, and more. Right-click it to choose what's shown. It steps aside when a game or video goes
fullscreen.

**On KDE Plasma: a panel widget.** The orb and the captions can live in your Plasma panel instead, as the
NEON Assistant widget: click to talk, middle-click to mute, and its pop-up has the recent lines and the
buttons. Install it from *Settings → Status bar → Panel widget* (see [INSTALL.md](INSTALL.md)).

**Notification cards.** New Windows notifications drop in over the status bar with Summarize and Open
buttons and an ✕ to close them. Depending on your settings, NEON asks whether to summarize, summarizes
straight away, reads them, or reads just "Alex says ...". Per-app rules, quiet hours and "always let
through" words are in *Settings → Notifications*. Notifications that Windows hides while you're in a
fullscreen game still reach NEON.

**Main window.** The conversation, with replies streaming in as they are written, and buttons for the
board, timers, notifications and settings.

**Quick box.** A borderless text box on a hotkey; the answer appears under it. It works over fullscreen
games without taking focus away from them.

**Board** (🗂). Columns and cards with due dates and times. Add cards by typing ("dinner with Mom at 4pm
tomorrow" sets the date and time), drag them between columns, double-click to edit, right-click for quick
dates. Cards are announced on the morning they're due, and ten minutes before a set time.

**Timers** (⏲). One-click timers, or type one ("25", "1:30", "half an hour") with a name.

**Recent notifications** (🔔). The last 20, with Open, Summarize and Remind me.

**Settings.** Seventeen pages with a search box. Rows that don't apply to your current choices are greyed
out. The Appearance, Status bar, Voice and Persona pages apply as you click; the rest when you press Save.
Export and import move everything through one file.

**Title bar.** NEON's windows use their own dark title bar by default (with Windows' snapping, shadows
and shortcuts intact); *Settings → Appearance* switches back to Windows' own.

---

## Making it yours

### Names and personas

*Settings → General* holds the assistant's name ("Nova") and yours. The AI knows both, and greets you by
name. A **persona** changes how the replies are worded, never what NEON can do: default, pirate,
Shakespearean, noir detective, hype coach, retro robot, zen, scientist, butler, gremlin, haiku, and
unsure (hesitant about everything, including its own hesitation). Each can have its own voice
(*Settings → Persona*).

### Voice and sounds

*Settings → Voice & sounds* picks the voice (dozens of natural Piper voices, downloaded when chosen, or
Windows' own), its speed and volume, and the sounds for "I'm listening", "I heard you" and notifications.
Next to any sound choice, **Make a sound...** opens a small editor: pick a tone, a few notes and their
lengths, hear it as you change it, and save it. Your sounds appear in every sound list.

### Listening

*Settings → Listening* picks the microphone, the speech recognition (offline on this PC by default, or
Google), and the wake word. NEON tells your voice apart from music, games and fans, and turns up a
quiet microphone. Tips for better recognition:

- Choose your **microphone itself**, not a stream or chat mix: a mix also carries game audio and music.
  NEON points this out when a mix is selected.
- If "hey nova" rarely wakes it, lower *Wake word → How sure it must be* (0.5 to 0.7 is typical). If it
  wakes by itself, raise it.
- *Speech recognition → Accuracy → Most accurate* understands the most; "Balanced" and "Fastest" suit
  slower PCs.
- Speak at a normal distance from the microphone. There's no need to pause after the wake word.

### Wake words

"hey nova" and "hey dan" train themselves the first time you pick them (a few minutes). Any other
phrase: *Settings → Listening → Train a wake word...*, record yourself saying it five or six times, and
press Train. The speech-recognition engine can listen for any phrase instead, at a higher CPU cost.

### Notifications

*Settings → Notifications* chooses what happens when one arrives, per-app rules, quiet hours, and
**Remove before reading**: text to take out of app names, titles and messages before they're read or
summarized (a server name, "- Outlook", anything in parentheses), one rule per line. Common rules are one
click away, a line between slashes is a pattern, and a preview shows the result as you type.

### The AI

*Settings → AI & chat* picks the Ollama model (qwen3:8b by default), the instructions it follows, and
whether a smaller, faster model (llama3.2) answers simple chit-chat and summaries first, handing anything
that needs real knowledge to the main model. You can also turn on checking facts against the web before
answering, choose the search engine, and show a Wikipedia pop-up with answers.

### Routines

A routine is a name and a list of steps (*Settings → Routines*). Each line is a command you could say,
or one of these:

```
open: Visual Studio Code
url: https://github.com
set the volume to 25 percent
wait: 2
run: C:\tools\standup.bat
say: Focus time. Go.
```

Then say "work mode". Give it a *when* and it also runs by itself: `weekdays at 9:00`,
`mon, wed and fri at 8am`, `when discord starts`, or `at startup`.

### Plugins

Drop a `.py` file in the `plugins` folder (*Settings → General → Open the data folder*) and say
"reload plugins":

```python
NAME = "Coffee"
DESCRIPTION = "Counts your coffees."

_count = 0

def _had_one(match, text):
    global _count
    _count += 1
    return f"That's {_count} today."

COMMANDS = [(r"^(?:i )?had (?:a|another) coffee$", _had_one)]
```

`setup(api)` is called once with `api.say`, `api.run`, `api.config`, `api.data_dir` and `api.log`.
Hooks: `on_reply(heard, reply)`, `on_notification(note)`, `on_timer(label, reminder)`,
`on_routine(name, result)`. Plugins come after the built-in commands, so they can add commands but
never break existing ones, and a broken plugin is reported and skipped. `plugins/example.py` is a
starting point.

---

## Privacy

Everything stays on your PC unless you choose otherwise.

- **Speech** is transcribed offline by default, and the wake word is heard by a small offline detector.
  Audio goes to Google only if you choose Google in *Settings → Listening* (or its online fallback).
- **The AI** is your own Ollama, on this PC.
- **Web lookups** (Wikipedia, DuckDuckGo, or the search engine you choose) happen only when a question
  needs one. The last 50 answers, their search results and place names are kept in `lookup_cache.json`
  for a while; *Settings → AI & chat → Forget recent lookups* clears them.
- **Memories** are in `memories.json` next to your settings. **Clipboard history** is kept in memory
  only and cleared when NEON closes; anything that looks like a password is never read aloud.
- **Your location** is what you type in *Settings → General*. Looking it up from your IP address is a
  switch, off by default.
- **Files** are searched by Everything on this PC; nothing leaves it.
- **Home Assistant** (if you turn it on) receives only the smart-home sentences you say, on your own network.
- **Web pages** you ask about are read by the extension, handed to NEON over `127.0.0.1` with a token,
  answered by your local AI, and forgotten.
- **Bitwarden**: only item names, addresses and usernames are held in memory while the vault is unlocked.
  Passwords and codes are fetched when you ask, go to the clipboard (cleared after 30 s, never into the
  history) or are typed after you say yes, and are never spoken or logged.
- **Secrets** (the Pear Desktop and browser tokens, a Brave Search key) live in Windows Credential
  Manager, not in files.
- **One-time downloads**: the speech model, voices, the wake-word feature models and the tool-picker.

---

## How it works

```
you ──speech──▶ listening.py (voice detection) ──▶ stt.py ──text──▶ assistant._route_utterance
                                                                        │
                        ├─▶ the parsers (exact wordings, instant) ──▶ timer, window, volume, board, files, …
                        ├─▶ the same parsers on a cleaned-up sentence ("um, could you… for me")
                        ├─▶ a question of fact ──▶ Wikipedia + Wikidata + web ──▶ answer
                        ├─▶ intent.py: the AI works out which command you meant
                        ├─▶ the Needle tool-picker (when Ollama isn't running)
                        └─▶ Ollama ──▶ conversation (fact-checked if you want)
                                               │
                 status bar + chat window + voice ◀── controller.py
```

| File | What it does |
| --- | --- |
| `main.py` | starts the app: windows, tray icon, global hotkeys, shutdown |
| `controller.py` | connects the background work to the windows |
| `assistant.py` | the settings, the router and most commands |
| `commands.py` `intent.py` | spoken-command parsers; understanding free-form speech |
| `listening.py` `vad.py` `stt.py` | microphone, voice detection, speech recognition |
| `wakeword.py` `wake_training.py` | the wake-word detector and training new phrases |
| `tts.py` `speech_text.py` `sounds.py` | the voice, numbers read as words, sounds and the beep maker's synthesis |
| `websearch.py` `lookup_cache.py` | grounded answers; reusing recent lookups |
| `filesearch.py` | file search through Everything |
| `homeassistant.py` | the smart home, through Home Assistant's Assist |
| `notifications.py` `calendar_feed.py` | Windows notifications; iCalendar feeds |
| `board.py` `timers.py` `routines.py` | the board, timers, routines |
| `browser_bridge.py` `browser_commands.py` `browser_extension/` | the browser link (Chrome, Edge, Brave, Firefox, Zen) |
| `bitwarden.py` | the vault, through Bitwarden's command-line tool |
| `memory_store.py` `clipboard.py` `persona.py` `fun.py` | memories, clipboard, personas, the silly bits |
| `system_control.py` `windows.py` `dictation.py` `media.py` `ytmusic.py` | the PC, windows, typing, media |
| `units.py` `mathcalc.py` `plugins.py` | conversions, arithmetic, plugins |
| `ui/` | status bar, windows, settings, welcome tour, editors, effects |
| `linuxdesk/` `osinfo.py` | Linux: Hyprland, KDE Plasma and the desktop tools |
| `plasmoid/` `panel_feed.py` | the KDE Plasma panel widget, and the feed it follows |

Running from source, your data (settings, memories, board, voices, logs) sits beside the code. The
packaged `.exe` keeps it in `%APPDATA%\NeonAssistant`. Set `NEON_DATA_DIR` to put it anywhere else.
When something goes wrong, `neon.log` (*Settings → General → Open the log file*) usually says why.

---

## For developers

```bat
python -m unittest discover -s tests -t . -v
```

About 590 tests, all offscreen and against a throwaway configuration: nothing touches your settings,
speaks, types into your windows, or changes your volume. `NEON_LIVE=1` adds tests that use real
windows (parked off-screen), real toasts and a running Everything. Screenshot tests compare each window
with `tests/baselines/`; accept a deliberate change with `NEON_UPDATE_BASELINES=1`.
`.github/workflows/tests.yml` runs the suite on Windows for every push, and the Linux port's tests on
Linux. On Windows those run too: `tests/linux_sim.py` imports every module with Windows hidden, and
`tests/linux_smoke.py` drives the real windows and hotkeys against a fake Hyprland (each config
dialect) and a fake KDE, checking the commands they'd send. The KDE panel widget's QML runs in plain Qt
with stand-ins for Plasma (`tests/test_plasmoid.py`). The Linux code lives in `linuxdesk/`; the
Windows modules hand over to it when `osinfo.IS_WINDOWS` is false.

`HANDOFF.md` explains how a sentence is routed and how to add a command; `suggestions.md` lists ideas
not done yet.
