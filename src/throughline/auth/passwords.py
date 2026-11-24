"""Password hashing (scrypt, standard library) and a sign-in throttle."""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

# 2**15 * 8 * 128 bytes = 32 MiB per hash: slow for an attacker, fine for a
# personal server that verifies a handful of sign-ins.
N, R, P = 2**15, 8, 1
MIN_LENGTH = 10
MAX_LENGTH = 1024


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32
    )


def _b64(data):
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.b64decode(text + "=" * (-len(text) % 4))


def check_strength(password):
    if len(password) < MIN_LENGTH:
        raise ValueError(f"a password needs at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise ValueError("that password is too long")


def hash_password(password):
    check_strength(password)
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_scrypt(password, salt, N, R, P))}"


# Verified when the account does not exist, so a wrong username costs as much
# time as a wrong password.
_DUMMY = f"scrypt${N}${R}${P}${_b64(b'0' * 16)}${_b64(b'0' * 32)}"


def verify(password, stored):
    """True when `password` matches `stored`; never raises on a bad hash."""
    stored = stored or _DUMMY
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt" or len(password) > MAX_LENGTH:
            return False
        actual = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, _unb64(digest)) and stored is not _DUMMY


class Throttle:
    """Failed sign-ins per key within a window. In process memory: one API
    process is the deployment shape, and a restart forgetting the count is
    acceptable for a personal server."""

    def __init__(self, limit, window_s):
        self.limit, self.window = limit, window_s
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key, now):
        q = self._failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
        return q

    def retry_after(self, key):
        """Seconds until `key` may try again, or 0."""
        now = time.monotonic()
        with self._lock:
            q = self._trim(key, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def fail(self, key):
        with self._lock:
            self._failures[key].append(time.monotonic())

    def clear(self, key):
        with self._lock:
            self._failures.pop(key, None)


# Five wrong passwords per account, twenty per client address, per 15 minutes.
by_account = Throttle(5, 900)
by_address = Throttle(20, 900)
