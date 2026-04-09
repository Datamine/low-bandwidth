from __future__ import annotations

from collections import deque
import csv
from dataclasses import dataclass
import io
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from typing import Any
from typing import NamedTuple

from .actions import recipe_ids_for_process
from .models import ProcessUsage, Snapshot

DEFAULT_SAMPLE_SECONDS = 2
ROLLING_AVERAGE_WINDOW_SECONDS = 60
_PID_SUFFIX = re.compile(r"^(?P<name>.+?)\.(?P<pid>\d+)$")
_APP_PATH = re.compile(r"/(?P<bundle>[^/]+)\.app/")
_NETHOGS_ROW = re.compile(
    r"^(?P<prefix>.+?)\s+(?P<sent>\d+(?:\.\d+)?)\s+(?P<received>\d+(?:\.\d+)?)$"
)
_NETHOGS_TRAILING_IDENTITY = re.compile(r"/(?P<pid>\d+)/(?:[^/\s]+)$")
_NETHOGS_REFRESH = "Refreshing:"
_SS_PID = re.compile(r"pid=(?P<pid>\d+)")


class ProcessInfo(NamedTuple):
    pid: int
    command: str
    executable: str
    bundle_name: str | None
    is_background: bool


@dataclass(slots=True)
class ParsedRow:
    pid: int | None
    name: str
    download_bytes: int
    upload_bytes: int


@dataclass(slots=True)
class SamplePoint:
    timestamp: float
    download_bytes: int
    upload_bytes: int


class NethogsIdentity(NamedTuple):
    command: str
    pid: int


class BandwidthCollector:
    def __init__(self, sample_seconds: int = DEFAULT_SAMPLE_SECONDS) -> None:
        self.sample_seconds = sample_seconds
        self.last_debug: dict[str, Any] = {}
        self._rolling_samples: dict[tuple[int | None, str], deque[SamplePoint]] = {}
        self._rolling_processes: dict[tuple[int | None, str], ProcessUsage] = {}

    def snapshot(self) -> Snapshot:
        system = platform.system()
        if system == "Darwin":
            snapshot = self._macos_snapshot()
            return self._with_rolling_average(snapshot)
        if system == "Linux":
            snapshot = self._linux_snapshot()
            return self._with_rolling_average(snapshot)
        self._clear_rolling_average()
        return Snapshot(
            supported=False,
            platform=system,
            collector="unsupported",
            sample_seconds=self.sample_seconds,
            averaging_window_seconds=None,
            processes=[],
            notices=["Live traffic collection is currently implemented for macOS and Linux only."],
        )

    def _macos_snapshot(self) -> Snapshot:
        command = [
            shutil.which("nettop") or "nettop",
            "-P",
            "-x",
            "-d",
            "-n",
            "-L",
            "1",
            "-s",
            str(self.sample_seconds),
            "-J",
            "bytes_in,bytes_out",
        ]
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(10, self.sample_seconds + 5),
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "nettop returned a non-zero status."
            return Snapshot(
                supported=False,
                platform="Darwin",
                collector="nettop",
                sample_seconds=self.sample_seconds,
                averaging_window_seconds=None,
                processes=[],
                notices=[detail],
            )

        process_map = _read_process_map()
        port_map = _read_port_map("Darwin")
        processes = _merge_rows(parse_nettop_output(completed.stdout), process_map, port_map, self.sample_seconds, "Darwin")
        notices = ["Live traffic is sampled in short bursts from macOS `nettop`."]
        return Snapshot(
            supported=True,
            platform="Darwin",
            collector="nettop",
            sample_seconds=self.sample_seconds,
            averaging_window_seconds=None,
            processes=processes,
            notices=notices,
        )

    def _linux_snapshot(self) -> Snapshot:
        nethogs = shutil.which("nethogs")
        if nethogs is None:
            self.last_debug = {"platform": "Linux", "collector": "nethogs", "error": "nethogs not found in PATH"}
            return Snapshot(
                supported=False,
                platform="Linux",
                collector="nethogs",
                sample_seconds=self.sample_seconds,
                averaging_window_seconds=None,
                processes=[],
                notices=["Linux collection requires `nethogs` in PATH. Run `./scripts/install-linux-deps.sh`."],
            )

        command = [
            nethogs,
            "-t",
            "-d",
            str(self.sample_seconds),
            "-c",
            "2",
        ]
        completed, used_sudo, sudo_attempted = _run_linux_nethogs(command, self.sample_seconds)
        trace_output = _nethogs_trace_output(completed)
        self.last_debug = {
            "platform": "Linux",
            "collector": "nethogs",
            "command": completed.args,
            "used_sudo": used_sudo,
            "sudo_attempted": sudo_attempted,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "trace_excerpt": trace_output.splitlines()[:20],
        }
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "nethogs returned a non-zero status."
            notices = [detail]
            if _looks_like_permission_error(detail):
                if sudo_attempted:
                    notices.append("The app also tried `sudo -n nethogs`, but sudo needs cached credentials or NOPASSWD access.")
                notices.append("Run `sudo -v` first, or grant `nethogs` the needed packet-capture capabilities.")
            return Snapshot(
                supported=False,
                platform="Linux",
                collector="nethogs",
                sample_seconds=self.sample_seconds,
                averaging_window_seconds=None,
                processes=[],
                notices=notices,
            )

        process_map = _read_process_map()
        port_map = _read_port_map("Linux")
        rows = parse_nethogs_output(trace_output, self.sample_seconds)
        processes = _merge_rows(rows, process_map, port_map, self.sample_seconds, "Linux")
        notices = [
            "Live traffic is sampled in short bursts from Linux `nethogs`.",
            "Linux collection uses `nethogs`, so it depends on packet-capture privileges and only shows traffic the tool can attribute to a process.",
        ]
        if used_sudo:
            notices.append("Linux collection is running `nethogs` through `sudo -n`.")
        if trace_output and not rows:
            notices.append("nethogs returned trace output, but no rows matched the parser. Run `python3 run.py --dump-snapshot`.")
        self.last_debug["parsed_rows"] = len(rows)
        self.last_debug["process_map_entries"] = len(process_map)
        self.last_debug["snapshot_processes"] = len(processes)
        return Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=self.sample_seconds,
            averaging_window_seconds=None,
            processes=processes,
            notices=notices,
        )

    def debug_payload(self) -> dict[str, Any]:
        return {
            "sample_seconds": self.sample_seconds,
            "debug": self.last_debug,
        }

    def _with_rolling_average(self, snapshot: Snapshot) -> Snapshot:
        if not snapshot.supported:
            self._clear_rolling_average()
            return snapshot

        now = time.time()
        for process in snapshot.processes:
            key = (process.pid, process.name)
            samples = self._rolling_samples.setdefault(key, deque())
            samples.append(
                SamplePoint(timestamp=now, download_bytes=process.download_bytes, upload_bytes=process.upload_bytes)
            )
            self._rolling_processes[key] = process

        self._prune_rolling_average(now)
        averaged_processes: list[ProcessUsage] = []
        for key, samples in list(self._rolling_samples.items()):
            if not samples:
                continue
            process = self._rolling_processes[key]
            download_bytes = sum(sample.download_bytes for sample in samples)
            upload_bytes = sum(sample.upload_bytes for sample in samples)
            total_bytes = download_bytes + upload_bytes
            window_seconds = _effective_window_seconds(samples, now, self.sample_seconds)
            averaged_processes.append(
                ProcessUsage(
                    pid=process.pid,
                    name=process.name,
                    display_name=process.display_name,
                    command=process.command,
                    executable=process.executable,
                    bundle_name=process.bundle_name,
                    ports=process.ports.copy(),
                    download_bytes=download_bytes,
                    upload_bytes=upload_bytes,
                    total_bytes=total_bytes,
                    download_rate_bps=download_bytes / window_seconds,
                    upload_rate_bps=upload_bytes / window_seconds,
                    total_rate_bps=total_bytes / window_seconds,
                    is_background=process.is_background,
                    recipe_ids=process.recipe_ids.copy(),
                )
            )

        averaged_processes.sort(key=lambda process: process.total_bytes, reverse=True)
        notices = [
            *snapshot.notices,
            f"Processes stay visible using a rolling {ROLLING_AVERAGE_WINDOW_SECONDS}-second average instead of a single refresh.",
        ]
        return Snapshot(
            supported=snapshot.supported,
            platform=snapshot.platform,
            collector=snapshot.collector,
            sample_seconds=snapshot.sample_seconds,
            averaging_window_seconds=ROLLING_AVERAGE_WINDOW_SECONDS,
            processes=averaged_processes,
            notices=notices,
            collected_at=snapshot.collected_at,
        )

    def _clear_rolling_average(self) -> None:
        self._rolling_samples.clear()
        self._rolling_processes.clear()

    def _prune_rolling_average(self, now: float) -> None:
        cutoff = now - ROLLING_AVERAGE_WINDOW_SECONDS
        empty_keys: list[tuple[int | None, str]] = []
        for key, samples in self._rolling_samples.items():
            while samples and samples[0].timestamp < cutoff:
                samples.popleft()
            if not samples:
                empty_keys.append(key)
        for key in empty_keys:
            self._rolling_samples.pop(key, None)
            self._rolling_processes.pop(key, None)


def _effective_window_seconds(samples: deque[SamplePoint], now: float, sample_seconds: int) -> float:
    if not samples:
        return float(max(sample_seconds, 1))
    covered_seconds = now - samples[0].timestamp + sample_seconds
    return float(max(1, min(ROLLING_AVERAGE_WINDOW_SECONDS, covered_seconds)))


def parse_nettop_output(output: str) -> list[ParsedRow]:
    rows = list(csv.reader(io.StringIO(output)))
    header_index = _header_index(rows)
    if header_index is not None:
        return _parse_csv_rows(rows[header_index], rows[header_index + 1 :])
    return _parse_fallback_rows(output.splitlines())


def parse_nethogs_output(output: str, sample_seconds: int) -> list[ParsedRow]:
    results: list[ParsedRow] = []
    sample_window = max(sample_seconds, 1)
    for raw_line in _latest_nethogs_block(output):
        line = raw_line.strip()
        if not line or line == _NETHOGS_REFRESH:
            continue
        if line.startswith("NetHogs version"):
            continue
        if line.startswith(("Adding local address", "Ethernet linklayer", "Decoding", "Opening device")):
            continue

        match = _NETHOGS_ROW.match(line)
        if match is None:
            continue

        identity = _nethogs_identity(match.group("prefix"))
        if identity is None:
            continue
        if identity.pid == 0:
            continue

        sent_rate = float(match.group("sent"))
        received_rate = float(match.group("received"))
        command = identity.command
        results.append(
            ParsedRow(
                pid=identity.pid,
                name=_program_name(command),
                download_bytes=int(received_rate * 1024 * sample_window),
                upload_bytes=int(sent_rate * 1024 * sample_window),
            )
        )
    return results


def _nethogs_trace_output(completed: subprocess.CompletedProcess[str]) -> str:
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    return stdout or stderr


def _latest_nethogs_block(output: str) -> list[str]:
    latest_block: list[str] = []
    current_block: list[str] = []
    saw_refresh = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == _NETHOGS_REFRESH:
            if current_block:
                latest_block = current_block
            current_block = []
            saw_refresh = True
            continue
        if saw_refresh:
            current_block.append(raw_line)
    if current_block:
        return current_block
    if latest_block:
        return latest_block
    return output.splitlines()


def _run_linux_nethogs(
    command: list[str],
    sample_seconds: int,
) -> tuple[subprocess.CompletedProcess[str], bool, bool]:
    completed = _run_command(command, sample_seconds)
    if completed.returncode == 0:
        return completed, False, False
    detail = completed.stderr.strip() or completed.stdout.strip()
    if not _looks_like_permission_error(detail):
        return completed, False, False
    sudo = shutil.which("sudo")
    if sudo is None:
        return completed, False, False
    sudo_completed = _run_command([sudo, "-n", *command], sample_seconds)
    if sudo_completed.returncode == 0:
        return sudo_completed, True, True
    return sudo_completed, False, True


def _run_command(command: list[str], sample_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=max(10, sample_seconds + 5),
    )


def _looks_like_permission_error(detail: str) -> bool:
    lowered = detail.casefold()
    return any(
        token in lowered
        for token in (
            "permission denied",
            "operation not permitted",
            "must be root",
            "cap_net_admin",
            "cap_net_raw",
            "no password was provided",
            "a password is required",
        )
    )


def _header_index(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows):
        normalized = {_normalize_column(column) for column in row}
        if "bytes_in" in normalized and "bytes_out" in normalized:
            return index
    return None


def _parse_csv_rows(header: list[str], rows: list[list[str]]) -> list[ParsedRow]:
    column_map = {_normalize_column(name): position for position, name in enumerate(header)}
    results: list[ParsedRow] = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        parsed = _parse_row_cells(row, column_map)
        if parsed is not None:
            results.append(parsed)
    return results


def _parse_row_cells(row: list[str], column_map: dict[str, int]) -> ParsedRow | None:
    bytes_in = _read_int_at(row, column_map, "bytes_in")
    bytes_out = _read_int_at(row, column_map, "bytes_out")
    if bytes_in is None or bytes_out is None:
        return None

    identifier = _row_identifier(row, column_map)
    if identifier is None:
        return None

    pid, name = _split_identifier(identifier)
    return ParsedRow(pid=pid, name=name, download_bytes=bytes_in, upload_bytes=bytes_out)


def _read_int_at(row: list[str], column_map: dict[str, int], key: str) -> int | None:
    position = column_map.get(key)
    if position is None or position >= len(row):
        return None
    raw_value = row[position].strip()
    return int(raw_value) if raw_value.isdigit() else None


def _row_identifier(row: list[str], column_map: dict[str, int]) -> str | None:
    for column_name in ("process", "process_name", "name", "command"):
        position = column_map.get(column_name)
        if position is not None and position < len(row):
            value = row[position].strip()
            if value:
                return value
    for value in row:
        candidate = value.strip()
        if candidate and not candidate.isdigit():
            return candidate
    return None


def _parse_fallback_rows(lines: list[str]) -> list[ParsedRow]:
    results: list[ParsedRow] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or "bytes_in" in stripped or "bytes_out" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.split(",") if cell.strip()]
        if len(cells) < 3:
            continue
        identifier = cells[0]
        if not cells[-2].isdigit() or not cells[-1].isdigit():
            continue
        pid, name = _split_identifier(identifier)
        results.append(
            ParsedRow(
                pid=pid,
                name=name,
                download_bytes=int(cells[-2]),
                upload_bytes=int(cells[-1]),
            )
        )
    return results


def _split_identifier(identifier: str) -> tuple[int | None, str]:
    match = _PID_SUFFIX.match(identifier.strip())
    if match is None:
        return None, identifier.strip()
    return int(match.group("pid")), match.group("name").strip()


def _normalize_column(name: str) -> str:
    return name.strip().casefold().replace(" ", "_")


def _nethogs_identity(prefix: str) -> NethogsIdentity | None:
    stripped = prefix.strip()
    match = _NETHOGS_TRAILING_IDENTITY.search(stripped)
    if match is None:
        return None
    command = stripped[: match.start()].strip()
    if not command:
        return None
    if " " in command:
        first, remainder = command.split(maxsplit=1)
        if remainder.startswith("/") and "/" not in first and ":" not in first:
            command = remainder
    return NethogsIdentity(command=command, pid=int(match.group("pid")))


def _program_name(command: str) -> str:
    friendly = _friendly_process_name(command)
    if friendly is not None:
        return friendly
    command = command.rstrip("/")
    if not command:
        return command
    return command.rsplit("/", maxsplit=1)[-1]


def _read_port_map(system_name: str) -> dict[int, list[str]]:
    if system_name == "Linux":
        return _read_linux_port_map()
    return {}


def _read_linux_port_map() -> dict[int, list[str]]:
    command = [shutil.which("ss") or "ss", "-H", "-tunp"]
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        return {}
    return parse_ss_output(completed.stdout)


def parse_ss_output(output: str) -> dict[int, list[str]]:
    port_map: dict[int, list[str]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=6)
        if len(parts) < 7:
            continue
        protocol = parts[0]
        local_port = _endpoint_port(parts[4])
        peer_port = _endpoint_port(parts[5])
        if local_port is None:
            continue
        description = _port_description(protocol, local_port, peer_port)
        if description is None:
            continue
        for pid_match in _SS_PID.finditer(parts[6]):
            pid = int(pid_match.group("pid"))
            existing = port_map.setdefault(pid, [])
            if description not in existing:
                existing.append(description)
    return port_map


def _endpoint_port(endpoint: str) -> str | None:
    candidate = endpoint.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]:" in candidate:
        return candidate.rsplit("]:", maxsplit=1)[-1]
    if ":" not in candidate:
        return candidate
    return candidate.rsplit(":", maxsplit=1)[-1]


def _port_description(protocol: str, local_port: str, peer_port: str | None) -> str | None:
    if local_port == "*":
        return None
    if peer_port is None or peer_port == "*":
        return f"{local_port}/{protocol}"
    return f"{local_port}->{peer_port}/{protocol}"


def _merge_rows(
    rows: list[ParsedRow],
    process_map: dict[int, ProcessInfo],
    port_map: dict[int, list[str]],
    sample_seconds: int,
    system_name: str,
) -> list[ProcessUsage]:
    merged: dict[tuple[int | None, str], ProcessUsage] = {}
    for row in rows:
        process_info = process_map.get(row.pid) if row.pid is not None else None
        canonical_name = _canonical_process_name(row.name, process_info)
        key = (row.pid, canonical_name)
        ports = port_map.get(row.pid, []) if row.pid is not None else []
        existing = merged.get(key)
        if existing is None:
            display_name = _display_name(canonical_name, process_info)
            command = process_info.command if process_info is not None else None
            executable = process_info.executable if process_info is not None else None
            bundle_name = process_info.bundle_name if process_info is not None else None
            is_background = process_info.is_background if process_info is not None else _looks_background(canonical_name)
            existing = ProcessUsage(
                pid=row.pid,
                name=canonical_name,
                display_name=display_name,
                command=command,
                executable=executable,
                bundle_name=bundle_name,
                ports=ports.copy(),
                download_bytes=0,
                upload_bytes=0,
                total_bytes=0,
                download_rate_bps=0.0,
                upload_rate_bps=0.0,
                total_rate_bps=0.0,
                is_background=is_background,
                recipe_ids=[],
            )
            merged[key] = existing
        elif ports and not existing.ports:
            existing.ports = ports.copy()

        existing.download_bytes += row.download_bytes
        existing.upload_bytes += row.upload_bytes
        existing.total_bytes += row.download_bytes + row.upload_bytes

    sample_window = float(max(sample_seconds, 1))
    for process in merged.values():
        process.download_rate_bps = process.download_bytes / sample_window
        process.upload_rate_bps = process.upload_bytes / sample_window
        process.total_rate_bps = process.total_bytes / sample_window
        process.recipe_ids = recipe_ids_for_process(process.name, process.command, system_name)

    return sorted(merged.values(), key=lambda process: process.total_bytes, reverse=True)


def _read_process_map() -> dict[int, ProcessInfo]:
    command = [shutil.which("ps") or "ps", "-axo", "pid=,comm=,command="]
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        return {}

    process_map: dict[int, ProcessInfo] = {}
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        executable = parts[1]
        command_text = parts[2]
        bundle_name = _bundle_name_from_command(command_text)
        process_map[pid] = ProcessInfo(
            pid=pid,
            command=command_text,
            executable=executable,
            bundle_name=bundle_name,
            is_background=_looks_background(executable) and bundle_name is None,
        )
    return process_map


def _bundle_name_from_command(command: str) -> str | None:
    match = _APP_PATH.search(command)
    if match is None:
        return None
    return match.group("bundle")


def _canonical_process_name(row_name: str, process_info: ProcessInfo | None) -> str:
    if process_info is not None:
        for candidate in (process_info.executable, process_info.command):
            friendly = _friendly_process_name(candidate)
            if friendly is not None:
                return friendly
    friendly_row_name = _friendly_process_name(row_name)
    if friendly_row_name is not None:
        return friendly_row_name
    return row_name.strip()


def _display_name(name: str, process_info: ProcessInfo | None) -> str:
    if process_info is not None and process_info.bundle_name is not None:
        return process_info.bundle_name
    return name


def _friendly_process_name(candidate: str | None) -> str | None:
    if candidate is None:
        return None
    stripped = candidate.strip()
    if not stripped:
        return None

    token = stripped.split(maxsplit=1)[0].rstrip(":")
    if not token:
        return None
    if token.startswith("/"):
        token = Path(token).name.rstrip(":")
    if not token or token.isdigit():
        return None
    return token


def _linux_interfaces() -> list[str]:
    net_class = Path("/sys/class/net")
    if not net_class.exists():
        return []
    return sorted(entry.name for entry in net_class.iterdir() if entry.is_dir())


def _looks_background(name: str) -> bool:
    lowered = name.casefold()
    return any(
        token in lowered
        for token in ("daemon", "agent", "updated", "service", "helper", "bird", "cloudd", "launchd", "nsurlsessiond")
    )
