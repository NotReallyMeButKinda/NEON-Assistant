"""
linuxdesk/single.py -- one NEON at a time, and the `neon` command (both on Linux and Windows).

A local socket (QLocalServer: a Unix socket on Linux, a named pipe on Windows) named after the user:
  * if another NEON already owns it, this one just hands over its command and exits;
  * the running NEON receives commands through it: `neon talk`, `neon quick`, `neon mute`...

That's how hotkeys work on Wayland, where apps can't grab keys globally: Hyprland (and KDE's custom
shortcuts) run `neon talk`, which sends "talk" here. NEON's own Hyprland binds are set up the same way.
"""

from __future__ import annotations

import getpass
import re

COMMANDS = ("show", "talk", "stop", "quick", "mute", "wake", "window", "dictation", "hold-start", "hold-stop",
            "settings", "board", "quit")


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return "neon-assistant-" + re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def send(command: str, timeout_ms: int = 1500) -> bool:
    """Send a command to the running NEON; False if none is running."""
    from PySide6.QtNetwork import QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer(server_name())
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write((command.strip() + "\n").encode("utf-8"))
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.waitForReadyRead(timeout_ms)          # NEON's "ok": it has read the command (closing sooner can lose it)
    socket.disconnectFromServer()
    return True


class CommandServer:
    """The running NEON's end: `listen()` claims the name (False if another NEON has it); each command line
    that arrives goes to on_command(name)."""

    def __init__(self, on_command):
        from PySide6.QtNetwork import QLocalServer
        self._on_command = on_command
        self._server = QLocalServer()
        self._server.newConnection.connect(self._accept)

    def listen(self) -> bool:
        from PySide6.QtNetwork import QLocalServer
        name = server_name()
        if send("show", 300):                       # someone answers: NEON is already running
            return False
        QLocalServer.removeServer(name)             # a stale socket file left by a crash
        return self._server.listen(name)

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(socket.deleteLater)

    def _read(self, socket) -> None:
        while socket.canReadLine():
            command = bytes(socket.readLine()).decode("utf-8", "replace").strip().lower()
            socket.write(b"ok\n")
            socket.flush()
            if command in COMMANDS:
                try:
                    self._on_command(command)
                except Exception:  # noqa: BLE001 -- a bad command must not take NEON down
                    pass
