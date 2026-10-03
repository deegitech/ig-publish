"""Access-token providers.

The token is read from exactly one configured source and kept in memory only:

* ``env``      -- an environment variable (default ``IG_ACCESS_TOKEN``);
* ``file``     -- a file that only its owner can read (mode ``0600`` or stricter, owned by the current user),
  for example a tmpfs file written by a systemd unit or a bind-mounted secret;
* ``keychain`` -- a macOS Keychain generic password (``security find-generic-password -w``);
* ``ssm``      -- an AWS SSM Parameter Store ``SecureString``, read with the ``aws`` CLI.

The token is never put on a command line, in a URL, in a log, in the journal or in the state file: it travels
only in the ``Authorization`` header and is registered with :data:`ig_publish.redact.redact` the moment it is read.
Helper programs (``ffmpeg``, ``ffprobe``, ``security``, ``aws``, the scheduler's ``notify_command``) get an
environment without the token variable (:func:`scrubbed_env`).

When the API endpoints are overridden for a local test server (see :mod:`ig_publish.graph`), none of the real
sources is consulted: only ``IG_PUBLISH_TEST_TOKEN`` is accepted, so a real token can never reach a test server.
"""
from __future__ import annotations

import getpass
import os
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ConfigError, TokenError
from .redact import redact

TEST_TOKEN_ENV = 'IG_PUBLISH_TEST_TOKEN'
DEFAULT_ENV_VAR = 'IG_ACCESS_TOKEN'
SOURCES = ('env', 'file', 'keychain', 'ssm')
SETUP_STORE = 'https://github.com/deegitech/ig-publish/blob/main/docs/setup.md#6-store-the-token-safely'
# The interactive `security add-generic-password ... -w` prompt (no value after -w) cuts input at 128 characters,
# observed (Oct 2026); Meta tokens are about 200. Hence the "$(pbpaste)" form: the shell history keeps the text
# "$(pbpaste)", never the token.
TRUNCATED_LENGTH = 128
# `security find-generic-password` exits with 44 (errSecItemNotFound) when there is no such item. Any other failure
# (a locked keychain without a GUI session: "User interaction is not allowed", a denied access ...) is not "missing".
KEYCHAIN_NOT_FOUND = 44


def keychain_store_command(service: str, account: str | None = None) -> str:
    """The command that stores the clipboard (the copied token) in the Keychain, ``-U`` replacing an older value."""
    acct = shlex.quote(account) if account else '"$USER"'
    return f'security add-generic-password -U -a {acct} -s {shlex.quote(service)} -w "$(pbpaste)"'


def keychain_howto(service: str, account: str | None = None) -> str:
    """One line: how to store the token in the macOS Keychain without truncating it."""
    return (f'type this line but do not press Enter yet: {keychain_store_command(service, account)} - then copy the '
            f'long-lived token, press Enter, and clear the clipboard with `pbcopy </dev/null`. Never leave -w without '
            f'a value: that prompt cuts input at {TRUNCATED_LENGTH} characters ({SETUP_STORE})')


def token_notes(token: str) -> list[str]:
    """Warnings about the *shape* of a token that was read successfully. Never contains the token."""
    notes = []
    if len(token) == TRUNCATED_LENGTH:
        notes.append(f'The token is exactly {TRUNCATED_LENGTH} characters long. If it was stored with the interactive '
                     f'`security ... -w` prompt, it was cut there: Meta tokens are longer, and that prompt cuts input '
                     f'at {TRUNCATED_LENGTH}, observed (Oct 2026). Store it again with the "$(pbpaste)" form '
                     f'({SETUP_STORE}).')
    if token.startswith('IG'):
        notes.append('The token starts with "IG", like an Instagram Login token (graph.instagram.com). ig-publish uses '
                     'the Instagram API with Facebook Login: make the token in the Graph API Explorer (step 4 of '
                     'docs/setup.md).')
    return notes


def scrubbed_env(token_env_var: str | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for helper processes: without the token variable (``IG_ACCESS_TOKEN`` and the
    configured ``[token] env_var``) and without any ``IG_PUBLISH_*`` variable."""
    drop = {DEFAULT_ENV_VAR, token_env_var or DEFAULT_ENV_VAR}
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k not in drop and not k.startswith('IG_PUBLISH_')}


@dataclass(frozen=True)
class TokenConfig:
    source: str = 'env'
    env_var: str = DEFAULT_ENV_VAR
    path: str | None = None
    keychain_service: str | None = None
    keychain_account: str | None = None
    ssm_parameter: str | None = None
    ssm_region: str | None = None
    ssm_profile: str | None = None
    timeout_seconds: float = 30.0

    def describe(self) -> str:
        """Where the token comes from, for messages (never the token itself)."""
        if self.source == 'env':
            return f'environment variable {self.env_var}'
        if self.source == 'file':
            return f'file {self.path}'
        if self.source == 'keychain':
            return f'macOS Keychain service {self.keychain_service!r}'
        if self.source == 'ssm':
            return f'AWS SSM parameter {self.ssm_parameter}'
        return self.source


def read_token(cfg: TokenConfig, *, test_mode: bool) -> str:
    """Return the access token, or raise :class:`TokenError` with a message that never contains it."""
    if test_mode:
        return _accept(os.environ.get(TEST_TOKEN_ENV, ''), f'{TEST_TOKEN_ENV} (local test server)')
    if os.environ.get(TEST_TOKEN_ENV):
        raise TokenError(f'{TEST_TOKEN_ENV} is set, but the API endpoints are the real Meta ones. That variable only '
                         f'works together with a local test server; unset it.', fix=f'unset {TEST_TOKEN_ENV}')
    if cfg.source == 'env':
        return _accept(os.environ.get(cfg.env_var, ''), f'environment variable {cfg.env_var}',
                       fix=f'in this shell: read -rs {cfg.env_var} && export {cfg.env_var} (paste the token; nothing '
                           f'is echoed or kept in the history), or configure a lasting [token] source: {SETUP_STORE}')
    if cfg.source == 'file':
        if not cfg.path:
            raise ConfigError('[token] path is required when source = "file"')
        return _accept(read_token_file(cfg.path), f'token file {cfg.path}', fix=_file_howto(cfg.path))
    if cfg.source == 'keychain':
        return _accept(_keychain(cfg), f'Keychain service {cfg.keychain_service!r}',
                       fix=keychain_howto(cfg.keychain_service or 'ig-publish', cfg.keychain_account))
    if cfg.source == 'ssm':
        return _accept(_ssm(cfg), f'SSM parameter {cfg.ssm_parameter}',
                       fix='store the token in the parameter (docs/deploy-ec2.md, step 2)')
    raise ConfigError(f'[token] source must be one of {", ".join(SOURCES)} (got {cfg.source!r})')


def _file_howto(path: str) -> str:
    p = shlex.quote(path)
    # mkdir -p (not install -d): an existing directory such as $HOME keeps its mode.
    d = shlex.quote(os.path.dirname(path) or '.')
    return (f"mkdir -p -m 700 {d} && read -rs T && (umask 077 && printf '%s' \"$T\" > {p}); unset T  (paste the "
            f'token; on a server the token unit writes the file: docs/deploy-ec2.md)')


def _looks_like_a_command(value: str) -> bool:
    low = value.lower()
    return 'add-generic-password' in low or 'pbpaste' in low or low.startswith(('security ', 'read ', 'export ',
                                                                                 'printf ', 'echo '))


def _accept(value: str, where: str, fix: str = '') -> str:
    """Check the shape of what a source returned. ``fix`` is that source's own store command (how-to)."""
    token = (value or '').strip()
    how = f': {fix}' if fix else f' ({SETUP_STORE})'
    if not token:
        raise TokenError(f'No access token: {where} is empty or not set.', fix=fix)
    if _looks_like_a_command(token):
        raise TokenError(f'The value in {where} is a shell command, not a token: the clipboard still held the '
                         f'command when it was stored.',
                         fix='store it again so that only the token is stored (the command and the token go in '
                             f'separately, never pasted together or as two lines at once){how}')
    if any(ch.isspace() for ch in token):
        raise TokenError(f'The access token from {where} contains whitespace; check the stored value.',
                         fix=f'store only the token itself, one line without spaces{how}')
    if token[0] in '"\'' or token[-1] in '"\'':
        raise TokenError(f'The access token from {where} is wrapped in quotes.',
                         fix=f'store the token without the quotes{how}')
    redact.register(token)
    return token


def read_token_file(path: str) -> str:
    """Read a token file after checking it is a regular file, owned by us and not readable by group/others.

    The checks run on the opened descriptor (``fstat``), so the file cannot be swapped between check and read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
    except FileNotFoundError:
        # Temporary: on a server the file is written by a separate unit that may not have run yet.
        raise TokenError(f'Token file not found: {path}', temporary=True, fix=_file_howto(path)) from None
    except OSError as e:
        raise TokenError(f'Cannot open token file {path}: {e.strerror}',
                         fix=f'the user that runs ig-publish must own the file and reach its directory: sudo chown '
                             f'"$(id -u)" {shlex.quote(path)} (the Docker image runs as uid 10001)') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TokenError(f'Token file {path} is not a regular file.',
                             fix='[token] path must name the file that holds the token, not a directory or a pipe')
        if st.st_mode & 0o077:
            raise TokenError(f'Token file {path} is accessible to group/others (mode {stat.S_IMODE(st.st_mode):04o}).',
                             fix=f'chmod 600 {shlex.quote(path)}')
        if hasattr(os, 'geteuid') and st.st_uid != os.geteuid():
            raise TokenError(f'Token file {path} is owned by uid {st.st_uid}, not by the current user '
                             f'(uid {os.geteuid()}).',
                             fix=f'sudo chown "$(id -u)" {shlex.quote(path)}, or run ig-publish as uid {st.st_uid} '
                                 f'(the Docker image runs as uid 10001)')
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            if sum(len(c) for c in chunks) > 1 << 20:
                raise TokenError(f'Token file {path} is larger than 1 MiB; it does not look like a token.',
                                 fix=f'the file must hold only the token: {_file_howto(path)}')
    finally:
        os.close(fd)
    try:
        return b''.join(chunks).decode('utf-8')
    except UnicodeDecodeError:
        raise TokenError(f'Token file {path} is not UTF-8 text.',
                         fix=f'the file must hold only the token: {_file_howto(path)}') from None


def _keychain(cfg: TokenConfig) -> str:
    if not cfg.keychain_service:
        raise ConfigError('[token] keychain_service is required when source = "keychain"')
    account = cfg.keychain_account or os.environ.get('USER') or getpass.getuser()
    tool = shutil.which('security')
    if not tool:
        raise TokenError('Token source "keychain" needs the macOS `security` tool, which was not found.',
                         fix=f'the Keychain exists on macOS only: elsewhere use source = "file" or "ssm" '
                             f'({SETUP_STORE})')
    try:
        r = subprocess.run([tool, 'find-generic-password', '-a', account, '-s', cfg.keychain_service, '-w'],
                           capture_output=True, text=True, timeout=cfg.timeout_seconds, stdin=subprocess.DEVNULL,
                           env=scrubbed_env(cfg.env_var))
    except subprocess.TimeoutExpired:
        raise TokenError(f'The Keychain did not answer within {cfg.timeout_seconds:g} s (locked, or a permission '
                         f'dialog is waiting; service {cfg.keychain_service!r}).', temporary=True,
                         fix='unlock the login keychain (security unlock-keychain) or answer the dialog on the screen; '
                             'for unattended runs (a scheduler, ssh) use source = "file"') from None
    if r.returncode == KEYCHAIN_NOT_FOUND:
        raise TokenError(f'No Keychain item for service {cfg.keychain_service!r} and account {account!r}.',
                         fix=keychain_howto(cfg.keychain_service, cfg.keychain_account))
    if r.returncode != 0:
        # stderr carries the reason, never the password (that goes to stdout with -w).
        detail = redact(' '.join((r.stderr or '').split())[-300:]) or 'no message'
        raise TokenError(f'The Keychain could not be read (service {cfg.keychain_service!r}, `security` exit '
                         f'{r.returncode}: {detail}).', temporary=True,
                         fix='unlock the login keychain (security unlock-keychain), or run ig-publish from a logged-in '
                             'desktop session; for unattended runs (a scheduler, ssh, launchd, cron) use '
                             f'source = "file" ({SETUP_STORE})')
    return r.stdout


def _ssm(cfg: TokenConfig) -> str:
    if not cfg.ssm_parameter:
        raise ConfigError('[token] ssm_parameter is required when source = "ssm"')
    aws = shutil.which('aws')
    if not aws:
        raise TokenError('Token source "ssm" needs the AWS CLI (`aws`), which was not found.',
                         fix='install the AWS CLI v2, or let a boot unit write the token to a 0600 file and use '
                             'source = "file" (docs/deploy-ec2.md)')
    cmd = [aws, 'ssm', 'get-parameter', '--name', cfg.ssm_parameter, '--with-decryption',
           '--query', 'Parameter.Value', '--output', 'text']
    if cfg.ssm_region:
        cmd += ['--region', cfg.ssm_region]
    if cfg.ssm_profile:
        cmd += ['--profile', cfg.ssm_profile]
    env = dict(scrubbed_env(cfg.env_var), AWS_PAGER='')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds,
                           stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise TokenError(f'`aws ssm get-parameter` did not finish within {cfg.timeout_seconds:g} s.',
                         temporary=True,
                         fix='check the route to SSM (internet or a VPC endpoint) and the instance credentials; on a '
                             'slow network raise [token] timeout_seconds') from None
    if r.returncode != 0:
        detail = redact((r.stderr or '').strip()[-400:])
        raise TokenError(f'`aws ssm get-parameter` failed (exit {r.returncode}) for {cfg.ssm_parameter}: {detail}',
                         temporary=True,
                         fix='check the parameter name and [token] ssm_region, and that this machine may read it '
                             '(IAM: ssm:GetParameter on the parameter, plus kms:Decrypt for a customer-managed key)')
    return r.stdout
