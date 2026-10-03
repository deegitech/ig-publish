# Security policy

ig-publish handles access tokens that can post to, and delete from, an Instagram account, so we take reports
seriously.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

Security fixes go into the latest release. Please upgrade before reporting.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a vulnerability.**

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/deegitech/ig-publish/security/advisories/new)
(repository *Security* tab -> *Report a vulnerability*). This uses GitHub's private vulnerability reporting, so
only you and the maintainers can see the report.

If that form is not available for some reason, open a public issue that says only that you have a security report
(no details), and a maintainer will open a private advisory for you.

Helpful details:

- the version (`ig-publish --version`), Python version and operating system;
- what an attacker can do, and what they need first (local access, a malicious manifest, a hostile network ...);
- the smallest steps or proof of concept that show it.

**Never include a real access token, account ID or private media in a report.** If a token was exposed, revoke it
first: on Facebook, Settings & privacy → Settings → Business integrations (or Apps and websites) → remove the app, or
change your Facebook password (names may differ); then make a new token and store it again
([docs/troubleshooting.md](docs/troubleshooting.md#token-and-login), "The token leaked"). We can work with redacted
output.

## What to expect

- We aim to acknowledge a report within 7 days and to agree on a plan and timeline with you after triage.
- We will credit you in the advisory and the changelog unless you prefer otherwise.
- We follow coordinated disclosure: please give us a reasonable time to release a fix before going public.

## Scope

In scope: anything in this repository, in particular token handling and redaction, the double-post safeguards
(state, journal, locks, container checks), command-line and configuration parsing, the scheduler, the Docker image
and the deployment examples.

Out of scope: vulnerabilities in Meta's platform (report those to Meta), in Python, FFmpeg or Docker themselves, and
issues that need an already compromised machine or account.
