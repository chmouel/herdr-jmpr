"""Theme-aware Textual workspace switcher with fzf search and vim mode."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Static

from .config import workspace_label
from .frecency import (
    CURRENT_MARK,
    OPEN_MARK,
    agent_icon,
    agents_by_path,
    current_project,
    display_path,
    format_agent_badges,
    open_paths,
    rank_paths,
)
from .herdr import Herdr, HerdrError
from .picker import (
    PickerError,
    Result,
    build_result,
    resolve_typed_or_picked,
    unknown_workspaces,
)
from .query import match_spans
from .settings import SettingsError, herdr_theme_name
from .state import StateError, snapshot_close_targets

__all__ = [
    "CANCELLED",
    "ConfirmCloseModal",
    "HERDR_INK_THEME",
    "HelpModal",
    "MAX_CLOSE_ROUNDS",
    "PREVIEW_MIN_WIDTH",
    "PickerApp",
    "Result",
    "Row",
    "RowList",
    "SelectWorkspaceModal",
]

CANCELLED = "cancelled"
PREVIEW_MIN_WIDTH = 90
MAX_CLOSE_ROUNDS = 5

HERDR_INK_THEME = Theme(
    name="herdr-ink",
    primary="#B48EF7",
    secondary="#7DCFFF",
    warning="#F2C97D",
    error="#F7768E",
    success="#7EE787",
    accent="#B48EF7",
    foreground="#ECEFF4",
    background="#161821",
    surface="#1E2230",
    panel="#2A3042",
    dark=True,
)

_THEME_ALIASES = {
    "catppuccin": "catppuccin-mocha",
    "catppuccin_mocha": "catppuccin-mocha",
    "catppuccin_latte": "catppuccin-latte",
    "rose_pine": "rose-pine",
    "rose_pine_moon": "rose-pine-moon",
    "rose_pine_dawn": "rose-pine-dawn",
    "tokyonight": "tokyo-night",
    "tokyo_night": "tokyo-night",
    "solarized_dark": "solarized-dark",
    "solarized_light": "solarized-light",
    "atom_one_dark": "atom-one-dark",
    "atom_one_light": "atom-one-light",
}


def _parse_hex(colour: str | None) -> tuple[int, int, int] | None:
    if not colour or not isinstance(colour, str) or not colour.startswith("#"):
        return None
    digits = colour[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    elif len(digits) == 8:
        digits = digits[:6]
    elif len(digits) != 6:
        return None
    try:
        return (
            int(digits[0:2], 16),
            int(digits[2:4], 16),
            int(digits[4:6], 16),
        )
    except ValueError:
        return None


def _blend_hex(base: str | None, accent: str | None, ratio: float) -> str | None:
    rgb_a = _parse_hex(base)
    rgb_b = _parse_hex(accent)
    if rgb_a is None or rgb_b is None:
        return None
    clamped = max(0.0, min(1.0, ratio))
    mixed = tuple(
        round(a + (b - a) * clamped) for a, b in zip(rgb_a, rgb_b, strict=True)
    )
    return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"


def _contrast_ink(colour: str | None) -> str | None:
    rgb = _parse_hex(colour)
    if rgb is None:
        return None
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return "#11131a" if luma > 145 else "#ffffff"


@dataclass(frozen=True)
class Palette:
    primary: str
    secondary: str
    warning: str
    success: str
    error: str
    foreground: str
    muted: str
    selection_bg: str | None


@dataclass
class Row:
    path: str
    shown: str
    marker: str
    label: str
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitInfo:
    short_hash: str
    relative_age: str
    subject: str


@dataclass(frozen=True)
class GitInfo:
    branch: str
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    is_worktree: bool = False
    commits: tuple[CommitInfo, ...] = ()


@dataclass(frozen=True)
class SessionInfo:
    agent: str
    status: str = ""
    title: str = ""


@dataclass(frozen=True)
class WorkspaceInfo:
    workspace_id: str
    tabs: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    sessions: tuple[SessionInfo, ...] = ()


@dataclass(frozen=True)
class PreviewData:
    path: str
    label: str
    loading: bool = False
    no_match: bool = False
    workspaces: tuple[WorkspaceInfo, ...] = ()
    git: GitInfo | None = None


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


def _parse_git_status_v2(output: str) -> dict[str, int | str] | None:
    if not output:
        return None
    branch = ""
    ahead = 0
    behind = 0
    staged = 0
    modified = 0
    untracked = 0
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab ") :].split():
                if token.startswith("+"):
                    try:
                        ahead = int(token[1:])
                    except ValueError:
                        pass
                elif token.startswith("-"):
                    try:
                        behind = int(token[1:])
                    except ValueError:
                        pass
        elif line.startswith(("1 ", "2 ")):
            fields = line.split(" ", 2)
            if len(fields) >= 2 and len(fields[1]) >= 2:
                xy = fields[1]
                if xy[0] != ".":
                    staged += 1
                if xy[1] != ".":
                    modified += 1
        elif line.startswith("u "):
            modified += 1
        elif line.startswith("? "):
            untracked += 1
    if not branch:
        return None
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
    }


def _short_age(age: str) -> str:
    parts = age.split()
    if len(parts) >= 2 and parts[0].isdigit():
        unit = parts[1].lower()
        for prefix, short in (
            ("second", "s"),
            ("minute", "m"),
            ("hour", "h"),
            ("day", "d"),
            ("week", "w"),
            ("month", "mo"),
            ("year", "y"),
        ):
            if unit.startswith(prefix):
                return f"{parts[0]}{short}"
    return age


def _parse_git_log(output: str) -> tuple[CommitInfo, ...]:
    commits: list[CommitInfo] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0]:
            commits.append(
                CommitInfo(
                    short_hash=parts[0].strip(),
                    relative_age=_short_age(parts[1].strip()),
                    subject=parts[2].strip(),
                )
            )
    return tuple(commits[:3])


def _load_git_info(path: str) -> GitInfo | None:
    if not os.path.isdir(path):
        return None
    status_out = _git(path, "status", "--porcelain=v2", "--branch")
    parsed = _parse_git_status_v2(status_out)
    if parsed is None:
        return None
    log_out = _git(path, "log", "-n", "3", "--pretty=format:%h%x09%cr%x09%s")
    return GitInfo(
        branch=str(parsed["branch"]),
        staged=int(parsed["staged"]),
        modified=int(parsed["modified"]),
        untracked=int(parsed["untracked"]),
        ahead=int(parsed["ahead"]),
        behind=int(parsed["behind"]),
        is_worktree=os.path.isfile(os.path.join(path, ".git")),
        commits=_parse_git_log(log_out),
    )


class Bar(Static):
    """Single-line responsive bar painted against its settled content width."""

    def __init__(self, builder: Callable[[int], Text], **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self._builder = builder

    def render(self) -> Content:
        width = self.content_size.width or self.size.width
        text = self._builder(max(10, width or 10))
        return Content.from_rich_text(text, console=self.app.console)


class RowList(Static):
    """Workspace rows with tinted active bar and preserved per-token colours."""

    POINTER = "\u258c"

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.rows: list[Row] = []
        self.spans: list[list[tuple[int, int]]] = []
        self.selected = 0
        self.selected_row_style = "bold reverse"
        self.palette = Palette(
            primary="cyan",
            secondary="cyan",
            warning="yellow",
            success="green",
            error="red",
            foreground="",
            muted="dim",
            selection_bg=None,
        )
        self.empty_hint: tuple[str, str] | None = None

    def on_resize(self, event: events.Resize) -> None:
        reflow = getattr(self.app, "reflow", None)
        if reflow is not None:
            reflow()

    def set_rows(
        self,
        rows: list[Row],
        spans: list[list[tuple[int, int]]],
        selected: int,
        selected_row_style: str | None = None,
        palette: Palette | None = None,
        empty_hint: tuple[str, str] | None = None,
    ) -> None:
        self.rows = rows
        self.spans = spans
        self.selected = selected
        if selected_row_style:
            self.selected_row_style = selected_row_style
        if palette is not None:
            self.palette = palette
        self.empty_hint = empty_hint
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
        palette = self.palette
        if not self.rows:
            if self.empty_hint is not None:
                message, style = self.empty_hint
            else:
                message = "  No matching workspaces — press M-a for all dirs or type a path"
                style = palette.muted
            line = Text(message, style=style)
            if width:
                line.truncate(width, overflow="ellipsis")
            text.append_text(line)
            return text

        tinted = self.selected_row_style.startswith("on ")
        for index, row in enumerate(self.rows):
            line = Text()
            selected = index == self.selected
            keep_tokens = not selected or tinted

            pointer_style = f"bold {palette.primary}" if keep_tokens else "bold"
            line.append(f"{self.POINTER} " if selected else "  ", style=pointer_style)

            if row.marker == CURRENT_MARK:
                marker_style = f"bold {palette.warning}" if keep_tokens else "bold"
            elif row.marker == OPEN_MARK:
                marker_style = f"bold {palette.success}" if keep_tokens else "bold"
            else:
                marker_style = ""
            line.append(row.marker, style=marker_style)
            line.append(" ")

            shown = row.shown
            suffix_start = shown.rfind("  [")
            if suffix_start != -1 and shown.endswith("]"):
                body, suffix = shown[:suffix_start], shown[suffix_start:]
            else:
                body, suffix = shown, ""
            dir_end = body.rfind("/") + 1
            hot = row.marker in (CURRENT_MARK, OPEN_MARK)
            line.append(shown)
            offset = 4

            if keep_tokens:
                if dir_end:
                    line.stylize(palette.muted, offset, offset + dir_end)
                if len(body) > dir_end:
                    base_style = (
                        f"bold {palette.foreground}".strip()
                        if (selected or hot)
                        else palette.foreground
                    )
                    if base_style:
                        line.stylize(base_style, offset + dir_end, offset + len(body))
                if suffix:
                    suffix_style = (
                        palette.secondary
                        if (selected or hot)
                        else f"italic {palette.secondary}"
                    )
                    gap_len = len(suffix) - len(suffix.lstrip(" "))
                    line.stylize(
                        suffix_style, offset + len(body) + gap_len, offset + len(shown)
                    )
            elif len(body) > dir_end:
                line.stylize("bold", offset + dir_end, offset + len(body))

            match_style = (
                f"bold underline {palette.warning}"
                if keep_tokens
                else "bold underline"
            )
            for start, end in self.spans[index]:
                start = max(0, min(start, len(shown)))
                end = max(start, min(end, len(shown)))
                if end > start:
                    line.stylize(match_style, offset + start, offset + end)

            if width:
                line.truncate(width, overflow="ellipsis", pad=False)
            if row.agents:
                badge = format_agent_badges(row.agents)
                badge_width = cell_len(badge)
                if width:
                    room = width - badge_width - 2
                    if room > 6 and line.cell_len > room:
                        line.truncate(room, overflow="ellipsis", pad=False)
                    if line.cell_len + 2 + badge_width <= width:
                        gap = width - line.cell_len - badge_width
                        line.append(" " * gap)
                        line.append(
                            badge,
                            style=f"bold {palette.warning}" if keep_tokens else "bold",
                        )
                else:
                    line.append("  ")
                    line.append(
                        badge,
                        style=f"bold {palette.warning}" if keep_tokens else "bold",
                    )
            if width:
                line.truncate(width, overflow="ellipsis", pad=selected)
            if selected:
                line.stylize(self.selected_row_style)
            text.append_text(line)
            text.append("\n")
        return text

    def on_click(self, event: events.Click) -> None:
        if not self.rows or event.button != 1:
            return
        index = event.y
        if 0 <= index < len(self.rows):
            selector = getattr(self.app, "select_row_at", None)
            if callable(selector):
                selector(index, activate=event.chain >= 2)


class ConfirmCloseModal(ModalScreen[bool]):
    def __init__(self, label: str, agents: list[str]):
        super().__init__()
        self.label = label
        self.agents = agents

    def _palette(self) -> Palette:
        getter = getattr(self.app, "palette", None)
        if callable(getter):
            return getter()
        return Palette("cyan", "cyan", "yellow", "green", "red", "", "dim", None)

    def _content(self) -> Text:
        palette = self._palette()
        text = Text()
        text.append("Close ", style=f"bold {palette.foreground}".strip())
        text.append(self.label, style=f"bold {palette.warning}")
        text.append("?\n\n")
        if self.agents:
            text.append("Active agents: ", style=palette.muted)
            text.append(", ".join(self.agents), style=f"bold {palette.error}")
            text.append("\n\n")
        text.append("[y/N]", style=f"bold {palette.warning}")
        text.append(" ")
        text.append("confirm", style=palette.muted)
        text.append("    ")
        text.append("Esc", style=f"bold {palette.primary}")
        text.append(" ")
        text.append("cancel", style=palette.muted)
        return text

    def compose(self) -> ComposeResult:
        panel = Static(self._content(), id="confirm")
        panel.border_title = " Close workspace "
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
        panel.border_title = " Workspace conflict "
        yield panel

    def _palette(self) -> Palette:
        getter = getattr(self.app, "palette", None)
        if callable(getter):
            return getter()
        return Palette("cyan", "cyan", "yellow", "green", "red", "", "dim", None)

    def _selection_style(self) -> str:
        style = getattr(self.app, "selection_style", None)
        return style() if callable(style) else "bold reverse"

    def _option_text(self) -> Text:
        palette = self._palette()
        row_style = self._selection_style()
        tinted = row_style.startswith("on ")
        text = Text()
        text.append("Select workspace for ", style=f"bold {palette.foreground}".strip())
        text.append(self.label, style=f"bold {palette.warning}")
        text.append("\n\n")
        options = self.options()
        width = max((cell_len(opt) + 3 for opt in options), default=24)
        for index, option in enumerate(options):
            line = Text()
            if index == self.index:
                pointer_style = f"bold {palette.primary}" if tinted else "bold"
                label_style = f"bold {palette.foreground}".strip() if tinted else ""
                line.append("\u258c ", style=pointer_style)
                line.append(option, style=label_style)
                line.truncate(width, pad=True)
                line.stylize(row_style)
            else:
                line.append("  " + option, style=palette.foreground)
            text.append_text(line)
            if index < len(options) - 1:
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
            ("alt-o", "open workspaces"),
            ("alt-v", "toggle preview"),
            ("ctrl-t", "switch theme"),
            ("ctrl-x", "close workspace"),
            ("? ctrl-h", "toggle help"),
        ],
    ),
    (
        "Insert mode",
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
        "Normal mode",
        [
            ("j/k", "move cursor"),
            ("gg G", "first / last"),
            ("ctrl-d/u", "half page"),
            ("H M L", "top / mid / bottom"),
            ("i a /", "insert mode"),
            ("dd", "clear query"),
            ("o", "open workspaces"),
            ("v", "toggle preview"),
            ("T", "switch theme"),
            ("x", "close workspace"),
            ("p t", "move pane / tab"),
            ("q", "quit"),
        ],
    ),
]


class HelpModal(ModalScreen[None]):
    def _palette(self) -> Palette:
        getter = getattr(self.app, "palette", None)
        if callable(getter):
            return getter()
        return Palette("cyan", "cyan", "yellow", "green", "red", "", "dim", None)

    @staticmethod
    def _entry(key: str, description: str, key_style: str, desc_style: str) -> Text:
        entry = Text()
        entry.append(f"  {key:<11}", style=key_style)
        entry.append(description, style=desc_style)
        return entry

    def _content(self) -> Text:
        palette = self._palette()
        key_style = f"bold {palette.foreground}".strip() or "bold"
        text = Text()
        for group_index, (title, items) in enumerate(HELP_GROUPS):
            if group_index:
                text.append("\n")
            text.append(title, style=f"bold {palette.primary}")
            text.append("\n")
            half = (len(items) + 1) // 2
            columns = [items[:half], items[half:]]
            widths = [
                max(
                    (
                        cell_len(
                            self._entry(key, description, key_style, palette.muted).plain
                        )
                        for key, description in column
                    ),
                    default=0,
                )
                for column in columns
            ]
            for index in range(half):
                for column_index, column in enumerate(columns):
                    if index < len(column):
                        entry = self._entry(
                            *column[index], key_style=key_style, desc_style=palette.muted
                        )
                        text.append_text(entry)
                        if column_index == 0:
                            text.append(" " * (widths[0] - entry.cell_len + 3))
                    elif column_index == 0:
                        text.append(" " * (widths[0] + 3))
                text.append("\n")
        return text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-scroll"):
            panel = Static(self._content(), id="help")
            panel.border_title = " Keyboard shortcuts "
            yield panel

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss(None)


class ThemePickerModal(ModalScreen[str | None]):
    def __init__(self, themes: list[str], current_theme: str):
        super().__init__()
        self.themes = themes
        self.original_theme = current_theme
        self.index = themes.index(current_theme) if current_theme in themes else 0

    def _palette(self) -> Palette:
        getter = getattr(self.app, "palette", None)
        if callable(getter):
            return getter()
        return Palette("cyan", "cyan", "yellow", "green", "red", "", "dim", None)

    def _selection_style(self) -> str:
        style = getattr(self.app, "selection_style", None)
        return style() if callable(style) else "bold reverse"

    def _theme_text(self) -> Text:
        palette = self._palette()
        row_style = self._selection_style()
        tinted = row_style.startswith("on ")
        text = Text()
        width = max((cell_len(name) + 6 for name in self.themes), default=28)
        for index, name in enumerate(self.themes):
            line = Text()
            selected = index == self.index
            active = name == self.original_theme
            if selected:
                pointer_style = f"bold {palette.primary}" if tinted else "bold"
                label_style = f"bold {palette.foreground}".strip() if tinted else ""
                line.append("\u258c ", style=pointer_style)
                line.append(name, style=label_style)
                if active:
                    line.append("  \u25cf", style=f"bold {palette.success}" if tinted else "")
                line.truncate(width, pad=True)
                line.stylize(row_style)
            else:
                line.append("  " + name, style=palette.foreground)
                if active:
                    line.append("  \u25cf", style=palette.success)
            text.append_text(line)
            if index < len(self.themes) - 1:
                text.append("\n")
        return text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="theme-scroll"):
            panel = Static(self._theme_text(), id="theme-picker")
            panel.border_title = " Switch theme (Esc revert) "
            yield panel

    def _preview_theme(self) -> None:
        chosen = self.themes[self.index]
        self.app.theme = chosen
        self.query_one("#theme-picker", Static).update(self._theme_text())

    def on_key(self, event: events.Key) -> None:
        event.stop()
        if event.key in ("escape", "q"):
            self.app.theme = self.original_theme
            self.dismiss(None)
        elif event.key in ("down", "j", "ctrl+n", "tab"):
            self.index = (self.index + 1) % len(self.themes)
            self._preview_theme()
        elif event.key in ("up", "k", "ctrl+p"):
            self.index = (self.index - 1) % len(self.themes)
            self._preview_theme()
        elif event.key in ("enter", "space"):
            self.dismiss(self.themes[self.index])


class PickerApp(App[Result | None]):
    TITLE = "herdr-zshz"

    BINDINGS = [
        Binding("ctrl+c", "quit_now", show=False, priority=True),
        Binding("ctrl+g", "quit_now", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    Screen { layout: vertical; background: $background; }
    #query {
        height: 2;
        background: $surface;
        padding: 0 2;
        border-bottom: solid $panel;
    }
    #body { height: 1fr; background: $background; }
    #list-scroll {
        width: 1fr;
        height: 1fr;
        margin-top: 1;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        background: $background;
    }
    #list { text-wrap: nowrap; text-overflow: ellipsis; }
    #preview {
        width: 40%;
        height: 1fr;
        padding: 1 2;
        background: $surface;
        border-left: solid $panel;
    }
    #status {
        height: 2;
        background: $surface;
        color: $text-muted;
        padding: 0 2;
        border-top: solid $panel;
    }
    ConfirmCloseModal, SelectWorkspaceModal, HelpModal, ThemePickerModal {
        align: center middle;
        background: $background 68%;
    }
    #confirm, #select, #help, #theme-picker {
        width: auto;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #confirm { border: round $warning; }
    #help-scroll, #theme-scroll { width: auto; height: auto; max-height: 85%; max-width: 92%; }
    #help { padding: 0 2; max-width: 100%; }
    #theme-picker { padding: 1 2; max-width: 100%; }
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
        theme_setting: str = "auto",
        workspaces: list[dict] | None = None,
        panes: list[dict] | None = None,
        agents: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__()
        self._ui_mounted = False
        self.register_theme(HERDR_INK_THEME)
        self.paths = {"git": paths_git, "all": paths_all}
        self.current = current
        self.opened = opened
        self.names = names
        self.move_mode = move_mode
        self.scope = "all" if show_all else "git"
        self._previous_scope = self.scope
        self.src_pane = src_pane
        self.herdr = herdr or Herdr()
        self.mapping_reader = mapping_reader or (lambda: {})
        self.strict_mapping_reader = strict_mapping_reader or self.mapping_reader
        self.history = history
        self.agents: dict[str, tuple[str, ...]] = dict(agents) if agents else {}
        if not self.agents and workspaces and panes:
            self.agents = agents_by_path(
                workspaces, panes, self.safe_mapping(), names
            )
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
        self.preview_data: PreviewData | None = None
        self.preview_override: bool | None = None
        self._preview_path = ""
        self._preview_cache: dict[str, PreviewData] = {}
        self._preview_generation = 0
        self._preview_timer = None
        self._snapshot_workspaces = workspaces
        self._snapshot_panes = panes
        self._last_width = 0
        self._frame_width = 0
        self.theme = self._resolve_theme(theme_setting)

    def _resolve_theme(self, configured: str) -> str:
        available = self.available_themes
        if configured == "auto":
            detected = herdr_theme_name()
            if detected:
                candidate = self._match_theme_name(detected, available)
                if candidate:
                    return candidate
            return "herdr-ink"
        candidate = self._match_theme_name(configured, available)
        if candidate is None:
            names = ", ".join(sorted(available))
            raise SettingsError(
                f"Unknown theme {configured!r}; available themes: {names}"
            )
        return candidate

    @staticmethod
    def _match_theme_name(name: str, available: set[str] | dict) -> str | None:
        lowered = name.strip().lower()
        if lowered in available:
            return lowered
        alias = _THEME_ALIASES.get(lowered)
        if alias and alias in available:
            return alias
        hyphenated = lowered.replace("_", "-")
        if hyphenated in available:
            return hyphenated
        return None

    def compose(self) -> ComposeResult:
        yield Bar(self._query_text, id="query")
        with Horizontal(id="body"):
            with VerticalScroll(id="list-scroll", can_focus=False):
                yield RowList(id="list")
            yield Static("", id="preview", markup=False)
        yield Bar(self._footer_text, id="status")

    def on_mount(self) -> None:
        self._ui_mounted = True
        self.set_rows()
        self.show_preview()

    def watch_theme(self, _theme_name: str) -> None:
        if getattr(self, "_ui_mounted", False):
            self.apply_filter(keep_selection=True)
            self.reflow()

    # -- theme & palette --------------------------------------------------

    def palette(self) -> Palette:
        theme = getattr(self, "current_theme", None)
        primary = getattr(theme, "primary", "") or ""
        secondary = getattr(theme, "secondary", "") or ""
        warning = getattr(theme, "warning", "") or ""
        success = getattr(theme, "success", "") or ""
        error = getattr(theme, "error", "") or ""
        foreground = getattr(theme, "foreground", "") or ""
        background = getattr(theme, "background", "") or ""
        surface = getattr(theme, "surface", "") or background
        dark = bool(getattr(theme, "dark", True))

        selection_bg = _blend_hex(
            background or surface, primary, 0.22 if dark else 0.16
        )
        muted = _blend_hex(background or surface, foreground, 0.56) or "dim"
        return Palette(
            primary=primary if _parse_hex(primary) else "cyan",
            secondary=secondary if _parse_hex(secondary) else "cyan",
            warning=warning if _parse_hex(warning) else "yellow",
            success=success if _parse_hex(success) else "green",
            error=error if _parse_hex(error) else "red",
            foreground=foreground if _parse_hex(foreground) else "",
            muted=muted,
            selection_bg=selection_bg,
        )

    def selection_style(
        self, attribute: str = "primary", fallback: str = "bold reverse"
    ) -> str:
        """Return the tinted row background style, or fallback for non-hex themes."""
        theme = getattr(self, "current_theme", None)
        colour = getattr(theme, attribute, "") or ""
        if _parse_hex(colour) is None:
            return fallback
        if attribute == "primary":
            background = (
                getattr(theme, "background", "")
                or getattr(theme, "surface", "")
                or "#161821"
            )
            dark = bool(getattr(theme, "dark", True))
            tinted = _blend_hex(background, colour, 0.22 if dark else 0.16)
            if tinted is not None:
                return f"on {tinted}"
        ink = _contrast_ink(colour)
        if ink is None:
            return fallback
        return f"bold {ink} on {colour}"

    def chip_style(self, attribute: str = "warning") -> str:
        theme = getattr(self, "current_theme", None)
        colour = getattr(theme, attribute, "") or ""
        ink = _contrast_ink(colour)
        if ink is None:
            return "bold reverse"
        return f"bold {ink} on {colour}"

    # -- rows -------------------------------------------------------------

    @property
    def show_all(self) -> bool:
        return self.scope == "all"

    @show_all.setter
    def show_all(self, value: bool) -> None:
        self.scope = "all" if value else "git"
        self._previous_scope = self.scope

    def active_paths(self) -> list[str]:
        if self.scope == "open":
            seen: set[str] = set()
            open_list: list[str] = []
            for path in self.paths["all"]:
                if (path == self.current or path in self.opened) and path not in seen:
                    seen.add(path)
                    open_list.append(path)
            return open_list
        return self.paths[self.scope]

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
            rows.append(
                Row(
                    path=path,
                    shown=shown,
                    marker=marker,
                    label=label,
                    agents=self.agents.get(path, ()),
                )
            )
        return rows

    def set_rows(self) -> None:
        self.rows = self.build_rows()
        self.apply_filter(keep_selection=True)

    def _empty_hint(self, palette: Palette) -> tuple[str, str]:
        typed = self.typed_target()
        if typed:
            return (
                f"  \u23ce  Open directory {display_path(typed)}",
                f"bold {palette.success}",
            )
        if self.scope == "open":
            scope_tip = "no open workspaces \u00b7 press M-o for all"
            return (f"  {scope_tip.capitalize()}", palette.muted)
        scope_tip = "type a path to open" if self.scope == "all" else "press M-a for all dirs"
        return (f"  No matching workspaces \u00b7 {scope_tip}", palette.muted)

    def apply_filter(self, keep_selection: bool = False) -> None:
        previous = (
            self.filtered[self.index].path
            if self.filtered and self.index < len(self.filtered)
            else None
        )
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
        palette = self.palette()
        self.row_list.set_rows(
            self.filtered,
            self.spans,
            self.index,
            self.selection_style(),
            palette=palette,
            empty_hint=self._empty_hint(palette),
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
        """Scroll only when the cursor crosses the viewport edge."""
        container = self.query_one("#list-scroll", VerticalScroll)
        height = max(1, container.size.height)
        top = max(0, int(container.scroll_offset.y))
        if self.index < top:
            container.scroll_to(y=self.index, animate=False)
        elif self.index >= top + height:
            container.scroll_to(y=self.index - height + 1, animate=False)

    def set_index(self, index: int) -> None:
        if not self.filtered:
            return
        self.index = max(0, min(index, len(self.filtered) - 1))
        palette = self.palette()
        self.row_list.set_rows(
            self.filtered,
            self.spans,
            self.index,
            self.selection_style(),
            palette=palette,
            empty_hint=self._empty_hint(palette),
        )
        self.scroll_selection()
        self.show_preview()

    def _content_width(self, selector: str) -> int:
        widget = self.query_one(selector)
        width = widget.content_size.width
        if width <= 0:
            width = (self._frame_width or self.size.width) - 4
        return max(10, width)

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

    def _query_segment(self, avail: int, palette: Palette) -> Text:
        avail = max(1, avail)
        cursor_style = Style(reverse=True)
        if not self.query and self.mode == "insert":
            segment = Text()
            segment.append(" ", style=cursor_style)
            if avail > 1:
                placeholder = self._ellipsise(
                    "Filter workspaces or type a path\u2026", avail - 1
                )
                segment.append(placeholder, style=palette.muted)
            return segment

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
            segment.append("\u2026", style=palette.muted)
        text_style = f"bold {palette.foreground}".strip()
        for index in range(start, end):
            if index == cursor:
                segment.append(cells[index], style=cursor_style)
            else:
                segment.append(cells[index], style=text_style)
        return segment

    def update_query_bar(self) -> None:
        self.query_one("#query", Static).refresh()

    def _query_text(self, width: int) -> Text:
        palette = self.palette()
        line = Text()

        if self.move_mode == "pane":
            line.append(" move pane ", style=self.chip_style("warning"))
            line.append(" ")
        elif self.move_mode == "tab":
            line.append(" move tab ", style=self.chip_style("warning"))
            line.append(" ")

        if self.mode == "normal":
            line.append(" normal ", style=self.chip_style("secondary"))
            line.append(" ")

        prompt = f"{self.scope} \u203a "
        line.append(prompt, style=f"bold {palette.primary}")

        count_text = f"{len(self.filtered)}/{len(self.rows)}"
        scope_text = f"[{self.scope}]"
        right = Text()
        right.append(count_text, style=palette.muted)
        right.append("  ")
        right.append(scope_text, style=f"bold {palette.secondary}")

        avail = max(1, width - line.cell_len - right.cell_len - 2)
        if self.error:
            line.append(
                self._ellipsise(self.error, avail), style=f"bold {palette.error}"
            )
        else:
            line.append_text(self._query_segment(avail, palette))

        gap = max(1, width - line.cell_len - right.cell_len)
        line.append(" " * gap)
        line.append_text(right)
        if line.cell_len > width:
            line.truncate(width, overflow="ellipsis")
        return line

    def _keycaps(self, parts: list[tuple[str, str]], wide: bool) -> Text:
        palette = self.palette()
        separator = "   " if wide else "  "
        line = Text()
        for index, (key, label) in enumerate(parts):
            if index:
                line.append(separator)
            line.append(key, style=f"bold {palette.primary}")
            line.append(" ")
            line.append(label, style=palette.muted)
        return line

    def _footer_text(self, width: int) -> Text:
        narrow = width < 88
        scope_action = "git only" if self.show_all else "all dirs"
        if self.mode == "insert":
            if narrow:
                parts = [
                    ("\u23ce", "jump"),
                    ("M-p/t", "move"),
                    ("M-a", "scope"),
                    ("M-o", "open"),
                    ("^X", "close"),
                    ("Esc", "normal"),
                    ("?", "help"),
                ]
            else:
                parts = [
                    ("\u23ce", "jump"),
                    ("M-p", "pane"),
                    ("M-t", "tab"),
                    ("M-a", scope_action),
                    ("M-o", "open"),
                    ("M-v", "preview"),
                    ("^T", "theme"),
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
                ("v", "preview"),
                ("p", "pane"),
                ("t", "tab"),
                ("x", "close"),
                ("?", "help"),
                ("q", "quit"),
            ]
        rendered = self._keycaps(parts, wide=not narrow)
        if rendered.cell_len > width:
            rendered.truncate(width, overflow="ellipsis")
        return rendered

    def update_status(self) -> None:
        self.update_query_bar()
        self.query_one("#status", Static).refresh()

    def set_error(self, message: str) -> None:
        self.error = message
        self.update_status()

    # -- preview ----------------------------------------------------------

    def _preview_should_display(self, width: int | None = None) -> bool:
        if self.preview_override is not None:
            return self.preview_override
        effective = width if width is not None else (self._frame_width or self.size.width)
        return effective >= PREVIEW_MIN_WIDTH

    def toggle_preview(self) -> None:
        current = self._preview_should_display()
        self.preview_override = not current
        self.update_preview()
        self.call_after_refresh(self.reflow)

    def _cancel_preview_timer(self) -> None:
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None

    def _schedule_preview_load(self, path: str) -> None:
        self._cancel_preview_timer()
        generation = self._preview_generation
        self._preview_timer = self.set_timer(
            0.06, lambda: self._fire_preview_load(path, generation)
        )

    def _fire_preview_load(self, path: str, generation: int) -> None:
        self._preview_timer = None
        if generation != self._preview_generation or path != self._preview_path:
            return
        self.load_preview(path)

    def show_preview(self) -> None:
        row = self.selected_row()
        if row is None:
            typed = self.typed_target()
            if typed:
                self._preview_path = typed
                cached = self._preview_cache.get(typed)
                if cached is not None:
                    self._cancel_preview_timer()
                    self.preview_data = cached
                    self.update_preview()
                    return
                label = workspace_label(typed, self.names)
                self.preview_data = PreviewData(path=typed, label=label, loading=True)
                self.update_preview()
                self._schedule_preview_load(typed)
                return
            self._cancel_preview_timer()
            self._preview_path = ""
            self.preview_data = PreviewData(path="", label="", no_match=True)
            self.update_preview()
            return
        self._preview_path = row.path
        cached = self._preview_cache.get(row.path)
        if cached is not None:
            self._cancel_preview_timer()
            self.preview_data = cached
            self.update_preview()
            return
        self.preview_data = PreviewData(path=row.path, label=row.label, loading=True)
        self.update_preview()
        self._schedule_preview_load(row.path)

    def invalidate_previews(self) -> None:
        self._cancel_preview_timer()
        self._preview_cache.clear()
        self._preview_path = ""
        self._preview_generation += 1
        self._snapshot_workspaces = None
        self._snapshot_panes = None

    def update_preview(self) -> None:
        preview = self.query_one("#preview", Static)
        preview.display = self._preview_should_display()
        if not preview.display:
            return
        preview.update(self._preview_text())

    def _preview_text(self) -> Text:
        palette = self.palette()
        data = self.preview_data
        text = Text()
        if data is None:
            return text
        width = max(12, self._content_width("#preview"))
        if data.no_match:
            text.append("No matching workspace\n", style=f"bold {palette.foreground}".strip())
            text.append(
                "Press Alt+A to search all directories, or type a directory path to open it directly.",
                style=palette.muted,
            )
            return text

        path = data.path
        label = data.label
        basename = os.path.basename(path.rstrip("/")) or path
        title = Text()
        title.append(basename, style=f"bold {palette.foreground}".strip() or "bold")
        if label and label != basename:
            title.append("  ")
            title.append(f"[{label}]", style=f"bold {palette.secondary}")
        title.truncate(width, overflow="ellipsis")
        text.append_text(title)
        text.append("\n")
        text.append(self._ellipsise(display_path(path), width), style=palette.muted)
        text.append("\n\n")

        if data.loading:
            text.append("\u25cc  Checking workspace\u2026\n", style=palette.muted)
        elif not data.workspaces:
            if self.selected_row() is None and self.typed_target() == path:
                text.append(
                    "\u23ce  Press Enter to open new workspace\n",
                    style=f"bold {palette.success}",
                )
            else:
                text.append("\u25cb  Closed\n", style=palette.muted)
        else:
            open_ids = ", ".join(ws.workspace_id for ws in data.workspaces)
            if path == self.current:
                text.append(
                    f"\u25c6  Current workspace ({open_ids})\n",
                    style=f"bold {palette.warning}",
                )
            else:
                text.append(
                    f"\u25cf  Open ({open_ids})\n",
                    style=f"bold {palette.success}",
                )
            for ws in data.workspaces:
                short_tabs = [
                    t[len(ws.workspace_id) + 1 :]
                    if t.startswith(f"{ws.workspace_id}:")
                    else t
                    for t in ws.tabs
                ]
                tabs = ", ".join(short_tabs) if short_tabs else "-"
                self._append_field(
                    text, "tabs", tabs, palette, width, style=palette.foreground
                )
                if ws.sessions:
                    agent_width = max(len(s.agent) for s in ws.sessions)
                    for idx, session in enumerate(ws.sessions):
                        prefix = f"  {'agents' if idx == 0 else '':<8}"
                        text.append(prefix, style=palette.muted)
                        agent_col = f"{agent_icon(session.agent)} {session.agent:<{agent_width}}"
                        text.append(agent_col, style=f"bold {palette.warning}")
                        if session.status == "working":
                            text.append(" \u25cf ", style=f"bold {palette.success}")
                        elif session.status and session.status != "unknown":
                            text.append(" \u25cb ", style=palette.muted)
                        else:
                            text.append("  ")
                        used = len(prefix) + cell_len(agent_col) + (3 if session.status and session.status != "unknown" else 2)
                        room = max(4, width - used)
                        if session.title:
                            text.append(
                                self._ellipsise(session.title, room),
                                style=palette.foreground,
                            )
                        text.append("\n")
                elif ws.agents:
                    agent_str = ", ".join(
                        f"{agent_icon(agent)} {agent}" for agent in ws.agents
                    )
                    self._append_field(
                        text,
                        "agents",
                        agent_str,
                        palette,
                        width,
                        style=f"bold {palette.warning}",
                    )

        git_info = data.git
        if git_info is not None:
            text.append("\n")
            git_header = Text()
            git_header.append(f"\u2387 {git_info.branch}", style=f"bold {palette.primary}")
            if git_info.is_worktree:
                git_header.append("  ")
                git_header.append("worktree", style=palette.muted)
            counts: list[str] = []
            if git_info.staged:
                counts.append(f"+{git_info.staged}")
            if git_info.modified:
                counts.append(f"~{git_info.modified}")
            if git_info.untracked:
                counts.append(f"?{git_info.untracked}")
            git_header.append("  ")
            if counts:
                git_header.append(" ".join(counts), style=f"bold {palette.warning}")
            else:
                git_header.append("clean", style=palette.success)
            if git_info.ahead:
                git_header.append(f"  \u2191{git_info.ahead}", style=f"bold {palette.secondary}")
            if git_info.behind:
                git_header.append(f"  \u2193{git_info.behind}", style=f"bold {palette.secondary}")
            git_header.truncate(width, overflow="ellipsis")
            text.append_text(git_header)
            text.append("\n")
            for commit in git_info.commits:
                commit_line = Text()
                commit_line.append(commit.short_hash, style=f"bold {palette.secondary}")
                commit_line.append(f" {commit.relative_age:<4} ", style=palette.muted)
                room = max(4, width - cell_len(commit_line.plain))
                commit_line.append(
                    self._ellipsise(commit.subject, room),
                    style=f"italic {palette.foreground}".strip(),
                )
                commit_line.truncate(width, overflow="ellipsis")
                text.append_text(commit_line)
                text.append("\n")
        return text

    @classmethod
    def _wrap_subject(cls, value: str, width: int, max_lines: int = 3) -> str:
        width = max(8, width)
        words = value.split()
        if not words:
            return ""
        lines: list[str] = []
        current = ""
        index = 0
        while index < len(words):
            word = words[index]
            candidate = f"{current} {word}".strip() if current else word
            if cell_len(candidate) <= width:
                current = candidate
                index += 1
                continue
            if not current:
                if len(lines) == max_lines - 1:
                    lines.append(cls._ellipsise(word, width))
                    return "\n".join(lines)
                cut = cls._cell_index(word, width)
                lines.append(word[: max(1, cut)])
                words[index] = word[max(1, cut) :]
                continue
            if len(lines) == max_lines - 1:
                remaining = " ".join([current] + words[index:])
                lines.append(cls._ellipsise(remaining, width))
                return "\n".join(lines)
            lines.append(current)
            current = ""
        if current:
            lines.append(current)
        return "\n".join(lines[:max_lines])

    def _append_field(
        self,
        text: Text,
        key: str,
        value: str,
        palette: Palette,
        width: int,
        style: str = "",
    ) -> None:
        prefix = f"  {key:<8}"
        room = max(1, width - len(prefix))
        text.append(prefix, style=palette.muted)
        text.append(self._ellipsise(value, room), style=style)
        text.append("\n")

    def on_resize(self, event: events.Resize) -> None:
        self._frame_width = event.size.width
        preview = self.query_one("#preview", Static)
        preview.display = self._preview_should_display(event.size.width)
        width = event.size.width
        if width == self._last_width:
            return
        self._last_width = width
        self.reflow()
        self.call_after_refresh(self.reflow)

    def reflow(self) -> None:
        if self.filtered:
            palette = self.palette()
            self.row_list.set_rows(
                self.filtered,
                self.spans,
                self.index,
                self.selection_style(),
                palette=palette,
                empty_hint=self._empty_hint(palette),
            )
        self.update_status()
        self.update_preview()

    @work(exclusive=True, thread=True, group="preview")
    def load_preview(self, path: str) -> None:
        generation = self._preview_generation
        workspaces = self._snapshot_workspaces
        panes = self._snapshot_panes
        if workspaces is None or panes is None:
            try:
                workspaces = self.herdr.workspaces()
                panes = self.herdr.panes()
            except HerdrError:
                workspaces, panes = [], []
        mapping = self.safe_mapping()
        label = workspace_label(path, self.names)
        open_ids = [
            str(workspace.get("workspace_id"))
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
        ws_infos: list[WorkspaceInfo] = []
        for workspace_id in open_ids:
            tabs = tuple(
                sorted(
                    {
                        str(pane.get("tab_id"))
                        for pane in panes
                        if pane.get("workspace_id") == workspace_id
                    }
                )
            )
            agents = tuple(
                sorted(
                    {
                        str(pane.get("agent"))
                        for pane in panes
                        if pane.get("workspace_id") == workspace_id
                        and pane.get("agent") is not None
                    }
                )
            )
            sessions: list[SessionInfo] = []
            for pane in panes:
                if pane.get("workspace_id") != workspace_id or pane.get("agent") is None:
                    continue
                tokens = pane.get("tokens")
                title = ""
                if isinstance(tokens, dict):
                    title = str(tokens.get("tl") or "").strip()
                if not title:
                    title = str(pane.get("terminal_title_stripped") or "").strip()
                sessions.append(
                    SessionInfo(
                        agent=str(pane.get("agent")),
                        status=str(pane.get("agent_status") or "").strip(),
                        title=title,
                    )
                )
            ws_infos.append(
                WorkspaceInfo(
                    workspace_id=workspace_id,
                    tabs=tabs,
                    agents=agents,
                    sessions=tuple(sessions),
                )
            )
        data = PreviewData(
            path=path,
            label=label,
            workspaces=tuple(ws_infos),
            git=_load_git_info(path),
        )
        try:
            self.call_from_thread(self._preview_loaded, path, data, generation)
        except Exception:  # noqa: BLE001 - app may have exited already
            pass

    def _preview_loaded(self, path: str, data: PreviewData, generation: int) -> None:
        if generation != self._preview_generation:
            return
        self._preview_cache[path] = data
        if path != self._preview_path:
            return
        self.preview_data = data
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
        if self.query.startswith(("/", "~")) or not self.filtered:
            return resolve_typed_or_picked(self.query, "")
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
        try:
            result = build_result(
                target, self.move_mode, self.src_pane, self.herdr, None
            )
        except (PickerError, HerdrError) as error:
            self.set_error(str(error))
            return
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
        unknown = unknown_workspaces(target, workspaces, panes, mapping, self.names)
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
        workspaces = self.herdr.workspaces()
        panes = self.herdr.panes()
        mapping = self.strict_mapping_reader()
        close_ids, agent_tuples, _ = snapshot_close_targets(
            path, workspaces, panes, mapping, self.names
        )
        return close_ids, agent_tuples

    def refresh_workspace_state(self) -> None:
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
        self.agents = agents_by_path(workspaces, panes, mapping, self.names)
        if self.history is not None:
            self.paths["git"] = rank_paths(self.history, self.current, self.opened, False)
            self.paths["all"] = rank_paths(self.history, self.current, self.opened, True)
        self.invalidate_previews()
        self.set_rows()

    @work(exclusive=True)
    async def show_help(self) -> None:
        await self.push_screen_wait(HelpModal())

    @work(exclusive=True)
    async def open_theme_picker(self) -> None:
        available = sorted(self.available_themes)
        if "herdr-ink" in available:
            available.remove("herdr-ink")
            available.insert(0, "herdr-ink")
        await self.push_screen_wait(ThemePickerModal(available, self.theme))

    def select_row_at(self, index: int, *, activate: bool = False) -> None:
        if not self.filtered:
            return
        self.set_index(index)
        if activate:
            self.accept()

    def toggle_all(self) -> None:
        if self.scope == "open":
            self.scope = "all"
        elif self.scope == "all":
            self.scope = "git"
        else:
            self.scope = "all"
        self._previous_scope = self.scope
        self.index = 0
        self.set_rows()

    def toggle_open_scope(self) -> None:
        if self.scope == "open":
            self.scope = self._previous_scope if self._previous_scope in ("git", "all") else "git"
        else:
            self._previous_scope = self.scope
            self.scope = "open"
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
            if handler is None and event.character:
                handler = self.KEY_NORMAL.get(event.character)
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
        "alt+o": lambda self: self.toggle_open_scope(),
        "alt+v": lambda self: self.toggle_preview(),
        "ctrl+t": lambda self: self.open_theme_picker(),
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
        "v": lambda self: self.toggle_preview(),
        "T": lambda self: self.open_theme_picker(),
        "x": lambda self: self.close_current(),
        "p": lambda self: self.start_move("pane"),
        "t": lambda self: self.start_move("tab"),
        "o": lambda self: self.toggle_open_scope(),
        "l": lambda self: self.accept(),
        "?": lambda self: self.show_help(),
        "q": lambda self: self.exit(None),
        "escape": lambda self: self.exit(None),
        "ctrl+h": lambda self: self.show_help(),
        "ctrl+x": lambda self: self.close_current(),
        "alt+p": lambda self: self.start_move("pane"),
        "alt+t": lambda self: self.start_move("tab"),
        "alt+a": lambda self: self.toggle_all(),
        "alt+o": lambda self: self.toggle_open_scope(),
        "alt+v": lambda self: self.toggle_preview(),
        "ctrl+t": lambda self: self.open_theme_picker(),
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
        return max(1, (self.size.height - 4) // 2)

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
        self.update_status()
        self.accept()
