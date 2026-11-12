# herdr-zshz

A [Herdr](https://herdr.dev) plugin that jumps to project workspaces ranked by
your `zsh-z` frecency history, in a Textual picker with fzf-style extended
search, a vim mode and a live project preview.

It is the Herdr plugin port of the `zsh-jump.sh` script: same frecency data
file, same ranking buckets, same workspace mapping rules, same close
reconfirmation and the same split replay when moving a tab.

## Install

```sh
git clone <this repository> ~/git/perso/herdr-zshz
cd ~/git/perso/herdr-zshz
uv sync
herdr plugin link .
```

The manifest declares `uv sync --frozen` as build and startup hook, so the
plugin virtualenv is created (and refreshed) automatically. `bin/herdr-zshz`
uses `<plugin_root>/.venv/bin/python` when present and falls back to
`uv run --project <plugin_root>`.

Requirements: Herdr >= 0.9.0, `uv`, Python >= 3.10 (the virtualenv also needs
`git` for the preview pane).

## Actions

| Action | What it does |
| --- | --- |
| Jump to project workspace… | Open the picker, jump to (or create) the workspace for the pick |
| Move pane to project workspace… | Move the focused pane there instead |
| Move tab to project workspace… | Move the focused tab there, replaying its split layout |
| Jump to directory (all zsh-z)… | Pick from every zsh-z directory, not only git roots |

Each action opens a popup with the picker (`placement = "popup"`, 80% x 80%).

## Configuration

Both files live in the plugin config directory, which
`herdr plugin config-dir herdr-zshz` prints
(`~/.config/herdr/plugins/config/herdr-zshz` by default).

### Workspace labels (`names.tsv`)

Labels default to the directory name. To override them, create `names.tsv`:

```tsv
# <path><TAB><label>
~/git/pac/main	PaC
~/git/pac/paco-cli	PaCo
```

A name also applies to the directories below its path; the closest path wins.
Lines that are empty or start with `#` are ignored. Any other line must contain
exactly one tab separating a non-empty path and a non-empty label; if it does
not, the plugin prints `Invalid names.tsv line <n>: <line>` and exits 1.

### Settings (`config.toml`)

Optional; without it the built-in defaults apply.

```toml
# "insert" (default) filters as you type; "normal" starts in the vim keymap,
# where you navigate with j/k and press i, a or / to start filtering.
start_mode = "normal"
```

`start_mode` accepts `insert` or `normal`. Unknown keys and invalid values make
the plugin print `Invalid <path>: ...` and exit 1 rather than silently ignoring
the file, so typos surface immediately. `HERDR_PLUGIN_CONFIG_DIR` overrides the
directory, which is handy for trying a different config without moving files.

## Picker keys

The picker starts in `INSERT` mode (or `NORMAL` mode when `start_mode = "normal"`
is set in `config.toml`) and lists git repository roots; `alt-a`
(or the `all` action) switches to every zsh-z directory.

Insert mode:

| Key | Action |
| --- | --- |
| typing | filter live (fzf extended search, smartcase) |
| `down`/`up`, `ctrl+n`/`ctrl+p`, `ctrl+j`/`ctrl+k` | move selection |
| `page_down`/`page_up`, `ctrl+f`/`ctrl+b` | page |
| `ctrl+a`/`ctrl+e` | home / end of the query |
| `ctrl+u` | kill to start |
| `ctrl+w`, `alt+backspace` | delete previous path/word segment |
| `ctrl+y` | yank |
| `tab` | fill the query with the highlighted path |
| `escape` | enter normal mode |
| `ctrl+c`/`ctrl+g` | quit |

Normal mode:

| Key | Action |
| --- | --- |
| `j`/`k`/`down`/`up` | move selection |
| `gg` / `G` | first / last item |
| `ctrl+d`/`ctrl+u` | half page |
| `ctrl+f`/`ctrl+b` | full page |
| `H`/`M`/`L` | top / middle / bottom of viewport |
| `i`, `a`, `/` | back to insert mode |
| `dd` | clear the query |
| `C` | clear the query and enter insert mode |
| `x` | close the project workspace |
| `p` / `t` | move pane / tab |
| `o`, `l`, `enter` | accept the selection |
| `?` | toggle help |
| `q`, `escape` | quit |

Both modes: `enter` jumps (a typed `/` or `~` path, or the query itself when it
is an existing directory and nothing else matches, wins over the highlighted
row), `alt+p` moves the pane, `alt+t` moves the tab, `alt+a` toggles the
`git>`/`all>` source, `ctrl+x` closes the project's workspace, `ctrl+h` toggles
help.

Search follows fzf `-e` extended search: space separated clauses are ANDed,
`|` separates OR alternatives inside a clause (`foo | bar baz`), and terms can
be negated (`!term`, `!^prefix`, `!suffix$`), anchored (`^prefix`, `suffix$`)
or forced to a plain substring (`'exact`). Rows keep their frecency order and
matched substrings are highlighted.

The cursor row is drawn with a `▌` pointer and painted in the theme's primary
colour with a contrasting foreground, so it stays visible even on terminals
where reverse video is indistinguishable. Rows also carry `◆` for the current
project and `●` for projects whose workspace is already open.

## How it decides where to go

1. A live workspace with the target label already mapped to the target path is
   used.
2. Otherwise an unmapped workspace with the label that has a pane sitting in the
   target path is used, and its matching tab is focused.
3. Otherwise the picker asks whether to create a new workspace or use one of the
   currently unmapped workspaces with that label.
4. Otherwise a workspace is created with `herdr workspace create`.

The mapping is updated under an exclusive `flock` on the state file lock, with
no user interaction inside the lock. Focus commands run after the lock is
released. When the server restarts, mappings are validated against
`session.json` before being kept.

Moves happen only after the picker has exited and all Herdr calls run with
`stdin` closed and `SIGHUP` ignored, so a popup that is hung up because its tab
became empty cannot abort the mapping or focus steps. Errors are appended to
`<state dir>/last-error.log`.

## State

The state directory is `${XDG_STATE_HOME:-~/.local/state}/herdr/plugins/state/herdr-zshz`
(or `HERDR_PLUGIN_STATE_DIR`). Each Herdr socket gets a
`<sha256(socket path)>.json` mapping file and a `.lock` next to it.

The mapping from the old `zsh-jump` script
(`${XDG_STATE_HOME:-~/.local/state}/herdr/zsh-jump/<hash>.json`) is imported
non-destructively the first time it is needed: the legacy file is read and
validated but never modified or removed.

## Development

```sh
make test     # uv run python -m unittest discover -s tests -v
make lint     # ruff
make compile  # byte-compile the package
make link     # herdr plugin link .
```

Layout:

- `herdr_zshz/config.py` — `names.tsv` loading and workspace labels
- `herdr_zshz/settings.py` — `config.toml` loading and defaults
- `herdr_zshz/frecency.py` — zsh-z data parsing, ranking and bucket ordering
- `herdr_zshz/query.py` — fzf extended-search matcher and highlight spans
- `herdr_zshz/state.py` — canonical/legacy state, recovery, atomic writes,
  close snapshots
- `herdr_zshz/herdr.py` — Herdr CLI wrapper and split layout reconstruction
- `herdr_zshz/tui.py` — Textual picker, modals and preview
- `herdr_zshz/cli.py` — flags, `open-picker`, lock transaction and moves

## License

MIT, see [LICENSE](LICENSE).
