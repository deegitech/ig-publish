# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-04

First public release.

### Added

- `ig-publish` command and `ig_publish` library: `init`, `doctor`, `check`, `prep`, `verify`, `plan`, `publish`,
  `status`, `delete`, `import`, `ack` and `schedule`.
- `doctor [--offline]`: read-only setup checks in setup order (configuration, manifest, FFmpeg with the encoders and
  filters `prep` uses, prepared media, state directory, token source and shape, token validity, permissions,
  Instagram account and type, Page link and tasks, quota, API usage). Every `✗` comes with the exact fix; exit 0
  when nothing failed, 1 otherwise. It writes nothing and never prints the token.
- A one-line `fix:` under every known Meta error code and subcode (token, permission, identity, publishing,
  rate-limit and upload errors), in the CLI, in publish stops and in scheduler notifications. Token errors name
  their fix too, and a stored shell command, a quoted token, a 128-character (truncated) token and an Instagram
  Login token are recognised without ever showing the value.
- `docs/setup.md` (setup from zero, every console click) and `docs/troubleshooting.md` (every error with its fix).
- Dry run by default everywhere: `publish`, `delete`, `import` and `ack` need `--apply`, and the scheduler publishes
  only when started with `schedule run --apply` (the Docker image's default command is `schedule dry`).
- Stories and Reels through the Instagram API with Facebook Login, with resumable uploads to rupload (no public
  hosting needed). Still images become short video stories.
- `prep`: ffmpeg re-encode to a conservative profile (H.264 High, no B-frames, closed GOP, AAC <= 128 kbps 48 kHz,
  `moov` first, no edit list) with independent verification, letterboxing or cropping for non-9:16 sources and
  copy-through for sources that already pass.
- Publishing safeguards: every container uploaded and `FINISHED` before the first publish; strict manifest order;
  never re-publish (state file plus a container status check after any error); holds on spam, limit and rate-limit
  codes; burst guard; reel spacing across runs; quota check; unknown-post and duplicate-caption checks; run lock;
  lost-state guard; journaled writes; read-back.
- Configuration file (TOML or JSON, strict: unknown keys are errors) with a JSON Schema; manifest JSON Schema.
- Configurable caption guards: limits, banned words, licensed-music names, required text, season-only words; source
  deny patterns; season windows.
- Token sources: environment variable, `0600` file, macOS Keychain, AWS SSM Parameter Store. Redaction of the token
  and token-like strings everywhere, including unexpected errors; helper processes (ffmpeg, the notifier ...) run
  without the token variable.
- Scheduler with a done file, retries after temporary stops (exit 75), protection against late bursts, graceful
  `SIGTERM` handling, notifications and its log on stdout (`docker logs`).
- `ack`: acknowledge posts made by hand or by another tool, so scheduled runs continue without publishing anything.
- Docker image (non-root, read-only), Compose example, AWS EC2 guide with a systemd unit that fetches the token from
  SSM into tmpfs.
- Offline test suite: a local mock of the Graph API and rupload, synthetic videos, fake `security` and `aws` tools.

[Unreleased]: https://github.com/deegitech/ig-publish/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deegitech/ig-publish/releases/tag/v0.1.0
