from __future__ import annotations

from collections import deque
import curses
from dataclasses import dataclass, field
import queue
import threading
import time
import textwrap

from .actions import ActionController
from .collector import BandwidthCollector
from .models import ActionResult, ProcessUsage, Recipe, Snapshot

AUTO_REFRESH_DELAY_SECONDS = 0.25
HIDE_SMALL_PROCESS_THRESHOLD_BYTES = 1024
TABLE_BYTES_WIDTH = 8
TABLE_PREFERRED_PROCESS_WIDTH = 18
TABLE_PREFERRED_PORTS_WIDTH = 18
TABLE_MIN_FLEX_WIDTH = 8
TABLE_COLUMN_GAP_COUNT = 6


def run_tui(collector: BandwidthCollector, actions: ActionController) -> None:
    app = TuiApp(collector=collector, actions=actions)
    curses.wrapper(app.run)


def recipe_shortcuts(recipes: list[Recipe]) -> dict[str, Recipe]:
    keys = "abcdefghijklmnopqrstuvwxyz"
    return {key: recipe for key, recipe in zip(keys, recipes, strict=False)}


def _toggle_marker(is_on: bool) -> str:
    return "[on]" if is_on else "[off]"


def commands_line_text(shortcuts: dict[str, Recipe], recipe_states: dict[str, bool], hide_small_processes: bool) -> str:
    commands = [
        "q quit",
        f"h {_toggle_marker(hide_small_processes)} hide<1KB",
        "t stop",
        "x kill",
    ]
    commands.extend(
        f"{key} {_toggle_marker(recipe_states.get(recipe.recipe_id, False))} {truncate(recipe.title, 22)}"
        for key, recipe in shortcuts.items()
    )
    return "Commands: " + " | ".join(commands)


def format_bytes(value: float) -> str:
    if value < 1024:
        return f"{int(value)}B"
    if value < 1024**2:
        return f"{value / 1024:.1f}K"
    if value < 1024**3:
        return f"{value / 1024**2:.1f}M"
    return f"{value / 1024**3:.2f}G"


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return f"{text[: width - 1]}…"


def wrapped_lines(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    if not text:
        return [""]
    return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or [""]


def format_ports(ports: list[str]) -> str:
    return ", ".join(ports) if ports else "-"


@dataclass(frozen=True, slots=True)
class TableLayout:
    pid_width: int
    process_width: int
    ports_width: int
    bytes_width: int


def table_layout(processes: list[ProcessUsage], available_width: int | None = None) -> TableLayout:
    pid_width = max(7, len("PID"), *(len(str(process.pid or "-")) for process in processes))
    layout = TableLayout(
        pid_width=pid_width,
        process_width=TABLE_PREFERRED_PROCESS_WIDTH,
        ports_width=TABLE_PREFERRED_PORTS_WIDTH,
        bytes_width=TABLE_BYTES_WIDTH,
    )
    if available_width is None:
        return layout

    fixed_width = (
        layout.pid_width
        + (layout.bytes_width * 4)
        + TABLE_COLUMN_GAP_COUNT
    )
    available_flex_width = max(TABLE_MIN_FLEX_WIDTH * 2, available_width - fixed_width)
    process_width = min(TABLE_PREFERRED_PROCESS_WIDTH, max(TABLE_MIN_FLEX_WIDTH, available_flex_width // 2))
    ports_width = min(TABLE_PREFERRED_PORTS_WIDTH, max(TABLE_MIN_FLEX_WIDTH, available_flex_width - process_width))

    if process_width + ports_width < available_flex_width:
        ports_width = min(TABLE_PREFERRED_PORTS_WIDTH, ports_width + (available_flex_width - process_width - ports_width))

    return TableLayout(
        pid_width=layout.pid_width,
        process_width=process_width,
        ports_width=ports_width,
        bytes_width=layout.bytes_width,
    )


def process_row_text(index: int, process: ProcessUsage, layout: TableLayout, display_name: str | None = None) -> str:
    name = truncate(display_name or process.display_name, layout.process_width)
    ports = truncate(format_ports(process.ports), layout.ports_width)
    return (
        f"{str(process.pid or '-'):>{layout.pid_width}} "
        f"{name:<{layout.process_width}} {ports:<{layout.ports_width}} "
        f"{format_bytes(process.download_bytes):>{layout.bytes_width}} "
        f"{format_bytes(process.upload_bytes):>{layout.bytes_width}} "
        f"{format_bytes(process.total_bytes):>{layout.bytes_width}} "
        f"{format_bytes(process.total_rate_bps):>{layout.bytes_width}}"
    )


def header_row_text(layout: TableLayout) -> str:
    return (
        f"{'PID':>{layout.pid_width}} "
        f"{'Process':<{layout.process_width}} {'Ports':<{layout.ports_width}} "
        f"{'Down':>{layout.bytes_width}} {'Up':>{layout.bytes_width}} "
        f"{'Total':>{layout.bytes_width}} {'Rate':>{layout.bytes_width}}"
    )


def selected_summary_text(selected: ProcessUsage) -> str:
    return f"Selected: {(selected.command or selected.name)} pid={selected.pid or '-'} total={format_bytes(selected.total_bytes)}"


def detail_block_height(selected: ProcessUsage | None, width: int) -> int:
    if selected is None:
        return 2
    return len(wrapped_lines(selected_summary_text(selected), width)) + 2


def process_identity(process: ProcessUsage | None) -> tuple[int | None, str | None, str] | None:
    if process is None:
        return None
    return (process.pid, process.command, process.name)


def total_rate_text(processes: list[ProcessUsage]) -> str:
    upload_rate = sum(process.upload_rate_bps for process in processes)
    download_rate = sum(process.download_rate_bps for process in processes)
    total_rate = upload_rate + download_rate
    return f"Total Rate: {format_bytes(total_rate)} ({format_bytes(upload_rate)} Up / {format_bytes(download_rate)} Down)"


@dataclass(slots=True)
class TuiApp:
    collector: BandwidthCollector
    actions: ActionController
    history: deque[ActionResult] = field(default_factory=lambda: deque(maxlen=8))
    selected_index: int | None = None
    hide_small_processes: bool = True
    snapshot: Snapshot | None = None
    status_message: str = "Loading…"
    _colors: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _snapshot_queue: queue.SimpleQueue[Snapshot] = field(default_factory=queue.SimpleQueue, init=False, repr=False)
    _error_queue: queue.SimpleQueue[str] = field(default_factory=queue.SimpleQueue, init=False, repr=False)
    _refresh_requested: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _shutdown_requested: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _collector_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _recipe_states: dict[str, bool] = field(default_factory=dict, init=False, repr=False)
    _killed_processes: set[tuple[int | None, str | None, str]] = field(default_factory=set, init=False, repr=False)

    def run(self, stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.timeout(200)
        self._init_colors()
        self._refresh_recipe_states()
        self.status_message = "Collecting snapshot…"
        self._start_collector_thread()
        self._request_snapshot_refresh("Collecting snapshot…")

        try:
            while True:
                self._drain_snapshot_queue()
                self._draw(stdscr)
                key = stdscr.getch()
                if key in (ord("q"), ord("Q")):
                    return
                if key == curses.KEY_RESIZE:
                    continue

                self._handle_keypress(key)
        finally:
            self._stop_collector_thread()

    def _handle_keypress(self, key: int) -> bool:
        if key in (curses.KEY_DOWN, ord("j"), ord("J")):
            self._move_selection(1)
            return True
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            self._move_selection(-1)
            return True
        if key in (ord("h"), ord("H")):
            self._toggle_hide_small_processes()
            return True
        if key in (ord("t"), ord("T")):
            self._act_on_selected_process("terminate")
            return True
        if key == ord("x"):
            self._act_on_selected_process("kill")
            return True

        recipe = recipe_shortcuts(self.actions.list_recipes()).get(chr(key).lower()) if 0 <= key < 256 else None
        if recipe is not None:
            self._record_result(self.actions.execute_recipe(recipe.recipe_id))
            self._refresh_recipe_states()
            self._request_snapshot_refresh("Refreshing snapshot…")
            return True
        return False

    def _move_selection(self, delta: int) -> None:
        process_count = len(self._visible_processes())
        if process_count == 0:
            self.selected_index = None
            return
        if self.selected_index is None:
            self.selected_index = 0 if delta >= 0 else process_count - 1
            return
        self.selected_index = min(max(self.selected_index + delta, 0), process_count - 1)

    def _act_on_selected_process(self, action: str) -> None:
        process = self._selected_process()
        if process is None or process.pid is None:
            self.status_message = "No selectable PID on the current row."
            return
        selected_identity = process_identity(process)
        self._record_result(self.actions.execute_process_action(process.pid, action))
        if action == "kill" and selected_identity is not None:
            self._killed_processes.add(selected_identity)
        self._request_snapshot_refresh("Refreshing snapshot…")

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        previous_identity = process_identity(self._selected_process())
        self.snapshot = snapshot
        current_identities = {process_identity(process) for process in snapshot.processes}
        self._killed_processes.intersection_update(identity for identity in current_identities if identity is not None)
        visible_processes = self._visible_processes()
        process_count = len(visible_processes)
        if process_count == 0:
            self.selected_index = None
        elif previous_identity is None:
            if self.selected_index is None:
                self.selected_index = 0
            else:
                self.selected_index = min(self.selected_index, process_count - 1)
        else:
            matching_index = next(
                (index for index, process in enumerate(visible_processes) if process_identity(process) == previous_identity),
                None,
            )
            self.selected_index = matching_index
        if previous_identity is not None and self.selected_index is None and process_count > 0:
            self.status_message = "Selected process disappeared on refresh."
            return
        self.status_message = f"Updated {time.strftime('%H:%M:%S')}"

    def _request_snapshot_refresh(self, message: str | None = None) -> None:
        if message is not None:
            self.status_message = message
        self._refresh_requested.set()

    def _refresh_recipe_states(self) -> None:
        self._recipe_states = self.actions.recipe_states()

    def _start_collector_thread(self) -> None:
        if self._collector_thread is not None:
            return
        self._collector_thread = threading.Thread(target=self._collector_loop, name="low-bandwidth-collector", daemon=True)
        self._collector_thread.start()

    def _stop_collector_thread(self) -> None:
        self._shutdown_requested.set()
        self._refresh_requested.set()
        if self._collector_thread is not None:
            self._collector_thread.join(timeout=1)
            self._collector_thread = None

    def _collector_loop(self) -> None:
        while not self._shutdown_requested.is_set():
            self._refresh_requested.wait(timeout=AUTO_REFRESH_DELAY_SECONDS)
            if self._shutdown_requested.is_set():
                return
            self._refresh_requested.clear()
            try:
                snapshot = self.collector.snapshot()
            except Exception as exc:  # pragma: no cover - defensive thread handoff
                self._error_queue.put(f"Snapshot failed: {type(exc).__name__}: {exc}")
                continue
            self._snapshot_queue.put(snapshot)

    def _drain_snapshot_queue(self) -> None:
        while True:
            try:
                snapshot = self._snapshot_queue.get_nowait()
            except queue.Empty:
                break
            self._apply_snapshot(snapshot)
        while True:
            try:
                message = self._error_queue.get_nowait()
            except queue.Empty:
                break
            self.status_message = message

    def _record_result(self, result: ActionResult) -> None:
        self.history.appendleft(result)
        self.status_message = f"{result.title}: {result.detail}"

    def _selected_process(self) -> ProcessUsage | None:
        visible_processes = self._visible_processes()
        if not visible_processes or self.selected_index is None:
            return None
        return visible_processes[self.selected_index]

    def _visible_processes(self) -> list[ProcessUsage]:
        if self.snapshot is None:
            return []
        if not self.hide_small_processes:
            return self.snapshot.processes
        return [
            process
            for process in self.snapshot.processes
            if process.total_bytes >= HIDE_SMALL_PROCESS_THRESHOLD_BYTES
        ]

    def _toggle_hide_small_processes(self) -> None:
        previous_identity = process_identity(self._selected_process())
        self.hide_small_processes = not self.hide_small_processes
        visible_processes = self._visible_processes()
        if not visible_processes:
            self.selected_index = None
        elif previous_identity is None:
            self.selected_index = min(self.selected_index or 0, len(visible_processes) - 1)
        else:
            self.selected_index = next(
                (index for index, process in enumerate(visible_processes) if process_identity(process) == previous_identity),
                None,
            )

        if self.hide_small_processes:
            if previous_identity is not None and self.selected_index is None:
                self.status_message = "Small-process filter enabled. Current selection is hidden."
            else:
                self.status_message = "Small-process filter enabled. Hiding rows below 1KB total."
            return
        if self.selected_index is None and visible_processes:
            self.selected_index = 0
        self.status_message = "Small-process filter disabled. Showing all rows."

    def _display_name(self, process: ProcessUsage) -> str:
        if process_identity(process) in self._killed_processes:
            return f"[killed] {process.display_name}"
        return process.display_name

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        snapshot = self.snapshot
        if snapshot is None:
            self._write(stdscr, 0, 0, "Loading snapshot…", width, curses.A_BOLD)
            stdscr.refresh()
            return

        recipes = self.actions.list_recipes()
        shortcuts = recipe_shortcuts(recipes)
        selected = self._selected_process()

        row = 0
        self._write(stdscr, row, 0, "Low Bandwidth TUI", width, self._attr("title"))
        row += 1
        window_summary = (
            f"sample={snapshot.sample_seconds}s avg={snapshot.averaging_window_seconds}s"
            if snapshot.averaging_window_seconds is not None
            else f"sample={snapshot.sample_seconds}s"
        )
        summary = f"collector={snapshot.collector} {window_summary} processes={len(self._visible_processes())}/{len(snapshot.processes)}"
        self._write(stdscr, row, 0, summary, width, self._attr("muted"))
        row += 1

        for line in wrapped_lines(" ".join(snapshot.notices) or "No notices.", width):
            self._write(stdscr, row, 0, line, width, self._attr("muted"))
            row += 1

        for line in wrapped_lines(commands_line_text(shortcuts, self._recipe_states, self.hide_small_processes), width):
            self._write(stdscr, row, 0, line, width, curses.A_BOLD)
            row += 1
        row += 1

        visible_processes = self._visible_processes()
        layout = table_layout(visible_processes, width)
        header = header_row_text(layout)
        self._write(stdscr, row, 0, header, width, curses.A_BOLD)

        table_top = row + 1
        detail_height = detail_block_height(selected, width)
        detail_top = max(table_top + 6, height - detail_height - 1)
        visible_rows = max(1, detail_top - table_top - 1)
        start_index = self._table_start(visible_rows)

        if not visible_processes:
            self._write(
                stdscr,
                table_top,
                0,
                "No visible process traffic found in the rolling average window.",
                width,
            )
        else:
            for row_offset, process in enumerate(visible_processes[start_index : start_index + visible_rows]):
                row = table_top + row_offset
                index = start_index + row_offset
                row_text = process_row_text(index, process, layout, display_name=self._display_name(process))
                attr = self._attr("selected") if self.selected_index is not None and index == self.selected_index else curses.A_NORMAL
                self._write(stdscr, row, 0, row_text, width, attr)

        self._draw_selected_block(stdscr, detail_top, width, selected)
        stdscr.refresh()

    def _draw_selected_block(
        self,
        stdscr: curses.window,
        top: int,
        width: int,
        selected: ProcessUsage | None,
    ) -> None:
        if selected is None:
            self._write(stdscr, top, 0, "Selected: none", width, curses.A_BOLD)
            self._write(
                stdscr,
                top + 1,
                0,
                f"Status: {self.status_message} | {total_rate_text(self._visible_processes())}",
                width,
                self._attr("muted"),
            )
            return

        selected_lines = wrapped_lines(selected_summary_text(selected), width)
        for offset, line in enumerate(selected_lines):
            self._write(stdscr, top + offset, 0, line, width, curses.A_BOLD)

        next_row = top + len(selected_lines)
        self._write(stdscr, next_row, 0, f"Ports: {format_ports(selected.ports)}", width, curses.A_BOLD)

        self._write(
            stdscr,
            next_row + 1,
            0,
            f"Status: {self.status_message} | {total_rate_text(self._visible_processes())}",
            width,
            self._status_attr(),
        )

    def _table_start(self, visible_rows: int) -> int:
        process_count = len(self._visible_processes())
        if process_count <= visible_rows:
            return 0
        if self.selected_index is None:
            return 0
        midpoint = visible_rows // 2
        return min(max(self.selected_index - midpoint, 0), process_count - visible_rows)

    def _init_colors(self) -> None:
        self._colors = {}
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        self._colors = {
            "selected": curses.color_pair(1) | curses.A_BOLD,
            "title": curses.color_pair(2) | curses.A_BOLD,
            "muted": curses.color_pair(4),
            "ok": curses.color_pair(3),
        }

    def _status_attr(self) -> int:
        latest = self.history[0] if self.history else None
        if latest is not None and latest.ok:
            return self._attr("ok")
        return self._attr("muted")

    def _attr(self, name: str) -> int:
        return self._colors.get(name, curses.A_NORMAL)

    def _write(self, stdscr: curses.window, row: int, col: int, text: str, width: int, attr: int = 0) -> None:
        if row < 0:
            return
        try:
            stdscr.addnstr(row, col, truncate(text, max(0, width - col)), max(0, width - col), attr)
        except curses.error:
            return
