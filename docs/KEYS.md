# Textual picker keys

## Insert mode

| Key | Action |
| --- | --- |
| typing | filter (fzf extended search, smartcase) |
| `down`/`up`, `ctrl+n`/`ctrl+p`, `ctrl+j`/`ctrl+k` | move the selection |
| `page_down`/`page_up`, `ctrl+f`/`ctrl+b` | page |
| `alt+1`..`alt+0` | jump to the numbered row (first 10) |
| `ctrl+a`/`ctrl+e` | start / end of the query |
| `ctrl+u` | delete to start |
| `ctrl+w`, `alt+backspace` | delete the previous word or path segment |
| `ctrl+y` | yank the query |
| `ctrl+down`/`ctrl+up` | next / previous workspace, skipping tab rows |
| `left`/`right` | fold / unfold the selected workspace |
| `tab` | fill the query with the highlighted path |
| `escape` | normal mode |
| `ctrl+c`/`ctrl+g` | quit |

## Normal mode

| Key | Action |
| --- | --- |
| `j`/`k`/`down`/`up` | move the selection |
| `1`..`9`, `0` | jump to the numbered row (first 10) |
| `gg` / `G` | first / last item |
| `ctrl+d`/`ctrl+u` | half page |
| `ctrl+f`/`ctrl+b` | full page |
| `H`/`M`/`L` | top / middle / bottom of the viewport |
| `]` / `[` | next / previous workspace, skipping tab rows |
| `h`, `left` | fold the workspace |
| `right`, `space` | unfold, or toggle the fold |
| `i`, `a`, `/` | insert mode |
| `dd` | clear the query |
| `C` | clear the query and enter insert mode |
| `y` | mark a tab (on a workspace row, all its tabs) |
| `p` | paste the marked tabs here and focus the moved tab; with no marks, move the pane |
| `t` | move the tab |
| `escape` | clear the marks; with no marks, quit |
| `x` | close the project workspace |
| `o` | open workspaces only |
| `tab` | show only agent tabs, flat, without the workspace tree |
| `e` | show or hide the tab tree |
| `v` | show or hide the preview |
| `T` | switch theme |
| `l`, `enter` | accept the selection |
| `?` | help |
| `q` | quit |

## Both modes

| Key | Action |
| --- | --- |
| `enter` | jump (a typed path wins over the highlighted row) |
| `alt+p` / `alt+t` | move the pane / tab |
| `alt+a` | git roots / all history directories |
| `alt+o` | open workspaces only |
| `alt+e` | show or hide the tab tree |
| `alt+v` | show or hide the preview |
| `ctrl+t` | live theme switcher |
| `ctrl+x` | close the project's workspace |
| `ctrl+h` | help |

Mouse: click a row to jump to it.
