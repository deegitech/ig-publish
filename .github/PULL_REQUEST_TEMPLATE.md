## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `pytest` passes locally
- [ ] `ruff check src tests examples` passes
- [ ] README / docs / CHANGELOG ("Unreleased") updated if behaviour or options changed
- [ ] The safety guarantees still hold (no double posts, dry run by default, token never printed or stored)
- [ ] No real tokens, account/page/media IDs, emails or private paths anywhere (code, tests, docs, commit messages)
- [ ] Platform behaviour is either linked to Meta's docs or marked "observed (Mon YYYY)", e.g. "observed (Oct 2026)"
