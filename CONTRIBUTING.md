# Contributing

Thanks for helping. sc-telemetry is an unofficial, community tool; please keep changes small and focused.

## Dev setup

```bash
uv sync                 # installs runtime deps + dev group (pytest, httpx)
uv run pytest -q        # full suite
uvx ruff check .        # optional lint
```

- Python 3.13 via [uv](https://docs.astral.sh/uv/). Do not use `pip` directly.
- The dashboard JS tests need `node` on `PATH` and are skipped without it.
- CI runs the suite on Ubuntu and Windows (`.github/workflows/ci.yml`).

## Ground rules

- **Tests must not need the game.** Use synthetic frames, `--mock`, synthetic `Game.log` lines and the fixtures in `tests/fixtures/`. A test that needs local data (e.g. recordings in `.tmp/`) must skip cleanly when it is missing.
- **No CIG-derived files.** Never commit `.proto` files, generated stubs, extracted game data or anything copied from the game install. The schema is extracted at runtime from the user's own `StarCitizen.exe`.
- **Privacy in fixtures and docs:** synthetic handles (e.g. `TestPilot_Alt`), zero-padded or obviously fake entity ids (`000000000001`), TEST-NET addresses (`192.0.2.x`) and made-up balances only. Never commit real recordings (`*.jsonl`), real `Game.log` excerpts with handles, ids, server addresses or session ids, or personal paths.
- **Local-only by design.** Keep loopback defaults, and put any new mutating or sensitive endpoint behind the existing Origin/Host guard. See [SECURITY.md](SECURITY.md).
- **Trigger actions** send real inputs to the game. New ones must stay inside the game's allowlist; destructive ones need the `confirm` flow.

## Docs

`docs/` is an Obsidian-compatible vault:

- kebab-case filenames; every note listed in both `docs/Home.md` (wikilinks) and `docs/README.md` (plain links);
- wikilinks only in those index notes, always paired with a plain Markdown link;
- regenerate frontmatter after adding or renaming notes: `uv run python scripts/obsidian_frontmatter.py`;
- record notable changes in [`docs/changelog.md`](docs/changelog.md).

## Pull requests

Describe what changed and how you tested it (live game, `--mock`, `--replay`, or tests only). Mention the game channel and build if you verified against a live client. By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
