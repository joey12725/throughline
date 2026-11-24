"""Idle detection on Wayland without dependencies: a tiny client of the
ext-idle-notify-v1 protocol (Hyprland, Sway, KDE Plasma 6, river, niri,
Wayfire, COSMIC). GNOME does not implement it (see idle.py for its D-Bus
fallback).

The client speaks the Wayland wire format over $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY:
it binds wl_seat and ext_idle_notifier_v1 and asks for an *input* idle
notification (version 2: video players' idle inhibitors are ignored, so
watching a video without touching anything still counts as away). The
compositor then sends `idled` after the timeout without input and `resumed`
on the next input.
"""

import os
import socket
import struct
import threading
import time

DISPLAY_ID = 1


def _string(text):
    raw = text.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


class _Conn:
    def __init__(self):
        name = os.getenv("WAYLAND_DISPLAY") or "wayland-0"
        path = name if name.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], name)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2
        self.buffer = b""

    def new_id(self):
        value = self.next_id
        self.next_id += 1
        return value

    def send(self, obj, opcode, payload=b""):
        size = 8 + len(payload)
        self.sock.sendall(struct.pack("<II", obj, (size << 16) | opcode) + payload)

    def events(self):
        """Yield (object, opcode, payload) for every complete message."""
        while True:
            while len(self.buffer) >= 8:
                obj, word = struct.unpack_from("<II", self.buffer)
                size = word >> 16
                if len(self.buffer) < size:
                    break
                payload = self.buffer[8:size]
                self.buffer = self.buffer[size:]
                yield obj, word & 0xFFFF, payload
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Wayland compositor closed the connection")
            self.buffer += chunk


def _parse_global(payload):
    name, length = struct.unpack_from("<II", payload)
    interface = payload[8 : 8 + length - 1].decode()
    offset = 8 + length + (-length % 4)
    (version,) = struct.unpack_from("<I", payload, offset)
    return name, interface, version


class WaylandIdle:
    """Tracks whether the user has touched keyboard or mouse within
    `timeout_s`. `available` is False when the compositor lacks the protocol.
    `idle_since` is the monotonic time input stopped (None while active)."""

    def __init__(self, timeout_s=60):
        self.timeout_ms = int(timeout_s * 1000)
        self.idle = False
        self.idle_since = None
        self.available = None
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="wayland-idle", daemon=True)

    def start(self):
        self.thread.start()
        self.ready.wait(5)
        return self.available

    def _run(self):
        try:
            conn = _Conn()
            registry = conn.new_id()
            conn.send(DISPLAY_ID, 1, struct.pack("<I", registry))  # get_registry
            callback = conn.new_id()
            conn.send(DISPLAY_ID, 0, struct.pack("<I", callback))  # sync
            globals_ = {}
            events = conn.events()
            for obj, opcode, payload in events:
                if obj == registry and opcode == 0:
                    name, interface, version = _parse_global(payload)
                    globals_.setdefault(interface, (name, version))
                elif obj == callback and opcode == 0:
                    break
                elif obj == DISPLAY_ID and opcode == 0:
                    raise ConnectionError("Wayland protocol error")
            if "ext_idle_notifier_v1" not in globals_ or "wl_seat" not in globals_:
                self.available = False
                self.ready.set()
                return
            seat_name, _ = globals_["wl_seat"]
            seat = conn.new_id()
            conn.send(registry, 0, struct.pack("<I", seat_name) + _string("wl_seat") + struct.pack("<II", 1, seat))
            notifier_name, notifier_version = globals_["ext_idle_notifier_v1"]
            version = min(notifier_version, 2)
            notifier = conn.new_id()
            conn.send(
                registry,
                0,
                struct.pack("<I", notifier_name) + _string("ext_idle_notifier_v1") + struct.pack("<II", version, notifier),
            )
            notification = conn.new_id()
            # get_input_idle_notification (v2) ignores idle inhibitors.
            opcode = 2 if version >= 2 else 1
            conn.send(notifier, opcode, struct.pack("<III", notification, self.timeout_ms, seat))
            self.available = True
            self.ready.set()
            for obj, opcode, payload in events:
                if obj == notification and opcode == 0:
                    self.idle = True
                    self.idle_since = time.monotonic() - self.timeout_ms / 1000
                elif obj == notification and opcode == 1:
                    self.idle = False
                    self.idle_since = None
                elif obj == DISPLAY_ID and opcode == 0:
                    raise ConnectionError("Wayland protocol error")
        except Exception as error:  # noqa: BLE001
            self.error = f"{type(error).__name__}: {error}"
            if self.available is None:
                self.available = False
            self.ready.set()
