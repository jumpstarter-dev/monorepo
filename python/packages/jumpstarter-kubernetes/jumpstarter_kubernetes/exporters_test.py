from kubernetes_asyncio.client.models import V1ObjectMeta, V1ObjectReference

from jumpstarter_kubernetes.exporters import V1Alpha1Exporter, V1Alpha1ExporterDevice, V1Alpha1ExporterStatus

TEST_EXPORTER = V1Alpha1Exporter(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Exporter",
    metadata=V1ObjectMeta(
        creation_timestamp="2021-10-01T00:00:00Z",
        generation=1,
        name="test-exporter",
        namespace="default",
        resource_version="1",
        uid="7a25eb81-6443-47ec-a62f-50165bffede8",
    ),
    status=V1Alpha1ExporterStatus(
        credential=V1ObjectReference(name="test-credential"),
        devices=[V1Alpha1ExporterDevice(labels={"test": "label"}, uuid="f4cf49ab-fc64-46c6-94e7-a40502eb77b1")],
        endpoint="https://test-exporter",
    ),
)


def test_exporter_dump_json():
    assert (
        TEST_EXPORTER.dump_json()
        == """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Exporter",
    "metadata": {
        "creationTimestamp": "2021-10-01T00:00:00Z",
        "generation": 1,
        "name": "test-exporter",
        "namespace": "default",
        "resourceVersion": "1",
        "uid": "7a25eb81-6443-47ec-a62f-50165bffede8"
    },
    "status": {
        "credential": {
            "name": "test-credential"
        },
        "devices": [
            {
                "labels": {
                    "test": "label"
                },
                "uuid": "f4cf49ab-fc64-46c6-94e7-a40502eb77b1"
            }
        ],
        "endpoint": "https://test-exporter",
        "exporterStatus": null,
        "statusMessage": null
    }
}"""
    )


def test_exporter_dump_yaml():
    assert (
        TEST_EXPORTER.dump_yaml()
        == """apiVersion: jumpstarter.dev/v1alpha1
kind: Exporter
metadata:
  creationTimestamp: '2021-10-01T00:00:00Z'
  generation: 1
  name: test-exporter
  namespace: default
  resourceVersion: '1'
  uid: 7a25eb81-6443-47ec-a62f-50165bffede8
status:
  credential:
    name: test-credential
  devices:
  - labels:
      test: label
    uuid: f4cf49ab-fc64-46c6-94e7-a40502eb77b1
  endpoint: https://test-exporter
  exporterStatus: null
  statusMessage: null
"""
    )


def test_exporter_from_dict():
    """Test V1Alpha1Exporter.from_dict"""
    from jumpstarter_kubernetes import V1Alpha1Exporter

    test_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Exporter",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-exporter",
            "namespace": "default",
            "resourceVersion": "1",
            "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
        },
        "status": {
            "credential": {"name": "test-credential"},
            "devices": [{"labels": {"test": "label"}, "uuid": "f4cf49ab-fc64-46c6-94e7-a40502eb77b1"}],
            "endpoint": "https://test-exporter",
        },
    }
    exporter = V1Alpha1Exporter.from_dict(test_dict)
    assert exporter.metadata.name == "test-exporter"
    assert exporter.status.endpoint == "https://test-exporter"
    assert len(exporter.status.devices) == 1
    assert exporter.status.devices[0].uuid == "f4cf49ab-fc64-46c6-94e7-a40502eb77b1"


def test_exporter_rich_add_columns_without_devices():
    """Test V1Alpha1Exporter.rich_add_columns without devices"""
    from unittest.mock import MagicMock

    from jumpstarter_kubernetes import V1Alpha1Exporter

    mock_table = MagicMock()
    V1Alpha1Exporter.rich_add_columns(mock_table, devices=False)
    assert mock_table.add_column.call_count == 5
    mock_table.add_column.assert_any_call("NAME", no_wrap=True)
    mock_table.add_column.assert_any_call("STATUS")
    mock_table.add_column.assert_any_call("ENDPOINT")
    mock_table.add_column.assert_any_call("DEVICES")
    mock_table.add_column.assert_any_call("AGE")


def test_exporter_rich_add_columns_with_devices():
    """Test V1Alpha1Exporter.rich_add_columns with devices"""
    from unittest.mock import MagicMock

    from jumpstarter_kubernetes import V1Alpha1Exporter

    mock_table = MagicMock()
    V1Alpha1Exporter.rich_add_columns(mock_table, devices=True)
    assert mock_table.add_column.call_count == 6
    mock_table.add_column.assert_any_call("NAME", no_wrap=True)
    mock_table.add_column.assert_any_call("STATUS")
    mock_table.add_column.assert_any_call("ENDPOINT")
    mock_table.add_column.assert_any_call("AGE")
    mock_table.add_column.assert_any_call("LABELS")
    mock_table.add_column.assert_any_call("UUID")


def test_exporter_rich_add_rows_without_devices():
    """Test V1Alpha1Exporter.rich_add_rows without devices flag"""
    from unittest.mock import MagicMock, patch

    mock_table = MagicMock()
    with patch("jumpstarter_kubernetes.exporters.time_since", return_value="5m"):
        TEST_EXPORTER.rich_add_rows(mock_table, devices=False)
    mock_table.add_row.assert_called_once()
    args = mock_table.add_row.call_args[0]
    assert args[0] == "test-exporter"
    assert args[1] == "Unknown"  # Status (shows "Unknown" when exporter_status is None)
    assert args[2] == "https://test-exporter"
    assert args[3] == "1"  # Number of devices
    assert args[4] == "5m"  # Age


def test_exporter_rich_add_rows_with_devices():
    """Test V1Alpha1Exporter.rich_add_rows with devices flag"""
    from unittest.mock import MagicMock, patch

    mock_table = MagicMock()
    with patch("jumpstarter_kubernetes.exporters.time_since", return_value="5m"):
        TEST_EXPORTER.rich_add_rows(mock_table, devices=True)
    mock_table.add_row.assert_called_once()
    args = mock_table.add_row.call_args[0]
    assert args[0] == "test-exporter"
    assert args[1] == "Unknown"  # Status (shows "Unknown" when exporter_status is None)
    assert args[2] == "https://test-exporter"
    assert args[3] == "5m"  # Age
    assert args[4] == "test:label"  # Labels
    assert args[5] == "f4cf49ab-fc64-46c6-94e7-a40502eb77b1"  # UUID


def test_exporter_rich_add_names():
    """Test V1Alpha1Exporter.rich_add_names"""
    names = []
    TEST_EXPORTER.rich_add_names(names)
    assert names == ["exporter.jumpstarter.dev/test-exporter"]


def test_exporter_list_from_dict():
    """Test V1Alpha1ExporterList.from_dict"""
    from jumpstarter_kubernetes import V1Alpha1ExporterList

    test_dict = {
        "items": [
            {
                "apiVersion": "jumpstarter.dev/v1alpha1",
                "kind": "Exporter",
                "metadata": {
                    "creationTimestamp": "2021-10-01T00:00:00Z",
                    "generation": 1,
                    "name": "exporter1",
                    "namespace": "default",
                    "resourceVersion": "1",
                    "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
                },
                "status": {
                    "credential": {"name": "cred1"},
                    "devices": [],
                    "endpoint": "https://exporter1",
                },
            }
        ]
    }
    exporter_list = V1Alpha1ExporterList.from_dict(test_dict)
    assert len(exporter_list.items) == 1
    assert exporter_list.items[0].metadata.name == "exporter1"


def test_exporter_list_rich_add_columns():
    """Test V1Alpha1ExporterList.rich_add_columns"""
    from unittest.mock import MagicMock

    from jumpstarter_kubernetes import V1Alpha1ExporterList

    mock_table = MagicMock()
    V1Alpha1ExporterList.rich_add_columns(mock_table, devices=False)
    assert mock_table.add_column.call_count == 5


def test_exporter_list_rich_add_columns_with_devices():
    """Test V1Alpha1ExporterList.rich_add_columns with devices"""
    from unittest.mock import MagicMock

    from jumpstarter_kubernetes import V1Alpha1ExporterList

    mock_table = MagicMock()
    V1Alpha1ExporterList.rich_add_columns(mock_table, devices=True)
    assert mock_table.add_column.call_count == 6


def test_exporter_list_rich_add_rows():
    """Test V1Alpha1ExporterList.rich_add_rows"""
    from unittest.mock import MagicMock, patch

    from jumpstarter_kubernetes import V1Alpha1ExporterList

    exporter_list = V1Alpha1ExporterList(items=[TEST_EXPORTER])
    mock_table = MagicMock()
    with patch("jumpstarter_kubernetes.exporters.time_since", return_value="5m"):
        exporter_list.rich_add_rows(mock_table, devices=False)
    assert mock_table.add_row.call_count == 1


def test_exporter_list_rich_add_rows_with_devices():
    """Test V1Alpha1ExporterList.rich_add_rows with devices"""
    from unittest.mock import MagicMock, patch

    from jumpstarter_kubernetes import V1Alpha1ExporterList

    exporter_list = V1Alpha1ExporterList(items=[TEST_EXPORTER])
    mock_table = MagicMock()
    with patch("jumpstarter_kubernetes.exporters.time_since", return_value="5m"):
        exporter_list.rich_add_rows(mock_table, devices=True)
    assert mock_table.add_row.call_count == 1


def test_exporter_list_rich_add_names():
    """Test V1Alpha1ExporterList.rich_add_names"""
    from jumpstarter_kubernetes import V1Alpha1ExporterList

    exporter_list = V1Alpha1ExporterList(items=[TEST_EXPORTER])
    names = []
    exporter_list.rich_add_names(names)
    assert names == ["exporter.jumpstarter.dev/test-exporter"]
