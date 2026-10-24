import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_runner import Config, ConfigStore, ToolError, cli
from ssh_runner import config as config_module
from ssh_runner.config import store as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "config.json"
        self.password = "test-only-$literal#secret"
        self.profile = {
            "host": "example.com",
            "user": "alice",
            "password": self.password,
        }
        self.data = {"version": 1, "servers": {"dev": self.profile}}
        self.write()

    def write(self, data=None):
        self.file.write_text(
            json.dumps(self.data if data is None else data, ensure_ascii=False),
            encoding="utf-8",
        )
        self.file.chmod(0o600)

    def test_default_config_ignores_working_directory(self):
        with mock.patch.object(Path, "home", return_value=Path(self.temp.name)):
            self.assertEqual(
                ConfigStore().path,
                Path(self.temp.name) / ".config/ssh-runner/config.json",
            )
            self.assertEqual(ConfigStore(self.file).path, self.file)

    def test_initialize_config_is_private_and_never_overwrites_files_or_symlinks(self):
        target = Path(self.temp.name) / "new" / "config.json"
        ConfigStore(target).initialize()
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            [name for name, _ in ConfigStore(target).load().servers], ["dev", "legacy"]
        )
        target.write_text("private-original")
        with self.assertRaises(ToolError):
            ConfigStore(target).initialize()
        self.assertEqual(target.read_text(), "private-original")
        target.unlink()
        destination = target.parent / "untouched.json"
        target.symlink_to(destination)
        with self.assertRaises(ToolError):
            ConfigStore(target).initialize()
        self.assertFalse(destination.exists())

    def test_init_config_cli_does_not_require_credentials_or_start_ssh(self):
        target = Path(self.temp.name) / "cli-config.json"
        output = io.StringIO()
        factory = mock.Mock()
        app = cli.Application(client_factory=factory, stdout=output)
        self.assertEqual(app.main(["--config", str(target), "config", "init"]), 0)
        self.assertEqual(
            json.loads(output.getvalue())["data"], {"config_created": str(target)}
        )
        self.assertTrue(target.is_file())
        factory.assert_not_called()

    def test_named_target_defaults_and_hidden_secrets(self):
        loaded = ConfigStore(self.file).select()[1]
        self.assertEqual(
            (loaded.host, loaded.user, loaded.port, loaded.auth_mode),
            ("example.com", "alice", None, "password"),
        )
        self.assertEqual(loaded.password, self.password)
        self.assertNotIn(self.password, repr(loaded))
        self.assertNotIn(self.password, repr(ConfigStore(self.file).load()))
        self.assertTrue(loaded.reuse_connection)

    def test_server_selection_never_silently_chooses_between_multiple_targets(self):
        self.data["servers"]["staging"] = {
            "host": "example.com",
            "user": "staging",
            "auth_mode": "keyboard-interactive",
            "password": "other-test-password",
        }
        self.write()
        with self.assertRaises(ToolError):
            ConfigStore(self.file).select()[1]
        self.assertIsNone(ConfigStore(self.file).select("staging")[1].port)
        self.assertEqual(ConfigStore(self.file).select("dev")[1].host, "example.com")
        for name in ("unknown", "", "bad name"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                ConfigStore(self.file).select(name)[1]
        self.data["default_server"] = "dev"
        self.write()
        self.assertEqual(
            ConfigStore(self.file).select()[1], ConfigStore(self.file).select("dev")[1]
        )

    def test_defaults_merge_env_by_key_and_replace_arrays(self):
        self.data["defaults"] = {
            "control_persist": 60,
            "send_env": ["UNSET_DEFAULT"],
            "env": {"APP_ENV": "shared", "LANG": "C.UTF-8"},
        }
        self.profile.update(env={"APP_ENV": "dev"}, send_env=[], control_persist=90)
        self.write()
        loaded = ConfigStore(self.file).select()[1]
        self.assertEqual(dict(loaded.env), {"APP_ENV": "dev", "LANG": "C.UTF-8"})
        self.assertEqual(loaded.send_env, ())
        self.assertEqual(loaded.control_persist, 90)

    def test_snapshot_preserves_quotes_unicode_and_all_settings(self):
        original = Config(
            "example.com",
            "alice",
            22,
            " 中文 'secret\" $HOME \\ ",
            auth_mode="keyboard-interactive",
            password_prompt="密码:",
            reuse_connection=False,
            control_persist=60,
            server_alive_interval=20,
            server_alive_count_max=4,
            connect_timeout=12,
            reconnect_delay=8,
            env={"APP_LABEL": ' 中文 " \\ # $HOME '},
        )
        self.file.write_text(config.serialize_config(original), encoding="utf-8")
        self.assertEqual(ConfigStore(self.file).select()[1], original)

    def test_local_environment_is_explicit_and_static_values_take_precedence(self):
        self.profile.update(send_env=["REGION", "APP_ENV"], env={"APP_ENV": "fixed"})
        self.write()
        with mock.patch.dict(
            os.environ,
            {"REGION": "cn", "APP_ENV": "local", "OTHER_SECRET": self.password},
        ):
            loaded = ConfigStore(self.file).select()[1]
        self.assertEqual(dict(loaded.env), {"REGION": "cn", "APP_ENV": "fixed"})
        self.assertEqual(loaded.send_env, ())
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ToolError):
            ConfigStore(self.file).select()[1]

    def test_only_selected_server_needs_password_and_local_environment(self):
        self.data["servers"]["unfinished"] = {
            **self.profile,
            "password": "",
            "send_env": ["UNSET_LOCAL"],
        }
        self.write()
        self.assertEqual(
            ConfigStore(self.file).select("dev")[1].password, self.password
        )
        with self.assertRaises(ToolError):
            ConfigStore(self.file).select("unfinished")[1]
        output = io.StringIO()
        factory = mock.Mock()
        app = cli.Application(client_factory=factory, stdout=output)
        self.assertEqual(app.main(["--config", str(self.file), "config", "list"]), 0)
        factory.assert_not_called()
        summaries = json.loads(output.getvalue())["data"]["servers"]
        self.assertFalse(summaries[1]["password_configured"])
        self.assertNotIn(self.password, output.getvalue())

    def test_environment_is_immutable_and_secret_values_hidden(self):
        values = {"APP_ENV": "initial"}
        loaded = Config("host", "user", 22, self.password, env=values)
        values["APP_ENV"] = "changed"
        self.assertEqual(dict(loaded.env), {"APP_ENV": "initial"})
        self.assertNotIn("initial", repr(loaded))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            loaded.env = ()

    def test_invalid_modes_prompts_types_and_connection_settings(self):
        invalid = [
            {"auth_mode": "auto"},
            {"auth_mode": []},
            {"host": 12},
            {"user": []},
            {"password": 12},
            {"password_prompt": {}},
            {"control_persist": 0},
            {"server_alive_interval": 0},
            {"server_alive_count_max": -1},
            {"connect_timeout": True},
            {"reconnect_delay": 0},
            {"reuse_connection": "true"},
            {"auth_mode": "password", "password_prompt": "OTP:"},
            {"auth_mode": "keyboard-interactive", "password_prompt": "New password:"},
            {"auth_mode": "auto", "password_prompt": "Password:"},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ToolError):
                Config(**{**self.profile, "port": 22, **overrides})

    def test_invalid_environment_values_names_and_limits(self):
        invalid = [
            {"env": {"BAD=KEY": "value"}},
            {"env": {"SSH_ASKPASS": "value"}},
            {"env": {"APP": self.password}},
            {"env": {"APP": "a\nb"}},
            {"env": {"APP": "a\x00b"}},
            {"env": {"APP": 1}},
            {"env": {"APP": "${HOME}"}},
            {"env": {"APP": "%h"}},
            {"env": {"APP": "a" * 4097}},
            {"env": {"V%d" % i: "" for i in range(65)}},
            {"send_env": "LANG"},
            {"send_env": ["LC_*"]},
            {"send_env": ["SSHPASS"]},
            {"send_env": ["LANG", "LANG"]},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ToolError):
                Config("host", "user", 22, self.password, **options)
        with mock.patch.dict(os.environ, {"APP": self.password}):
            with self.assertRaises(ToolError):
                config_module.resolve_environment(
                    Config("host", "user", 22, self.password, send_env=["APP"])
                )

    def test_permissions_symlinks_owner_and_non_regular_files(self):
        self.file.chmod(0o644)
        with self.assertRaises(ToolError):
            ConfigStore(self.file).select()[1]
        self.file.chmod(0o600)
        link = self.file.parent / "link"
        link.symlink_to(self.file)
        fifo = self.file.parent / "fifo"
        os.mkfifo(fifo, 0o600)
        for path in (link, fifo):
            with self.assertRaises(ToolError):
                ConfigStore(path).select()[1]
        with mock.patch.object(config.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ToolError):
                ConfigStore(self.file).select()[1]

    def test_schema_rejects_unknown_fields_bad_defaults_and_incomplete_profiles(self):
        cases = [
            {},
            [],
            {**self.data, "version": True},
            {**self.data, "version": 2},
            {**self.data, "default_server": "missing"},
            {**self.data, "secret": self.password},
            {**self.data, "defaults": {"password": self.password}},
            {**self.data, "defaults": {"control_persist": 0}},
            {**self.data, "servers": {}},
            {**self.data, "servers": {"bad name": self.profile}},
            {**self.data, "servers": {"dev": {"host": "host"}}},
            {**self.data, "servers": {"dev": {**self.profile, "env": []}}},
            {**self.data, "servers": {"dev": {**self.profile, "send_env": {}}}},
            {**self.data, "servers": {"dev": {**self.profile, "typo": self.password}}},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(ToolError) as caught:
                    ConfigStore(self.file).load()
                self.assertNotIn(self.password, str(caught.exception))

    def test_invalid_json_duplicate_keys_and_non_finite_numbers_never_leak_contents(
        self,
    ):
        cases = [
            self.password,
            '{"version":1,"version":1}',
            '{"version":1,"servers":{"dev":{"password":"'
            + self.password
            + '","password":"x"}}}',
            '{"version":NaN}',
            '{"version":Infinity}',
            '{"secret":"' + self.password + '"',
            "[" * 1500 + "]" * 1500,
        ]
        for contents in cases:
            self.file.write_text(contents)
            with self.assertRaises(ToolError) as caught:
                ConfigStore(self.file).load()
            self.assertNotIn(self.password, str(caught.exception))

    def test_utf8_size_and_growth_limits(self):
        for contents in (b"\xff", b" " * (config.MAX_CONFIG_BYTES + 1)):
            self.file.write_bytes(contents)
            with self.assertRaises(ToolError):
                ConfigStore(self.file).select()[1]
        self.write()
        info = os.stat(self.file)
        self.file.write_bytes(b" " * (config.MAX_CONFIG_BYTES + 1))
        with mock.patch.object(config.os, "fstat", return_value=info):
            with self.assertRaises(ToolError):
                ConfigStore(self.file).select()[1]
        self.profile["password"] = "\ud800"
        self.file.write_text(json.dumps(self.data))
        with self.assertRaises(ToolError):
            ConfigStore(self.file).select()[1]

    def test_ipv6_literals_and_key_auth_without_password(self):
        for host in ("::1", "::ffff:192.0.2.1", "2001:db8::1"):
            self.assertEqual(Config(host, "alice", 22, self.password).host, host)
        for host in ("[::1]", "not:ipv6", "-bad", "::1%en0"):
            with self.assertRaises(ToolError):
                Config(host, "alice", 22, self.password)
        self.profile.pop("password")
        self.profile["auth_mode"] = "key"
        self.write()
        self.assertEqual(ConfigStore(self.file).select()[1].password, "")

    def test_password_and_port_bounds_and_empty_template_credentials(self):
        for password in ("a\nb", "a\rb", "a\x00b", "中" * 342):
            with self.subTest(password=password), self.assertRaises(ToolError):
                Config("host", "user", 22, password)
        for port in (0, 65536, True, "22"):
            with self.subTest(port=port), self.assertRaises(ToolError):
                Config("host", "user", port, self.password)
        with self.assertRaises(ToolError):
            Config("host", "user", 22, "").require_password()
        self.assertEqual(len(Config("host", "user", 22, "a" * 1023).password), 1023)

    def test_runtime_settings_inherit_and_roundtrip(self):
        self.data["defaults"] = {
            "ssh_binary": "/custom ssh",
            "remote_python": "/custom python",
            "job_root": "~/custom jobs",
        }
        self.profile["remote_python"] = "python3.8"
        self.write()
        config = ConfigStore(self.file).select()[1]
        self.assertEqual(config.ssh_binary, "/custom ssh")
        self.assertEqual(config.remote_python, "python3.8")
        self.assertEqual(config.job_root, "~/custom jobs")
        for field in ("ssh_binary", "remote_python", "job_root"):
            for value in ("", None, "bad\x00path", "bad\npath"):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ToolError
                ):
                    dataclasses.replace(config, **{field: value})


if __name__ == "__main__":
    unittest.main()
