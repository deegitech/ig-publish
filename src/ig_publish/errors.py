"""Exceptions and exit codes.

Exit codes are part of the CLI contract (the scheduler depends on them):

* ``0``  -- done.
* ``1``  -- permanent error, or a stop *after* something was written. Do not retry blindly.
* ``2``  -- command-line usage error.
* ``75`` -- ``EX_TEMPFAIL``: stopped *before anything was written*, for a reason that passes with time
  (run lock busy, hold active, recent posts the state file does not know, API quota/state mismatch, burst guard,
  reel spacing, not enough quota, usage limit, a read error, a missing token file, season not open yet).
  Running the same command later is safe.
"""
from __future__ import annotations

import json

from .redact import redact

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LATER = 75


class IgPublishError(Exception):
    """Base class for every error this package raises on purpose."""

    exit_code = EXIT_ERROR


class ConfigError(IgPublishError):
    """The configuration file, the manifest or a command-line value is invalid."""


class Stop(IgPublishError):
    """Permanent stop: it needs a human decision, or something was already written."""


class TemporaryStop(IgPublishError):
    """Stopped before anything was written, for a reason that passes with time (exit 75)."""

    exit_code = EXIT_LATER


class TokenError(IgPublishError):
    """The access token could not be read.

    ``temporary`` marks conditions that are worth retrying later (for example a locked macOS keychain).
    ``fix`` is the one-line remedy the CLI prints under the error (empty when the message already says it).
    Neither ever contains the token.
    """

    def __init__(self, message: str, temporary: bool = False, fix: str = '') -> None:
        super().__init__(message)
        self.temporary = temporary
        self.fix = fix


class UsageLimit(IgPublishError):
    """Meta's usage headers crossed the configured threshold, so no further request was sent."""


class GraphError(IgPublishError):
    """An error answer from the Graph API (or a network failure, ``status == 0``).

    ``err`` is Meta's ``error`` object (already redacted), ``status`` the HTTP status, ``where`` the request.
    ``unknown_outcome`` marks an answer ig-publish could not interpret although the HTTP status says success (for
    example ``media_publish`` without a media ID): whether the write took effect is unknown, like after a 5xx.
    """

    def __init__(self, status: int, err: dict, where: str, *, unknown_outcome: bool = False) -> None:
        self.status = status
        self.err = err if isinstance(err, dict) else {'message': str(err)}
        self.where = where
        self.unknown_outcome = unknown_outcome
        super().__init__(self.text())

    @property
    def code(self):
        return self.err.get('code')

    @property
    def subcode(self):
        return self.err.get('error_subcode')

    def text(self) -> str:
        e = self.err
        parts = [f'{self.where}: HTTP {self.status}' if self.status else f'{self.where}: no answer']
        if e.get('code') is not None:  # absent for network failures and answers ig-publish made itself
            parts.append(f"code {e.get('code')}" + (f"/{e.get('error_subcode')}" if e.get('error_subcode') else ''))
        if e.get('type'):
            parts.append(f"type {e.get('type')}")
        parts.append(f"message: {e.get('message')}")
        if e.get('error_user_title') or e.get('error_user_msg'):
            parts.append(f"user: {e.get('error_user_title', '')} - {e.get('error_user_msg', '')}")
        if e.get('error_data'):
            parts.append(f"data: {json.dumps(e.get('error_data'), ensure_ascii=False)[:800]}")
        if e.get('fbtrace_id'):
            parts.append(f"fbtrace {e.get('fbtrace_id')}")
        return redact(' | '.join(parts))
