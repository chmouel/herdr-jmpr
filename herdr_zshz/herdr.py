"""Thin wrapper over the Herdr CLI plus layout reconstruction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess


class HerdrError(Exception):
    pass


def binary() -> str:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


class Herdr:
    def __init__(self, executable: str | None = None):
        self.executable = executable or binary()

    def run(self, *args: str) -> dict:
        try:
            completed = subprocess.run(
                [self.executable, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise HerdrError(f"could not run {self.executable}: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise HerdrError(f"herdr {' '.join(args)} failed: {detail}")
        try:
            return json.loads(completed.stdout)
        except ValueError as error:
            raise HerdrError(
                f"herdr {' '.join(args)} returned invalid JSON: {completed.stdout}"
            ) from error

    def result(self, *args: str) -> dict:
        return self.run(*args).get("result") or {}

    def workspaces(self) -> list[dict]:
        return self.result("workspace", "list").get("workspaces") or []

    def panes(self, workspace: str | None = None) -> list[dict]:
        args = ["pane", "list"]
        if workspace:
            args += ["--workspace", workspace]
        return self.result(*args).get("panes") or []

    def status_socket(self) -> str:
        response = self.run("status", "--json")
        candidates = [response]
        if isinstance(response, dict):
            candidates.append(response.get("result"))
        for payload in candidates:
            if not isinstance(payload, dict):
                continue
            server = payload.get("server")
            if not isinstance(server, dict):
                continue
            socket = server.get("socket")
            if isinstance(socket, str) and socket:
                return socket
        raise HerdrError(
            "herdr status --json did not report server.socket: " + json.dumps(response)
        )

    def pane_layout(self, pane_id: str) -> dict:
        return self.result("pane", "layout", "--pane", pane_id)["layout"]

    def tab_label(self, tab_id: str) -> str | None:
        return (self.result("tab", "get", tab_id).get("tab") or {}).get("label")

    def pane_move(self, *args: str) -> dict:
        command = ["pane", "move", *args, "--no-focus"]
        response = self.run(*command)
        move = (response.get("result") or {}).get("move_result") or {}
        if not move.get("changed"):
            raise HerdrError(
                "herdr refused the move: herdr " + " ".join(command) + "\n" + json.dumps(response)
            )
        return move.get("pane") or {}

    def workspace_create(self, cwd: str, label: str) -> dict:
        response = self.run("workspace", "create", "--cwd", cwd, "--label", label, "--focus")
        workspace = (response.get("result") or {}).get("workspace") or {}
        if not workspace.get("workspace_id"):
            raise HerdrError(
                "herdr workspace create did not return a workspace ID\n" + json.dumps(response)
            )
        return workspace

    def workspace_focus(self, workspace_id: str) -> None:
        self.run("workspace", "focus", workspace_id)

    def tab_focus(self, tab_id: str) -> None:
        self.run("tab", "focus", tab_id)

    def workspace_close(self, workspace_id: str) -> None:
        self.run("workspace", "close", workspace_id)


def _same(left: dict, right: dict) -> bool:
    return all(left[key] == right[key] for key in ("x", "y", "width", "height"))


def layout_ops(layout: dict) -> list[str]:
    """Rebuild the split tree from rects, as zsh-jump does with jq.

    The second child of a split is the largest rect flush with the split's far
    edge. Returns the root anchor followed by one ``pane target direction
    ratio`` line per split, in replay order.
    """

    panes = layout.get("panes") or []
    splits = layout.get("splits") or []
    rects = [pane["rect"] for pane in panes] + [split["rect"] for split in splits]

    def build(area: dict) -> dict:
        split = next((s for s in splits if _same(s["rect"], area)), None)
        if split is None:
            pane = next((p for p in panes if _same(p["rect"], area)), None)
            if pane is None:
                raise HerdrError(f"could not map layout area {area} to a pane")
            return {"pane": pane["pane_id"]}
        right = split["direction"] == "right"
        if right:
            candidates = [
                rect
                for rect in rects
                if rect["y"] == area["y"]
                and rect["height"] == area["height"]
                and rect["x"] > area["x"]
                and rect["x"] + rect["width"] == area["x"] + area["width"]
            ]
        else:
            candidates = [
                rect
                for rect in rects
                if rect["x"] == area["x"]
                and rect["width"] == area["width"]
                and rect["y"] > area["y"]
                and rect["y"] + rect["height"] == area["y"] + area["height"]
            ]
        if not candidates:
            raise HerdrError(f"could not find the second child of split {area}")
        second = max(candidates, key=lambda rect: rect["width"] * rect["height"])
        first_area = dict(area)
        if right:
            first_area["width"] = second["x"] - area["x"]
        else:
            first_area["height"] = second["y"] - area["y"]
        return {
            "direction": split["direction"],
            "ratio": split["ratio"],
            "first": build(first_area),
            "second": build(second),
        }

    def anchor(node: dict) -> str:
        return node["pane"] if "pane" in node else anchor(node["first"])

    tree = build(layout["area"])
    lines: list[str] = [anchor(tree)]

    def ops(node: dict) -> None:
        if "pane" in node:
            return
        lines.append(
            "\t".join(
                [anchor(node["second"]), anchor(node["first"]), node["direction"], str(node["ratio"])]
            )
        )
        ops(node["first"])
        ops(node["second"])

    ops(tree)
    return lines
