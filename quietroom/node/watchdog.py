"""systemd sd_notify client (READY/WATCHDOG/STOPPING/STATUS) over $NOTIFY_SOCKET.

No systemd library dependency: the protocol is one datagram per message.
Abstract sockets (leading @) are handled per sd_notify(3).
"""

from __future__ import annotations
import os
import socket


class SdNotifier:
    def __init__(self, socket_path=None, sender=None):
        self.socket_path = os.environ.get("NOTIFY_SOCKET") if socket_path is None else socket_path
        self._sender = sender

    def _send(self, msg):
        if self._sender is not None:
            self._sender(msg)
            return True
        if not self.socket_path:
            return False
        path = "\0" + self.socket_path[1:] if self.socket_path.startswith("@") else self.socket_path
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(msg.encode(), path)
            return True
        except OSError:
            return False
        finally:
            s.close()

    def ready(self):
        return self._send("READY=1")

    def watchdog(self):
        return self._send("WATCHDOG=1")

    def stopping(self):
        return self._send("STOPPING=1")

    def status(self, text):
        return self._send(f"STATUS={text}")
