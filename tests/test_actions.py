from __future__ import annotations

import unittest

from src.actions import ActionController, recipe_catalog, recipe_ids_for_process


class ActionRulesTests(unittest.TestCase):
    def test_catalog_contains_expected_presets(self) -> None:
        catalog = recipe_catalog("Darwin")
        self.assertIn("pause-icloud-sync", catalog)
        self.assertIn("disable-system-update-checks", catalog)
        self.assertEqual(list(catalog), [
            "pause-icloud-sync",
            "pause-app-store-downloads",
            "disable-system-update-checks",
            "enable-system-update-checks",
        ])

    def test_linux_catalog_is_empty(self) -> None:
        self.assertEqual(recipe_catalog("Linux"), {})

    def test_recipe_ids_match_background_sync_processes(self) -> None:
        recipes = recipe_ids_for_process(
            "bird",
            "/System/Library/PrivateFrameworks/CloudDocsDaemon.framework/bird",
            "Darwin",
        )
        self.assertEqual(recipes, ["pause-icloud-sync"])

    def test_recipe_ids_match_store_agents(self) -> None:
        recipes = recipe_ids_for_process(
            "storedownloadd",
            "/System/Library/PrivateFrameworks/CommerceKit.framework",
            "Darwin",
        )
        self.assertEqual(recipes, ["disable-system-update-checks", "pause-app-store-downloads"])

    def test_linux_action_controller_hides_platform_specific_presets(self) -> None:
        controller = ActionController(system_name="Linux")
        self.assertEqual(controller.list_recipes(), [])
