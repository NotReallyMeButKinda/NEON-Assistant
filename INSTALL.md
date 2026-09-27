# Installing NEON Assistant

This guide installs the program, then each feature that needs something extra. Only the first section is
required: every other feature is optional, and NEON works without it (it tells you what's missing when
you ask for it). For what NEON does and how to use it, see [README.md](README.md).

| Feature | Needs | Section |
| --- | --- | --- |
| The program, voice commands, offline speech, the voice | Python and `requirements.txt` | [The program](#the-program) |
| Running on Linux (Arch: Hyprland, KDE Plasma) | a few desktop tools | [On Linux](#on-linux-arch-hyprland-or-kde-plasma) |
| Conversation, summaries, natural wording | Ollama and a model | [The local AI](#the-local-ai-ollama) |
| Offline speech recognition on an NVIDIA GPU | CUDA libraries | [Speech recognition](#speech-recognition) |
| More or different voices | nothing (downloaded in Settings) | [Voices](#voices) |
| "hey nova" and other wake words | nothing (trained on first use) | [Wake word](#wake-word) |
| Windows notifications | a Windows permission | [Notifications](#notifications) |
| Finding files | Everything | [File search](#file-search-everything) |
| Passwords | Bitwarden's command-line tool | [Passwords](#passwords-bitwarden) |
| Reading web pages, tabs | the NEON extension for Chrome, Edge, Brave, Firefox or Zen | [The browser extension](#the-browser-extension) |
| Controlling YouTube Music | Pear Desktop | [Music](#music-pear-desktop) |
| Lights, heating, locks... | Home Assistant | [Smart home](#smart-home-home-assistant) |
| Your calendar | an iCalendar link | [Calendar](#calendar) |
| Other search engines | a key or your own server | [Web search](#web-search) |
| Selected text in stubborn apps | `uiautomation` | [Selected text](#selected-text) |
| A standalone `.exe` | `pyinstaller` | [Building the .exe](#building-the-exe) |

---

## The program

**You need:** Windows 10 or 11, and [Python](https://www.python.org/downloads/) 3.10 or newer (NEON is
developed on 3.14). When installing Python, tick *Add python.exe to PATH*. A microphone is optional:
you can type instead. About 1.5 GB of disk space covers the program, the speech model and a voice.

1. **Get the code.** Download or clone the project into a folder of your choice, for example
   `C:\NEON`. Avoid very long paths.

2. **Install the Python packages.** Open a terminal in that folder and run:

   ```bat
   pip install -r requirements.txt
   ```

3. **Fetch the tool-picker** (a small model that understands commands when the local AI isn't
   running). Run once:

   ```bat
   needle fetch --generation 2
   ```

   If Windows says `needle` isn't recognized, Python's scripts folder isn't on your PATH. Run it by its
   full path instead, for example
   `%APPDATA%\Python\Python314\Scripts\needle.exe fetch --generation 2` (use your Python version's
   folder), or reinstall Python with *Add to PATH* ticked.

4. **Allow the microphone.** Windows Settings → *Privacy & security → Microphone*: turn on
   *Microphone access* and *Let desktop apps access your microphone*.

5. **Start NEON:**

   ```bat
   python main.py
   ```

   Double-clicking `main.py` works too; it runs without a console window (messages go to `neon.log`).
   The first start opens the welcome tour, downloads the speech model (145 MB) and a voice (about
   60 MB), and scans your Start menu for apps. After that, everything works offline.

**Shortcuts:** the welcome tour and *Settings → General → Startup* can put NEON in the Start menu and
on the desktop, like any other app.

**Start with Windows:** *Settings → General → Start automatically when I sign in to Windows*, and
*Start hidden* if you'd rather see only the status bar and the tray icon.

**Where your data goes:** settings, memories, the board, voices, models and the log are stored beside the
code (the `.exe` uses `%APPDATA%\NeonAssistant`). To keep them somewhere else, set the environment
variable `NEON_DATA_DIR` to a folder before starting NEON.

**Updating:** replace the code with the new version, run `pip install -r requirements.txt` again, and
start NEON. Your settings are kept and converted automatically when a new version changes them.

---

## On Linux (Arch: Hyprland or KDE Plasma)

NEON runs on Linux too, developed for Arch with **Hyprland** first and **KDE Plasma** second (both on
Wayland). Other desktops mostly work, with fewer desktop features. Everything above applies, except the
Windows-only steps; this section replaces them.

1. **Desktop tools.** NEON uses the standard ones instead of Windows APIs:

   ```sh
   sudo pacman -S --needed python portaudio wl-clipboard playerctl libnotify espeak-ng plocate libsecret xdg-utils
   # Hyprland:
   sudo pacman -S --needed wtype grim slurp
   # KDE Plasma:
   sudo pacman -S --needed ydotool spectacle      # and kdotool from the AUR, for "close Firefox" and friends
   ```

2. **Python packages** go in a virtual environment (Arch doesn't allow `pip install` system-wide):

   ```sh
   cd ~/neon                                   # wherever you put the code
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/needle fetch --generation 2
   ```

3. **Start NEON:** `.venv/bin/python main.py`. Settings and data are kept beside the code, or in
   `~/.local/share/neon-assistant` if that folder isn't writable. *Settings → General → Startup* can add
   NEON to the app menu, the desktop and autostart.

**What's different on Linux**

| | Hyprland | KDE Plasma |
| --- | --- | --- |
| Hotkeys | NEON adds them as Hyprland keybinds itself (and again after every config reload) | Through the desktop's shortcut portal: Plasma asks you to confirm them once, then they're in *System Settings → Keyboard → Shortcuts* |
| Status bar | Floats on every workspace and reserves its strip, like Waybar | Best as the **NEON panel widget** (below). NEON's own bar also works, kept in place by a KWin window rule, but maximized windows go under it |
| "Close Discord?" outline | The window's own border turns your accent colour | Not shown (the question is spoken as usual) |
| Typing and dictation | `wtype` | `ydotool`: run `systemctl --user enable --now ydotool`; if nothing gets typed, your user needs access to `/dev/uinput` (see the ArchWiki's *ydotool* page) |
| Window commands | built in (`hyprctl`) | `kdotool` |
| Hiding the desktop's pop-ups | swaync, dunst or mako | Plasma's own |
| Starting with the PC | needs uwsm (or dex), or add `exec-once = <path>/.venv/bin/python <path>/main.py --minimized` to `hyprland.conf` | built in |
| Tray icon | needs a tray, like Waybar's `tray` module; without one, start NEON again (or `--command show`) to bring its window back | built in |

- **The KDE panel widget.** *Settings → Status bar → Panel widget → Install the panel widget*, then right-click
  your panel, *Add Widgets...*, and drag **NEON Assistant** onto it. It shows the orb and what NEON hears and
  says; a click starts listening (or opens its pop-up: recent lines, Talk, Type, Mute, Wake word, Dictation,
  Open, Settings), a middle-click mutes, and right-click has the rest. With it, you can turn NEON's own status
  bar off. It talks to NEON on `127.0.0.1` only (port 47812, changeable on both sides); when NEON isn't
  running, it says so and can start it.
- **Hotkeys from your own config.** Any keybind or script can drive NEON with
  `.venv/bin/python main.py --command talk` (or `quick`, `mute`, `wake`, `window`, `dictation`, `stop`,
  `show`, `settings`, `board`, `quit`; `hold-start` / `hold-stop` for hold-to-talk on press and release).
  On Hyprland: `bind = SUPER, N, exec, ~/neon/.venv/bin/python ~/neon/main.py --command talk`.
- **NEON never edits your Hyprland or KWin config files** except for its own window rules in
  `~/.config/kwinrulesrc` on KDE (listed in *System Settings → Window Management → Window Rules* as
  "NEON Assistant: ..."). On Hyprland, everything it sets lasts until Hyprland restarts, and NEON sets it
  again when it starts. Both Hyprland config styles work: `hyprland.conf` (0.53 syntax or older) and
  `hyprland.lua`.
- **Local AI:** `sudo pacman -S ollama-rocm` for AMD graphics (`ollama-cuda` for NVIDIA), then
  `sudo systemctl enable --now ollama`. Some Radeon cards (the RX 6700 XT, for one) need
  `HSA_OVERRIDE_GFX_VERSION=10.3.0`: add it with `sudo systemctl edit ollama` under `[Service]` as
  `Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"`.
- **File search** uses `plocate` instead of Everything: `sudo systemctl enable --now plocate-updatedb.timer`
  keeps its index fresh (run `sudo updatedb` once to build it now).
- **Passwords and tokens** are kept in KWallet or GNOME Keyring through `secret-tool`, instead of Windows'
  Credential Manager. **Selected text** is read from the selection itself (no Ctrl+C trick needed).
- **Not on Linux:** the Copilot key, NEON's own title bar (the desktop draws title bars; Hyprland draws none),
  and reading the notifications that arrived while NEON was closed.

---

## The local AI (Ollama)

Without it, every command still works; with it, NEON can chat, summarize notifications and pages,
understand commands however you word them, and answer questions from web results.

1. Install [Ollama](https://ollama.com/download) and let it start with Windows (it runs in the tray).
2. Download the model NEON uses by default (5.2 GB):

   ```bat
   ollama pull qwen3:8b
   ```

   It wants about 6 GB of graphics memory. On a smaller or older graphics card, pick a lighter model in
   *Settings → AI & chat → Model* (`qwen3:4b` is a good choice). That list can also download models for
   you.
3. **Optional: a faster model for simple questions.** Download `llama3.2` (2 GB) and turn on
   *Settings → AI & chat → Answer simple questions with a faster model*:

   ```bat
   ollama pull llama3.2
   ```

4. **Optional: recall memories by meaning** ("how do I get online" finds the wifi password you told it):

   ```bat
   ollama pull nomic-embed-text
   ```

   Then turn on *Settings → Memory → Match memories by meaning, not just by words*.

If Ollama runs on another PC, set its address in *Settings → AI & chat → Connection*.

---

## Speech recognition

Offline recognition (Whisper, through `faster-whisper`) is installed by `requirements.txt` and is the
default: your voice never leaves the PC. *Settings → Listening → Speech recognition → Accuracy* picks the
model (Fastest 75 MB, Balanced 145 MB, Most accurate 480 MB), downloaded the first time it's used.

**On an NVIDIA graphics card** it can run on the GPU, which is several times faster. Install NVIDIA's
CUDA 12 libraries for it:

```bat
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
```

then choose *Run it on → The graphics card* (or leave it on *Automatic*). AMD and Intel graphics aren't
supported by Whisper; it runs on the processor instead, which is fine for the Balanced model.

**Google instead:** *Recognize speech → With Google* needs no download but sends each command's audio to
Google.

**Better recognition:** see *Listening* in the [README](README.md#listening). In short: choose your
microphone itself (not a stream or chat mix), and keep *Tell my voice apart from background sound* on.

---

## Voices

The voice is Piper, installed by `requirements.txt`. The default voice downloads on first start; others
download when you pick them in *Settings → Voice & sounds* (20 to 120 MB each, from Piper's official
voice collection). Windows' own voices work too, with nothing to download: set *Voice engine* to
*Windows*.

---

## Wake word

Nothing to install. "hey nova" (the default) and "hey dan" train themselves the first time they're used,
which takes a few minutes and downloads 2 MB of feature models once. To use any other phrase, see
*Settings → Listening → Train a wake word...*.

openWakeWord's stock phrases ("hey jarvis", "alexa", "hey mycroft") need one more package:

```bat
pip install openwakeword
```

---

## Notifications

NEON reads your Windows notifications through Windows' official notification-listener permission.

1. Turn on *Settings → Notifications → Show Windows notifications here* (the welcome tour asks too).
2. The first time, Windows may ask whether NEON can access your notifications: allow it. If notifications
   never arrive, open Windows Settings → *Privacy & security → Notifications* and allow access, then
   restart NEON.
3. *Settings → Notifications → Show a test notification* checks that the card appears.

Nothing else is needed. Notifications that Windows hides while a game is fullscreen still reach NEON.

---

## File search (Everything)

"Find the file called resume" uses [Everything](https://www.voidtools.com), a free, instant file-name
search.

1. Install it:

   ```bat
   winget install voidtools.Everything
   ```

   or download the installer from voidtools.com. Install the regular version, not *Everything Lite*
   (Lite can't answer other programs).
2. Let it finish its first index (a few seconds to a minute).

That's all. NEON starts Everything in the background if it isn't running.
*Settings → Apps & PC → Finding files* shows whether it's found, and can include Windows and program
folders in searches.

---

## Passwords (Bitwarden)

1. Install Bitwarden's official command-line tool:

   ```bat
   winget install Bitwarden.CLI
   ```

2. Log in once, in a terminal (NEON never sees this):

   ```bat
   bw login
   ```

   With a self-hosted server, run `bw config server https://your.server` first.
3. Turn on *Settings → Passwords → Use my Bitwarden vault*.

The first time you ask for a password, NEON asks for your master password in a small box (never by
voice) and locks the vault again after 10 minutes without use. If you already keep `bw` unlocked (a
`BW_SESSION` environment variable), NEON uses that without asking.

---

## The browser extension

For "summarize this page", "what tabs do I have open" and the like. It works in **Chrome, Edge and Brave**
(and other Chromium browsers) and in **Firefox and Zen**. It only ever talks to NEON on this PC
(`127.0.0.1`), and only when you ask about a page.

**Get the files.** The packaged `.exe` comes with them, next to `NeonAssistant.exe`. From source, run:

```bat
python build_extension.py
```

This makes `dist\neon-bridge.xpi` (Firefox, Zen) and the folder `dist\neon-bridge-chrome` (Chrome, Edge,
Brave; also as `neon-bridge-chrome.zip`).

**Chrome, Edge or Brave**

1. Open the extensions page: `chrome://extensions` (Edge: `edge://extensions`, Brave: `brave://extensions`).
2. Turn on **Developer mode** (a switch at the top right, or in the left sidebar in Edge).
3. Press **Load unpacked** and choose the `neon-bridge-chrome` folder. Leave the folder where it is: the
   browser runs the extension from there.

**Zen or Firefox**

1. Open `about:config` and set `xpinstall.signatures.required` to `false`. Zen and Firefox Developer
   Edition / Nightly allow this; regular Firefox doesn't keep unsigned extensions.
2. Open `about:addons` → the gear icon → *Install Add-on From File...* → choose `neon-bridge.xpi`.

**Then, in any browser**

1. In NEON, *Settings → Browser*: turn on *Talk to the browser extension* and press *Copy the token*.
2. Click the NEON icon in the browser's toolbar (in Chrome, under the puzzle-piece icon: pin it), paste the
   token, and press *Save and connect*. *Settings → Browser* then shows "Connected".

---

## Music (Pear Desktop)

Play, pause and skip work with any player through the media keys. For "like this song", "play Glass
Beach on YouTube Music" and exact seeking, NEON controls [Pear Desktop](https://github.com/pear-devs/pear-desktop)
(formerly YouTube Music Desktop):

1. Install Pear Desktop and sign in.
2. In Pear Desktop's *Plugins* menu, enable **API Server** (leave the port at 26538).
3. Turn on *Settings → Music → Control Pear Desktop when it's running*. The first time NEON connects,
   Pear Desktop may ask you to approve it.

---

## Smart home (Home Assistant)

Optional, and off until you turn it on. NEON passes smart-home requests ("turn off the kitchen lights", "set
the thermostat to 70", "is the front door locked") to [Home Assistant](https://www.home-assistant.io)'s own
voice assistant, Assist, and says its answer. Nothing extra is installed on the PC.

1. **Make an access token.** In Home Assistant, open your profile (bottom left) → *Security* →
   *Long-lived access tokens* → *Create token*, name it "NEON", and copy it. Home Assistant shows it only once.
2. **Choose what NEON may control.** Home Assistant → *Settings → Voice assistants → Expose*: the devices
   listed there are the ones Assist (and so NEON) can see and control.
3. **Connect NEON.** *Settings → Smart home*: turn on *Control my home through Home Assistant*, enter the
   address you open Home Assistant at (for example `http://homeassistant.local:8123` or
   `http://192.168.1.20:8123`), paste the token, and press *Test connection*. It should say how many devices
   it found.

Unlocking a door, opening the garage or a gate, and disarming an alarm ask first; *Ask before unlocking...*
turns that off. The token is kept in Windows Credential Manager, never in the settings file.

---

## Calendar

For "what's my next meeting" and reminders before events: in Google Calendar or Outlook, copy your
calendar's secret iCal address (Google: *Settings → your calendar → Secret address in iCal format*;
Outlook: *Settings → Calendar → Shared calendars → Publish a calendar*), and paste it into
*Settings → Board & calendar → Calendar address*. A `.ics` file on disk works too.

---

## Web search

Questions of fact use Wikipedia plus DuckDuckGo, with nothing to set up. In
*Settings → AI & chat → Searching the web* you can switch to:

- **Brave Search:** get a free key at [brave.com/search/api](https://brave.com/search/api) and paste it
  in. It's kept in Windows Credential Manager.
- **SearXNG, your own server:** enter its address, and enable the JSON format in the server's
  `settings.yml` (`search: formats: [html, json]`).

**Your location** for the weather: type a town in *Settings → General → Location*, or allow looking it up
from your internet connection.

---

## Selected text

"Summarize this" reads the text you've highlighted by copying it. A few apps (and terminals) block that;
for those, install:

```bat
pip install uiautomation
```

---

## Hotkeys

Nothing to install. Set keys in *Settings → Hotkeys*. The Copilot key (and Win+C) can be taken over for
talking, hold-to-talk, dictation or the quick box.

---

## Building the .exe

To run NEON without Python installed (on this PC or another):

```bat
pip install pyinstaller
python build_exe.py --out C:\nb
```

The program is in `C:\nb\dist\NeonAssistant\`; run `NeonAssistant.exe` from there, or copy that folder
anywhere; `README.md`, `INSTALL.md` and the browser extension (`neon-bridge.xpi` for Firefox / Zen, the
`neon-bridge-chrome` folder and `.zip` for Chrome / Edge / Brave) are copied next to the `.exe`. No voices or AI models are included: pick them in the app and they download the first time.
To check a build, run `NeonAssistant.exe --selftest` (with NEON closed): it opens every window off-screen,
loads the speech engines, quits, and writes the results to `selftest.txt` in `%APPDATA%\NeonAssistant`.
Use a short output path like `C:\nb` if the build fails on a very long path. The `.exe` keeps its
data in `%APPDATA%\NeonAssistant`, so a newer build can replace it without losing settings. On a new PC,
run `needle fetch --generation 2` once, or the tool-picker stays off (everything else works).

---

## Troubleshooting

- **Something doesn't work:** *Settings → General → Open the log file* (`neon.log`). It almost always
  says why.
- **"Missing dependency" when starting:** run `pip install -r requirements.txt` again.
- **It doesn't hear you:** check the microphone in *Settings → Listening* (and Windows' microphone
  permission above). A mix device from streaming software also carries game audio; choose the microphone
  itself.
- **The AI doesn't answer:** make sure Ollama is running (its tray icon) and the model in
  *Settings → AI & chat* is downloaded.
- **Try without touching your setup:** set `NEON_DATA_DIR` to an empty folder and start NEON; it behaves
  like a fresh install.

---

## Uninstalling

1. Turn off *Settings → General → Start automatically when I sign in to Windows*, and quit NEON.
2. Delete the program folder (and `%APPDATA%\NeonAssistant` if you used the `.exe`).
3. Optional: remove the entries starting with `NeonAssistant/` from Windows Credential Manager (*Windows
   Credentials → Generic Credentials*), and uninstall Ollama, Everything or the Bitwarden tool if you
   installed them only for NEON.
