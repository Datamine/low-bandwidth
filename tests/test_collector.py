from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from src.collector import BandwidthCollector, parse_lsof_output, parse_nethogs_output, parse_nettop_output, parse_ss_output


class CollectorParsingTests(unittest.TestCase):
    def test_parse_csv_output(self) -> None:
        raw = "\n".join(
            [
                "process,bytes_in,bytes_out",
                "Google Chrome.284,156895,152847",
                "bird.91,1024,2048",
            ]
        )
        rows = parse_nettop_output(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].pid, 284)
        self.assertEqual(rows[0].name, "Google Chrome")
        self.assertEqual(rows[0].download_bytes, 156895)

    def test_parse_fallback_rows(self) -> None:
        raw = "\n".join(
            [
                "Google Chrome.284,156895,152847",
                "bird.91,1024,2048",
            ]
        )
        rows = parse_nettop_output(raw)
        self.assertEqual(rows[1].pid, 91)
        self.assertEqual(rows[1].upload_bytes, 2048)

    def test_parse_nethogs_trace_output(self) -> None:
        raw = "\n".join(
            [
                "NetHogs version 0.8.7",
                "Refreshing:",
                "unknown TCP/0/0 0.010 0.020",
                "Refreshing:",
                "curl/321/1000 0.250 1.500",
                "eth0 /usr/bin/ping/654/root 0.125 0.125",
                "TOTAL 0.375 1.625",
            ]
        )
        rows = parse_nethogs_output(raw, sample_seconds=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].pid, 321)
        self.assertEqual(rows[0].name, "curl")
        self.assertEqual(rows[0].download_bytes, 3072)
        self.assertEqual(rows[1].pid, 654)
        self.assertEqual(rows[1].upload_bytes, 256)

    def test_parse_nethogs_trace_output_keeps_human_readable_ssh_name(self) -> None:
        raw = "\n".join(
            [
                "Refreshing:",
                "sshd: v@pts/2/2862891/1000 0.182812 0.08125",
            ]
        )
        rows = parse_nethogs_output(raw, sample_seconds=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pid, 2862891)
        self.assertEqual(rows[0].name, "sshd")

    def test_parse_ss_output_tracks_ports_by_pid(self) -> None:
        raw = "\n".join(
            [
                'tcp ESTAB 0 0 192.168.0.22:58124 91.189.91.81:443 users:(("curl",pid=321,fd=3))',
                'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolved",pid=88,fd=14))',
            ]
        )
        port_map = parse_ss_output(raw)
        self.assertEqual(port_map[321], ["58124->443/tcp"])
        self.assertEqual(port_map[88], ["53/udp"])

    def test_parse_lsof_output_tracks_ports_by_pid(self) -> None:
        raw = "\n".join(
            [
                "p321",
                "Ptcp",
                "n192.168.0.22:58124->91.189.91.81:443",
                "p88",
                "Pudp",
                "n*:53",
            ]
        )
        port_map = parse_lsof_output(raw)
        self.assertEqual(port_map[321], ["58124->443/tcp"])
        self.assertEqual(port_map[88], ["53/udp"])

    def test_macos_snapshot_uses_lsof_ports(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        nettop_output = "\n".join(
            [
                "process,bytes_in,bytes_out",
                "curl.321,2048,1024",
            ]
        )
        ps_output = "321 /usr/bin/curl /usr/bin/curl https://example.com"
        lsof_output = "\n".join(
            [
                "p321",
                "Ptcp",
                "n192.168.0.22:58124->91.189.91.81:443",
            ]
        )
        with (
            patch("src.collector.platform.system", return_value="Darwin"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nettop"],
                        returncode=0,
                        stdout=nettop_output,
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["ps"],
                        returncode=0,
                        stdout=ps_output,
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["lsof"],
                        returncode=0,
                        stdout=lsof_output,
                        stderr="",
                    ),
                ],
            ),
        ):
            snapshot = collector.snapshot()
        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.collector, "nettop")
        self.assertEqual(snapshot.processes[0].pid, 321)
        self.assertEqual(snapshot.processes[0].ports, ["58124->443/tcp"])

    def test_macos_snapshot_uses_deltas_not_cumulative_totals(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        first_nettop_output = "\n".join(
            [
                "process,bytes_in,bytes_out",
                "mDNSResponder.449,1000000,500000",
            ]
        )
        second_nettop_output = "\n".join(
            [
                "process,bytes_in,bytes_out",
                "mDNSResponder.449,1002048,501024",
            ]
        )
        ps_output = "449 /usr/sbin/mDNSResponder /usr/sbin/mDNSResponder"
        with (
            patch("src.collector.platform.system", return_value="Darwin"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("src.collector.time.time", side_effect=[100.0, 102.0]),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(args=["nettop"], returncode=0, stdout=first_nettop_output, stderr=""),
                    subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr=""),
                    subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="", stderr=""),
                    subprocess.CompletedProcess(args=["nettop"], returncode=0, stdout=second_nettop_output, stderr=""),
                    subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr=""),
                    subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="", stderr=""),
                ],
            ),
        ):
            first = collector.snapshot()
            second = collector.snapshot()
        self.assertEqual(first.processes[0].total_bytes, 0)
        self.assertEqual(second.processes[0].download_bytes, 2048)
        self.assertEqual(second.processes[0].upload_bytes, 1024)
        self.assertEqual(second.processes[0].total_bytes, 3072)

    def test_linux_snapshot_requires_nethogs(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: None if name == "nethogs" else name),
        ):
            snapshot = collector.snapshot()
        self.assertFalse(snapshot.supported)
        self.assertEqual(snapshot.collector, "nethogs")
        self.assertIn("nethogs", snapshot.notices[0])

    def test_linux_snapshot_uses_nethogs_output(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        nethogs_output = "\n".join(
            [
                "Refreshing:",
                "/usr/bin/curl/321/root 0.250 1.500",
                "/usr/bin/python3/111/root 0.500 0.125",
            ]
        )
        ps_output = "\n".join(
            [
                "321 /usr/bin/curl /usr/bin/curl https://example.com",
                "111 /usr/bin/python3 /usr/bin/python3 -m http.server",
            ]
        )
        ss_output = 'tcp ESTAB 0 0 192.168.0.22:58124 91.189.91.81:443 users:(("curl",pid=321,fd=3))'
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nethogs"],
                        returncode=0,
                        stdout="",
                        stderr=nethogs_output,
                    ),
                    subprocess.CompletedProcess(
                        args=["ps"],
                        returncode=0,
                        stdout=ps_output,
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["ss"],
                        returncode=0,
                        stdout=ss_output,
                        stderr="",
                    ),
                ],
            ) as mock_run,
        ):
            snapshot = collector.snapshot()
        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.collector, "nethogs")
        self.assertEqual(snapshot.processes[0].pid, 321)
        self.assertEqual(snapshot.processes[0].display_name, "curl")
        self.assertEqual(snapshot.processes[0].command, "/usr/bin/curl https://example.com")
        self.assertEqual(snapshot.processes[0].ports, ["58124->443/tcp"])
        self.assertEqual(snapshot.processes[0].download_bytes, 3072)
        self.assertEqual(snapshot.processes[0].total_rate_bps, 1792.0)
        self.assertEqual(mock_run.call_args_list[0].args[0], ["/usr/bin/nethogs", "-t", "-d", "2", "-c", "2"])
        self.assertEqual(collector.debug_payload()["debug"]["parsed_rows"], 2)

    def test_linux_snapshot_retries_with_sudo_after_permission_error(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        nethogs_output = "\n".join(
            [
                "Refreshing:",
                "curl/321/1000 0.250 1.500",
            ]
        )
        ps_output = "321 /usr/bin/curl /usr/bin/curl https://example.com"
        ss_output = 'tcp ESTAB 0 0 192.168.0.22:58124 91.189.91.81:443 users:(("curl",pid=321,fd=3))'
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nethogs"],
                        returncode=1,
                        stdout="",
                        stderr="Error: you must be root to run NetHogs!",
                    ),
                    subprocess.CompletedProcess(
                        args=["sudo", "-n", "nethogs"],
                        returncode=0,
                        stdout="",
                        stderr=nethogs_output,
                    ),
                    subprocess.CompletedProcess(
                        args=["ps"],
                        returncode=0,
                        stdout=ps_output,
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["ss"],
                        returncode=0,
                        stdout=ss_output,
                        stderr="",
                    ),
                ],
            ),
        ):
            snapshot = collector.snapshot()
        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.processes[0].name, "curl")
        self.assertEqual(snapshot.processes[0].ports, ["58124->443/tcp"])
        self.assertIn("sudo -n", " ".join(snapshot.notices))

    def test_linux_snapshot_reports_failed_sudo_retry(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nethogs"],
                        returncode=1,
                        stdout="",
                        stderr="Error: you must be root to run NetHogs!",
                    ),
                    subprocess.CompletedProcess(
                        args=["sudo", "-n", "nethogs"],
                        returncode=1,
                        stdout="",
                        stderr="sudo: a password is required",
                    ),
                ],
            ),
        ):
            snapshot = collector.snapshot()
        self.assertFalse(snapshot.supported)
        self.assertIn("sudo -n nethogs", " ".join(snapshot.notices))

    def test_linux_snapshot_tolerates_missing_ps_metadata(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        nethogs_output = "\n".join(
            [
                "Refreshing:",
                "curl/321/1000 0.250 1.500",
            ]
        )
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nethogs"],
                        returncode=0,
                        stdout="",
                        stderr=nethogs_output,
                    ),
                    subprocess.CompletedProcess(
                        args=["ps"],
                        returncode=0,
                        stdout="",
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["ss"],
                        returncode=0,
                        stdout='tcp ESTAB 0 0 192.168.0.22:58124 91.189.91.81:443 users:(("curl",pid=321,fd=3))',
                        stderr="",
                    ),
                ],
            ),
        ):
            snapshot = collector.snapshot()
        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.processes[0].pid, 321)
        self.assertEqual(snapshot.processes[0].display_name, "curl")
        self.assertIsNone(snapshot.processes[0].command)
        self.assertEqual(snapshot.processes[0].ports, ["58124->443/tcp"])

    def test_linux_snapshot_prefers_ps_name_for_human_readable_display(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        nethogs_output = "\n".join(
            [
                "Refreshing:",
                "sshd: v@pts/2/2862891/1000 0.182812 0.08125",
            ]
        )
        ps_output = "2862891 sshd: sshd: v@pts/2"
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(
                        args=["nethogs"],
                        returncode=0,
                        stdout="",
                        stderr=nethogs_output,
                    ),
                    subprocess.CompletedProcess(
                        args=["ps"],
                        returncode=0,
                        stdout=ps_output,
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=["ss"],
                        returncode=0,
                        stdout="",
                        stderr="",
                    ),
                ],
            ),
        ):
            snapshot = collector.snapshot()
        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.processes[0].name, "sshd")
        self.assertEqual(snapshot.processes[0].display_name, "sshd")
        self.assertEqual(snapshot.processes[0].command, "sshd: v@pts/2")

    def test_rolling_average_keeps_recent_process_visible(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        active_output = "\n".join(
            [
                "Refreshing:",
                "curl/321/1000 0.250 1.500",
            ]
        )
        idle_output = "Refreshing:"
        ps_output = "321 /usr/bin/curl /usr/bin/curl https://example.com"
        ss_output = 'tcp ESTAB 0 0 192.168.0.22:58124 91.189.91.81:443 users:(("curl",pid=321,fd=3))'
        with (
            patch("src.collector.platform.system", return_value="Linux"),
            patch("src.collector.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("src.collector.time.time", side_effect=[100.0, 130.0, 170.0]),
            patch(
                "src.collector.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(args=["nethogs"], returncode=0, stdout="", stderr=active_output),
                    subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr=""),
                    subprocess.CompletedProcess(args=["ss"], returncode=0, stdout=ss_output, stderr=""),
                    subprocess.CompletedProcess(args=["nethogs"], returncode=0, stdout="", stderr=idle_output),
                    subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr=""),
                    subprocess.CompletedProcess(args=["ss"], returncode=0, stdout=ss_output, stderr=""),
                    subprocess.CompletedProcess(args=["nethogs"], returncode=0, stdout="", stderr=idle_output),
                    subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr=""),
                    subprocess.CompletedProcess(args=["ss"], returncode=0, stdout=ss_output, stderr=""),
                ],
            ),
        ):
            first = collector.snapshot()
            second = collector.snapshot()
            third = collector.snapshot()
        self.assertEqual(first.averaging_window_seconds, 60)
        self.assertEqual(first.processes[0].total_bytes, 3584)
        self.assertEqual(second.processes[0].pid, 321)
        self.assertEqual(second.processes[0].total_bytes, 3584)
        self.assertAlmostEqual(second.processes[0].total_rate_bps, 112.0)
        self.assertEqual(third.processes, [])

    def test_other_platform_snapshot_returns_notice(self) -> None:
        collector = BandwidthCollector(sample_seconds=2)
        with patch("src.collector.platform.system", return_value="Windows"):
            snapshot = collector.snapshot()
        self.assertFalse(snapshot.supported)
        self.assertTrue(snapshot.notices)
