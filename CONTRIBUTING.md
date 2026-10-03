# Contributing to ig-publish

Thanks for helping! Bug reports, documentation fixes and pull requests are all welcome.

## Ground rules

- **Never commit real data.** No access tokens, account/page/media IDs, emails, private file paths, or real
  captions from your account, not even in tests or issue text. Use obvious placeholders: `17841401234567890` for an
  Instagram user ID, `1234567890` for a Page ID, `you@example.com`, `example.brand`. CI runs a gitleaks scan on every
  push and pull request.
- **Tests stay offline.** They must never call Meta, AWS or any other live service. Use the mock server in
  `tests/mock_graph.py`, fake command-line tools on `PATH`, and synthetic media made with ffmpeg.
- **Safety first.** Changes to the publish path must keep every guarantee in the README's
  [Security model](README.md#security-model). If you change how a write happens, add a test that shows a crash
  or an error at that point cannot cause a double post.
- **Standard library only** at runtime (the one exception is `tomli` on Python 3.10, which has no `tomllib`). A new
  runtime dependency needs a very good reason and should be optional.
- Be kind; see the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.10+ and FFmpeg 5.1+ (`ffmpeg` and `ffprobe` on `PATH`).

```bash
git clone https://github.com/deegitech/ig-publish.git
cd ig-publish
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # the whole suite, about 40 seconds
ruff check src tests examples
```

`python -m unittest discover -s tests` works too (the JSON Schema tests are skipped without `jsonschema`).
The end-to-end tests run the real CLI as a subprocess against the local mock; they are skipped if ffmpeg is not
installed.

Style: the code is not run through an auto-formatter, so please do not reformat whole files (`ruff format` would
rewrite most of them). Keep the surrounding style: single quotes, lines up to 120 characters, type hints where they
are cheap. `ruff check` is the gate, with the version pinned in `pyproject.toml`.

## Pull requests

1. Open an issue first for larger changes, so we can agree on the approach.
2. Keep pull requests focused; one topic each.
3. Add or update tests. A bug fix should come with a test that fails without it.
4. Update the README, `docs/` and `CHANGELOG.md` ("Unreleased") when behaviour or options change.
5. Make sure `pytest` and `ruff check` pass locally; CI runs them on Python 3.10-3.13.

Commit messages: a short imperative summary line ("Add --approve to plan"), a blank line, then the why.

## Platform behaviour

When you document how Instagram or Meta behaves, say where it comes from: link Meta's documentation, or mark it as
**observed (Mon YYYY)**, e.g. *observed (Oct 2026)*, if you saw it yourself. Do not present observations as rules.
Console menus are renamed often: give the menu path plus the label variants you have seen, and say that names may
differ. A new Meta error code that ig-publish should explain goes into `src/ig_publish/hints.py` (with a test) and
`docs/troubleshooting.md`.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
