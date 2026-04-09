from __future__ import annotations

import unittest
from unittest.mock import patch

from src.actions import ActionController, recipe_catalog
from src.collector import BandwidthCollector
from src.models import ProcessUsage, Snapshot
from src.tui import (
    commands_line_text,
    detail_block_height,
    total_rate_text,
    process_identity,
    status_text,
    TuiApp,
    format_bytes,
    header_row_text,
    process_row_text,
    recipe_shortcuts,
    selected_summary_text,
    table_layout,
    truncate,
    wrapped_lines,
)


class TuiHelpersTests(unittest.TestCase):
    def _process(self, pid: int, name: str, *, command: str | None = None) -> ProcessUsage:
        return ProcessUsage(
            pid=pid,
            name=name,
            display_name=name,
            command=command or f"/usr/bin/{name}",
            executable=command or f"/usr/bin/{name}",
            bundle_name=None,
            ports=[],
            download_bytes=0,
            upload_bytes=0,
            total_bytes=0,
            instant_download_rate_bps=0,
            instant_upload_rate_bps=0,
            instant_total_rate_bps=0,
            download_rate_bps=0,
            upload_rate_bps=0,
            total_rate_bps=0,
            is_background=False,
        )

    def test_recipe_shortcuts_are_stable_letters(self) -> None:
        shortcuts = recipe_shortcuts(list(recipe_catalog("Darwin").values()))
        self.assertIn("a", shortcuts)
        self.assertIn("b", shortcuts)
        self.assertEqual(shortcuts["a"].recipe_id, "toggle-icloud-sync")

    def test_commands_line_includes_common_controls_without_presets(self) -> None:
        self.assertEqual(
            commands_line_text({}, {}, True),
            "Commands: q quit | h hide<1KB [on] | t stop | x kill",
        )

    def test_commands_line_includes_toggle_states_for_recipes(self) -> None:
        recipes = list(recipe_catalog("Darwin").values())
        shortcuts = recipe_shortcuts(recipes)
        self.assertEqual(
            commands_line_text(
                shortcuts,
                {
                    "toggle-icloud-sync": True,
                    "toggle-app-store-downloads": False,
                    "toggle-system-update-checks": True,
                },
                True,
            ),
            "Commands: q quit | h hide<1KB [on] | t stop | x kill | a iCloud blocker [on] | b App Store blocker [off] | c Update check blocker [on]",
        )

    def test_format_bytes_uses_compact_units(self) -> None:
        self.assertEqual(format_bytes(512), "512B")
        self.assertEqual(format_bytes(2048), "2.0K")

    def test_truncate_uses_ellipsis(self) -> None:
        self.assertEqual(truncate("abcdef", 4), "abc…")

    def test_wrapped_lines_breaks_long_notice_text(self) -> None:
        self.assertEqual(wrapped_lines("abcdef ghij", 6), ["abcdef", "ghij"])

    def test_table_rows_align_with_header_for_wide_pid_and_ports(self) -> None:
        process = ProcessUsage(
            pid=2916472,
            name="tailscaled",
            display_name="tailscaled",
            command="/usr/sbin/tailscaled",
            executable="/usr/sbin/tailscaled",
            bundle_name=None,
            ports=["48950->443/tcp", "41641->3478/udp"],
            download_bytes=567,
            upload_bytes=1843,
            total_bytes=2410,
            instant_download_rate_bps=20,
            instant_upload_rate_bps=20,
            instant_total_rate_bps=40,
            download_rate_bps=20,
            upload_rate_bps=20,
            total_rate_bps=40,
            is_background=False,
        )
        layout = table_layout([process], 128)
        header = header_row_text(layout)
        row = process_row_text(1, process, layout)

        self.assertEqual(len(header), len(row))
        self.assertEqual(row.index("2916472") + len("2916472"), header.index("PID") + len("PID"))
        self.assertEqual(row.index("tailscaled"), header.index("Process"))
        self.assertEqual(row.index("48950->443/tcp"), header.index("Ports"))
        self.assertEqual(row.index("567B") + len("567B"), header.index("Down") + len("Down"))
        self.assertEqual(row.index("1.8K") + len("1.8K"), header.index("Up") + len("Up"))
        self.assertEqual(row.index("2.4K") + len("2.4K"), header.index("Total") + len("Total"))
        self.assertEqual(row.index("40B") + len("40B"), header.index("Instant Rate") + len("Instant Rate"))
        self.assertEqual(row.rindex("40B") + len("40B"), header.index("60s Avg Rate") + len("60s Avg Rate"))

    def test_detail_block_height_grows_for_wrapped_selected_summary(self) -> None:
        process = self._process(
            2916472,
            "curl",
            command="/usr/bin/curl https://releases.ubuntu.com/noble/ubuntu-24.04.4-live-server-amd64.iso -o /dev/null",
        )
        process.ports = ["48950->443/tcp"]
        process.download_bytes = 1024
        process.upload_bytes = 128
        process.total_bytes = 1152
        process.instant_download_rate_bps = 512
        process.instant_upload_rate_bps = 64
        process.instant_total_rate_bps = 576
        process.download_rate_bps = 512
        process.upload_rate_bps = 64
        process.total_rate_bps = 576

        summary_lines = wrapped_lines(selected_summary_text(process), 32)
        status_lines = wrapped_lines(status_text("Updated 12:00:00", [process]), 32)
        self.assertGreater(len(summary_lines), 1)
        self.assertEqual(detail_block_height(process, 32, "Updated 12:00:00", [process]), len(summary_lines) + 1 + len(status_lines))

    def test_process_identity_uses_pid_command_and_name(self) -> None:
        process = self._process(42, "curl")
        self.assertEqual(process_identity(process), (42, "/usr/bin/curl", "curl"))

    def test_total_rate_text_summarizes_visible_rates(self) -> None:
        first = self._process(100, "first")
        first.instant_upload_rate_bps = 100
        first.instant_download_rate_bps = 23
        second = self._process(200, "second")
        second.instant_upload_rate_bps = 924
        second.instant_download_rate_bps = 1001
        self.assertEqual(total_rate_text([first, second]), "Total Rate: 2.0K (1.0K Up / 1.0K Down)")

    def test_status_text_combines_message_and_total_rate(self) -> None:
        process = self._process(100, "first")
        process.instant_upload_rate_bps = 1024
        process.instant_download_rate_bps = 512
        self.assertEqual(
            status_text("Enabled iCloud blocker", [process]),
            "Status: Enabled iCloud blocker | Total Rate: 1.5K (1.0K Up / 512B Down)",
        )

    def test_process_row_text_can_show_killed_prefix(self) -> None:
        process = self._process(42, "curl")
        layout = table_layout([process], 96)
        row = process_row_text(0, process, layout, display_name="[killed] curl")
        self.assertIn("[killed] curl", row)

    def test_display_name_can_show_stopped_prefix(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        process = self._process(42, "curl")
        app._stopped_processes.add(process_identity(process))
        self.assertEqual(app._display_name(process), "[stopped] curl")

    def test_toggle_hide_small_processes_hides_rows_below_1kb(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        small = self._process(100, "small")
        small.total_bytes = 512
        big = self._process(200, "big")
        big.total_bytes = 2048
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[small, big],
            notices=[],
        )
        app.selected_index = 1

        app.hide_small_processes = False
        app._toggle_hide_small_processes()

        self.assertTrue(app.hide_small_processes)
        self.assertEqual([process.pid for process in app._visible_processes()], [200])
        self.assertEqual(app.selected_index, 0)
        self.assertEqual(app._selected_process().pid, 200)

    def test_toggle_hide_small_processes_clears_selection_if_current_row_is_hidden(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        small = self._process(100, "small")
        small.total_bytes = 512
        big = self._process(200, "big")
        big.total_bytes = 2048
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[small, big],
            notices=[],
        )
        app.selected_index = 0

        app.hide_small_processes = False
        app._toggle_hide_small_processes()

        self.assertIsNone(app.selected_index)
        self.assertEqual(app.status_message, "Small-process filter enabled. Current selection is hidden.")

    def test_apply_snapshot_preserves_selected_process_across_reorder(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        app.hide_small_processes = False
        first = self._process(100, "first")
        second = self._process(200, "second")
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[first, second],
            notices=[],
        )
        app.selected_index = 1
        app._apply_snapshot(
            Snapshot(
                supported=True,
                platform="Linux",
                collector="nethogs",
                sample_seconds=2,
                averaging_window_seconds=60,
                processes=[second, first],
                notices=[],
            )
        )
        self.assertEqual(app.selected_index, 0)
        self.assertIsNotNone(app.snapshot)
        self.assertEqual(app._selected_process().pid, 200)
        self.assertFalse(app.status_message.endswith("."))

    def test_apply_snapshot_clears_selection_if_selected_process_disappears(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        app.hide_small_processes = False
        first = self._process(100, "first")
        second = self._process(200, "second")
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[first, second],
            notices=[],
        )
        app.selected_index = 1
        app._apply_snapshot(
            Snapshot(
                supported=True,
                platform="Linux",
                collector="nethogs",
                sample_seconds=2,
                averaging_window_seconds=60,
                processes=[first],
                notices=[],
            )
        )
        self.assertIsNone(app.selected_index)
        self.assertEqual(app.status_message, "Selected process disappeared on refresh.")

    def test_request_snapshot_refresh_sets_event_and_status(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        app._request_snapshot_refresh("Refreshing snapshot…")
        self.assertEqual(app.status_message, "Refreshing snapshot…")
        self.assertTrue(app._refresh_requested.is_set())

    def test_apply_snapshot_preserves_recent_action_status_until_hold_expires(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        process = self._process(100, "first")
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[process],
            notices=[],
        )
        app.status_message = "Enabled iCloud blocker"
        app._status_hold_until = 999.0

        with patch("src.tui.time.monotonic", return_value=100.0):
            app._apply_snapshot(
                Snapshot(
                    supported=True,
                    platform="Linux",
                    collector="nethogs",
                    sample_seconds=2,
                    averaging_window_seconds=60,
                    processes=[process],
                    notices=[],
                )
            )
        self.assertEqual(app.status_message, "Enabled iCloud blocker")

    def test_apply_snapshot_updates_status_after_hold_expires(self) -> None:
        app = TuiApp(collector=BandwidthCollector(), actions=ActionController(system_name="Linux"))
        process = self._process(100, "first")
        app.snapshot = Snapshot(
            supported=True,
            platform="Linux",
            collector="nethogs",
            sample_seconds=2,
            averaging_window_seconds=60,
            processes=[process],
            notices=[],
        )
        app.status_message = "Enabled iCloud blocker"
        app._status_hold_until = 100.0

        with (
            patch("src.tui.time.monotonic", return_value=101.0),
            patch("src.tui.time.strftime", return_value="12:00:00"),
        ):
            app._apply_snapshot(
                Snapshot(
                    supported=True,
                    platform="Linux",
                    collector="nethogs",
                    sample_seconds=2,
                    averaging_window_seconds=60,
                    processes=[process],
                    notices=[],
                )
            )
        self.assertEqual(app.status_message, "Updated 12:00:00")
