# herdr-jmpr

A [Herdr](https://herdr.dev) plugin that jumps to project workspaces ranked by
your `zsh-z` frecency history. It includes a theme-aware Textual workspace
switcher (with fzf-style extended search, vim normal mode, and a live workspace
preview) alongside `fzf`, `skim` (`sk`), and `gum` backends.

## Install

```sh
git clone <this repository> ~/git/perso/herdr-jmpr
cd ~/git/perso/herdr-jmpr
uv sync
herdr plugin link .
```

The manifest declares `uv sync --frozen` as build and startup hook, so the
plugin virtualenv is created (and refreshed) automatically. `bin/herdr-jmpr`
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
`herdr plugin config-dir herdr-jmpr` prints
(`~/.config/herdr/plugins/config/herdr-jmpr` by default).

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
# Picker backend: "textual" (default), "fzf", "skim", "gum", or "auto".
# "auto" probes PATH in order: fzf -> sk -> gum -> textual.
backend = "textual"

# Textual theme: "auto" (default, syncs Herdr's [theme].name or falls back to
# "herdr-ink"), or any registered Textual theme name such as "dracula",
# "tokyo-night", "catppuccin-mocha", "rose-pine", "nord", or "gruvbox".
theme = "auto"

# Textual initial mode: "insert" (default) filters as you type; "normal" starts
# in the vim keymap, where you navigate with j/k and press i, a, or / to filter.
start_mode = "insert"

# History source: "auto" (default, prefers zsh-z and falls back to zoxide),
# "zshz", or "zoxide".
history = "auto"

# Show open workspace tabs as a Unicode tree with agent badges and direct tab
# jumping (default: true). Override per invocation with --tabs / --no-tabs or
# toggle live in Textual with alt-e (e in normal mode).
show_tabs = true
```

You can also override the backend per invocation with `--backend <name>` (both
on `herdr-jmpr` and `herdr-jmpr open-picker`) or via `HERDR_JMPR_BACKEND`, and
the initial mode with `--vim` / `--normal`, `--no-vim` / `--insert`, or
`--mode <insert|normal>` (`HERDR_JMPR_START_MODE`).

Unknown keys and invalid values make the plugin print `Invalid <path>: ...` and
exit 1 rather than silently ignoring the file, so typos surface immediately.
`HERDR_PLUGIN_CONFIG_DIR` overrides the directory, which is handy for trying a
different config without moving files.

## Picker backends and keys

- `textual` (default) runs the built-in theme-aware workspace switcher documented
  below.
- `fzf` uses a reverse 16-color layout with `Enter` to jump, `alt-p` to move
  pane, `alt-t` to move tab, `alt-a` to toggle between `git>` and `all>`,
  `alt-o` to toggle open workspaces only (`open>`), `ctrl-x` to close the
  selected workspace and reload live markers, and `ctrl-h` to toggle the bottom
  help drawer.
- `skim` (`sk`) supports the same `Enter`, `alt-p`, `alt-t`, `alt-a`, `alt-o`,
  `ctrl-x`, and `ctrl-h` bindings.
- `gum` runs `gum filter` with frecency order preserved and typed-path support.

### Textual picker keys

The Textual picker starts in insert mode (or normal mode when
`start_mode = "normal"` is set in `config.toml`) and lists git repository
roots; `alt+a` (or the `all` action) switches to every zsh-z directory.

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
| `o` | toggle open workspaces only |
| `v` | toggle workspace preview |
| `T` | switch theme live |
| `p` / `t` | move pane / tab |
| `l`, `enter` | accept the selection |
| `?` | toggle help |
| `q`, `escape` | quit |

Both modes: `enter` jumps (a typed `/` or `~` path, or the query itself when it
is an existing directory and nothing else matches, wins over the highlighted
row), `alt+p` moves the pane, `alt+t` moves the tab, `alt+a` toggles the
`git` / `all` scope, `alt+o` toggles open workspaces only, `alt+v` toggles the
workspace preview, `ctrl+t` opens the live theme switcher, `ctrl+x` closes the
project's workspace, `?` (normal mode) toggles help. Mouse clicks select rows
and double-clicking jumps immediately.

Search follows fzf `-e` extended search: space separated clauses are ANDed,
`|` separates OR alternatives inside a clause (`foo | bar baz`), and terms can
be negated (`!term`, `!^prefix`, `!suffix$`), anchored (`^prefix`, `suffix$`)
or forced to a plain substring (`'exact`). Rows keep their frecency order and
matched substrings are highlighted.

The active row carries a `▌` pointer over a subtle tinted highlight surface so
directory paths, `[label]` tags, `⚡` active-agent badges, and fuzzy match
underlines stay legible. Rows also show `◆` for the current project and `●` for
projects whose workspace is already open.

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

The state directory is `${XDG_STATE_HOME:-~/.local/state}/herdr/plugins/state/herdr-jmpr`
(or `HERDR_PLUGIN_STATE_DIR`). Each Herdr socket gets a
`<sha256(socket path)>.json` mapping file and a `.lock` next to it.

## Development

```sh
make test     # uv run python -m unittest discover -s tests -v
make lint     # ruff
make compile  # byte-compile the package
make link     # herdr plugin link .
```

Layout:

- `herdr_jmpr/config.py`: `names.tsv` loading and workspace labels
- `herdr_jmpr/settings.py`: `config.toml` loading, theme lookup, and defaults
- `herdr_jmpr/frecency.py`: zsh-z data parsing, ranking, and bucket ordering
- `herdr_jmpr/query.py`: fzf extended-search matcher and highlight spans
- `herdr_jmpr/state.py`: canonical/legacy state, recovery, atomic writes,
  close snapshots
- `herdr_jmpr/herdr.py`: Herdr CLI wrapper and split layout reconstruction
- `herdr_jmpr/picker.py`: backend resolution and `fzf`, `skim`, and `gum` runners
- `herdr_jmpr/tui.py`: theme-aware Textual picker, modals, and preview
- `herdr_jmpr/cli.py`: flags, `open-picker`, lock transaction, and moves

## License

MIT, see [LICENSE](LICENSE).
