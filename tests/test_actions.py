from __future__ import annotations

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
