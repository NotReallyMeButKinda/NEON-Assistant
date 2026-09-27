# PyInstaller spec for the assistant.   Build with:   python build_exe.py
#
# One-folder build (fast start-up, easy to debug). The user's settings, voices, logs and caches live
# in %APPDATA%\NeonAssistant (see app_paths.py), so upgrading never touches them. No voices, speech
# models or AI models are bundled: the app downloads the ones the user picks on first use.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

extension = [(str(p), str(Path("browser_extension") / p.parent.relative_to("browser_extension")))
             for p in Path("browser_extension").rglob("*")
             if p.is_file() and p.suffix not in (".zip", ".xpi") and not p.name.startswith(".")]
datas = [("notify_watcher.ps1", "."),            # the PowerShell helper that reads Windows notifications
         ("media_watcher.ps1", "."),             # ...and the one that reads / controls media sessions
         *extension]                             # the Zen / Firefox extension (Settings > Browser opens it)
binaries, hiddenimports = [], ["pyttsx3.drivers", "pyttsx3.drivers.sapi5", "comtypes", "win32com", "tzdata",
                              # imported inside functions: listed so a build can never miss them
                              "filesearch", "lookup_cache", "vad", "shortcuts", "homeassistant", "ui.beep_maker",
                              "ui.window_highlight", "osinfo", "linuxdesk.single"]

# Packages that carry data files / native libraries PyInstaller can't see by import analysis alone.
for package in ("piper", "needle", "onnxruntime", "faster_whisper", "ctranslate2", "tokenizers", "speech_recognition"):
    try:
        d, b, h = collect_all(package)
    except Exception:                            # optional (e.g. faster_whisper): build without it
        continue
    datas += d
    binaries += b
    hiddenimports += h

# Models we never load stay out (filtered after Analysis, since packages' own hooks add files too): Piper's Hebrew / Arabic diacritic models (~26 MB; English voices
# don't use them) and speech_recognition's offline PocketSphinx model (~38 MB; NEON uses Google or
# Whisper). So do speech_recognition's Linux / macOS flac binaries.
def _unused(dest):
    parts = Path(dest).parts
    return ((dest.endswith(".onnx") and ("hebrew" in parts or "tashkeel" in parts))
            or "pocketsphinx-data" in parts
            or parts[-1] in ("flac-linux-x86", "flac-linux-x86_64", "flac-mac"))


a = Analysis(["main.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "pytest", "test", "tests"], noarchive=False)
a.datas = [entry for entry in a.datas if not _unused(entry[0])]
a.binaries = [entry for entry in a.binaries if not _unused(entry[0])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="NeonAssistant", console=False,
          disable_windowed_traceback=False, icon="icons/neon.ico",
          version="version_info.txt")            # "Neon Assistant" as Windows' name for the program
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NeonAssistant")
