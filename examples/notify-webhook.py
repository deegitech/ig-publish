#!/usr/bin/env python3
"""Example notifier for `[schedule] notify_command`: post the message to a chat webhook.

    notify_command = ["python3", "/data/notify-webhook.py", "--url-file", "/data/secrets/webhook"]

The scheduler appends the message as the last argument. The webhook URL is a secret, so it is read from a file
(`--url-file PATH`, else $NOTIFY_WEBHOOK_FILE, else /run/secrets/ig-publish/webhook), never from the command line.
The file must be a regular file owned by the user that runs this script and closed to group and others
(chmod 600). The JSON body carries both "text" (Slack-style) and "content" (Discord-style). Standard library only.
Exit status 0 when the webhook accepted the message, 1 otherwise (the scheduler logs a failure).
"""
import json
import os
import stat
import sys
import urllib.error
import urllib.request

DEFAULT_FILE = '/run/secrets/ig-publish/webhook'


def read_url(path: str) -> str:
    """Open first, then check the opened file, so it cannot be swapped between the check and the read."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'{path} is not a regular file')
        if st.st_uid != os.geteuid():
            raise ValueError(f'{path} is owned by uid {st.st_uid}, not by the user running this (uid {os.geteuid()})')
        if st.st_mode & 0o077:
            raise ValueError(f'{path} is accessible to group or others; run: chmod 600 {path}')
        return os.read(fd, 4096).decode('utf-8').strip()
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    args = argv[1:]
    path = os.environ.get('NOTIFY_WEBHOOK_FILE', DEFAULT_FILE)
    if len(args) >= 2 and args[0] == '--url-file':
        path, args = args[1], args[2:]
    message = args[-1] if args else ''
    try:
        url = read_url(path)
    except OSError as e:
        print(f'cannot read the webhook file {path}: {e.strerror}', file=sys.stderr)
        return 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not url.startswith('https://'):
        print('the webhook URL must start with https://', file=sys.stderr)
        return 1
    body = json.dumps({'text': f'ig-publish: {message}', 'content': f'ig-publish: {message}'}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 0 if 200 <= r.status < 300 else 1
    except urllib.error.HTTPError as e:  # the URL itself is never printed
        print(f'the webhook answered HTTP {e.code}', file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        print(f'the webhook request failed: {getattr(e, "reason", e)}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
