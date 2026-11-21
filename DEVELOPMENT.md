# Development

## Commands

```sh
make test     # uv run python -m unittest discover -s tests -v
make lint     # ruff
make compile  # byte-compile the package
make check    # compile, lint, and test (what CI runs)
make link     # herdr plugin link .
```

CI runs `make check` on Python 3.10, 3.12, and 3.13.

## Screenshots

`scripts/screenshots.py` renders the images in `docs/screenshots/` from demo
repositories in a throwaway `$HOME`. It writes SVG, and PNG when `rsvg-convert`
is installed:

```sh
uv run python scripts/screenshots.py
```

## Layout

- `herdr_jmpr/config.py`: `names.tsv` loading and workspace labels
- `herdr_jmpr/settings.py`: `config.toml` loading, theme lookup, and defaults
- `herdr_jmpr/frecency.py`: zsh-z and zoxide history, ranking, bucket order, tab and agent data
- `herdr_jmpr/query.py`: fzf extended-search matcher, highlight spans, and match scoring
- `herdr_jmpr/state.py`: canonical and legacy state, recovery, atomic writes, close snapshots
- `herdr_jmpr/herdr.py`: Herdr CLI wrapper over the socket API (`api snapshot`), workspace tokens, and split layout reconstruction
- `herdr_jmpr/picker.py`: backend resolution and the `fzf`, `skim`, and `gum` runners
- `herdr_jmpr/tui.py`: Textual picker, tab tree, modals, and preview
- `herdr_jmpr/cli.py`: flags, `open-picker`, lock transaction, and moves
- `bin/herdr-jmpr`: launcher that uses the plugin virtualenv
- `herdr-plugin.toml`: Herdr manifest (actions, popup panes, startup and event hooks)
- `scripts/screenshots.py`: README screenshot generator

See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request expectations.
