from __future__ import annotations

from collections.abc import Iterable
import os
import platform
import shlex
import shutil
import signal
import subprocess
import time
from typing import NamedTuple

from .models import ActionResult, Recipe


class LaunchdService(NamedTuple):
    domain_template: str
    label: str


class ToggleRecipeConfig(NamedTuple):
    recipe: Recipe
    process_names: tuple[str, ...]
    launchd_services: tuple[LaunchdService, ...] = ()
    softwareupdate_schedule: bool = False


def _toggle_recipe(
    recipe_id: str,
    title: str,
    summary: str,
    instructions: str,
    command_preview: str,
    *,
    admin_required: bool,
) -> Recipe:
    return Recipe(
        recipe_id=recipe_id,
        title=title,
        summary=summary,
        instructions=instructions,
        command_preview=command_preview,
        admin_required=admin_required,
        temporary=False,
        disruptive=True,
    )


def _toggle_recipes() -> dict[str, ToggleRecipeConfig]:
    return {
        "toggle-icloud-sync": ToggleRecipeConfig(
            recipe=_toggle_recipe(
                "toggle-icloud-sync",
                "iCloud blocker",
                "Toggles a restart blocker for the main iCloud sync daemons.",
                "Uses launchctl disable/enable on the known iCloud agents, and stops running sync daemons when turning the blocker on.",
                "launchctl disable|enable gui/$UID/com.apple.bird gui/$UID/com.apple.cloudd",
                admin_required=False,
            ),
            process_names=("bird", "cloudd"),
            launchd_services=(
                LaunchdService("gui/{uid}", "com.apple.bird"),
                LaunchdService("gui/{uid}", "com.apple.cloudd"),
            ),
        ),
        "toggle-app-store-downloads": ToggleRecipeConfig(
            recipe=_toggle_recipe(
                "toggle-app-store-downloads",
                "App Store blocker",
                "Toggles a restart blocker for the common App Store and asset download services.",
                "Uses launchctl disable/enable on the known App Store services, and stops active download daemons when turning the blocker on.",
                "launchctl disable|enable gui/$UID/com.apple.appstoreagent system/com.apple.storeassetd gui/$UID/com.apple.storedownloadd",
                admin_required=True,
            ),
            process_names=("appstoreagent", "storeassetd", "storedownloadd"),
            launchd_services=(
                LaunchdService("gui/{uid}", "com.apple.appstoreagent"),
                LaunchdService("system", "com.apple.storeassetd"),
                LaunchdService("gui/{uid}", "com.apple.storedownloadd"),
            ),
        ),
        "toggle-system-update-checks": ToggleRecipeConfig(
            recipe=_toggle_recipe(
                "toggle-system-update-checks",
                "Update check blocker",
                "Toggles the background software update check scheduler.",
                "Turns `softwareupdate --schedule` off when the blocker is enabled, disables the background softwareupdated service, and restores both when disabled.",
                "softwareupdate --schedule off|on && launchctl disable|enable system/com.apple.softwareupdated",
                admin_required=True,
            ),
            process_names=("softwareupdated",),
            launchd_services=(LaunchdService("system", "com.apple.softwareupdated"),),
            softwareupdate_schedule=True,
        ),
    }


def recipe_catalog(system_name: str = "Darwin") -> dict[str, Recipe]:
    if system_name != "Darwin":
        return {}
    return {recipe_id: config.recipe for recipe_id, config in _toggle_recipes().items()}


def recipe_ids_for_process(name: str, command: str | None, system_name: str = "Darwin") -> list[str]:
    if system_name != "Darwin":
        return []
    signature = " ".join(part for part in [name, command or ""]).casefold()
    recipe_ids: list[str] = []
    if any(token in signature for token in ("bird", "cloudd", "icloud")):
        recipe_ids.append("toggle-icloud-sync")
    if any(token in signature for token in ("softwareupdated", "storeassetd", "storedownloadd", "appstoreagent")):
        recipe_ids.extend(["toggle-app-store-downloads", "toggle-system-update-checks"])
    return sorted(set(recipe_ids))


class ActionController:
    def __init__(self, system_name: str | None = None) -> None:
        self._system = system_name or platform.system()
        self._recipes = recipe_catalog(self._system)
        self._toggle_recipes = _toggle_recipes() if self._system == "Darwin" else {}

    @property
    def supported(self) -> bool:
        return self._system == "Darwin"

    @property
    def platform_name(self) -> str:
        return self._system

    def list_recipes(self) -> list[Recipe]:
        return list(self._recipes.values())

    def recipe_states(self) -> dict[str, bool]:
        if not self.supported:
            return {}
        return {recipe_id: self.recipe_state(recipe_id) for recipe_id in self._toggle_recipes}

    def recipe_state(self, recipe_id: str) -> bool:
        config = self._toggle_recipes.get(recipe_id)
        if config is None or not self.supported:
            return False
        if config.softwareupdate_schedule:
            return _softwareupdate_schedule_disabled(
                self._command_path("softwareupdate"),
                sudo_command=self._command_path("sudo"),
                use_sudo=config.recipe.admin_required,
            )
        if not config.launchd_services:
            return False
        launchctl = self._command_path("launchctl")
        sudo = self._command_path("sudo")
        return any(
            _launchctl_service_disabled(
                service,
                launchctl,
                sudo_command=sudo,
                use_sudo=config.recipe.admin_required,
            )
            for service in config.launchd_services
        )

    def execute_process_action(self, pid: int, action: str) -> ActionResult:
        if not self.supported:
            return ActionResult(ok=False, title="Unsupported platform", detail="Process controls only work on macOS.")

        signal_name = self._signal_for_action(action)
        if signal_name is None:
            return ActionResult(ok=False, title="Unknown action", detail=f"Unsupported process action: {action}")

        try:
            os.kill(pid, signal_name)
        except ProcessLookupError:
            return ActionResult(ok=False, title="Process not found", detail=f"PID {pid} is no longer running.")
        except PermissionError:
            return ActionResult(
                ok=False,
                title="Permission denied",
                detail=f"macOS refused to signal PID {pid}. Try running the dashboard with higher privileges.",
            )

        if signal_name == signal.SIGTERM:
            if _wait_for_process_exit(pid):
                return ActionResult(ok=True, title="Stopped", detail=f"PID {pid} exited after SIGTERM.")
            return ActionResult(ok=True, title="Stop requested", detail=f"Sent SIGTERM to PID {pid}, but it is still running.")

        return ActionResult(ok=True, title="Force stopped", detail=f"Sent SIGKILL to PID {pid}.")

    def execute_recipe(self, recipe_id: str) -> ActionResult:
        config = self._toggle_recipes.get(recipe_id)
        if config is None:
            return ActionResult(ok=False, title="Unknown preset", detail=f"No preset exists for {recipe_id}.")
        if not self.supported:
            return ActionResult(ok=False, title="Unsupported platform", detail="Presets only work on macOS.")
        turning_on = not self.recipe_state(recipe_id)
        return self._execute_toggle_recipe(config, turning_on)

    def _kill_named_processes(self, process_names: Iterable[str]) -> ActionResult:
        targets = [name.casefold() for name in process_names]
        matched: list[str] = []

        for pid, command in _iter_processes():
            candidate = command.casefold()
            if any(target in candidate for target in targets):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    return ActionResult(ok=False, title="Permission denied", detail="macOS refused to stop one of the matching processes.")
                matched.append(f"{pid}:{command}")
        return ActionResult(
            ok=True,
            title="Stopped matching processes",
            detail=f"Stopped {len(matched)} process(es): {', '.join(matched)}" if matched else "No matching processes were running.",
        )

    def _run_recipe_command(self, recipe: Recipe, command: list[str], *, require_admin: bool = False) -> ActionResult:
        prepared_command = self._prepare_command(command, require_admin=require_admin)
        completed = subprocess.run(  # noqa: S603
            prepared_command,
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        detail = completed.stdout.strip() or completed.stderr.strip() or "Command finished."
        return ActionResult(
            ok=completed.returncode == 0,
            title=recipe.title,
            detail=detail,
            command=_format_command(prepared_command),
            stdout=completed.stdout.strip() or None,
            stderr=completed.stderr.strip() or None,
        )

    def _execute_toggle_recipe(self, config: ToggleRecipeConfig, turning_on: bool) -> ActionResult:
        failures: list[str] = []
        executed: list[str] = []
        details: list[str] = []

        if config.softwareupdate_schedule:
            schedule_result = self._toggle_softwareupdate_schedule(config, turning_on)
            if schedule_result.command:
                executed.append(schedule_result.command)
            if schedule_result.ok:
                details.append(schedule_result.detail)
            else:
                failures.append(schedule_result.detail)

        if config.launchd_services:
            launchd_result = self._toggle_launchd_blocker(config, turning_on)
            if launchd_result.command:
                executed.append(launchd_result.command)
            if launchd_result.ok:
                details.append(launchd_result.detail)
            else:
                failures.append(launchd_result.detail)

        stopped = self._kill_named_processes(config.process_names) if turning_on else None
        if stopped is not None:
            if stopped.ok:
                details.append(stopped.detail)
            else:
                failures.append(stopped.detail)

        verified_state = self.recipe_state(config.recipe.recipe_id)
        if turning_on and not verified_state:
            failures.append("The blocker commands ran, but launchd/softwareupdate did not verify as blocked.")
        if not turning_on and verified_state:
            failures.append("The unblock commands ran, but launchd/softwareupdate still reports the blocker as active.")

        title_prefix = "Enabled" if turning_on else "Disabled"
        detail = " ".join(part for part in details if part).strip()
        if not detail:
            detail = "Restart blocking is active." if turning_on else "Restart blocking is inactive."
        if failures:
            if detail:
                detail = f"{detail} {' ; '.join(failures)}"
            else:
                detail = " ; ".join(failures)
            return ActionResult(
                ok=False,
                title=f"{title_prefix} {config.recipe.title}",
                detail=detail,
                command=" && ".join(executed) or None,
            )

        return ActionResult(
            ok=True,
            title=f"{title_prefix} {config.recipe.title}",
            detail=detail,
            command=" && ".join(executed) or None,
        )

    def _toggle_launchd_blocker(self, config: ToggleRecipeConfig, turning_on: bool) -> ActionResult:
        launchctl = self._command_path("launchctl")
        executed: list[str] = []
        failures: list[str] = []

        for service in config.launchd_services:
            candidates = _service_candidates(service)
            candidate_succeeded = False
            candidate_failures: list[str] = []
            for candidate in candidates:
                target = _launchctl_target(candidate)
                commands = [[launchctl, "disable" if turning_on else "enable", target]]
                if turning_on:
                    commands.append([launchctl, "bootout", target])
                current_executed: list[str] = []
                current_failures: list[str] = []
                for command in commands:
                    prepared_command = self._prepare_command(command, require_admin=config.recipe.admin_required)
                    completed = subprocess.run(  # noqa: S603
                        prepared_command,
                        capture_output=True,
                        check=False,
                        text=True,
                        timeout=20,
                    )
                    current_executed.append(_format_command(prepared_command))
                    if completed.returncode != 0:
                        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
                        if turning_on and command[1] == "bootout" and _launchctl_absent_target(detail):
                            continue
                        current_failures.append(f"{_format_command(prepared_command)} => {detail}")
                executed.extend(current_executed)
                if not current_failures:
                    candidate_succeeded = True
                    break
                candidate_failures.extend(current_failures)
            if not candidate_succeeded:
                failures.extend(candidate_failures)

        detail = "Restart blocking is active." if turning_on else "Restart blocking is inactive."
        return ActionResult(
            ok=not failures,
            title=config.recipe.title,
            detail=detail if not failures else " ; ".join(failures),
            command=" && ".join(executed) or None,
        )

    def _toggle_softwareupdate_schedule(self, config: ToggleRecipeConfig, turning_on: bool) -> ActionResult:
        recipe = config.recipe
        command = [self._command_path("softwareupdate"), "--schedule", "off" if turning_on else "on"]
        result = self._run_recipe_command(recipe, command, require_admin=recipe.admin_required)
        verb = "Enabled" if turning_on else "Disabled"
        result.title = f"{verb} {recipe.title}"
        return result

    def _command_path(self, command: str) -> str:
        return shutil.which(command) or command

    def _prepare_command(self, command: list[str], *, require_admin: bool) -> list[str]:
        if not require_admin or os.geteuid() == 0:
            return command
        sudo = self._command_path("sudo")
        return [sudo, "-n", *command]

    def _signal_for_action(self, action: str) -> signal.Signals | None:
        if action == "terminate":
            return signal.SIGTERM
        if action == "kill":
            return signal.SIGKILL
        return None


def _launchd_uid() -> int:
    sudo_uid = os.environ.get("SUDO_UID")
    if os.geteuid() == 0 and sudo_uid and sudo_uid.isdigit():
        return int(sudo_uid)
    return os.getuid()


def _launchctl_target(service: LaunchdService) -> str:
    return f"{service.domain_template.format(uid=_launchd_uid())}/{service.label}"


def _launchctl_service_disabled(
    service: LaunchdService,
    launchctl: str,
    *,
    sudo_command: str,
    use_sudo: bool,
) -> bool:
    for candidate in _service_candidates(service):
        domain = candidate.domain_template.format(uid=_launchd_uid())
        command = [launchctl, "print-disabled", domain]
        if use_sudo and os.geteuid() != 0:
            command = [sudo_command, "-n", *command]
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        if completed.returncode != 0:
            continue
        lowered = completed.stdout.casefold()
        label = candidate.label.casefold()
        if f'"{label}" => true' in lowered or f"{label} => true" in lowered:
            return True
    return False


def _softwareupdate_schedule_disabled(softwareupdate: str, *, sudo_command: str, use_sudo: bool) -> bool:
    command = [softwareupdate, "--schedule"]
    if use_sudo and os.geteuid() != 0:
        command = [sudo_command, "-n", *command]
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        return False
    lowered = " ".join(part.strip() for part in (completed.stdout, completed.stderr) if part).casefold()
    if "automatic check is off" in lowered:
        return True
    if "automatic check is on" in lowered:
        return False
    return False


def _service_candidates(service: LaunchdService) -> list[LaunchdService]:
    if service.domain_template.startswith("gui/"):
        return [service, LaunchdService(service.domain_template.replace("gui/", "user/", 1), service.label)]
    return [service]


def _launchctl_absent_target(detail: str) -> bool:
    lowered = detail.casefold()
    return any(
        token in lowered
        for token in (
            "could not find service",
            "service cannot load in requested session",
            "no such process",
            "not found",
            "unknown service",
        )
    )


def _wait_for_process_exit(pid: int, timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _iter_processes() -> list[tuple[int, str]]:
    command = [shutil.which("ps") or "ps", "-axo", "pid=,command="]
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        return []

    processes: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        processes.append((int(parts[0]), parts[1]))
    return processes
