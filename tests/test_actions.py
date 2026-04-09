from __future__ import annotations

import subprocess
import unittest

from unittest.mock import patch

from src.actions import ActionController, recipe_catalog, recipe_ids_for_process


class ActionRulesTests(unittest.TestCase):
    def test_catalog_contains_expected_presets(self) -> None:
        catalog = recipe_catalog("Darwin")
        self.assertIn("toggle-icloud-sync", catalog)
        self.assertIn("toggle-system-update-checks", catalog)
        self.assertEqual(list(catalog), [
            "toggle-icloud-sync",
            "toggle-app-store-downloads",
            "toggle-system-update-checks",
        ])

    def test_linux_catalog_is_empty(self) -> None:
        self.assertEqual(recipe_catalog("Linux"), {})

    def test_recipe_ids_match_background_sync_processes(self) -> None:
        recipes = recipe_ids_for_process(
            "bird",
            "/System/Library/PrivateFrameworks/CloudDocsDaemon.framework/bird",
            "Darwin",
        )
        self.assertEqual(recipes, ["toggle-icloud-sync"])

    def test_recipe_ids_match_store_agents(self) -> None:
        recipes = recipe_ids_for_process(
            "storedownloadd",
            "/System/Library/PrivateFrameworks/CommerceKit.framework",
            "Darwin",
        )
        self.assertEqual(recipes, ["toggle-app-store-downloads", "toggle-system-update-checks"])

    def test_linux_action_controller_hides_platform_specific_presets(self) -> None:
        controller = ActionController(system_name="Linux")
        self.assertEqual(controller.list_recipes(), [])

    def test_recipe_states_reads_toggle_status(self) -> None:
        controller = ActionController(system_name="Darwin")
        with patch.object(controller, "recipe_state", side_effect=[True, False, True]):
            self.assertEqual(
                controller.recipe_states(),
                {
                    "toggle-icloud-sync": True,
                    "toggle-app-store-downloads": False,
                    "toggle-system-update-checks": True,
                },
            )

    def test_execute_process_action_marks_confirmed_stop(self) -> None:
        controller = ActionController(system_name="Darwin")
        with (
            patch("src.actions.os.kill") as mock_kill,
            patch("src.actions._wait_for_process_exit", return_value=True),
        ):
            result = controller.execute_process_action(123, "terminate")
        mock_kill.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.title, "Stopped")
        self.assertIn("exited after SIGTERM", result.detail)

    def test_execute_process_action_leaves_unconfirmed_stop_as_request(self) -> None:
        controller = ActionController(system_name="Darwin")
        with (
            patch("src.actions.os.kill") as mock_kill,
            patch("src.actions._wait_for_process_exit", return_value=False),
        ):
            result = controller.execute_process_action(123, "terminate")
        mock_kill.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.title, "Stop requested")
        self.assertIn("still running", result.detail)

    def test_execute_recipe_turning_on_launchd_blocker_disables_and_boots_out_services(self) -> None:
        controller = ActionController(system_name="Darwin")
        commands_run: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands_run.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(controller, "recipe_state", return_value=False),
            patch("src.actions.os.getuid", return_value=501),
            patch.object(controller, "_command_path", side_effect=lambda command: f"/usr/bin/{command}"),
            patch("src.actions.subprocess.run", side_effect=fake_run),
            patch.object(controller, "_kill_named_processes", return_value=type("Result", (), {"ok": True, "detail": "Stopped 0 process(es)."})()),
        ):
            result = controller.execute_recipe("toggle-icloud-sync")

        self.assertTrue(result.ok)
        self.assertEqual(
            commands_run,
            [
                ["/usr/bin/launchctl", "disable", "gui/501/com.apple.bird"],
                ["/usr/bin/launchctl", "bootout", "gui/501/com.apple.bird"],
                ["/usr/bin/launchctl", "disable", "gui/501/com.apple.cloudd"],
                ["/usr/bin/launchctl", "bootout", "gui/501/com.apple.cloudd"],
            ],
        )

    def test_execute_recipe_admin_required_uses_sudo(self) -> None:
        controller = ActionController(system_name="Darwin")
        commands_run: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands_run.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(controller, "recipe_state", return_value=False),
            patch("src.actions.os.geteuid", return_value=501),
            patch.object(controller, "_command_path", side_effect=lambda command: f"/usr/bin/{command}"),
            patch("src.actions.subprocess.run", side_effect=fake_run),
            patch.object(controller, "_kill_named_processes", return_value=type("Result", (), {"ok": True, "detail": "Stopped 0 process(es)."})()),
        ):
            result = controller.execute_recipe("toggle-system-update-checks")

        self.assertTrue(result.ok)
        self.assertEqual(
            commands_run,
            [["/usr/bin/sudo", "-n", "/usr/bin/softwareupdate", "--schedule", "off"]],
        )

    def test_recipe_state_admin_required_uses_sudo(self) -> None:
        controller = ActionController(system_name="Darwin")
        with (
            patch("src.actions.os.geteuid", return_value=501),
            patch.object(controller, "_command_path", side_effect=lambda command: f"/usr/bin/{command}"),
            patch("src.actions._softwareupdate_schedule_disabled", return_value=True) as schedule_disabled,
        ):
            self.assertTrue(controller.recipe_state("toggle-system-update-checks"))

        schedule_disabled.assert_called_once_with(
            "/usr/bin/softwareupdate",
            sudo_command="/usr/bin/sudo",
            use_sudo=True,
        )
