"""Regression coverage for explicit keys and native OpenSSH Host aliases."""

import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_runner import Config, ConfigStore, ToolError, cli
from ssh_runner.config.checks import check_local_files
from ssh_runner.config.store import serialize_config
from ssh_runner.ssh.transport import OpenSSHTransport


class KeyAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh-key-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ssh_config = self.root / ".ssh_config"
        self.ssh_config.write_text(
            "Host aliyun\n HostName server.example.com\n User deploy\n Port 2222\n"
            " IdentityFile ~/.ssh/fixture-key\n IdentitiesOnly yes\n"
            " ProxyJump jump.example.com\n"
        )
        self.config = Config(
            "aliyun", auth_mode="key", ssh_config_file=str(self.ssh_config)
        )
        self.transport = OpenSSHTransport()

    def test_alias_inherits_user_port_identity_and_jump_without_network(self):
        args = self.transport.arguments(self.config)
        self.assertNotIn("-l", args)
        self.assertNotIn("-p", args)
        self.assertEqual(args[-1], "aliyun")
        resolved, settings = self.transport.resolve_config(self.config)
        self.assertEqual(
            (resolved.host, resolved.user, resolved.port), ("aliyun", "deploy", 2222)
        )
        self.assertIn("hostname server.example.com\n", settings)
        self.assertIn("identityfile ~/.ssh/fixture-key\n", settings)
        self.assertIn("proxyjump jump.example.com\n", settings)
        self.assertIn("identitiesonly yes\n", settings)
        self.assertEqual(self.transport.arguments(resolved)[-1], "aliyun")

    def test_explicit_user_port_and_identity_policy_override_alias(self):
        selected = dataclasses.replace(
            self.config, user="other", port=2200, identities_only=False
        )
        resolved, settings = self.transport.resolve_config(selected)
        self.assertEqual((resolved.user, resolved.port), ("other", 2200))
        self.assertIn("identitiesonly no\n", settings)

    def test_password_prompt_uses_inherited_user_and_effective_host(self):
        selected = dataclasses.replace(
            self.config, auth_mode="password", password="fixture-password"
        )
        resolved, _ = self.transport.resolve_config(selected)
        self.assertEqual(
            resolved.password_prompt, "deploy@server.example.com's password:"
        )

    def test_explicit_file_options_preserve_literal_spaces_and_quotes(self):
        identity = self.root / 'identity with "quotes"'
        certificate = self.root / 'certificate with "quotes".pub'
        known_hosts = self.root / 'known hosts with "quotes"'
        for file in (identity, certificate, known_hosts):
            file.write_text("fixture")
            file.chmod(0o600)
        selected = dataclasses.replace(
            self.config,
            identity_file=str(identity),
            certificate_file=str(certificate),
            known_hosts_file=str(known_hosts),
        )
        args = self.transport.arguments(selected)
        self.assertEqual(args[args.index("-i") + 1], str(identity))
        resolved, settings = self.transport.resolve_config(selected)
        self.assertIn("identitiesonly yes\n", settings)
        # ssh -G exposes path values after ssh_config parsing.
        self.assertIn("certificatefile " + str(certificate) + "\n", settings)
        self.assertIn(str(known_hosts), settings)
        check_local_files(resolved)

    def test_key_snapshot_roundtrip_and_summaries_hide_file_paths(self):
        selected = dataclasses.replace(self.config, identity_file="~/fixture-identity")
        path = self.root / "config.json"
        path.write_text(serialize_config(selected))
        path.chmod(0o600)
        store = ConfigStore(path)
        self.assertEqual(store.select()[1], selected)
        summary = store.load().summaries()[0]
        self.assertTrue(summary["identity_file_configured"])
        self.assertTrue(summary["ssh_config_file_configured"])
        self.assertNotIn("fixture-identity", json.dumps(summary))
        self.assertEqual(selected.identity_file, str(Path.home() / "fixture-identity"))

    def test_bad_paths_and_identity_policy_fail_validation(self):
        for field in (
            "identity_file",
            "certificate_file",
            "known_hosts_file",
            "ssh_config_file",
        ):
            for value in (
                None,
                [],
                "bad\x00path",
                "bad\npath",
                "%h/key",
                "${HOME}/key",
                "x" * 4097,
            ):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ToolError
                ):
                    dataclasses.replace(self.config, **{field: value})
        for value in ("yes", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ToolError):
                dataclasses.replace(self.config, identities_only=value)

    def test_local_check_rejects_missing_nonregular_and_open_private_keys(self):
        for field in (
            "identity_file",
            "certificate_file",
            "known_hosts_file",
            "ssh_config_file",
        ):
            for path in (self.root / "missing", self.root):
                with self.subTest(field=field, path=path), self.assertRaises(ToolError):
                    check_local_files(
                        dataclasses.replace(self.config, **{field: str(path)})
                    )
        key = self.root / "private-key"
        key.write_text("fixture")
        key.chmod(0o644)
        with self.assertRaises(ToolError):
            check_local_files(dataclasses.replace(self.config, identity_file=str(key)))
        key.chmod(0o600)
        check_local_files(dataclasses.replace(self.config, identity_file=str(key)))
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ToolError):
            check_local_files(dataclasses.replace(self.config, identity_file=str(fifo)))

    def test_config_check_does_not_invoke_ssh_and_rejects_missing_identity(self):
        path = self.root / "config.json"
        path.write_text(serialize_config(self.config))
        path.chmod(0o600)
        with mock.patch.object(OpenSSHTransport, "resolve_config") as resolve:
            output = io.StringIO()
            self.assertEqual(
                cli.Application(stdout=output).main(
                    ["--config", str(path), "config", "check"]
                ),
                0,
            )
            resolve.assert_not_called()
            path.write_text(
                serialize_config(
                    dataclasses.replace(
                        self.config, identity_file=str(self.root / "missing")
                    )
                )
            )
            output = io.StringIO()
            self.assertEqual(
                cli.Application(stdout=output).main(
                    ["--config", str(path), "config", "check"]
                ),
                2,
            )
            self.assertIn("identity_file", json.loads(output.getvalue())["stderr"])
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
