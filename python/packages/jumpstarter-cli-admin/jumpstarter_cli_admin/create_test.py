import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import click
from click.testing import CliRunner
from jumpstarter_kubernetes import (
    ClientsV1Alpha1Api,
    ExportersV1Alpha1Api,
    V1Alpha1Client,
    V1Alpha1ClientStatus,
    V1Alpha1Exporter,
    V1Alpha1ExporterStatus,
)
from kubernetes_asyncio.client.models import V1ObjectMeta, V1ObjectReference

from jumpstarter_cli_admin.test_utils import json_equal

from .create import create
from jumpstarter.config.client import ClientConfigV1Alpha1, ClientConfigV1Alpha1Drivers
from jumpstarter.config.common import ObjectMeta
from jumpstarter.config.exporter import ExporterConfigV1Alpha1
from jumpstarter.config.tls import TLSConfigV1Alpha1

# Generate a random client name
CLIENT_NAME = uuid.uuid4().hex
# Default config path
CLIENT_CONFIG_PATH = ClientConfigV1Alpha1.CLIENT_CONFIGS_PATH / (CLIENT_NAME + ".yaml")

CLIENT_ENDPOINT = "grpc://example.com:443"
CLIENT_TOKEN = "dGhpc2lzYXRva2VuLTEyMzQxMjM0MTIzNEyMzQtc2Rxd3Jxd2VycXdlcnF3ZXJxd2VyLTEyMzQxMjM0MTIz"
DRIVER_NAME = "jumpstarter.Testing"

CLIENT_OBJECT = V1Alpha1Client(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Client",
    metadata=V1ObjectMeta(namespace="default", name=CLIENT_NAME, creation_timestamp="2024-01-01T21:00:00Z"),
    status=V1Alpha1ClientStatus(
        endpoint=CLIENT_ENDPOINT, credential=V1ObjectReference(name=f"{CLIENT_NAME}-credential")
    ),
)

CLIENT_JSON = """{{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Client",
    "metadata": {{
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "{name}",
        "namespace": "default"
    }},
    "status": {{
        "credential": {{
            "name": "{name}-credential"
        }},
        "endpoint": "{endpoint}"
    }}
}}
""".format(name=CLIENT_NAME, endpoint=CLIENT_ENDPOINT)

CLIENT_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Client
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: {name}
  namespace: default
status:
  credential:
    name: {name}-credential
  endpoint: {endpoint}

""".format(name=CLIENT_NAME, endpoint=CLIENT_ENDPOINT)

UNSAFE_CLIENT_CONFIG = ClientConfigV1Alpha1(
    alias=CLIENT_NAME,
    metadata=ObjectMeta(namespace="default", name=CLIENT_NAME),
    endpoint=CLIENT_ENDPOINT,
    token=CLIENT_TOKEN,
    drivers=ClientConfigV1Alpha1Drivers(allow=[], unsafe=True),
)

CLIENT_CONFIG = ClientConfigV1Alpha1(
    alias=CLIENT_NAME,
    metadata=ObjectMeta(namespace="default", name=CLIENT_NAME),
    endpoint=CLIENT_ENDPOINT,
    token=CLIENT_TOKEN,
    drivers=ClientConfigV1Alpha1Drivers(allow=[], unsafe=True),
)

INSECURE_TLS_CLIENT_CONFIG = ClientConfigV1Alpha1(
    alias=CLIENT_NAME,
    metadata=ObjectMeta(namespace="default", name=CLIENT_NAME),
    endpoint=CLIENT_ENDPOINT,
    token=CLIENT_TOKEN,
    tls=TLSConfigV1Alpha1(insecure=True),
    drivers=ClientConfigV1Alpha1Drivers(allow=[], unsafe=True),
)


@patch.object(ClientConfigV1Alpha1, "save")
@patch.object(ClientsV1Alpha1Api, "get_client_config")
@patch.object(ClientsV1Alpha1Api, "create_client", return_value=CLIENT_OBJECT)
@patch.object(ClientsV1Alpha1Api, "_load_kube_config")
def test_create_client(
    _mock_load_kube_config, _mock_create_client, mock_get_client_config: AsyncMock, mock_save_client: Mock
):
    runner = CliRunner()

    # Don't save client config save = n
    result = runner.invoke(create, ["client", CLIENT_NAME], input="n\n")
    assert result.exit_code == 0
    assert "Creating client" in result.output
    assert CLIENT_NAME in result.output
    assert "Client configuration successfully saved" not in result.output
    mock_save_client.assert_not_called()
    mock_save_client.reset_mock()

    # Insecure TLS config is returned
    mock_get_client_config.return_value = INSECURE_TLS_CLIENT_CONFIG

    # Save with prompts accept insecure = Y, save = Y, unsafe = Y
    result = runner.invoke(create, ["client", "--insecure-tls-config", CLIENT_NAME], input="Y\nY\nY\n")
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(INSECURE_TLS_CLIENT_CONFIG, None)
    mock_save_client.reset_mock()

    # Save no interactive and insecure tls
    result = runner.invoke(
        create, ["client", "--insecure-tls-config", "--unsafe", "--save", "--nointeractive", CLIENT_NAME]
    )
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(INSECURE_TLS_CLIENT_CONFIG, None)
    mock_save_client.reset_mock()

    # Insecure TLS config is returned
    mock_get_client_config.return_value = INSECURE_TLS_CLIENT_CONFIG

    # Save with prompts accept insecure = N
    result = runner.invoke(create, ["client", "--insecure-tls-config", CLIENT_NAME], input="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.output

    # Unsafe client config is returned
    mock_get_client_config.return_value = UNSAFE_CLIENT_CONFIG

    # Save with prompts save = Y, unsafe = Y
    result = runner.invoke(create, ["client", CLIENT_NAME], input="Y\nY\n")
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(UNSAFE_CLIENT_CONFIG, None)
    mock_save_client.reset_mock()

    # Save with unsafe with custom output file
    out = f"/tmp/{CLIENT_NAME}.yaml"
    result = runner.invoke(create, ["client", CLIENT_NAME, "--unsafe", "--out", out], input="\n\n")
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(UNSAFE_CLIENT_CONFIG, str(Path(out).resolve()))
    mock_save_client.reset_mock()

    # Regular client config is returned
    mock_get_client_config.return_value = CLIENT_CONFIG

    # Save with arguments
    result = runner.invoke(create, ["client", CLIENT_NAME, "--save", "--allow", DRIVER_NAME], input="n\n")
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(CLIENT_CONFIG, None)
    mock_save_client.reset_mock()

    # Save with prompts, save = Y, unsafe = n, allow = DRIVER_NAME
    result = runner.invoke(create, ["client", CLIENT_NAME], input=f"Y\nn\n{DRIVER_NAME}\n")
    assert result.exit_code == 0
    assert "Client configuration successfully saved" in result.output
    mock_save_client.assert_called_once_with(CLIENT_CONFIG, None)
    mock_save_client.reset_mock()

    # Save with nointeractive
    result = runner.invoke(create, ["client", CLIENT_NAME, "--nointeractive"])
    assert result.exit_code == 0
    assert "Creating client" in result.output
    mock_save_client.assert_not_called()
    mock_save_client.reset_mock()

    # With JSON output
    result = runner.invoke(create, ["client", CLIENT_NAME, "--nointeractive", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, CLIENT_JSON)
    mock_save_client.assert_not_called()
    mock_save_client.reset_mock()

    # With YAML output
    result = runner.invoke(create, ["client", CLIENT_NAME, "--nointeractive", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == CLIENT_YAML
    mock_save_client.assert_not_called()
    mock_save_client.reset_mock()

    # With name output
    result = runner.invoke(create, ["client", CLIENT_NAME, "--nointeractive", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == f"client.jumpstarter.dev/{CLIENT_NAME}\n"
    mock_save_client.assert_not_called()
    mock_save_client.reset_mock()


# Generate a random exporter name
EXPORTER_NAME = uuid.uuid4().hex
EXPORTER_ENDPOINT = "grpc://example.com:443"
EXPORTER_TOKEN = "dGhpc2lzYXRva2VuLTEyMzQxMjM0MTIzNEyMzQtc2Rxd3Jxd2VycXdlcnF3ZXJxd2VyLTEyMzQxMjM0MTIz"
# Default config path
default_config_path = ExporterConfigV1Alpha1.BASE_PATH / (EXPORTER_NAME + ".yaml")
# Create a test exporter config
EXPORTER_OBJECT = V1Alpha1Exporter(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Exporter",
    metadata=V1ObjectMeta(namespace="default", name=EXPORTER_NAME, creation_timestamp="2024-01-01T21:00:00Z"),
    status=V1Alpha1ExporterStatus(
        endpoint=EXPORTER_ENDPOINT, credential=V1ObjectReference(name=f"{EXPORTER_NAME}-credential"), devices=[]
    ),
)

EXPORTER_JSON = """{{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Exporter",
    "metadata": {{
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "{name}",
        "namespace": "default"
    }},
    "status": {{
        "credential": {{
            "name": "{name}-credential"
        }},
        "devices": [],
        "endpoint": "{endpoint}",
        "exporterStatus": null,
        "statusMessage": null
    }}
}}
""".format(name=EXPORTER_NAME, endpoint=EXPORTER_ENDPOINT)

EXPORTER_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Exporter
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: {name}
  namespace: default
status:
  credential:
    name: {name}-credential
  devices: []
  endpoint: {endpoint}
  exporterStatus: null
  statusMessage: null

""".format(name=EXPORTER_NAME, endpoint=EXPORTER_ENDPOINT)

EXPORTER_CONFIG = ExporterConfigV1Alpha1(
    alias=EXPORTER_NAME,
    metadata=ObjectMeta(namespace="default", name=EXPORTER_NAME),
    endpoint=EXPORTER_ENDPOINT,
    token=EXPORTER_TOKEN,
)

INSECURE_TLS_EXPORTER_CONFIG = ExporterConfigV1Alpha1(
    alias=EXPORTER_NAME,
    metadata=ObjectMeta(namespace="default", name=EXPORTER_NAME),
    endpoint=EXPORTER_ENDPOINT,
    token=EXPORTER_TOKEN,
    tls=TLSConfigV1Alpha1(insecure=True),
)


@patch.object(ExporterConfigV1Alpha1, "save")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
@patch.object(ExportersV1Alpha1Api, "create_exporter", return_value=EXPORTER_OBJECT)
@patch.object(ExportersV1Alpha1Api, "get_exporter_config", return_value=EXPORTER_CONFIG)
def test_create_exporter(
    _get_exporter_config_mock, _create_exporter_mock, _load_kube_config_mock, save_exporter_mock: Mock
):
    runner = CliRunner()

    # Don't save exporter config
    result = runner.invoke(create, ["exporter", EXPORTER_NAME, "--label", "foo=bar"], input="n\n")
    assert result.exit_code == 0
    assert "Creating exporter" in result.output
    assert EXPORTER_NAME in result.output
    assert "Exporter configuration successfully saved" not in result.output
    save_exporter_mock.assert_not_called()
    save_exporter_mock.reset_mock()

    # Insecure TLS config is returned
    _get_exporter_config_mock.return_value = INSECURE_TLS_EXPORTER_CONFIG
    # Save with prompts accept insecure = Y, save = Y
    result = runner.invoke(
        create, ["exporter", "--insecure-tls-config", EXPORTER_NAME, "--label", "foo=bar"], input="Y\nY\n"
    )
    assert result.exit_code == 0
    assert "Exporter configuration successfully saved" in result.output
    save_exporter_mock.assert_called_once_with(INSECURE_TLS_EXPORTER_CONFIG, None)
    save_exporter_mock.reset_mock()

    _get_exporter_config_mock.return_value = INSECURE_TLS_EXPORTER_CONFIG
    # Save with prompts accept no interactive
    result = runner.invoke(
        create, ["exporter", "--insecure-tls-config", "--nointeractive", "--save", EXPORTER_NAME, "--label", "foo=bar"]
    )
    assert result.exit_code == 0
    assert "Exporter configuration successfully saved" in result.output
    save_exporter_mock.assert_called_once_with(INSECURE_TLS_EXPORTER_CONFIG, None)
    save_exporter_mock.reset_mock()

    # Insecure TLS config is returned
    _get_exporter_config_mock.return_value = INSECURE_TLS_EXPORTER_CONFIG
    # Save with prompts accept insecure = N
    result = runner.invoke(
        create, ["exporter", "--insecure-tls-config", EXPORTER_NAME, "--label", "foo=bar"], input="n\n"
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output

    # Save with prompts
    result = runner.invoke(create, ["exporter", EXPORTER_NAME, "--label", "foo=bar"], input="Y\n")
    assert result.exit_code == 0
    assert "Exporter configuration successfully saved" in result.output
    save_exporter_mock.assert_called_once_with(EXPORTER_CONFIG, None)
    save_exporter_mock.reset_mock()

    # Save with arguments
    result = runner.invoke(create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--save"])
    assert result.exit_code == 0
    assert "Exporter configuration successfully saved" in result.output
    save_exporter_mock.assert_called_once_with(EXPORTER_CONFIG, None)
    save_exporter_mock.reset_mock()

    # Save with arguments and custom path
    out = f"/tmp/{EXPORTER_NAME}.yaml"
    result = runner.invoke(create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--out", out])
    assert result.exit_code == 0
    assert "Exporter configuration successfully saved" in result.output
    save_exporter_mock.assert_called_once_with(EXPORTER_CONFIG, str(Path(out).resolve()))
    save_exporter_mock.reset_mock()

    # Save with nointeractive
    result = runner.invoke(create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--nointeractive"])
    assert result.exit_code == 0
    assert "Creating exporter" in result.output
    save_exporter_mock.assert_not_called()
    save_exporter_mock.reset_mock()

    # Save with JSON output
    result = runner.invoke(
        create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--nointeractive", "--output", "json"]
    )
    assert result.exit_code == 0
    assert json_equal(result.output, EXPORTER_JSON)
    save_exporter_mock.assert_not_called()
    save_exporter_mock.reset_mock()

    # Save with YAML output
    result = runner.invoke(
        create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--nointeractive", "--output", "yaml"]
    )
    assert result.exit_code == 0
    assert result.output == EXPORTER_YAML
    save_exporter_mock.assert_not_called()
    save_exporter_mock.reset_mock()

    # Save with name output
    result = runner.invoke(
        create, ["exporter", EXPORTER_NAME, "--label", "foo=bar", "--nointeractive", "--output", "name"]
    )
    assert result.exit_code == 0
    assert result.output == f"exporter.jumpstarter.dev/{EXPORTER_NAME}\n"
    save_exporter_mock.assert_not_called()
    save_exporter_mock.reset_mock()


class TestClusterCreation:
    """Test cluster creation commands."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_kind_minimal(self, mock_validate, mock_create):
        """Test creating a Kind cluster with minimal options"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind"])

        assert result.exit_code == 0
        assert "Creating kind cluster" in result.output
        mock_validate.assert_called_once_with("kind", None)
        mock_create.assert_called_once()

        # Verify the arguments passed to create_cluster_and_install
        args, kwargs = mock_create.call_args
        assert args[0] == "kind"  # cluster_type
        assert args[1] is False  # force_recreate_cluster
        assert args[2] == "test-cluster"  # cluster_name
        assert args[3] == ""  # kind_extra_args
        assert args[4] == ""  # minikube_extra_args
        assert args[5] == "kind"  # kind binary
        assert args[6] == "minikube"  # minikube binary

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_minikube_minimal(self, mock_validate, mock_create):
        """Test creating a Minikube cluster with minimal options"""
        mock_validate.return_value = "minikube"
        mock_create.return_value = None

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--minikube", "minikube"])

        assert result.exit_code == 0
        assert "Creating minikube cluster" in result.output
        mock_validate.assert_called_once_with(None, "minikube")
        mock_create.assert_called_once()

        # Verify the arguments passed to create_cluster_and_install
        args, kwargs = mock_create.call_args
        assert args[0] == "minikube"  # cluster_type
        assert args[1] is False  # force_recreate_cluster
        assert args[2] == "test-cluster"  # cluster_name

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_auto_detect(self, mock_validate, mock_create):
        """Test auto-detection of cluster type when neither --kind nor --minikube is specified"""
        mock_validate.return_value = "kind"  # Auto-detected as kind
        mock_create.return_value = None

        result = self.runner.invoke(create, ["cluster", "test-cluster"])

        assert result.exit_code == 0
        assert "Auto-detected kind as the cluster type" in result.output
        mock_validate.assert_called_once_with(None, None)
        mock_create.assert_called_once()

    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_both_types_error(self, mock_validate):
        """Test that specifying both --kind and --minikube raises an error"""
        mock_validate.side_effect = click.ClickException("You can only select one local cluster type")

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind", "--minikube", "minikube"])

        assert result.exit_code != 0
        assert "You can only select one local cluster type" in result.output
        mock_validate.assert_called_once_with("kind", "minikube")

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_with_jumpstarter_installation(self, mock_validate, mock_create):
        """Test creating cluster and installing Jumpstarter (default behavior)"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind"])

        assert result.exit_code == 0
        assert "and installing Jumpstarter" in result.output
        assert "created and Jumpstarter installed successfully" in result.output
        mock_create.assert_called_once()

        # Verify install_jumpstarter is True by default
        kwargs = mock_create.call_args[1]
        assert kwargs.get("install_jumpstarter", True) is True

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_skip_install(self, mock_validate, mock_create):
        """Test creating cluster with --skip-install flag"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind", "--skip-install"])

        assert result.exit_code == 0
        assert "Creating kind cluster" in result.output
        assert "is ready for Jumpstarter installation" in result.output
        mock_create.assert_called_once()

        # Verify install_jumpstarter is False
        kwargs = mock_create.call_args[1]
        assert kwargs.get("install_jumpstarter", True) is False

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_with_custom_chart(self, mock_validate, mock_create):
        """Test with custom chart and version options"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, [
            "cluster", "test-cluster", "--kind", "kind",
            "--chart", "oci://custom.registry/helm/chart",
            "--chart-name", "custom-jumpstarter",
            "--version", "v1.2.3"
        ])

        assert result.exit_code == 0
        mock_create.assert_called_once()

        # Verify custom chart options
        kwargs = mock_create.call_args[1]
        assert kwargs.get("chart") == "oci://custom.registry/helm/chart"
        assert kwargs.get("chart_name") == "custom-jumpstarter"
        assert kwargs.get("version") == "v1.2.3"

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_with_custom_endpoints(self, mock_validate, mock_create):
        """Test with custom IP, basedomain, and endpoints"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, [
            "cluster", "test-cluster", "--kind", "kind",
            "--ip", "192.168.1.100",
            "--basedomain", "custom.example.com",
            "--grpc-endpoint", "grpc.custom.example.com:9000",
            "--router-endpoint", "router.custom.example.com:9001"
        ])

        assert result.exit_code == 0
        mock_create.assert_called_once()

        # Verify custom endpoint options
        kwargs = mock_create.call_args[1]
        assert kwargs.get("ip") == "192.168.1.100"
        assert kwargs.get("basedomain") == "custom.example.com"
        assert kwargs.get("grpc_endpoint") == "grpc.custom.example.com:9000"
        assert kwargs.get("router_endpoint") == "router.custom.example.com:9001"

    @patch("jumpstarter_cli_admin.create.click.confirm")
    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_force_recreate_confirmed(self, mock_validate, mock_create, mock_confirm):
        """Test force recreate with user confirmation"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None
        mock_confirm.return_value = True

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind", "--force-recreate"])

        assert result.exit_code == 0
        mock_create.assert_called_once()

        # Verify force_recreate_cluster is True
        args = mock_create.call_args[0]
        assert args[1] is True  # force_recreate_cluster

    @patch("jumpstarter_cli_admin.create.click.confirm")
    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_force_recreate_cancelled(self, mock_validate, mock_create, mock_confirm):
        """Test force recreate when user cancels"""
        mock_validate.return_value = "kind"
        mock_create.side_effect = click.Abort()
        mock_confirm.return_value = False

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind", "--force-recreate"])

        assert result.exit_code != 0
        # Note: create_cluster_and_install itself handles the confirmation

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_with_extra_args(self, mock_validate, mock_create):
        """Test with extra Kind/Minikube arguments"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        result = self.runner.invoke(create, [
            "cluster", "test-cluster", "--kind", "kind",
            "--kind-extra-args", "--verbosity=1 --retain",
            "--minikube-extra-args", "--memory=4096"
        ])

        assert result.exit_code == 0
        mock_create.assert_called_once()

        # Verify extra args
        args = mock_create.call_args[0]
        assert args[3] == "--verbosity=1 --retain"  # kind_extra_args
        assert args[4] == "--memory=4096"  # minikube_extra_args

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_with_extra_certs(self, mock_validate, mock_create):
        """Test with custom CA certificates"""
        mock_validate.return_value = "kind"
        mock_create.return_value = None

        # Create a temporary cert file for the test
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.crt', delete=False) as f:
            f.write("dummy cert content")
            cert_path = f.name

        try:
            result = self.runner.invoke(create, [
                "cluster", "test-cluster", "--kind", "kind",
                "--extra-certs", cert_path
            ])

            assert result.exit_code == 0
            mock_create.assert_called_once()

            # Verify extra_certs parameter (it's positional arg 7, index 7)
            # Note: Click resolves the path, so we need to check the resolved version
            args = mock_create.call_args[0]
            import os
            assert args[7] == os.path.realpath(cert_path)  # extra_certs
        finally:
            # Clean up
            import os
            os.unlink(cert_path)

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_helm_not_installed(self, mock_validate, mock_create):
        """Test error when helm is not installed (for installation)"""
        mock_validate.return_value = "kind"
        mock_create.side_effect = click.ClickException("helm is not installed")

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind"])

        assert result.exit_code != 0
        assert "helm is not installed" in result.output

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_kind_not_installed(self, mock_validate, mock_create):
        """Test error when kind is not installed"""
        mock_validate.return_value = "kind"
        mock_create.side_effect = click.ClickException("kind is not installed")

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--kind", "kind"])

        assert result.exit_code != 0
        assert "kind is not installed" in result.output

    @patch("jumpstarter_cli_admin.create.create_cluster_and_install")
    @patch("jumpstarter_cli_admin.create.validate_cluster_type_selection")
    def test_create_cluster_minikube_not_installed(self, mock_validate, mock_create):
        """Test error when minikube is not installed"""
        mock_validate.return_value = "minikube"
        mock_create.side_effect = click.ClickException("minikube is not installed")

        result = self.runner.invoke(create, ["cluster", "test-cluster", "--minikube", "minikube"])

        assert result.exit_code != 0
        assert "minikube is not installed" in result.output
