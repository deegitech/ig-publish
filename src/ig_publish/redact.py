"""Secret redaction for everything this package prints or writes.

Every line on stdout/stderr, every journal and state write and every scheduler log line goes through
:data:`redact`. It removes the exact access token once it has been loaded (``register``) and, as a second
line of defence, anything that looks like a Meta/Instagram token or a credential-bearing query parameter.
"""
from __future__ import annotations

import re
import threading

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Facebook Login user/page tokens.
    (re.compile(r'EAA[A-Za-z0-9]{16,}'), 'EAA***'),
    # Instagram Login tokens (not used by this tool, redacted anyway).
    (re.compile(r'IG(?:QV|AA)[A-Za-z0-9_\-]{20,}'), 'IG***'),
    # Credentials in query strings or form bodies.
    (re.compile(r'((?:access_token|input_token|client_secret|appsecret_proof|fb_exchange_token)=)[^&\s"\']+'),
     r'\1***'),
    # Authorization header values echoed back by a server or a proxy.
    (re.compile(r'\b(Bearer|OAuth)(\s+)[A-Za-z0-9._~+/=\-]{12,}'), r'\1\2***'),
]


class Redactor:
    """Callable that returns ``str(value)`` with every registered secret and token-like string removed."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str | None) -> None:
        """Remember a secret (the access token) so it is replaced verbatim from now on."""
        if secret and len(secret) >= 6:
            with self._lock:
                self._secrets.add(secret)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def __call__(self, value: object) -> str:
        s = str(value)
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for sec in secrets:
            s = s.replace(sec, '***')
        for rx, rep in _PATTERNS:
            s = rx.sub(rep, s)
        return s


redact = Redactor()
