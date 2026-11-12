"""Textual two-panel picker with fzf-style search and a vim mode."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

from .config import workspace_label
from .frecency import (
    CURRENT_MARK,
    OPEN_MARK,
    current_project,
    display_path,
    open_paths,
    rank_paths,
)
from .herdr import Herdr, HerdrError
from .query import match_spans
from .state import StateError, snapshot_close_targets

CANCELLED = "cancelled"
PREVIEW_MIN_WIDTH = 90
MAX_CLOSE_ROUNDS = 5


@dataclass
class Row:
    path: str
    shown: str
    marker: str
    label: str


@dataclass
class Result:
    kind: str
    path: str
    choice: tuple[str, str | None] | None = None
    src_pane: str = ""
    src_ws: str = ""
    src_tab: str = ""
    src_layout: dict | None = None
    tab_label: str | None = None


def _git(path: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", path, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


class Bar(Static):
    """A single-line bar painted from its own width.

    Right-aligned fields need the widget's *final* width. Building the text
    while the widget paints (rather than pushing it in on resize) avoids the
    one-frame lag where ``content_size`` still describes the previous layout.
    """

    def __init__(self, builder, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self._builder = builder

    def render(self) -> Content:
        width = self.content_size.width or self.size.width
        text = self._builder(max(10, width or 10))
        # Resolve the style names into Style objects, as Static.update() would,
        # so spans keep their attributes for anything inspecting them.
        return Content.from_rich_text(text, console=self.app.console)


class RowList(Static):
    """The rendered picker rows; selection is drawn into the text."""

    POINTER = "\u258c"

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.rows: list[Row] = []
        self.spans: list[list[tuple[int, int]]] = []
        self.selected = 0
        self.selected_row_style = "bold reverse"

    def on_resize(self, event: events.Resize) -> None:
        # Layout has settled by the time this fires, so the sibling fields can
        # re-render against their real widths.
        reflow = getattr(self.app, "reflow", None)
        if reflow is not None:
            reflow()

    def set_rows(
        self,
        rows: list[Row],
        spans: list[list[tuple[int, int]]],
        selected: int,
        selected_row_style: str | None = None,
    ) -> None:
        self.rows = rows
        self.spans = spans
        self.selected = selected
        if selected_row_style:
            self.selected_row_style = selected_row_style
        self.styles.height = max(len(rows), 1)
        self.update(self._build_text())

    def _row_width(self) -> int:
        for width in (self.size.width, self.container_size.width):
            if width > 0:
                return width
        return 0

    def _build_text(self) -> Text:
        text = Text()
        width = self._row_width()
        for index, row in enumerate(self.rows):
            line = Text()
            selected = index == self.selected
            # fzf's pointer bar: a colour-independent cue that survives
            # terminals where reverse video is indistinguishable.
            line.append(self.POINTER if selected else " ", style="bold")
            if row.marker == CURRENT_MARK:
                marker_style = "bold" if selected else "bold yellow"
            elif row.marker == OPEN_MARK:
                marker_style = "bold" if selected else "bold cyan"
            else:
                marker_style = ""
            line.append(row.marker, style=marker_style)
            line.append(" ")
            # row.shown is appended untouched so the match spans keep their
            # offsets; hierarchy is painted over slices of that run.
            shown = row.shown
            suffix_start = shown.rfind("  [")
            if suffix_start != -1 and shown.endswith("]"):
                body, suffix = shown[:suffix_start], shown[suffix_start:]
            else:
                body, suffix = shown, ""
            dir_end = body.rfind("/") + 1
            hot = row.marker in (CURRENT_MARK, OPEN_MARK)
            line.append(shown)
            offset = 3
            if selected:
                # No foreground colours here: selected_row_style wins.
                if len(body) > dir_end:
                    line.stylize("bold", offset + dir_end, offset + len(body))
            else:
                if dir_end:
                    line.stylize("dim", offset, offset + dir_end)
                if hot and len(body) > dir_end:
                    line.stylize("bold", offset + dir_end, offset + len(body))
                if suffix:
                    suffix_style = "cyan" if hot else "dim italic cyan"
                    line.stylize(suffix_style, offset + len(body), offset + len(shown))
            match_style = "bold underline" if selected else "bold yellow underline"
            for start, end in self.spans[index]:
                start = max(0, min(start, len(shown)))
                end = max(start, min(end, len(shown)))
                if end <= start:
                    continue
                line.stylize(match_style, offset + start, offset + end)
            if width:
                # Truncate instead of wrapping, and give the cursor row a bar
                # that spans the whole column rather than just its text.
                line.truncate(width, overflow="ellipsis", pad=selected)
            if selected:
                line.stylize(self.selected_row_style)
            text.append_text(line)
            text.append("\n")
        return text


class ConfirmCloseModal(ModalScreen[bool]):
    def __init__(self, label: str, agents: list[str]):
        super().__init__()
        self.label = label
        self.agents = agents

    def _content(self) -> Text:
        text = Text()
        text.append("Close ", style="bold")
        text.append(self.label, style="bold yellow")
        text.append("?\n\n")
        if self.agents:
            text.append("Running there: ", style="dim")
            text.append(", ".join(self.agents), style="bold red")
            text.append("\n\n")
        text.append("[", style="dim")
        text.append("y", style="bold reverse")
        text.append("/", style="dim")
        text.append("N", style="bold reverse")
        text.append("]", style="dim")
        text.append(" close    ", style="dim")
        text.append(" Esc ", style="bold")
        text.append(" cancel", style="dim")
        return text

    def compose(self) -> ComposeResult:
        panel = Static(self._content(), id="confirm")
        panel.border_title = " Confirm Close "
        yield panel

    def on_key(self, event: events.Key) -> None:
        event.stop()
        if event.key in ("y", "Y"):
            self.dismiss(True)
        elif event.key in ("n", "N", "escape", "enter", "q"):
            self.dismiss(False)


class SelectWorkspaceModal(ModalScreen[tuple[str, str | None] | None]):
    def __init__(self, label: str, unknown_ids: list[str]):
        super().__init__()
        self.label = label
        self.unknown_ids = unknown_ids
        self.index = 0

    def options(self) -> list[str]:
        return ["Create new workspace"] + [
            f"Use existing workspace {workspace_id}" for workspace_id in self.unknown_ids
        ]

    def compose(self) -> ComposeResult:
        panel = Static(self._option_text(), id="select")
        panel.border_title = " Workspace Conflict "
        yield panel

    def _selection_style(self) -> str:
        style = getattr(self.app, "selection_style", None)
        return style() if callable(style) else "bold reverse"

    def _option_text(self) -> Text:
        text = Text()
        text.append("Select workspace for ", style="bold")
        text.append(self.label, style="bold yellow")
        text.append("\n\n")
        for index, option in enumerate(self.options()):
            if index == self.index:
                style = self._selection_style()
                text.append("\u258c ", style=style)
                text.append(option, style=style)
            else:
                text.append("  " + option)
            if index < len(self.options()) - 1:
                text.append("\n")
        return text

    def on_key(self, event: events.Key) -> None:
        event.stop()
        if event.key in ("escape", "q"):
            self.dismiss(None)
        elif event.key in ("down", "j", "ctrl+n", "tab"):
            self.index = (self.index + 1) % len(self.options())
            self.query_one("#select", Static).update(self._option_text())
        elif event.key in ("up", "k", "ctrl+p"):
            self.index = (self.index - 1) % len(self.options())
            self.query_one("#select", Static).update(self._option_text())
        elif event.key in ("enter", "space"):
            self.query_one("#select", Static).update(self._option_text())
            if self.index == 0:
                self.dismiss(("create", None))
            else:
                self.dismiss(("use", self.unknown_ids[self.index - 1]))


HELP_GROUPS = [
    (
        "Global",
        [
            ("enter", "jump / accept"),
            ("alt-p", "move pane here"),
            ("alt-t", "move tab here"),
            ("alt-a", "git / all dirs"),
            ("ctrl-x", "close workspace"),
            ("? ctrl-h", "toggle help"),
        ],
    ),
    (
        "Insert",
        [
            ("a-z", "type to filter"),
            ("ctrl-n/p", "move cursor"),
            ("tab", "fill selected path"),
            ("ctrl-a/e", "query start / end"),
            ("ctrl-w", "delete word"),
            ("escape", "normal mode"),
        ],
    ),
    (
        "Normal",
        [
            ("j/k", "move cursor"),
            ("gg G", "first / last"),
            ("ctrl-d/u", "half page up/down"),
            ("H M L", "top/mid/bottom"),
            ("i a /", "insert mode"),
            ("dd", "clear query"),
            ("x", "close workspace"),
            ("p t", "move pane/tab"),
            ("q", "quit"),
        ],
    ),
]


class HelpModal(ModalScreen[None]):
    @staticmethod
    def _entry(key: str, description: str) -> Text:
        entry = Text()
        entry.append(f"  {key:<12}", style="bold")
        entry.append(description, style="dim")
        return entry

    def _content(self) -> Text:
        text = Text()
        for group_index, (title, items) in enumerate(HELP_GROUPS):
            if group_index:
                text.append("\n")
            text.append(title.upper(), style="bold cyan")
            text.append("\n")
            half = (len(items) + 1) // 2
            columns = [items[:half], items[half:]]
            widths = [
                max(
                    (cell_len(self._entry(key, description).plain) for key, description in column),
                    default=0,
                )
                for column in columns
            ]
            for index in range(half):
                for column_index, column in enumerate(columns):
                    if index < len(column):
                        entry = self._entry(*column[index])
                        text.append_text(entry)
                        if column_index == 0:
                            text.append(" " * (widths[0] - entry.cell_len + 2))
                    elif column_index == 0:
                        text.append(" " * (widths[0] + 2))
                text.append("\n")
        return text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-scroll"):
            panel = Static(self._content(), id="help")
            panel.border_title = " Keyboard Shortcuts "
            yield panel

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss(None)


class PickerApp(App[Result | None]):
    BINDINGS = [
        Binding("ctrl+c", "quit_now", show=False, priority=True),
        Binding("ctrl+g", "quit_now", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    Screen { layout: vertical; background: $background; }
    #title { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #query { height: 1; background: $boost; padding: 0 1; }
    #body { height: 1fr; background: $background; }
    #list-scroll { width: 1fr; height: 1fr; scrollbar-size-vertical: 1; }
    #list { text-wrap: nowrap; text-overflow: ellipsis; }
    #preview {
        width: 38%;
        height: 1fr;
        padding: 0 1;
        border-left: solid $panel;
    }
    #status { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    ConfirmCloseModal, SelectWorkspaceModal, HelpModal {
        align: center middle;
        background: $background 60%;
    }
    #confirm, #select, #help {
        width: auto;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #confirm { border: round $warning; }
    #help-scroll { width: auto; height: auto; max-height: 100%; max-width: 90%; }
    #help { max-width: 100%; }
    """

    def __init__(
        self,
        *,
        paths_git: list[str],
        paths_all: list[str],
        current: str | None,
        opened: set[str],
        names: dict[str, str],
        move_mode: str | None = None,
        show_all: bool = False,
        src_pane: str = "",
        herdr: Herdr | None = None,
        mapping_reader: Callable[[], dict[str, str]] | None = None,
        strict_mapping_reader: Callable[[], dict[str, str]] | None = None,
        history: list[str] | None = None,
        start_mode: str = "insert",
    ) -> None:
        super().__init__()
        self.paths = {"git": paths_git, "all": paths_all}
        self.current = current
        self.opened = opened
        self.names = names
        self.move_mode = move_mode
        self.show_all = show_all
        self.src_pane = src_pane
        self.herdr = herdr or Herdr()
        self.mapping_reader = mapping_reader or (lambda: {})
        self.strict_mapping_reader = strict_mapping_reader or self.mapping_reader
        self.history = history
        self.mode = start_mode if start_mode in ("insert", "normal") else "insert"
        self.query = ""
        self.cursor = 0
        self.yank = ""
        self.pending = ""
        self.error = ""
        self.rows: list[Row] = []
        self.filtered: list[Row] = []
        self.spans: list[list[tuple[int, int]]] = []
        self.index = 0
        self.preview_lines: list[str] = []
        self._preview_path = ""
        self._preview_cache: dict[str, list[str]] = {}
        self._preview_generation = 0
        self._last_width = 0
        self._frame_width = 0

    def compose(self) -> ComposeResult:
        yield Bar(self._title_text, id="title")
        yield Bar(self._query_text, id="query")
        with Horizontal(id="body"):
            # Not focusable: otherwise the container eats down/up and scrolls on
            # its own, which drags the list along with every cursor move.
            with VerticalScroll(id="list-scroll", can_focus=False):
                yield RowList(id="list")
            yield Static("", id="preview", markup=False)
        yield Bar(self._footer_text, id="status")

    def on_mount(self) -> None:
        self.set_rows()
        self.show_preview()

    # -- rows -------------------------------------------------------------

    def active_paths(self) -> list[str]:
        return self.paths["all" if self.show_all else "git"]

    def build_rows(self) -> list[Row]:
        rows = []
        for path in self.active_paths():
            label = workspace_label(path, self.names)
            shown = display_path(path)
            base = os.path.basename(path.rstrip("/")) or path
            if label != base:
                shown = f"{shown}  [{label}]"
            marker = (
                CURRENT_MARK
                if path == self.current
                else OPEN_MARK
                if path in self.opened
                else " "
            )
            rows.append(Row(path=path, shown=shown, marker=marker, label=label))
        return rows

    def set_rows(self) -> None:
        self.rows = self.build_rows()
        self.apply_filter(keep_selection=True)
        self.update_title()

    def apply_filter(self, keep_selection: bool = False) -> None:
        previous = self.filtered[self.index].path if self.filtered and self.index < len(self.filtered) else None
        self.filtered = []
        self.spans = []
        for row in self.rows:
            spans = match_spans(self.query, row.shown)
            if spans is not None:
                self.filtered.append(row)
                self.spans.append(sorted(spans))
        if keep_selection and previous is not None:
            for index, row in enumerate(self.filtered):
                if row.path == previous:
                    self.index = index
                    break
            else:
                self.index = 0
        self.index = max(0, min(self.index, len(self.filtered) - 1))
        self.row_list.set_rows(
            self.filtered, self.spans, self.index, self.selection_style()
        )
        self.scroll_selection()
        self.update_status()
        self.show_preview()

    @property
    def row_list(self) -> RowList:
        return self.query_one("#list", RowList)

    def selected_row(self) -> Row | None:
        if 0 <= self.index < len(self.filtered):
            return self.filtered[self.index]
        return None

    def scroll_selection(self) -> None:
        """Scroll only when the cursor leaves the viewport, like fzf.

        The list stays put while the cursor moves inside it; it scrolls by the
        minimum needed to keep the cursor visible.
        """
        container = self.query_one("#list-scroll", VerticalScroll)
        height = max(1, container.size.height)
        top = max(0, int(container.scroll_offset.y))
        if self.index < top:
            container.scroll_to(y=self.index, animate=False)
        elif self.index >= top + height:
            container.scroll_to(y=self.index - height + 1, animate=False)

    def selection_style(
        self, attribute: str = "primary", fallback: str = "bold reverse"
    ) -> str:
        """A selection bar that contrasts whatever theme the popup runs with.

        Reverse video alone can be invisible on some profiles, so paint the row
        with a theme colour and the readable one of black/white.
        """
        theme = getattr(self.app, "current_theme", None)
        colour = getattr(theme, attribute, "") or ""
        if not colour.startswith("#") or len(colour) not in (4, 7):
            return fallback
        digits = colour[1:]
        if len(digits) == 3:
            digits = "".join(character * 2 for character in digits)
        try:
            red, green, blue = (int(digits[index : index + 2], 16) for index in (0, 2, 4))
        except ValueError:
            return fallback
        # Rec. 601 luma, good enough to pick a legible foreground.
        luma = 0.299 * red + 0.587 * green + 0.114 * blue
        foreground = "#000000" if luma > 140 else "#ffffff"
        return f"bold {foreground} on {colour}"

    def set_index(self, index: int) -> None:
        if not self.filtered:
            return
        self.index = max(0, min(index, len(self.filtered) - 1))
        self.row_list.set_rows(
            self.filtered, self.spans, self.index, self.selection_style()
        )
        self.scroll_selection()
        self.show_preview()

    def _content_width(self, selector: str) -> int:
        widget = self.query_one(selector)
        width = widget.content_size.width
        # content_size can still describe the previous size during a resize;
        # on_resize schedules a reflow once layout settles, and this fallback
        # keeps the first, pre-layout paint inside the new frame.
        if width <= 0:
            width = (self._frame_width or self.size.width) - 2
        return max(10, width)

    def update_title(self) -> None:
        self.query_one("#title", Static).refresh()

    def _title_text(self, width: int) -> Text:
        hint = "[all dirs]" if self.show_all else "[git roots]"
        line = Text()
        if self.move_mode == "pane":
            line.append(" MOVE PANE ", style="bold reverse yellow")
        elif self.move_mode == "tab":
            line.append(" MOVE TAB ", style="bold reverse yellow")
        else:
            line.append("Select project workspace")
        gap = max(1, width - cell_len(str(line)) - cell_len(hint))
        line.append(" " * gap)
        line.append(hint, style="dim")
        return line

    @staticmethod
    def _cell_index(cells: str, width: int) -> int:
        used = 0
        for index, char in enumerate(cells):
            char_width = cell_len(char)
            if used + char_width > width:
                return index
            used += char_width
        return len(cells)

    @staticmethod
    def _ellipsise(value: str, width: int) -> str:
        if width <= 0 or cell_len(value) <= width:
            return value
        if width == 1:
            return "\u2026"
        room = width - 1
        used = 0
        for index, char in enumerate(value):
            char_width = cell_len(char)
            if used + char_width > room:
                return value[:index] + "\u2026"
            used += char_width
        return value

    def _query_segment(self, avail: int) -> Text:
        avail = max(1, avail)
        query, cursor = self.query, self.cursor
        cells = query + (" " if cursor == len(query) else "")
        if not cells:
            cells = " "
        cursor = max(0, min(cursor, len(cells) - 1))
        total = cell_len(cells)
        start, end, ellipsis = 0, len(cells), False
        if total > avail:
            before = cell_len(cells[:cursor])
            if before + cell_len(cells[cursor]) <= avail:
                end = self._cell_index(cells, avail)
            else:
                # Reserve one cell for the leading ellipsis and grow the
                # window backwards from the cursor so it always stays visible.
                ellipsis = True
                budget = max(1, avail - 1)
                start = cursor
                used = cell_len(cells[cursor])
                while start > 0 and used + cell_len(cells[start - 1]) <= budget:
                    start -= 1
                    used += cell_len(cells[start])
                if start == 0:
                    ellipsis = False
                end = cursor + 1
                while end < len(cells) and used + cell_len(cells[end]) <= budget:
                    used += cell_len(cells[end])
                    end += 1
        segment = Text()
        if ellipsis:
            segment.append("\u2026", style="dim")
        for index in range(start, end):
            segment.append(cells[index], style="reverse" if index == cursor else "")
        return segment

    def update_query_bar(self) -> None:
        self.query_one("#query", Static).refresh()

    def _query_text(self, width: int) -> Text:
        pill = " INSERT " if self.mode == "insert" else " NORMAL "
        if self.mode == "insert":
            pill_style = "bold reverse"
        else:
            pill_style = self.selection_style("secondary")
        meta_style = self.selection_style("secondary")
        prompt = "all\u276f " if self.show_all else "git\u276f "
        meta = f"{len(self.filtered)}/{len(self.rows)}"
        prefix = f"{pill}{prompt}"
        prefix_width = cell_len(prefix)
        meta_width = cell_len(meta)
        avail = max(1, width - prefix_width - meta_width - 1)
        line = Text()
        line.append(pill, style=pill_style)
        line.append(prompt, style="bold cyan")
        if self.error:
            line.append(self._ellipsise(self.error, avail), style="bold red")
        else:
            line.append_text(self._query_segment(avail))
        gap = max(1, width - line.cell_len - meta_width)
        line.append(" " * gap)
        line.append(meta, style=meta_style)
        if line.cell_len > width:
            line.truncate(width, overflow="ellipsis")
        return line

    @staticmethod
    def _keycaps(parts: list[tuple[str, str]]) -> Text:
        line = Text()
        for index, (key, label) in enumerate(parts):
            if index:
                line.append("  ")
            line.append(key, style="bold")
            line.append(f" {label}", style="dim")
        return line

    def _footer_text(self, width: int) -> Text:
        narrow = width < 90
        if self.mode == "insert":
            if narrow:
                parts = [
                    ("\u23ce", "jump"),
                    ("M-p/t", "move"),
                    ("M-a", "all"),
                    ("^X", "close"),
                    ("Esc", "normal"),
                    ("?", "help"),
                ]
            else:
                parts = [
                    ("\u23ce", "jump"),
                    ("M-p", "pane"),
                    ("M-t", "tab"),
                    ("M-a", "all dirs"),
                    ("^X", "close"),
                    ("Esc", "normal"),
                    ("?", "help"),
                ]
        elif narrow:
            parts = [
                ("\u23ce/l", "jump"),
                ("j/k", "move"),
                ("i", "filter"),
                ("dd", "clear"),
                ("p/t", "move"),
                ("x", "close"),
                ("?", "help"),
                ("q", "quit"),
            ]
        else:
            parts = [
                ("\u23ce/l", "jump"),
                ("j/k", "move"),
                ("i/a/\u2571", "filter"),
                ("dd", "clear"),
                ("p", "pane"),
                ("t", "tab"),
                ("x", "close"),
                ("?", "help"),
                ("q", "quit"),
            ]
        return self._keycaps(parts)

    def update_status(self) -> None:
        self.update_query_bar()
        self.query_one("#status", Static).refresh()

    def set_error(self, message: str) -> None:
        self.error = message
        self.update_status()

    # -- preview ----------------------------------------------------------

    def show_preview(self) -> None:
        row = self.selected_row()
        if row is None:
            self._preview_path = ""
            self.preview_lines = ["(no match)"]
            self.update_preview()
            return
        self._preview_path = row.path
        cached = self._preview_cache.get(row.path)
        if cached is not None:
            self.preview_lines = cached
            self.update_preview()
            return
        self.preview_lines = [row.path, f"label: {row.label}"]
        self.update_preview()
        self.load_preview(row.path)

    def invalidate_previews(self) -> None:
        self._preview_cache.clear()
        self._preview_path = ""
        # Workers started before this point would cache data we just dropped.
        self._preview_generation += 1

    def update_preview(self) -> None:
        preview = self.query_one("#preview", Static)
        preview.display = (self._frame_width or self.size.width) >= PREVIEW_MIN_WIDTH
        if not preview.display:
            return
        preview.update(self._preview_text())

    def _preview_text(self) -> Text:
        lines = self.preview_lines
        text = Text()
        if not lines:
            return text
        path = lines[0]
        if path == "(no match)":
            text.append(path, style="dim")
            return text
        width = max(10, self._content_width("#preview") - 1)
        basename = os.path.basename(path.rstrip("/")) or path
        text.append(basename, style="bold")
        text.append("\n")
        text.append(self._fit(display_path(path), width), style="dim")
        text.append("\n\n")

        label = ""
        open_line: str | None = None
        details: list[str] = []
        git_line: str | None = None
        last_line: str | None = None
        for line in lines[1:]:
            if line.startswith("label: "):
                label = line[len("label: ") :]
            elif line.startswith("open: "):
                open_line = line[len("open: ") :]
            elif line.strip().startswith("tabs:"):
                details.append(line.strip())
            elif line.startswith("git: "):
                git_line = line[len("git: ") :]
            elif line.startswith("last: "):
                last_line = line[len("last: ") :]

        text.append("WORKSPACE", style="dim")
        text.append("\n")
        self._append_field(text, "label", label)
        if open_line is None:
            self._append_field(text, "status", "loading\u2026", style="dim")
        elif open_line == "no":
            self._append_field(text, "status", "\u25cb closed", style="dim")
        else:
            self._append_field(text, "status", f"\u25cf open ({open_line})", style="bold green")
            if not details:
                self._append_field(text, "tabs", "-")
            for detail in details:
                body = detail[len("tabs:") :].strip()
                if "agents:" in body:
                    tabs, _, agents = body.partition("agents:")
                    tabs, agents = tabs.strip(" ,"), agents.strip()
                else:
                    tabs, agents = body, ""
                self._append_field(text, "tabs", tabs or "-")
                if agents:
                    self._append_field(text, "agents", agents, style="bold yellow")

        if git_line:
            text.append("\u2500" * width, style="dim")
            text.append("\n")
            text.append("GIT", style="dim")
            text.append("\n")
            branch, _, rest = git_line.partition(" (")
            state, _, tail = rest.partition(")")
            key = f"{'branch':<9}"
            text.append(f"  {key}", style="dim")
            text.append(branch, style="bold")
            if state:
                text.append(" (")
                text.append(state, style="bold yellow" if state.startswith("dirty") else "green")
                text.append(")")
            if tail:
                text.append(tail, style="cyan")
            text.append("\n")
            if last_line:
                key = f"{'commit':<9}"
                text.append(f"  {key}", style="dim")
                room = max(1, width - len(key) - 2)
                text.append(self._fit(last_line, room), style="italic")
        return text

    @staticmethod
    def _fit(value: str, width: int) -> str:
        if width <= 0 or len(value) <= width:
            return value
        if width == 1:
            return "\u2026"
        return value[: width - 1] + "\u2026"

    @staticmethod
    def _append_field(text: Text, key: str, value: str, style: str = "") -> None:
        text.append(f"  {key:<9}", style="dim")
        text.append(value, style=style)
        text.append("\n")

    def on_resize(self, event: events.Resize) -> None:
        preview = self.query_one("#preview", Static)
        preview.display = event.size.width >= PREVIEW_MIN_WIDTH
        self._frame_width = event.size.width
        width = event.size.width
        if width == self._last_width:
            return
        self._last_width = width
        # Inside this handler a single-column growth can still be described by
        # the previous layout, so paint now and once more after it settles.
        self.reflow()
        self.call_after_refresh(self.reflow)

    def reflow(self) -> None:
        self.update_title()
        self.update_status()
        self.update_preview()

    @work(exclusive=True, thread=True)
    def load_preview(self, path: str) -> None:
        generation = self._preview_generation
        try:
            workspaces = self.herdr.workspaces()
            panes = self.herdr.panes()
        except HerdrError:
            workspaces, panes = [], []
        mapping = self.safe_mapping()
        label = workspace_label(path, self.names)
        lines = [path, f"label: {label}"]
        open_ids = [
            workspace.get("workspace_id")
            for workspace in workspaces
            if workspace.get("label") == label
            and (
                mapping.get(workspace.get("workspace_id")) == path
                or any(
                    pane.get("workspace_id") == workspace.get("workspace_id")
                    and pane.get("cwd") == path
                    for pane in panes
                )
            )
        ]
        if open_ids:
            lines.append("open: " + ", ".join(str(workspace_id) for workspace_id in open_ids))
            for workspace_id in open_ids:
                tabs = sorted(
                    {
                        str(pane.get("tab_id"))
                        for pane in panes
                        if pane.get("workspace_id") == workspace_id
                    }
                )
                agents = sorted(
                    {
                        str(pane.get("agent"))
                        for pane in panes
                        if pane.get("workspace_id") == workspace_id
                        and pane.get("agent") is not None
                    }
                )
                detail = f"  tabs: {', '.join(tabs) or '-'}"
                if agents:
                    detail += f"  agents: {', '.join(agents)}"
                lines.append(detail)
        else:
            lines.append("open: no")
        if os.path.isdir(path):
            branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
            if branch:
                counts = _git(path, "rev-list", "--left-right", "--count", "@{u}...HEAD")
                upstream = ""
                if counts:
                    behind, ahead = counts.split()
                    if ahead != "0":
                        upstream += f" ↑{ahead}"
                    if behind != "0":
                        upstream += f" ↓{behind}"
                dirty = "dirty" if _git(path, "status", "--porcelain") else "clean"
                lines.append(f"git: {branch} ({dirty}){upstream}")
                subject = _git(path, "log", "-1", "--pretty=%s")
                if subject:
                    lines.append(f"last: {subject}")
        try:
            self.call_from_thread(self._preview_loaded, path, lines, generation)
        except Exception:  # noqa: BLE001 - app may have exited already
            pass

    def _preview_loaded(self, path: str, lines: list[str], generation: int) -> None:
        if generation != self._preview_generation:
            return
        self._preview_cache[path] = lines
        if path != self._preview_path:
            return
        self.preview_lines = lines
        self.update_preview()

    def safe_mapping(self) -> dict[str, str]:
        try:
            return self.mapping_reader()
        except StateError:
            return {}

    # -- actions ----------------------------------------------------------

    def action_quit_now(self) -> None:
        self.exit(None)

    def typed_target(self) -> str | None:
        if not self.query:
            return None
        typed = os.path.expanduser(self.query)
        if (self.query.startswith(("/", "~")) or not self.filtered) and os.path.isdir(
            typed
        ):
            return os.path.abspath(typed)
        return None

    def accept_target(self) -> str | None:
        typed = self.typed_target()
        if typed:
            return typed
        row = self.selected_row()
        return row.path if row else None

    @work(exclusive=True)
    async def accept(self) -> None:
        target = self.accept_target()
        if target is None:
            return
        result = Result(kind="jump", path=target, src_pane=self.src_pane)
        if self.move_mode:
            if not self.src_pane:
                self.set_error("No source pane to move (HERDR_ACTIVE_PANE_ID is unset)")
                return
            result.kind = f"move-{self.move_mode}"
            try:
                layout = self.herdr.pane_layout(self.src_pane)
            except HerdrError as error:
                self.set_error(str(error))
                return
            result.src_layout = layout
            result.src_ws = layout.get("workspace_id", "")
            result.src_tab = layout.get("tab_id", "")
            result.tab_label = "moved"
            if self.move_mode == "tab":
                try:
                    result.tab_label = self.herdr.tab_label(result.src_tab) or "moved"
                except HerdrError:
                    result.tab_label = "moved"
        choice = await self.resolve_choice(target)
        if choice is CANCELLED:
            return
        result.choice = choice
        self.exit(result)

    async def resolve_choice(self, target: str) -> tuple[str, str | None] | None | str:
        try:
            workspaces = self.herdr.workspaces()
            panes = self.herdr.panes()
        except HerdrError as error:
            self.set_error(str(error))
            return CANCELLED
        mapping = self.mapping_reader()
        label = workspace_label(target, self.names)
        live = [
            workspace
            for workspace in workspaces
            if workspace.get("label") == label
        ]
        if any(mapping.get(workspace.get("workspace_id")) == target for workspace in live):
            return None
        if any(
            workspace.get("workspace_id") not in mapping
            and any(
                pane.get("workspace_id") == workspace.get("workspace_id")
                and pane.get("cwd") == target
                for pane in panes
            )
            for workspace in live
        ):
            return None
        unknown = [
            str(workspace.get("workspace_id"))
            for workspace in live
            if workspace.get("workspace_id") not in mapping
        ]
        if not unknown:
            return None
        choice = await self.push_screen_wait(SelectWorkspaceModal(label, unknown))
        return CANCELLED if choice is None else choice

    @work(exclusive=True)
    async def close_current(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        path = row.path
        label = workspace_label(path, self.names)
        try:
            close_ids, agent_tuples = self.close_snapshot(path)
        except StateError as error:
            self.set_error(f"Invalid workspace mapping: {error.path}")
            return
        except HerdrError as error:
            self.set_error(str(error))
            return
        if not close_ids:
            self.refresh_workspace_state()
            return
        approved_ids = set(close_ids)
        approved_tuples: set[tuple[str, str, str]] = set()
        for _ in range(MAX_CLOSE_ROUNDS):
            unapproved = agent_tuples - approved_tuples
            if unapproved:
                names = sorted({agent for _, _, agent in unapproved})
                approved = await self.push_screen_wait(
                    ConfirmCloseModal(label, names)
                )
                if not approved:
                    return
                approved_tuples |= unapproved
                approved_ids |= set(close_ids)
            try:
                close_ids, agent_tuples = self.close_snapshot(path)
            except StateError as error:
                self.set_error(f"Invalid workspace mapping: {error.path}")
                return
            except HerdrError as error:
                self.set_error(str(error))
                return
            if agent_tuples - approved_tuples:
                continue
            for workspace_id in [w for w in close_ids if w in approved_ids]:
                try:
                    self.herdr.workspace_close(workspace_id)
                except HerdrError as error:
                    self.set_error(str(error))
                    return
            self.refresh_workspace_state()
            return
        self.set_error(
            f"Close aborted: {label} kept changing after {MAX_CLOSE_ROUNDS} attempts"
        )

    def close_snapshot(self, path: str) -> tuple[list[str], set[tuple[str, str, str]]]:
        """Fresh close targets; raises StateError/HerdrError instead of guessing."""

        workspaces = self.herdr.workspaces()
        panes = self.herdr.panes()
        mapping = self.strict_mapping_reader()
        close_ids, agent_tuples, _ = snapshot_close_targets(
            path, workspaces, panes, mapping, self.names
        )
        return close_ids, agent_tuples

    def refresh_workspace_state(self) -> None:
        """Recompute open/current status and both rankings from fresh data."""

        try:
            workspaces = self.herdr.workspaces()
            panes = self.herdr.panes()
        except HerdrError as error:
            self.set_error(str(error))
            self.set_rows()
            return
        mapping = self.safe_mapping()
        self.opened = open_paths(workspaces, panes, mapping, self.names)
        self.current = current_project(self.src_pane, panes, workspaces, mapping, self.names)
        if self.history is not None:
            self.paths["git"] = rank_paths(self.history, self.current, self.opened, False)
            self.paths["all"] = rank_paths(self.history, self.current, self.opened, True)
        self.invalidate_previews()
        self.set_rows()

    @work(exclusive=True)
    async def show_help(self) -> None:
        await self.push_screen_wait(HelpModal())

    def toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.index = 0
        self.set_rows()

    # -- editing ----------------------------------------------------------

    def insert_text(self, text: str) -> None:
        self.query = self.query[: self.cursor] + text + self.query[self.cursor :]
        self.cursor += len(text)
        self.apply_filter()

    def backspace(self) -> None:
        if self.cursor:
            self.query = self.query[: self.cursor - 1] + self.query[self.cursor :]
            self.cursor -= 1
            self.apply_filter()

    def delete_word(self) -> None:
        if not self.cursor:
            return
        start = self.cursor
        end = start
        while start and self.query[start - 1] in " /":
            start -= 1
        while start and self.query[start - 1] not in " /":
            start -= 1
        while start and self.query[start - 1] == "/":
            start -= 1
        self.yank = self.query[start:end]
        self.query = self.query[:start] + self.query[end:]
        self.cursor = start
        self.apply_filter()

    def kill_to_start(self) -> None:
        self.yank = self.query[: self.cursor]
        self.query = self.query[self.cursor :]
        self.cursor = 0
        self.apply_filter()

    def yank_text(self) -> None:
        self.insert_text(self.yank)

    def clear_query(self) -> None:
        self.query = ""
        self.cursor = 0
        self.apply_filter()

    def page(self, delta: int) -> None:
        container = self.query_one("#list-scroll", VerticalScroll)
        self.set_index(self.index + delta * max(1, container.size.height - 1))

    # -- keys -------------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        key = event.key
        if self.mode == "insert" and event.is_printable and event.character:
            self.error = ""
            self.insert_text(event.character)
            event.stop()
            return
        if self.mode == "normal" and self.pending:
            pending, self.pending = self.pending, ""
            if pending == "g" and key == "g":
                self.error = ""
                self.set_index(0)
                event.stop()
                return
            if pending == "d" and key == "d":
                self.error = ""
                self.clear_query()
                event.stop()
                return
        handler = self.KEY_INSERT.get(key) if self.mode == "insert" else None
        if handler is None and self.mode == "normal":
            handler = self.KEY_NORMAL.get(key)
        if handler is None:
            return
        self.pending = ""
        self.error = ""
        handler(self)
        event.stop()

    def set_cursor(self, position: int) -> None:
        self.cursor = max(0, min(position, len(self.query)))
        self.update_status()

    def _start_pending(self, character: str) -> None:
        self.pending = character

    KEY_INSERT = {
        "down": lambda self: self.set_index(self.index + 1),
        "up": lambda self: self.set_index(self.index - 1),
        "ctrl+n": lambda self: self.set_index(self.index + 1),
        "ctrl+p": lambda self: self.set_index(self.index - 1),
        "ctrl+j": lambda self: self.set_index(self.index + 1),
        "ctrl+k": lambda self: self.set_index(self.index - 1),
        "page_down": lambda self: self.page(1),
        "page_up": lambda self: self.page(-1),
        "ctrl+f": lambda self: self.page(1),
        "ctrl+b": lambda self: self.page(-1),
        "ctrl+a": lambda self: self.set_cursor(0),
        "ctrl+e": lambda self: self.set_cursor(len(self.query)),
        "ctrl+u": lambda self: self.kill_to_start(),
        "ctrl+w": lambda self: self.delete_word(),
        "alt+backspace": lambda self: self.delete_word(),
        "ctrl+y": lambda self: self.yank_text(),
        "backspace": lambda self: self.backspace(),
        "tab": lambda self: self._complete_path(),
        "escape": lambda self: self.set_mode("normal"),
        "ctrl+h": lambda self: self.show_help(),
        "ctrl+x": lambda self: self.close_current(),
        "alt+p": lambda self: self.start_move("pane"),
        "alt+t": lambda self: self.start_move("tab"),
        "alt+a": lambda self: self.toggle_all(),
        "enter": lambda self: self.accept(),
    }

    KEY_NORMAL = {
        "j": lambda self: self.set_index(self.index + 1),
        "k": lambda self: self.set_index(self.index - 1),
        "down": lambda self: self.set_index(self.index + 1),
        "up": lambda self: self.set_index(self.index - 1),
        "g": lambda self: self._start_pending("g"),
        "G": lambda self: self.set_index(len(self.filtered) - 1),
        "ctrl+d": lambda self: self.set_index(self.index + max(1, self._half_page())),
        "ctrl+u": lambda self: self.set_index(self.index - max(1, self._half_page())),
        "ctrl+f": lambda self: self.page(1),
        "ctrl+b": lambda self: self.page(-1),
        "H": lambda self: self.set_index(self._viewport_bounds()[0]),
        "M": lambda self: self.set_index(self._viewport_middle()),
        "L": lambda self: self.set_index(self._viewport_bounds()[1]),
        "i": lambda self: self.set_mode("insert"),
        "a": lambda self: self.set_mode("insert"),
        "/": lambda self: self.set_mode("insert"),
        "d": lambda self: self._start_pending("d"),
        "C": lambda self: (self.clear_query(), self.set_mode("insert")),
        "x": lambda self: self.close_current(),
        "p": lambda self: self.start_move("pane"),
        "t": lambda self: self.start_move("tab"),
        "o": lambda self: self.accept(),
        "l": lambda self: self.accept(),
        "?": lambda self: self.show_help(),
        "q": lambda self: self.exit(None),
        "escape": lambda self: self.exit(None),
        "ctrl+h": lambda self: self.show_help(),
        "ctrl+x": lambda self: self.close_current(),
        "alt+p": lambda self: self.start_move("pane"),
        "alt+t": lambda self: self.start_move("tab"),
        "alt+a": lambda self: self.toggle_all(),
        "enter": lambda self: self.accept(),
    }

    def _viewport_bounds(self) -> tuple[int, int]:
        container = self.query_one("#list-scroll", VerticalScroll)
        height = max(1, container.size.height)
        top = max(0, int(container.scroll_offset.y))
        if not self.filtered:
            return 0, 0
        top = min(top, len(self.filtered) - 1)
        bottom = min(len(self.filtered) - 1, top + height - 1)
        return top, max(top, bottom)

    def _viewport_middle(self) -> int:
        top, bottom = self._viewport_bounds()
        return (top + bottom) // 2

    def _half_page(self) -> int:
        return max(1, (self.size.height - 3) // 2)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.update_status()

    def _complete_path(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        self.query = row.path
        self.cursor = len(self.query)
        self.apply_filter()

    def start_move(self, mode: str) -> None:
        self.move_mode = mode
        self.update_title()
        self.accept()
