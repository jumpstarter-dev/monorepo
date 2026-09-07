import base64
import json
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner
from jumpstarter_protocol import kubernetes_pb2

from jumpstarter_cli.describe import describe

from jumpstarter.client.grpc import Exporter, Lease, LeaseList
from jumpstarter.common import ExporterStatus
from jumpstarter.common.exceptions import ConnectionError

CLIENT_TOKEN_PLACEHOLDER = "not-a-real-token"


def _make_jwt(exp_offset_seconds=3600, include_exp=True):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_data = {
        "sub": "test-subject",
        "iss": "https://localhost:8085",
        "iat": int(time.time()),
    }
    if include_exp:
        payload_data["exp"] = int(time.time()) + exp_offset_seconds
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fake-signature"


def _make_condition(type="Ready", status="True", reason="Ready", message="lease is ready"):
    condition = kubernetes_pb2.Condition(type=type, status=status, reason=reason, message=message)
    condition.lastTransitionTime.seconds = int(datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp())
    return condition


def _make_lease(name="lease-1", conditions=None, exporter="exporter-1", **kwargs):
    return Lease(
        namespace="default",
        name=name,
        selector="board=rpi4",
        duration=timedelta(minutes=30),
        client="my-client",
        exporter=exporter,
        conditions=conditions if conditions is not None else [_make_condition()],
        effective_begin_time=datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        tags={"build": "1234"},
        context={"purpose": "ci"},
        **kwargs,
    )


def _make_exporter(lease=None):
    return Exporter(
        namespace="default",
        name="exporter-1",
        labels={"board": "rpi4", "env": "test"},
        online=True,
        status=ExporterStatus.AVAILABLE,
        enabled=True,
        lease=lease,
    )


def _exporter_config(exporter=None, leases=()):
    """A client config that answers the way the real one does.

    GetExporter never carries a lease, so a test that hangs one on the exporter
    is testing something the server cannot do.
    """
    config = MagicMock()
    config.get_exporter.return_value = exporter if exporter is not None else _make_exporter()
    config.list_leases.return_value = LeaseList(leases=list(leases), next_page_token=None)
    return config


def _patch_remote_config(config):
    mock_cls = MagicMock()
    mock_cls.load.return_value = config
    return patch("jumpstarter_cli_common.config.ClientConfigV1Alpha1", mock_cls)


def _make_client_config(alias="test-client", token=None, refresh_token=None):
    config = MagicMock()
    config.alias = alias
    config.path = Path(f"/home/user/.config/jumpstarter/clients/{alias}.yaml")
    config.metadata.name = "my-client"
    config.metadata.namespace = "default"
    config.endpoint = "grpc.example.com:443"
    config.token = token
    config.refresh_token = refresh_token
    config.tls.ca = ""
    config.tls.insecure = False
    config.drivers.allow = ["jumpstarter_driver_power"]
    config.drivers.unsafe = False
    return config


def _patch_client_configs(config, current_alias=None, default_client=None):
    client_cls = MagicMock()
    client_cls.load.return_value = config
    client_cls.list.return_value = MagicMock(current_config=current_alias)
    user_cls = MagicMock()
    user_cls.load_or_create.return_value.config.current_client = default_client
    stack = ExitStack()
    stack.enter_context(patch("jumpstarter_cli.describe.ClientConfigV1Alpha1", client_cls))
    stack.enter_context(patch("jumpstarter_cli.describe.UserConfigV1Alpha1", user_cls))
    return stack


class TestDescribeExporter:
    def setup_method(self):
        self.runner = CliRunner()

    def test_pretty_output(self):
        config = _exporter_config()
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "exporter-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Name:" in result.output
        assert "exporter-1" in result.output
        assert "Namespace:" in result.output
        assert "board=rpi4" in result.output
        assert "env=test" in result.output
        assert "Online:" in result.output
        assert "AVAILABLE" in result.output
        assert "Enabled:" in result.output
        assert "Lease:  <none>" in result.output
        config.get_exporter.assert_called_once_with("exporter-1")

    def test_pretty_output_with_lease(self):
        # The lease comes from ListLeases, not from the exporter itself.
        config = _exporter_config(leases=[_make_lease()])
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "exporter-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Lease:" in result.output
        assert "lease-1" in result.output
        assert "my-client" in result.output
        assert "In-Use" in result.output
        assert "0:30:00" in result.output
        config.list_leases.assert_called_once_with(only_active=True)

    def test_a_lease_on_another_exporter_is_not_reported(self):
        config = _exporter_config(leases=[_make_lease(name="lease-2", exporter="exporter-2")])
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "exporter-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Lease:  <none>" in result.output
        assert "lease-2" not in result.output

    def test_pretty_output_deprecated_labels(self):
        exporter = _make_exporter()
        exporter.deprecated_labels = {"old-key": "Use new-key instead"}
        config = _exporter_config(exporter)
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "exporter-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Deprecated Labels:" in result.output
        assert "old-key=Use new-key instead" in result.output

    def test_json_output(self):
        config = _exporter_config(leases=[_make_lease()])
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "exporter-1", "--client", "test", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["name"] == "exporter-1"
        assert data["namespace"] == "default"
        assert data["labels"] == {"board": "rpi4", "env": "test"}
        assert data["online"] is True
        assert data["lease"]["name"] == "lease-1"

    def test_unknown_name(self):
        config = _exporter_config()
        config.get_exporter.side_effect = ConnectionError("exporter 'missing' not found")
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["exporter", "missing", "--client", "test"])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestDescribeLease:
    def setup_method(self):
        self.runner = CliRunner()

    def test_pretty_output(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Name:" in result.output
        assert "lease-1" in result.output
        assert "Selector:" in result.output
        assert "board=rpi4" in result.output
        assert "exporter-1" in result.output
        assert "my-client" in result.output
        assert "In-Use" in result.output
        assert "0:30:00" in result.output
        assert "Effective Begin Time:" in result.output
        assert "build=1234" in result.output
        assert "purpose=ci" in result.output
        assert "Conditions:" in result.output
        assert "Ready" in result.output
        assert "lease is ready" in result.output
        config.get_lease.assert_called_once_with(name="lease-1")

    def test_pretty_output_no_conditions(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease(conditions=[])
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Conditions:  <none>" in result.output
        assert "Unknown" in result.output

    def test_json_output(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["name"] == "lease-1"
        assert data["selector"] == "board=rpi4"
        assert data["client"] == "my-client"
        assert data["exporter"] == "exporter-1"
        assert data["conditions"][0]["type"] == "Ready"

    def test_unknown_name(self):
        config = MagicMock()
        config.get_lease.side_effect = ConnectionError("lease 'missing' not found")
        with _patch_remote_config(config):
            result = self.runner.invoke(describe, ["lease", "missing", "--client", "test"])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestDescribeClient:
    def setup_method(self):
        self.runner = CliRunner()

    def test_pretty_output(self):
        token = _make_jwt(exp_offset_seconds=7200)
        config = _make_client_config(token=token, refresh_token="fake-refresh")
        with _patch_client_configs(config, current_alias="test-client"):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "Alias:" in result.output
        assert "test-client" in result.output
        assert "Current:" in result.output
        assert "my-client" in result.output
        assert "grpc.example.com:443" in result.output
        assert "jumpstarter_driver_power" in result.output
        assert "valid" in result.output
        assert "Refresh Token Stored:" in result.output
        assert token not in result.output
        assert "fake-refresh" not in result.output

    def test_pretty_output_not_current(self):
        config = _make_client_config()
        with _patch_client_configs(config, current_alias="other-client"):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "Current:" in result.output
        assert "no" in result.output

    def test_default_client(self):
        config = _make_client_config()
        with _patch_client_configs(config, current_alias="test-client", default_client=config):
            result = self.runner.invoke(describe, ["client"])
        assert result.exit_code == 0, result.output
        assert "test-client" in result.output
        assert "yes" in result.output

    def test_no_default_client(self):
        config = _make_client_config()
        with _patch_client_configs(config, default_client=None):
            result = self.runner.invoke(describe, ["client"])
        assert result.exit_code != 0
        assert "no default client" in result.output

    def test_json_output_never_contains_token(self):
        token = _make_jwt(exp_offset_seconds=7200)
        config = _make_client_config(token=token, refresh_token="fake-refresh")
        with _patch_client_configs(config, current_alias="test-client"):
            result = self.runner.invoke(describe, ["client", "test-client", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["alias"] == "test-client"
        assert data["current"] is True
        assert data["endpoint"] == "grpc.example.com:443"
        assert data["tokenExpiry"] is not None
        assert data["tokenStatus"].startswith("valid")
        assert data["refreshTokenStored"] is True
        assert token not in result.output
        assert "fake-refresh" not in result.output

    def test_yaml_output_never_contains_token(self):
        token = _make_jwt(exp_offset_seconds=7200)
        config = _make_client_config(token=token, refresh_token="fake-refresh")
        with _patch_client_configs(config, current_alias="test-client"):
            result = self.runner.invoke(describe, ["client", "test-client", "-o", "yaml"])
        assert result.exit_code == 0, result.output
        assert token not in result.output
        assert "fake-refresh" not in result.output
        assert "tokenStatus" in result.output

    def test_no_token(self):
        config = _make_client_config(token=None)
        with _patch_client_configs(config):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "no token" in result.output

    def test_malformed_token(self):
        config = _make_client_config(token=CLIENT_TOKEN_PLACEHOLDER)
        with _patch_client_configs(config):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "malformed" in result.output
        assert "Traceback" not in result.output

    def test_malformed_token_json(self):
        config = _make_client_config(token=CLIENT_TOKEN_PLACEHOLDER)
        with _patch_client_configs(config):
            result = self.runner.invoke(describe, ["client", "test-client", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tokenStatus"] == "malformed"
        assert data["tokenExpiry"] is None

    def test_token_without_exp_claim(self):
        config = _make_client_config(token=_make_jwt(include_exp=False))
        with _patch_client_configs(config):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "no expiry claim" in result.output

    def test_expired_token(self):
        config = _make_client_config(token=_make_jwt(exp_offset_seconds=-3600))
        with _patch_client_configs(config):
            result = self.runner.invoke(describe, ["client", "test-client"])
        assert result.exit_code == 0, result.output
        assert "expired" in result.output


class TestDescribeGroup:
    def test_subcommands_registered(self):
        assert set(describe.commands) == {"exporter", "lease", "client"}

    def test_desc_alias(self):
        from jumpstarter_cli.jmp import jmp

        ctx = MagicMock()
        ctx.fail = MagicMock()
        assert jmp.get_command(ctx, "desc") is describe


_DRIVER_TREE = {
    "drivers": [
        {
            "path": "client",
            "driver_path": [],
            "class": "jumpstarter_driver_composite.client.CompositeClient",
            "description": None,
            "methods": [],
        },
        {
            "path": "client.power",
            "driver_path": ["power"],
            "class": "jumpstarter_driver_power.client.PowerClient",
            "description": None,
            "methods": ["cycle", "off", "on"],
        },
    ],
    "cli_tree": {
        "name": "j",
        "help": "Generic composite device",
        "params": [],
        "subcommands": {
            "power": {
                "name": "power",
                "help": "Power control",
                "params": [],
                "subcommands": {
                    "on": {"name": "on", "help": "Turn power on", "params": [], "subcommands": {}},
                    "off": {"name": "off", "help": "Turn power off", "params": [], "subcommands": {}},
                },
            }
        },
    },
}


class TestDescribeLeaseDrivers:
    def setup_method(self):
        self.runner = CliRunner()

    def test_devices_is_not_an_alias_for_driver_introspection(self):
        result = self.runner.invoke(describe, ["lease", "lease-1", "--devices"])
        assert result.exit_code == 2
        assert "No such option: --devices" in result.output

    def test_pretty_output_drivers(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", return_value=_DRIVER_TREE) as mock_drivers,
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "--drivers"])
        assert result.exit_code == 0, result.output
        assert "Drivers:" in result.output
        assert "(root)" in result.output
        assert "jumpstarter_driver_power.client.PowerClient" in result.output
        assert "cycle, off, on" in result.output
        assert "Commands:" in result.output
        assert "j power on" in result.output
        assert "Turn power on" in result.output
        mock_drivers.assert_called_once_with(config, "lease-1")

    def test_pretty_output_no_drivers_flag(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", return_value=_DRIVER_TREE) as mock_drivers,
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test"])
        assert result.exit_code == 0, result.output
        assert "Drivers:" not in result.output
        mock_drivers.assert_not_called()

    def test_json_output_drivers(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", return_value=_DRIVER_TREE),
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "--drivers", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["lease"]["name"] == "lease-1"
        assert data["driver_tree"]["drivers"][1]["class"] == "jumpstarter_driver_power.client.PowerClient"
        assert data["driver_tree"]["cli_tree"]["subcommands"]["power"]["subcommands"]["on"]["help"] == "Turn power on"

    def test_yaml_output_drivers(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", return_value=_DRIVER_TREE),
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "--drivers", "-o", "yaml"])
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(result.output)
        assert data["lease"]["name"] == "lease-1"
        assert data["driver_tree"] == _DRIVER_TREE
        assert "devices" not in data

    def test_json_without_drivers_preserves_the_lease_shape(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers") as mock_drivers,
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "-o", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["name"] == "lease-1"
        assert "lease" not in data
        assert "driver_tree" not in data
        mock_drivers.assert_not_called()

    def test_driver_connection_failure_is_reported(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", side_effect=ConnectionError("exporter unreachable")),
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "--drivers", "-o", "json"])
        assert result.exit_code != 0
        assert "exporter unreachable" in result.output

    def test_stub_root_cli_tree_none(self):
        config = MagicMock()
        config.get_lease.return_value = _make_lease()
        driver_tree = {"drivers": _DRIVER_TREE["drivers"], "cli_tree": None}
        with (
            _patch_remote_config(config),
            patch("jumpstarter_cli.describe.describe_drivers", return_value=driver_tree),
        ):
            result = self.runner.invoke(describe, ["lease", "lease-1", "--client", "test", "--drivers"])
        assert result.exit_code == 0, result.output
        assert "Commands:  <none>" in result.output
