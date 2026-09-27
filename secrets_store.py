"""
secrets_store.py -- keep tokens in Windows Credential Manager instead of a plain file.

Uses the Win32 credential API through ctypes (no extra package). The values are protected by the
user's Windows login. If the API is unavailable, callers keep their old fallback (a file).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import osinfo

_advapi = ctypes.WinDLL("advapi32", use_last_error=True) if osinfo.IS_WINDOWS else None
CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2
PREFIX = "NeonAssistant/"


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


if _advapi is not None:
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]


def set_secret(name: str, value: str) -> bool:
    """Store `value` under `name`. Returns False if Windows refused."""
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Type, cred.Persist = CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE
    cred.TargetName, cred.UserName = PREFIX + name, "neon"
    cred.CredentialBlobSize, cred.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    return bool(_advapi.CredWriteW(ctypes.byref(cred), 0))


def get_secret(name: str) -> str | None:
    pcred = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(PREFIX + name, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        return None
    try:
        cred = pcred.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        _advapi.CredFree(pcred)


def delete_secret(name: str) -> bool:
    return bool(_advapi.CredDeleteW(PREFIX + name, CRED_TYPE_GENERIC, 0))


if not osinfo.IS_WINDOWS:                       # Linux: the desktop's keyring (Secret Service)
    from linuxdesk.secrets import delete_secret, get_secret, set_secret  # noqa: F401,F811
