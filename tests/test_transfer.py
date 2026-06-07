import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macsetup.cli import build_parser
from macsetup.transfer import (
    DEFAULT_RSYNC_EXCLUDES,
    ArchiveKind,
    ExcludeInputs,
    TransferMethod,
    build_ditto_create_command,
    build_extract_command,
    build_find_manifest_command,
    build_rsync_from_command,
    build_rsync_remote_shell,
    build_tar_create_command,
    build_tar_create_pipeline,
    choose_auto_method,
    collect_excludes,
    find_prune_names,
    is_lan_ipv4,
    netmask_to_prefix,
    parse_rsync_filter_excludes,
    rsync_daemon_url,
    rsync_from_host_source_dest,
    rsync_remote_source,
    tar_exclude_patterns,
    write_rsyncd_files,
)


class TransferNetworkTests(unittest.TestCase):
    def test_lan_ipv4_allows_private_and_link_local_ranges(self) -> None:
        self.assertTrue(is_lan_ipv4("10.0.0.1"))
        self.assertTrue(is_lan_ipv4("172.16.0.1"))
        self.assertTrue(is_lan_ipv4("172.31.255.254"))
        self.assertTrue(is_lan_ipv4("192.168.1.20"))
        self.assertTrue(is_lan_ipv4("169.254.10.20"))

    def test_lan_ipv4_rejects_public_loopback_and_unspecified_ranges(self) -> None:
        self.assertFalse(is_lan_ipv4("8.8.8.8"))
        self.assertFalse(is_lan_ipv4("172.32.0.1"))
        self.assertFalse(is_lan_ipv4("127.0.0.1"))
        self.assertFalse(is_lan_ipv4("0.0.0.0"))
        self.assertFalse(is_lan_ipv4("not-an-ip"))


class TransferCommandTests(unittest.TestCase):
    def test_main_cli_has_sync_branch(self) -> None:
        args = build_parser().parse_args(
            ["sync", "ditto-recv", "--bind", "192.168.1.20"]
        )
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_command, "ditto-recv")
        self.assertEqual(args.bind, "192.168.1.20")

    def test_main_cli_has_auto_sync_branch(self) -> None:
        args = build_parser().parse_args(
            ["sync", "auto-sync", "--host", "192.168.1.20", "--source", "/tmp/source"]
        )
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_command, "auto-sync")
        self.assertEqual(args.host, "192.168.1.20")

    def test_main_cli_has_rsync_daemon_server_branch(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "rsync-daemon-server",
                "--bind",
                "192.168.1.20",
                "--dest",
                "/tmp/dest",
            ]
        )
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_command, "rsync-daemon-server")
        self.assertEqual(args.bind, "192.168.1.20")

    def test_main_cli_has_rsync_from_branch(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "rsync-from",
                "--host",
                "192.168.1.10",
                "--user",
                "matt",
                "--source",
                "~/repos/",
                "--dest",
                "/Users/matt/repos/",
                "--ssh-port",
                "2222",
                "--ssh-option",
                "BatchMode=yes",
            ]
        )
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_command, "rsync-from")
        self.assertEqual(args.host, "192.168.1.10")
        self.assertEqual(args.user, "matt")
        self.assertEqual(args.source, "~/repos/")
        self.assertEqual(args.dest, "/Users/matt/repos/")
        self.assertEqual(args.ssh_port, 2222)
        self.assertEqual(args.ssh_option, ["BatchMode=yes"])

    def test_receiver_servers_are_persistent_by_default_with_once_escape_hatch(
        self,
    ) -> None:
        persistent = build_parser().parse_args(
            ["sync", "auto-server", "--bind", "192.168.1.20"]
        )
        one_shot = build_parser().parse_args(
            ["sync", "auto-server", "--bind", "192.168.1.20", "--once"]
        )
        tar_one_shot = build_parser().parse_args(
            ["sync", "tar-recv", "--bind", "192.168.1.20", "--once"]
        )
        self.assertFalse(persistent.once)
        self.assertTrue(one_shot.once)
        self.assertTrue(tar_one_shot.once)

    def test_transfer_wrappers_use_uv_without_pythonpath(self) -> None:
        for script in Path("scripts").glob("*.sh"):
            body = script.read_text(encoding="utf-8")
            self.assertIn(
                'uv --directory "$repo_root" run macsetup-transfer', body, script.name
            )
            self.assertNotIn("PYTHONPATH", body, script.name)
            self.assertNotIn("python3 -m", body, script.name)

    def test_auto_method_uses_ditto_without_excludes(self) -> None:
        decision = choose_auto_method(
            requested=TransferMethod.AUTO,
            has_rsync_dest=False,
            excludes=ExcludeInputs((), ()),
            ditto_port=17000,
            tar_port=17001,
        )
        self.assertIs(decision.method, TransferMethod.DITTO)
        self.assertEqual(decision.port, 17000)

    def test_auto_method_uses_tar_with_excludes(self) -> None:
        decision = choose_auto_method(
            requested=TransferMethod.AUTO,
            has_rsync_dest=False,
            excludes=ExcludeInputs(("large-cache/",), ()),
            ditto_port=17000,
            tar_port=17001,
        )
        self.assertIs(decision.method, TransferMethod.TAR)
        self.assertEqual(decision.port, 17001)
        self.assertEqual(decision.excludes.patterns, ("large-cache/",))

    def test_auto_sync_collects_managed_defaults_for_initial_archives(self) -> None:
        args = build_parser().parse_args(
            ["sync", "auto-sync", "--host", "192.168.1.20", "--source", "/tmp/source"]
        )
        excludes = collect_excludes(args)
        self.assertIn("node_modules/", excludes.patterns)
        self.assertIn(".hypothesis/", excludes.patterns)
        self.assertIn(".tmp/", excludes.patterns)

    def test_auto_sync_can_disable_managed_defaults_for_ditto(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "auto-sync",
                "--host",
                "192.168.1.20",
                "--source",
                "/tmp/source",
                "--no-default-excludes",
            ]
        )
        excludes = collect_excludes(args)
        self.assertFalse(excludes.present)

    def test_auto_method_uses_rsync_with_rsync_dest(self) -> None:
        decision = choose_auto_method(
            requested=TransferMethod.AUTO,
            has_rsync_dest=True,
            excludes=ExcludeInputs(("large-cache/",), ()),
            ditto_port=17000,
            tar_port=17001,
        )
        self.assertIs(decision.method, TransferMethod.RSYNC)
        self.assertIsNone(decision.port)

    def test_auto_method_supports_rsync_daemon_override(self) -> None:
        decision = choose_auto_method(
            requested=TransferMethod.RSYNC_DAEMON,
            has_rsync_dest=False,
            excludes=ExcludeInputs(("large-cache/",), ()),
            ditto_port=17000,
            tar_port=17001,
        )
        self.assertIs(decision.method, TransferMethod.RSYNC_DAEMON)
        self.assertIsNone(decision.port)

    def test_ditto_override_rejects_excludes(self) -> None:
        with self.assertRaises(SystemExit):
            choose_auto_method(
                requested=TransferMethod.DITTO,
                has_rsync_dest=False,
                excludes=ExcludeInputs(("large-cache/",), ()),
                ditto_port=17000,
                tar_port=17001,
            )

    def test_ditto_create_uses_keep_parent_by_default(self) -> None:
        self.assertEqual(
            build_ditto_create_command(
                Path("/tmp/source-tree"), keep_parent=True, sudo=False
            ),
            ["ditto", "-c", "--keepParent", "/tmp/source-tree", "-"],
        )

    def test_tar_create_supports_exclude_files(self) -> None:
        command = build_tar_create_command(
            Path("/tmp/source-tree"),
            keep_parent=True,
            excludes=("large-cache/",),
            exclude_from=("local/rsync-excludes.txt",),
            sudo=False,
        )
        self.assertIn("--exclude=large-cache/", command)
        self.assertIn("--exclude-from=local/rsync-excludes.txt", command)
        self.assertEqual(command[-1], "source-tree")

    def test_tar_commands_can_skip_mac_metadata_flags(self) -> None:
        create = build_tar_create_command(
            Path("/tmp/source-tree"),
            keep_parent=True,
            excludes=(),
            exclude_from=(),
            sudo=False,
            mac_metadata=False,
        )
        extract = build_extract_command(
            ArchiveKind.TAR, Path("/tmp/dest"), sudo=False, mac_metadata=False
        )
        self.assertNotIn("--mac-metadata", create)
        self.assertNotIn("--xattrs", create)
        self.assertNotIn("--mac-metadata", extract)
        self.assertNotIn("--xattrs", extract)

    def test_tar_pipeline_uses_find_manifest_to_skip_sockets(self) -> None:
        plan = build_tar_create_pipeline(
            Path("/tmp/source-tree"),
            keep_parent=True,
            excludes=("node_modules/", ".venv/"),
            exclude_from=(),
            sudo=False,
        )
        self.assertEqual(plan.cwd, Path("/tmp").resolve())
        self.assertEqual(plan.commands[0][0:2], ("find", "source-tree"))
        self.assertIn("-type", plan.commands[0])
        self.assertIn("s", plan.commands[0])
        self.assertIn("--null", plan.commands[1])
        self.assertIn("--no-recursion", plan.commands[1])
        self.assertIn("-T", plan.commands[1])

    def test_find_manifest_prunes_leaf_directory_excludes(self) -> None:
        self.assertEqual(
            find_prune_names(
                ("node_modules/", ".venv/", "path/specific/", ".DS_Store")
            ),
            ("node_modules", ".venv"),
        )
        command = build_find_manifest_command(
            "source-tree", excludes=("node_modules/",), sudo=False
        )
        self.assertIn("-prune", command)

    def test_default_rsync_excludes_are_generic(self) -> None:
        expected = (
            ".venv/",
            "__pycache__/",
            "node_modules/",
            ".uv-cache/",
            ".ruff_cache/",
            ".hypothesis/",
            ".tmp/",
            "CMakeFiles/",
            "build/",
            "dist/",
            "target/",
        )
        for pattern in expected:
            self.assertIn(pattern, DEFAULT_RSYNC_EXCLUDES)

    def test_rsync_filter_parser_uses_exclude_rules_only(self) -> None:
        self.assertEqual(
            parse_rsync_filter_excludes(
                "# comment\n- node_modules/\n+ keep-me\n- .tmp/\n"
            ),
            ("node_modules/", ".tmp/"),
        )

    def test_tar_exclude_patterns_add_recursive_leaf_variants(self) -> None:
        self.assertIn("*/node_modules", tar_exclude_patterns(("node_modules/",)))
        self.assertIn("node_modules/*", tar_exclude_patterns(("node_modules/",)))
        self.assertIn("*/node_modules/*", tar_exclude_patterns(("node_modules/",)))
        self.assertIn("*/.DS_Store", tar_exclude_patterns((".DS_Store",)))

    def test_auto_sync_accepts_archive_progress_and_metadata_options(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "auto-sync",
                "--host",
                "192.168.1.20",
                "--source",
                "/tmp/source",
                "--no-mac-metadata",
                "--estimate-size",
            ]
        )
        self.assertTrue(args.no_mac_metadata)
        self.assertTrue(args.estimate_size)

    def test_rsync_daemon_url_includes_auth_port_module_and_path(self) -> None:
        self.assertEqual(
            rsync_daemon_url(
                host="192.168.1.20",
                port=18730,
                module="sync",
                user="macsetup",
                module_path="repos",
            ),
            "rsync://macsetup@192.168.1.20:18730/sync/repos/",
        )

    def test_rsync_from_command_pulls_remote_source_over_ssh(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "rsync-from",
                "--host",
                "192.168.1.10",
                "--user",
                "matt",
                "--source",
                "~/repos/",
                "--dest",
                "/Users/matt/repos/",
                "--ssh-port",
                "2222",
                "--ssh-option",
                "BatchMode=yes",
                "--dry-run",
                "--itemize",
                "--no-progress",
                "--no-default-excludes",
            ]
        )
        with patch(
            "macsetup.transfer.rsync_help",
            return_value="--mkpath\n--acls\n--xattrs\n--fileflags\n--crtimes\n",
        ):
            command = build_rsync_from_command(args)
        self.assertEqual(
            command,
            [
                "rsync",
                "-a",
                "--whole-file",
                "--no-compress",
                "--partial",
                "--acls",
                "--xattrs",
                "--fileflags",
                "--crtimes",
                "--delete",
                "--delete-after",
                "--dry-run",
                "--itemize-changes",
                "--mkpath",
                "-e",
                "ssh -p 2222 -o BatchMode=yes",
                "matt@192.168.1.10:~/repos/",
                "/Users/matt/repos/",
            ],
        )

    def test_rsync_from_helpers_keep_ssh_user_optional(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "rsync-from",
                "192.168.1.10",
                "~/repos/",
                "/Users/matt/repos/",
            ]
        )
        host, source, dest = rsync_from_host_source_dest(args)
        self.assertEqual(build_rsync_remote_shell(args), [])
        self.assertEqual(
            (host, source, dest),
            ("192.168.1.10", "~/repos/", "/Users/matt/repos/"),
        )
        self.assertEqual(
            rsync_remote_source(host=host, user=args.user, source=source),
            "192.168.1.10:~/repos/",
        )

    def test_netmask_to_prefix_accepts_macos_hex_netmask(self) -> None:
        self.assertEqual(netmask_to_prefix("0xffffff00"), 24)

    def test_rsyncd_config_is_authenticated_and_lan_scoped(self) -> None:
        with TemporaryDirectory() as dirname:
            config_path, secrets_path = write_rsyncd_files(
                temp_dir=Path(dirname),
                bind_ip="192.168.1.20",
                port=18730,
                module="sync",
                user="macsetup",
                password="secret",
                dest=Path("/tmp/dest"),
                hosts_allow=("192.168.1.0/24",),
            )
            config = config_path.read_text(encoding="utf-8")
            self.assertIn("auth users = macsetup:rw", config)
            self.assertIn("hosts allow = 192.168.1.0/24", config)
            self.assertIn("hosts deny = *", config)
            self.assertIn(f"secrets file = {secrets_path}", config)
            self.assertEqual(oct(secrets_path.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
