"""
linuxdesk/secrets.py -- tokens (Pear Desktop, the browser extension, Brave Search, Home Assistant) in the
desktop's keyring on Linux, the way Windows keeps them in Credential Manager.

Uses the freedesktop Secret Service, which KDE (KWallet / ksecretd) and GNOME Keyring both provide, through
`secret-tool` (Arch: the libsecret package). If the Python `keyring` package is installed and secret-tool
isn't, that's used instead. With neither, secrets aren't saved (the callers keep their old fallbacks).
"""

from __future__ import annotations

import osinfo

SERVICE = "neon-assistant"


def _attrs(name: str) -> list[str]:
    return ["service", SERVICE, "key", name]


def set_secret(name: str, value: str) -> bool:
    if osinfo.which("secret-tool"):
        return osinfo.run(["secret-tool", "store", "--label", f"NEON Assistant: {name}", *_attrs(name)],
                          timeout=10, input_text=str(value)).ok
    try:
        import keyring
        keyring.set_password(SERVICE, name, str(value))
        return True
    except Exception:  # noqa: BLE001 -- no keyring package, or no backend
        return False


def get_secret(name: str) -> str | None:
    if osinfo.which("secret-tool"):
        done = osinfo.run(["secret-tool", "lookup", *_attrs(name)], timeout=10)
        return done.out if done.ok and done.out else None
    try:
        import keyring
        return keyring.get_password(SERVICE, name)
    except Exception:  # noqa: BLE001
        return None


def delete_secret(name: str) -> bool:
    if osinfo.which("secret-tool"):
        return osinfo.run(["secret-tool", "clear", *_attrs(name)], timeout=10).ok
    try:
        import keyring
        keyring.delete_password(SERVICE, name)
        return True
    except Exception:  # noqa: BLE001
        return False
