from __future__ import annotations

from collections.abc import Iterable
import os
import platform
import shlex
import shutil
import signal
import subprocess

from .models import ActionResult, Recipe


def recipe_catalog(system_name: str = "Darwin") -> dict[str, Recipe]:
    if system_name != "Darwin":
        return {}
    return {
        "pause-icloud-sync": Recipe(
            recipe_id="pause-icloud-sync",
            title="Pause iCloud sync",
            summary="Temporarily stops the main iCloud sync daemons.",
            instructions="Temporary. macOS may restart these later if iCloud features are still enabled.",
            command_preview="kill bird cloudd",
            admin_required=False,
            temporary=True,
            disruptive=True,
        ),
        "pause-app-store-downloads": Recipe(
            recipe_id="pause-app-store-downloads",
            title="Pause App Store downloads",
            summary="Stops the common App Store download and asset daemons.",
            instructions="Temporary. Use this when the App Store or background downloads are chewing through a hotspot.",
            command_preview="kill appstoreagent storeassetd storedownloadd",
            admin_required=False,
            temporary=True,
            disruptive=True,
        ),
        "disable-system-update-checks": Recipe(
            recipe_id="disable-system-update-checks",
            title="Disable system update checks",
            summary="Runs the system softwareupdate scheduler toggle.",
            instructions="Usually needs admin rights. Re-enable it after the flight so you do not forget about security updates.",
            command_preview="softwareupdate --schedule off",
            admin_required=True,
            temporary=False,
            disruptive=True,
        ),
        "enable-system-update-checks": Recipe(
            recipe_id="enable-system-update-checks",
            title="Re-enable system update checks",
            summary="Turns the automatic background update check back on.",
            instructions="Usually needs admin rights.",
            command_preview="softwareupdate --schedule on",
            admin_required=True,
            temporary=False,
            disruptive=False,
        ),
        "open-system-settings": Recipe(
            recipe_id="open-system-settings",
            title="Open System Settings",
            summary="Opens System Settings so you can disable sync and update features persistently.",
            instructions="Use this for settings that Apple does not expose as a stable command-line toggle.",
            command_preview="open -a 'System Settings'",
            admin_required=False,
            temporary=False,
            disruptive=False,
        ),
        "open-app-store": Recipe(
            recipe_id="open-app-store",
            title="Open App Store",
            summary="Open the App Store app so you can disable app auto-updates manually.",
            instructions="Useful when App Store background traffic keeps coming back.",
            command_preview="open -a 'App Store'",
            admin_required=False,
            temporary=False,
            disruptive=False,
        ),
    }


def recipe_ids_for_process(name: str, command: str | None, system_name: str = "Darwin") -> list[str]:
    if system_name != "Darwin":
        return []
    signature = " ".join(part for part in [name, command or ""]).casefold()
    recipe_ids: list[str] = []
    if any(token in signature for token in ("bird", "cloudd", "icloud")):
        recipe_ids.extend(["pause-icloud-sync", "open-system-settings"])
    if any(token in signature for token in ("softwareupdated", "storeassetd", "storedownloadd", "appstoreagent")):
        recipe_ids.extend(
            ["pause-app-store-downloads", "disable-system-update-checks", "open-system-settings", "open-app-store"]
        )
    if any(token in signature for token in ("dropbox", "onedrive", "google drive", "creative cloud", "steam")):
        recipe_ids.append("open-system-settings")
    return sorted(set(recipe_ids))


class ActionController:
    def __init__(self, system_name: str | None = None) -> None:
        self._system = system_name or platform.system()
        self._recipes = recipe_catalog(self._system)

    @property
    def supported(self) -> bool:
        return self._system == "Darwin"

    @property
    def platform_name(self) -> str:
        return self._system

    def list_recipes(self) -> list[Recipe]:
        return list(self._recipes.values())

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

        verb = "Force stopped" if signal_name == signal.SIGKILL else "Stopped"
        return ActionResult(ok=True, title=verb, detail=f"Sent {signal_name.name} to PID {pid}.")

    def execute_recipe(self, recipe_id: str) -> ActionResult:
        recipe = self._recipes.get(recipe_id)
        if recipe is None:
            return ActionResult(ok=False, title="Unknown preset", detail=f"No preset exists for {recipe_id}.")
        if not self.supported:
            return ActionResult(ok=False, title="Unsupported platform", detail="Presets only work on macOS.")

        if recipe_id == "pause-icloud-sync":
            return self._kill_named_processes(recipe, ["bird", "cloudd"])
        if recipe_id == "pause-app-store-downloads":
            return self._kill_named_processes(recipe, ["appstoreagent", "storeassetd", "storedownloadd"])
        if recipe_id == "disable-system-update-checks":
            return self._run_recipe_command(recipe, [self._command_path("softwareupdate"), "--schedule", "off"])
        if recipe_id == "enable-system-update-checks":
            return self._run_recipe_command(recipe, [self._command_path("softwareupdate"), "--schedule", "on"])
        if recipe_id == "open-system-settings":
            return self._run_recipe_command(recipe, [self._command_path("open"), "-a", "System Settings"])
        if recipe_id == "open-app-store":
            return self._run_recipe_command(recipe, [self._command_path("open"), "-a", "App Store"])

        return ActionResult(ok=False, title="Unknown preset", detail=f"No executor is wired for {recipe_id}.")

    def _kill_named_processes(self, recipe: Recipe, process_names: Iterable[str]) -> ActionResult:
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
                    return ActionResult(
                        ok=False,
                        title=recipe.title,
                        detail="macOS refused to stop one of the matching processes.",
                        command=recipe.command_preview,
                    )
                matched.append(f"{pid}:{command}")

        if not matched:
            return ActionResult(
                ok=True,
                title=recipe.title,
                detail="No matching processes were running.",
                command=recipe.command_preview,
            )

        return ActionResult(
            ok=True,
            title=recipe.title,
            detail=f"Stopped {len(matched)} process(es): {', '.join(matched)}",
            command=recipe.command_preview,
        )

    def _run_recipe_command(self, recipe: Recipe, command: list[str]) -> ActionResult:
        completed = subprocess.run(  # noqa: S603
            command,
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
            command=_format_command(command),
            stdout=completed.stdout.strip() or None,
            stderr=completed.stderr.strip() or None,
        )

    def _command_path(self, command: str) -> str:
        return shutil.which(command) or command

    def _signal_for_action(self, action: str) -> signal.Signals | None:
        if action == "terminate":
            return signal.SIGTERM
        if action == "kill":
            return signal.SIGKILL
        return None


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
