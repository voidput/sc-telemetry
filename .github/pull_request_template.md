## What

## Why

## How tested

- [ ] `uv run pytest -q`
- [ ] `--mock` / `--replay` dashboard check
- [ ] Live game (channel + build: )

## Checklist

- [ ] No CIG-derived files (`.proto`, stubs, extracted game data) added
- [ ] Fixtures and docs use synthetic handles / ids / addresses only; no real recordings or logs
- [ ] New mutating or sensitive endpoints sit behind the Origin/Host guard
- [ ] Docs updated (`docs/`, indexes, `scripts/obsidian_frontmatter.py` run) and `docs/changelog.md` entry added
