from unittest.mock import AsyncMock, patch

from click.testing import CliRunner
from jumpstarter_kubernetes import (
    ClientsV1Alpha1Api,
    ExportersV1Alpha1Api,
    LeasesV1Alpha1Api,
    V1Alpha1Client,
    V1Alpha1ClientList,
    V1Alpha1ClientStatus,
    V1Alpha1Exporter,
    V1Alpha1ExporterDevice,
    V1Alpha1ExporterList,
    V1Alpha1ExporterStatus,
    V1Alpha1Lease,
    V1Alpha1LeaseList,
    V1Alpha1LeaseSelector,
    V1Alpha1LeaseSpec,
    V1Alpha1LeaseStatus,
)
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import V1Condition, V1ObjectMeta, V1ObjectReference
from kubernetes_asyncio.config.config_exception import ConfigException

from jumpstarter_cli_admin.test_utils import json_equal

from .get import get


class MockResponse:
    status: int
    reason: str
    data: str

    def __init__(self, status: int, reason: str, body: str):
        self.status = status
        self.reason = reason
        self.data = body

    def getheaders(self):
        return {}


TEST_CLIENT = V1Alpha1Client(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Client",
    metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
    status=V1Alpha1ClientStatus(
        endpoint="grpc://example.com:443", credential=V1ObjectReference(name="test-credential")
    ),
)

TEST_CLIENT_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Client",
    "metadata": {
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "test",
        "namespace": "testing"
    },
    "status": {
        "credential": {
            "name": "test-credential"
        },
        "endpoint": "grpc://example.com:443"
    }
}
"""

TEST_CLIENT_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Client
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: test
  namespace: testing
status:
  credential:
    name: test-credential
  endpoint: grpc://example.com:443

"""


@patch.object(ClientsV1Alpha1Api, "get_client")
@patch.object(ClientsV1Alpha1Api, "_load_kube_config")
def test_get_client(_load_kube_config_mock, get_client_mock: AsyncMock):
    runner = CliRunner()

    # Get a single client
    get_client_mock.return_value = TEST_CLIENT
    result = runner.invoke(get, ["client", "test"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "grpc://example.com:443" in result.output
    get_client_mock.reset_mock()

    # Get a single client JSON output
    get_client_mock.return_value = TEST_CLIENT
    result = runner.invoke(get, ["client", "test", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, TEST_CLIENT_JSON)
    get_client_mock.reset_mock()

    # Get a single client YAML output
    get_client_mock.return_value = TEST_CLIENT
    result = runner.invoke(get, ["client", "test", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == TEST_CLIENT_YAML
    get_client_mock.reset_mock()

    # Get a single client name output
    get_client_mock.return_value = TEST_CLIENT
    result = runner.invoke(get, ["client", "test", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == "client.jumpstarter.dev/test\n"
    get_client_mock.reset_mock()

    # No client found
    get_client_mock.side_effect = ApiException(
        http_resp=MockResponse(
            404,
            "Not Found",
            '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"clients.jumpstarter.dev "test" not found","reason":"NotFound","details":{"name":"hello","group":"jumpstarter.dev","kind":"clients"},"code":404}',  # noqa: E501
        )
    )
    result = runner.invoke(get, ["client", "hello"])
    assert result.exit_code == 1
    assert "NotFound" in result.output
    get_client_mock.reset_mock()


CLIENTS_LIST = V1Alpha1ClientList(
    items=[
        V1Alpha1Client(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Client",
            metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ClientStatus(
                endpoint="grpc://example.com:443", credential=V1ObjectReference(name="test-credential")
            ),
        ),
        V1Alpha1Client(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Client",
            metadata=V1ObjectMeta(name="another", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ClientStatus(
                endpoint="grpc://example.com:443", credential=V1ObjectReference(name="another-credential")
            ),
        ),
    ]
)

CLIENTS_LIST_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "items": [
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Client",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "test",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "test-credential"
                },
                "endpoint": "grpc://example.com:443"
            }
        },
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Client",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "another",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "another-credential"
                },
                "endpoint": "grpc://example.com:443"
            }
        }
    ],
    "kind": "ClientList"
}
"""

CLIENTS_LIST_EMPTY_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "items": [],
    "kind": "ClientList"
}
"""

CLIENTS_LIST_YAML = """apiVersion: jumpstarter.dev/v1alpha1
items:
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Client
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: test
    namespace: testing
  status:
    credential:
      name: test-credential
    endpoint: grpc://example.com:443
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Client
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: another
    namespace: testing
  status:
    credential:
      name: another-credential
    endpoint: grpc://example.com:443
kind: ClientList

"""

CLIENTS_LIST_NAME = """client.jumpstarter.dev/test
client.jumpstarter.dev/another
"""

CLIENTS_LIST_EMPTY_YAML = """apiVersion: jumpstarter.dev/v1alpha1
items: []
kind: ClientList

"""


@patch.object(ClientsV1Alpha1Api, "list_clients")
@patch.object(ClientsV1Alpha1Api, "_load_kube_config")
def test_get_clients(_load_kube_config_mock, list_clients_mock: AsyncMock):
    runner = CliRunner()

    # List clients
    list_clients_mock.return_value = CLIENTS_LIST
    result = runner.invoke(get, ["clients"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "another" in result.output
    assert "grpc://example.com:443" in result.output
    list_clients_mock.reset_mock()

    # List clients JSON output
    list_clients_mock.return_value = CLIENTS_LIST
    result = runner.invoke(get, ["clients", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, CLIENTS_LIST_JSON)
    list_clients_mock.reset_mock()

    # List clients YAML output
    list_clients_mock.return_value = CLIENTS_LIST
    result = runner.invoke(get, ["clients", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == CLIENTS_LIST_YAML
    list_clients_mock.reset_mock()

    # List clients name output
    list_clients_mock.return_value = CLIENTS_LIST
    result = runner.invoke(get, ["clients", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == CLIENTS_LIST_NAME
    list_clients_mock.reset_mock()

    # No clients found
    list_clients_mock.return_value = V1Alpha1ClientList(items=[])
    result = runner.invoke(get, ["clients"])
    assert result.exit_code == 0
    assert "No resources found" in result.output
    list_clients_mock.reset_mock()

    # No clients found JSON output
    list_clients_mock.return_value = V1Alpha1ClientList(items=[])
    result = runner.invoke(get, ["clients", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, CLIENTS_LIST_EMPTY_JSON)
    list_clients_mock.reset_mock()

    # No clients found YAML output
    list_clients_mock.return_value = V1Alpha1ClientList(items=[])
    result = runner.invoke(get, ["clients", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == CLIENTS_LIST_EMPTY_YAML
    list_clients_mock.reset_mock()

    # No clients found name output
    list_clients_mock.return_value = V1Alpha1ClientList(items=[])
    result = runner.invoke(get, ["clients", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == ""
    list_clients_mock.reset_mock()


TEST_EXPORTER = V1Alpha1Exporter(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Exporter",
    metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
    status=V1Alpha1ExporterStatus(
        endpoint="grpc://example.com:443",
        credential=V1ObjectReference(name="test-credential"),
        devices=[],
        exporter_status="Available",
    ),
)

TEST_EXPORTER_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Exporter",
    "metadata": {
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "test",
        "namespace": "testing"
    },
    "status": {
        "credential": {
            "name": "test-credential"
        },
        "devices": [],
        "endpoint": "grpc://example.com:443",
        "exporterStatus": "Available",
        "statusMessage": null
    }
}
"""

TEST_EXPORTER_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Exporter
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: test
  namespace: testing
status:
  credential:
    name: test-credential
  devices: []
  endpoint: grpc://example.com:443
  exporterStatus: Available
  statusMessage: null

"""


@patch.object(ExportersV1Alpha1Api, "get_exporter")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
def test_get_exporter(_load_kube_config_mock, get_exporter_mock: AsyncMock):
    runner = CliRunner()

    # Get a single exporter
    get_exporter_mock.return_value = TEST_EXPORTER
    result = runner.invoke(get, ["exporter", "test"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "Available" in result.output
    assert "grpc://example.com:443" in result.output
    get_exporter_mock.reset_mock()

    # Get a single exporter JSON output
    get_exporter_mock.return_value = TEST_EXPORTER
    result = runner.invoke(get, ["exporter", "test", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, TEST_EXPORTER_JSON)
    get_exporter_mock.reset_mock()

    # Get a single exporter YAML output
    get_exporter_mock.return_value = TEST_EXPORTER
    result = runner.invoke(get, ["exporter", "test", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == TEST_EXPORTER_YAML
    get_exporter_mock.reset_mock()

    # Get a single exporter name output
    get_exporter_mock.return_value = TEST_EXPORTER
    result = runner.invoke(get, ["exporter", "test", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == "exporter.jumpstarter.dev/test\n"
    get_exporter_mock.reset_mock()

    # No exporter found
    get_exporter_mock.side_effect = ApiException(
        http_resp=MockResponse(
            404,
            "Not Found",
            '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"exporters.jumpstarter.dev "test" not found","reason":"NotFound","details":{"name":"hello","group":"jumpstarter.dev","kind":"exporters"},"code":404}',  # noqa: E501
        )
    )
    result = runner.invoke(get, ["exporter", "hello"])
    assert result.exit_code == 1
    assert "NotFound" in result.output


TEST_EXPORTER_DEVICES = V1Alpha1Exporter(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Exporter",
    metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
    status=V1Alpha1ExporterStatus(
        endpoint="grpc://example.com:443",
        credential=V1ObjectReference(name="test-credential"),
        devices=[
            V1Alpha1ExporterDevice(labels={"hardware": "rpi4"}, uuid="82a8ac0d-d7ff-4009-8948-18a3c5c607b1"),
            V1Alpha1ExporterDevice(labels={"hardware": "rpi4"}, uuid="f7cd30ac-64a3-42c6-ba31-b25f033b97c1"),
        ],
        exporter_status="Available",
    ),
)

TEST_EXPORTER_DEVICES_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Exporter",
    "metadata": {
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "test",
        "namespace": "testing"
    },
    "status": {
        "credential": {
            "name": "test-credential"
        },
        "devices": [
            {
                "labels": {
                    "hardware": "rpi4"
                },
                "uuid": "82a8ac0d-d7ff-4009-8948-18a3c5c607b1"
            },
            {
                "labels": {
                    "hardware": "rpi4"
                },
                "uuid": "f7cd30ac-64a3-42c6-ba31-b25f033b97c1"
            }
        ],
        "endpoint": "grpc://example.com:443",
        "exporterStatus": "Available",
        "statusMessage": null
    }
}
"""

TEST_EXPORTER_DEVICES_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Exporter
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: test
  namespace: testing
status:
  credential:
    name: test-credential
  devices:
  - labels:
      hardware: rpi4
    uuid: 82a8ac0d-d7ff-4009-8948-18a3c5c607b1
  - labels:
      hardware: rpi4
    uuid: f7cd30ac-64a3-42c6-ba31-b25f033b97c1
  endpoint: grpc://example.com:443
  exporterStatus: Available
  statusMessage: null

"""


@patch.object(ExportersV1Alpha1Api, "get_exporter")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
def test_get_exporter_devices(_load_kube_config_mock, get_exporter_mock: AsyncMock):
    runner = CliRunner()
    # Returns exporter
    get_exporter_mock.return_value = TEST_EXPORTER_DEVICES
    result = runner.invoke(get, ["exporter", "test", "--devices"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "Available" in result.output
    assert "grpc://example.com:443" in result.output
    assert "hardware:rpi4" in result.output
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b1" in result.output
    assert "f7cd30ac-64a3-42c6-ba31-b25f033b97c1" in result.output
    get_exporter_mock.reset_mock()

    # Returns exporter JSON output
    get_exporter_mock.return_value = TEST_EXPORTER_DEVICES
    result = runner.invoke(get, ["exporter", "test", "--devices", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, TEST_EXPORTER_DEVICES_JSON)
    get_exporter_mock.reset_mock()

    # Returns exporter YAML output
    get_exporter_mock.return_value = TEST_EXPORTER_DEVICES
    result = runner.invoke(get, ["exporter", "test", "--devices", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == TEST_EXPORTER_DEVICES_YAML
    get_exporter_mock.reset_mock()

    # Returns exporter name output
    get_exporter_mock.return_value = TEST_EXPORTER_DEVICES
    result = runner.invoke(get, ["exporter", "test", "--devices", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == "exporter.jumpstarter.dev/test\n"
    get_exporter_mock.reset_mock()

    # No exporter found
    get_exporter_mock.side_effect = ApiException(
        http_resp=MockResponse(
            404,
            "Not Found",
            '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"exporters.jumpstarter.dev "test" not found","reason":"NotFound","details":{"name":"hello","group":"jumpstarter.dev","kind":"exporters"},"code":404}',  # noqa: E501
        )
    )
    result = runner.invoke(get, ["exporter", "hello", "--devices"])
    assert result.exit_code == 1
    assert "NotFound" in result.output


EXPORTERS_LIST = V1Alpha1ExporterList(
    items=[
        V1Alpha1Exporter(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Exporter",
            metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ExporterStatus(
                endpoint="grpc://example.com:443",
                credential=V1ObjectReference(name="test-credential"),
                devices=[],
                exporter_status="Available",
            ),
        ),
        V1Alpha1Exporter(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Exporter",
            metadata=V1ObjectMeta(name="another", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ExporterStatus(
                endpoint="grpc://example.com:443",
                credential=V1ObjectReference(name="another-credential"),
                devices=[],
                exporter_status="Available",
            ),
        ),
    ]
)

EXPORTERS_LIST_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "items": [
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Exporter",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "test",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "test-credential"
                },
                "devices": [],
                "endpoint": "grpc://example.com:443",
                "exporterStatus": "Available",
                "statusMessage": null
            }
        },
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Exporter",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "another",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "another-credential"
                },
                "devices": [],
                "endpoint": "grpc://example.com:443",
                "exporterStatus": "Available",
                "statusMessage": null
            }
        }
    ],
    "kind": "ExporterList"
}
"""

EXPORTERS_LIST_YAML = """apiVersion: jumpstarter.dev/v1alpha1
items:
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Exporter
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: test
    namespace: testing
  status:
    credential:
      name: test-credential
    devices: []
    endpoint: grpc://example.com:443
    exporterStatus: Available
    statusMessage: null
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Exporter
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: another
    namespace: testing
  status:
    credential:
      name: another-credential
    devices: []
    endpoint: grpc://example.com:443
    exporterStatus: Available
    statusMessage: null
kind: ExporterList

"""

EXPORTERS_LIST_NAME = """exporter.jumpstarter.dev/test
exporter.jumpstarter.dev/another
"""


@patch.object(ExportersV1Alpha1Api, "list_exporters")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
def test_get_exporters(_load_kube_config_mock, list_exporters_mock: AsyncMock):
    runner = CliRunner()

    # List exporters
    list_exporters_mock.return_value = EXPORTERS_LIST
    result = runner.invoke(get, ["exporters"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "another" in result.output
    assert "Available" in result.output
    list_exporters_mock.reset_mock()

    # List exporters JSON output
    list_exporters_mock.return_value = EXPORTERS_LIST
    result = runner.invoke(get, ["exporters", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, EXPORTERS_LIST_JSON)
    list_exporters_mock.reset_mock()

    # List exporters YAML output
    list_exporters_mock.return_value = EXPORTERS_LIST
    result = runner.invoke(get, ["exporters", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == EXPORTERS_LIST_YAML
    list_exporters_mock.reset_mock()

    # List exporters name output
    list_exporters_mock.return_value = EXPORTERS_LIST
    result = runner.invoke(get, ["exporters", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == EXPORTERS_LIST_NAME
    list_exporters_mock.reset_mock()

    # No exporters found
    with patch.object(
        ExportersV1Alpha1Api,
        "list_exporters",
        return_value=V1Alpha1ExporterList(items=[]),
    ):
        result = runner.invoke(get, ["exporters"])
        assert result.exit_code == 0
        assert "No resources found" in result.output


EXPORTER_DEVICES_LIST = V1Alpha1ExporterList(
    items=[
        V1Alpha1Exporter(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Exporter",
            metadata=V1ObjectMeta(name="test", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ExporterStatus(
                endpoint="grpc://example.com:443",
                credential=V1ObjectReference(name="test-credential"),
                devices=[
                    V1Alpha1ExporterDevice(labels={"hardware": "rpi4"}, uuid="82a8ac0d-d7ff-4009-8948-18a3c5c607b1")
                ],
                exporter_status="Available",
            ),
        ),
        V1Alpha1Exporter(
            api_version="jumpstarter.dev/v1alpha1",
            kind="Exporter",
            metadata=V1ObjectMeta(name="another", namespace="testing", creation_timestamp="2024-01-01T21:00:00Z"),
            status=V1Alpha1ExporterStatus(
                endpoint="grpc://example.com:443",
                credential=V1ObjectReference(name="another-credential"),
                devices=[
                    V1Alpha1ExporterDevice(labels={"hardware": "rpi4"}, uuid="f7cd30ac-64a3-42c6-ba31-b25f033b97c1"),
                ],
                exporter_status="Available",
            ),
        ),
    ]
)

EXPORTERS_DEVICES_LIST_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "items": [
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Exporter",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "test",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "test-credential"
                },
                "devices": [
                    {
                        "labels": {
                            "hardware": "rpi4"
                        },
                        "uuid": "82a8ac0d-d7ff-4009-8948-18a3c5c607b1"
                    }
                ],
                "endpoint": "grpc://example.com:443",
                "exporterStatus": "Available",
                "statusMessage": null
            }
        },
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Exporter",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "another",
                "namespace": "testing"
            },
            "status": {
                "credential": {
                    "name": "another-credential"
                },
                "devices": [
                    {
                        "labels": {
                            "hardware": "rpi4"
                        },
                        "uuid": "f7cd30ac-64a3-42c6-ba31-b25f033b97c1"
                    }
                ],
                "endpoint": "grpc://example.com:443",
                "exporterStatus": "Available",
                "statusMessage": null
            }
        }
    ],
    "kind": "ExporterList"
}
"""

EXPORTERS_DEVICES_LIST_YAML = """apiVersion: jumpstarter.dev/v1alpha1
items:
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Exporter
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: test
    namespace: testing
  status:
    credential:
      name: test-credential
    devices:
    - labels:
        hardware: rpi4
      uuid: 82a8ac0d-d7ff-4009-8948-18a3c5c607b1
    endpoint: grpc://example.com:443
    exporterStatus: Available
    statusMessage: null
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Exporter
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: another
    namespace: testing
  status:
    credential:
      name: another-credential
    devices:
    - labels:
        hardware: rpi4
      uuid: f7cd30ac-64a3-42c6-ba31-b25f033b97c1
    endpoint: grpc://example.com:443
    exporterStatus: Available
    statusMessage: null
kind: ExporterList

"""

EXPORTERS_DEVICES_LIST_NAME = EXPORTERS_LIST_NAME


@patch.object(ExportersV1Alpha1Api, "list_exporters")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
def test_get_exporters_devices(_load_kube_config_mock, list_exporters_mock: AsyncMock):
    runner = CliRunner()

    # List exporters
    list_exporters_mock.return_value = EXPORTER_DEVICES_LIST
    result = runner.invoke(get, ["exporters", "--devices"])
    assert result.exit_code == 0
    assert "test" in result.output
    assert "another" in result.output
    assert "Available" in result.output
    assert "hardware:rpi4" in result.output
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b1" in result.output
    assert "f7cd30ac-64a3-42c6-ba31-b25f033b97c1" in result.output
    list_exporters_mock.reset_mock()

    # List exporters JSON output
    list_exporters_mock.return_value = EXPORTER_DEVICES_LIST
    result = runner.invoke(get, ["exporters", "--devices", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, EXPORTERS_DEVICES_LIST_JSON)
    list_exporters_mock.reset_mock()

    # List exporters YAML output
    list_exporters_mock.return_value = EXPORTER_DEVICES_LIST
    result = runner.invoke(get, ["exporters", "--devices", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == EXPORTERS_DEVICES_LIST_YAML
    list_exporters_mock.reset_mock()

    # List exporters name output
    list_exporters_mock.return_value = EXPORTER_DEVICES_LIST
    result = runner.invoke(get, ["exporters", "--devices", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == EXPORTERS_DEVICES_LIST_NAME
    list_exporters_mock.reset_mock()

    # No exporters found
    list_exporters_mock.return_value = V1Alpha1ExporterList(items=[])
    result = runner.invoke(get, ["exporters", "--devices"])
    assert result.exit_code == 0
    assert "No resources found" in result.output


IN_PROGRESS_LEASE = V1Alpha1Lease(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Lease",
    metadata=V1ObjectMeta(
        name="82a8ac0d-d7ff-4009-8948-18a3c5c607b1",
        namespace="testing",
        creation_timestamp="2024-01-01T21:00:00Z",
    ),
    status=V1Alpha1LeaseStatus(
        begin_time="2024-01-01T21:00:00Z",
        end_time=None,
        ended=False,
        exporter=V1ObjectReference(name="test_exporter"),
        conditions=[
            V1Condition(
                last_transition_time="2024-01-01T21:00:00Z",
                message="",
                observed_generation=1,
                reason="Ready",
                status="True",
                type="Ready",
            )
        ],
    ),
    spec=V1Alpha1LeaseSpec(
        client=V1ObjectReference(name="test_client"),
        duration="5m",
        selector=V1Alpha1LeaseSelector(match_labels={"hardware": "rpi4"}),
    ),
)

FINISHED_LEASE = V1Alpha1Lease(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Lease",
    metadata=V1ObjectMeta(
        name="82a8ac0d-d7ff-4009-8948-18a3c5c607b2",
        namespace="testing",
        creation_timestamp="2024-01-01T21:00:00Z",
    ),
    status=V1Alpha1LeaseStatus(
        begin_time="2024-01-01T21:00:00Z",
        end_time="2024-01-01T22:00:00Z",
        ended=True,
        exporter=V1ObjectReference(name="test_exporter"),
        conditions=[
            V1Condition(
                last_transition_time="2024-01-01T22:00:00Z",
                message="",
                observed_generation=1,
                reason="Expired",
                status="False",
                type="Ready",
            )
        ],
    ),
    spec=V1Alpha1LeaseSpec(
        client=V1ObjectReference(name="test_client"),
        duration="1h",
        selector=V1Alpha1LeaseSelector(match_labels={}),
    ),
)

FINISHED_LEASE_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Lease",
    "metadata": {
        "creationTimestamp": "2024-01-01T21:00:00Z",
        "name": "82a8ac0d-d7ff-4009-8948-18a3c5c607b2",
        "namespace": "testing"
    },
    "spec": {
        "client": {
            "name": "test_client"
        },
        "duration": "1h",
        "selector": {
            "matchLabels": {}
        }
    },
    "status": {
        "beginTime": "2024-01-01T21:00:00Z",
        "conditions": [
            {
                "lastTransitionTime": "2024-01-01T22:00:00Z",
                "message": "",
                "observedGeneration": 1,
                "reason": "Expired",
                "status": "False",
                "type": "Ready"
            }
        ],
        "endTime": "2024-01-01T22:00:00Z",
        "ended": true,
        "exporter": {
            "name": "test_exporter"
        }
    }
}
"""

FINISHED_LEASE_YAML = """apiVersion: jumpstarter.dev/v1alpha1
kind: Lease
metadata:
  creationTimestamp: '2024-01-01T21:00:00Z'
  name: 82a8ac0d-d7ff-4009-8948-18a3c5c607b2
  namespace: testing
spec:
  client:
    name: test_client
  duration: 1h
  selector:
    matchLabels: {}
status:
  beginTime: '2024-01-01T21:00:00Z'
  conditions:
  - lastTransitionTime: '2024-01-01T22:00:00Z'
    message: ''
    observedGeneration: 1
    reason: Expired
    status: 'False'
    type: Ready
  endTime: '2024-01-01T22:00:00Z'
  ended: true
  exporter:
    name: test_exporter

"""


@patch.object(LeasesV1Alpha1Api, "get_lease")
@patch.object(LeasesV1Alpha1Api, "_load_kube_config")
def test_get_lease(_load_kube_config_mock, get_lease_mock: AsyncMock):
    runner = CliRunner()

    # Get an in progress lease
    get_lease_mock.return_value = IN_PROGRESS_LEASE
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b1"])
    assert result.exit_code == 0
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b1" in result.output
    assert "test_client" in result.output
    assert "test_exporter" in result.output
    assert "hardware:rpi4" in result.output
    assert "InProgress" in result.output
    assert "Ready" in result.output
    assert "2024-01-01T21:00:00Z" in result.output
    assert "5m" in result.output
    get_lease_mock.reset_mock()

    # Get a finished lease
    get_lease_mock.return_value = FINISHED_LEASE
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b2"])
    assert result.exit_code == 0
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b2" in result.output
    assert "test_client" in result.output
    assert "test_exporter" in result.output
    assert "*" in result.output
    assert "Ended" in result.output
    assert "Complete" in result.output
    assert "2024-01-01T21:00:00Z" in result.output
    assert "2024-01-01T22:00:00Z" in result.output
    assert "1h" in result.output
    get_lease_mock.reset_mock()

    # Get a finished lease JSON output
    get_lease_mock.return_value = FINISHED_LEASE
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b2", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, FINISHED_LEASE_JSON)
    get_lease_mock.reset_mock()

    # Get a finished lease YAML output
    get_lease_mock.return_value = FINISHED_LEASE
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b2", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == FINISHED_LEASE_YAML
    get_lease_mock.reset_mock()

    # Get a finished lease name output
    get_lease_mock.return_value = FINISHED_LEASE
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b2", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == "lease.jumpstarter.dev/82a8ac0d-d7ff-4009-8948-18a3c5c607b2\n"
    get_lease_mock.reset_mock()

    # No lease found
    get_lease_mock.side_effect = ApiException(
        http_resp=MockResponse(
            404,
            "Not Found",
            '{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"leases.jumpstarter.dev "82a8ac0d-d7ff-4009-8948-18a3c5c607b1" not found","reason":"NotFound","details":{"name":"82a8ac0d-d7ff-4009-8948-18a3c5c607b1","group":"jumpstarter.dev","kind":"leases"},"code":404}',  # noqa: E501
        )
    )
    result = runner.invoke(get, ["lease", "82a8ac0d-d7ff-4009-8948-18a3c5c607b1"])
    assert result.exit_code == 1
    assert "NotFound" in result.output


LEASES_LIST_JSON = """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "items": [
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Lease",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "82a8ac0d-d7ff-4009-8948-18a3c5c607b1",
                "namespace": "testing"
            },
            "spec": {
                "client": {
                    "name": "test_client"
                },
                "duration": "5m",
                "selector": {
                    "matchLabels": {
                        "hardware": "rpi4"
                    }
                }
            },
            "status": {
                "beginTime": "2024-01-01T21:00:00Z",
                "conditions": [
                    {
                        "lastTransitionTime": "2024-01-01T21:00:00Z",
                        "message": "",
                        "observedGeneration": 1,
                        "reason": "Ready",
                        "status": "True",
                        "type": "Ready"
                    }
                ],
                "endTime": null,
                "ended": false,
                "exporter": {
                    "name": "test_exporter"
                }
            }
        },
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Lease",
            "metadata": {
                "creationTimestamp": "2024-01-01T21:00:00Z",
                "name": "82a8ac0d-d7ff-4009-8948-18a3c5c607b2",
                "namespace": "testing"
            },
            "spec": {
                "client": {
                    "name": "test_client"
                },
                "duration": "1h",
                "selector": {
                    "matchLabels": {}
                }
            },
            "status": {
                "beginTime": "2024-01-01T21:00:00Z",
                "conditions": [
                    {
                        "lastTransitionTime": "2024-01-01T22:00:00Z",
                        "message": "",
                        "observedGeneration": 1,
                        "reason": "Expired",
                        "status": "False",
                        "type": "Ready"
                    }
                ],
                "endTime": "2024-01-01T22:00:00Z",
                "ended": true,
                "exporter": {
                    "name": "test_exporter"
                }
            }
        }
    ],
    "kind": "LeaseList"
}
"""

LEASES_LIST_YAML = """apiVersion: jumpstarter.dev/v1alpha1
items:
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Lease
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: 82a8ac0d-d7ff-4009-8948-18a3c5c607b1
    namespace: testing
  spec:
    client:
      name: test_client
    duration: 5m
    selector:
      matchLabels:
        hardware: rpi4
  status:
    beginTime: '2024-01-01T21:00:00Z'
    conditions:
    - lastTransitionTime: '2024-01-01T21:00:00Z'
      message: ''
      observedGeneration: 1
      reason: Ready
      status: 'True'
      type: Ready
    endTime: null
    ended: false
    exporter:
      name: test_exporter
- apiVersion: jumpstarter.dev/v1alpha1
  kind: Lease
  metadata:
    creationTimestamp: '2024-01-01T21:00:00Z'
    name: 82a8ac0d-d7ff-4009-8948-18a3c5c607b2
    namespace: testing
  spec:
    client:
      name: test_client
    duration: 1h
    selector:
      matchLabels: {}
  status:
    beginTime: '2024-01-01T21:00:00Z'
    conditions:
    - lastTransitionTime: '2024-01-01T22:00:00Z'
      message: ''
      observedGeneration: 1
      reason: Expired
      status: 'False'
      type: Ready
    endTime: '2024-01-01T22:00:00Z'
    ended: true
    exporter:
      name: test_exporter
kind: LeaseList

"""

LEASES_LIST_NAME = """lease.jumpstarter.dev/82a8ac0d-d7ff-4009-8948-18a3c5c607b1
lease.jumpstarter.dev/82a8ac0d-d7ff-4009-8948-18a3c5c607b2
"""


@patch.object(LeasesV1Alpha1Api, "list_leases")
@patch.object(LeasesV1Alpha1Api, "_load_kube_config")
def test_get_leases(_load_kube_config_mock, list_leases_mock: AsyncMock):
    runner = CliRunner()

    # Found leases
    list_leases_mock.return_value = V1Alpha1LeaseList(items=[IN_PROGRESS_LEASE, FINISHED_LEASE])
    result = runner.invoke(get, ["leases"])
    assert result.exit_code == 0
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b1" in result.output
    assert "82a8ac0d-d7ff-4009-8948-18a3c5c607b2" in result.output
    assert "test_client" in result.output
    assert "test_exporter" in result.output
    assert "hardware:rpi4" in result.output
    assert "*" in result.output
    assert "InProgress" in result.output
    assert "Ended" in result.output
    assert "Complete" in result.output
    assert "Ready" in result.output
    assert "2024-01-01T21:00:00Z" in result.output
    assert "5m" in result.output
    assert "1h" in result.output
    list_leases_mock.reset_mock()

    # Found leases JSON output
    list_leases_mock.return_value = V1Alpha1LeaseList(items=[IN_PROGRESS_LEASE, FINISHED_LEASE])
    result = runner.invoke(get, ["leases", "--output", "json"])
    assert result.exit_code == 0
    assert json_equal(result.output, LEASES_LIST_JSON)
    list_leases_mock.reset_mock()

    # Found leases YAML output
    list_leases_mock.return_value = V1Alpha1LeaseList(items=[IN_PROGRESS_LEASE, FINISHED_LEASE])
    result = runner.invoke(get, ["leases", "--output", "yaml"])
    assert result.exit_code == 0
    assert result.output == LEASES_LIST_YAML
    list_leases_mock.reset_mock()

    # Found leases name output
    list_leases_mock.return_value = V1Alpha1LeaseList(items=[IN_PROGRESS_LEASE, FINISHED_LEASE])
    result = runner.invoke(get, ["leases", "--output", "name"])
    assert result.exit_code == 0
    assert result.output == LEASES_LIST_NAME
    list_leases_mock.reset_mock()

    # No leases found
    list_leases_mock.return_value = V1Alpha1LeaseList(items=[])
    result = runner.invoke(get, ["leases"])
    assert result.exit_code == 0
    assert "No resources found" in result.output


# Test ConfigException handling
@patch.object(ClientsV1Alpha1Api, "get_client")
@patch.object(ClientsV1Alpha1Api, "_load_kube_config")
def test_get_client_config_exception(_load_kube_config_mock, get_client_mock: AsyncMock):
    runner = CliRunner()
    get_client_mock.side_effect = ConfigException("Invalid kubeconfig")
    result = runner.invoke(get, ["client", "test"])
    assert result.exit_code == 1
    assert "kubeconfig" in result.output.lower()


@patch.object(ExportersV1Alpha1Api, "get_exporter")
@patch.object(ExportersV1Alpha1Api, "_load_kube_config")
def test_get_exporter_config_exception(_load_kube_config_mock, get_exporter_mock: AsyncMock):
    runner = CliRunner()
    get_exporter_mock.side_effect = ConfigException("Invalid kubeconfig")
    result = runner.invoke(get, ["exporter", "test"])
    assert result.exit_code == 1
    assert "kubeconfig" in result.output.lower()


@patch.object(LeasesV1Alpha1Api, "get_lease")
@patch.object(LeasesV1Alpha1Api, "_load_kube_config")
def test_get_lease_config_exception(_load_kube_config_mock, get_lease_mock: AsyncMock):
    runner = CliRunner()
    get_lease_mock.side_effect = ConfigException("Invalid kubeconfig")
    result = runner.invoke(get, ["lease", "test"])
    assert result.exit_code == 1
    assert "kubeconfig" in result.output.lower()


# Test get cluster commands
@patch("jumpstarter_cli_admin.get.get_cluster_info")
def test_get_cluster_by_name(get_cluster_info_mock: AsyncMock):
    from jumpstarter_kubernetes import V1Alpha1ClusterInfo, V1Alpha1JumpstarterInstance

    runner = CliRunner()
    cluster_info = V1Alpha1ClusterInfo(
        name="kind-test",
        cluster="kind-kind-test",
        server="https://127.0.0.1:6443",
        user="kind-kind-test",
        namespace="default",
        is_current=True,
        type="kind",
        accessible=True,
        version="1.28.0",
        jumpstarter=V1Alpha1JumpstarterInstance(installed=True, version="0.1.0", namespace="jumpstarter"),
    )
    get_cluster_info_mock.return_value = cluster_info
    result = runner.invoke(get, ["cluster", "kind-test"])
    assert result.exit_code == 0
    assert "kind-test" in result.output


@patch("jumpstarter_cli_admin.get.get_cluster_info")
def test_get_cluster_not_found(get_cluster_info_mock: AsyncMock):
    from jumpstarter_kubernetes import V1Alpha1ClusterInfo, V1Alpha1JumpstarterInstance

    runner = CliRunner()
    cluster_info = V1Alpha1ClusterInfo(
        name="nonexistent",
        cluster="nonexistent",
        server="",
        user="",
        namespace="default",
        is_current=False,
        type="remote",
        accessible=False,
        jumpstarter=V1Alpha1JumpstarterInstance(installed=False),
        error="context not found",
    )
    get_cluster_info_mock.return_value = cluster_info
    result = runner.invoke(get, ["cluster", "nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


@patch("jumpstarter_cli_admin.get.get_cluster_info")
def test_get_cluster_error(get_cluster_info_mock: AsyncMock):
    runner = CliRunner()
    get_cluster_info_mock.side_effect = Exception("Unexpected error")
    result = runner.invoke(get, ["cluster", "test"])
    assert result.exit_code == 1
    assert "error" in result.output.lower()


@patch("jumpstarter_cli_admin.get.list_clusters")
def test_get_clusters_list(list_clusters_mock: AsyncMock):
    from jumpstarter_kubernetes import V1Alpha1ClusterInfo, V1Alpha1ClusterList, V1Alpha1JumpstarterInstance

    runner = CliRunner()
    cluster_list = V1Alpha1ClusterList(
        items=[
            V1Alpha1ClusterInfo(
                name="kind-test",
                cluster="kind-kind-test",
                server="https://127.0.0.1:6443",
                user="kind-kind-test",
                namespace="default",
                is_current=True,
                type="kind",
                accessible=True,
                version="1.28.0",
                jumpstarter=V1Alpha1JumpstarterInstance(installed=True, version="0.1.0", namespace="jumpstarter"),
            ),
            V1Alpha1ClusterInfo(
                name="minikube",
                cluster="minikube",
                server="https://192.168.49.2:8443",
                user="minikube",
                namespace="default",
                is_current=False,
                type="minikube",
                accessible=True,
                version="1.28.0",
                jumpstarter=V1Alpha1JumpstarterInstance(installed=False),
            ),
        ]
    )
    list_clusters_mock.return_value = cluster_list
    result = runner.invoke(get, ["clusters"])
    assert result.exit_code == 0
    assert "kind-test" in result.output
    assert "minikube" in result.output


@patch("jumpstarter_cli_admin.get.list_clusters")
def test_get_clusters_error(list_clusters_mock: AsyncMock):
    runner = CliRunner()
    list_clusters_mock.side_effect = Exception("Unexpected error")
    result = runner.invoke(get, ["clusters"])
    assert result.exit_code == 1
    assert "error" in result.output.lower()
