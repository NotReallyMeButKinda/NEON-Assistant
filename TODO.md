# TODO / ideas for later

Nothing pending.

Recently done: long-term memory (`memory_store.py`), personas (`persona.py`), unit conversion
(`units.py`), controlling the PC (`system_control.py`), routines (`routines.py`), plugins
(`plugins.py`), dictation (`dictation.py`), clipboard history (`clipboard.py`), fun and the status
bar effects (`fun.py`, `ui/effects.py`), the voice spectrum, the timer countdown and its spoken
warning, conversation mode, `NEON_DATA_DIR`, and a README.

Follow-ups worth considering:
- `pip install uiautomation` enables the no-clipboard fallback for apps that block Ctrl+C (and for
  terminals, which never get a Ctrl+C sent to them).
- `ollama pull nomic-embed-text` turns on recall by meaning ("how do I get online" -> the wifi password).
- The router could move into `router.py` next (the pure parsers are already in `commands.py`).

The full list, with sizes and reasons, is in `suggestions.md`.
