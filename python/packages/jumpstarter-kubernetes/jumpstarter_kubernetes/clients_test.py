import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import V1ConfigMap, V1ObjectMeta, V1Secret

from jumpstarter_kubernetes import V1Alpha1Client, V1Alpha1ClientStatus
from jumpstarter_kubernetes.clients import ClientsV1Alpha1Api
from jumpstarter_kubernetes.exceptions import CredentialNotReadyError

TEST_CLIENT = V1Alpha1Client(
    api_version="jumpstarter.dev/v1alpha1",
    kind="Client",
    metadata=V1ObjectMeta(
        creation_timestamp="2021-10-01T00:00:00Z",
        generation=1,
        name="test-client",
        namespace="default",
        resource_version="1",
        uid="7a25eb81-6443-47ec-a62f-50165bffede8",
    ),
    status=V1Alpha1ClientStatus(credential=None, endpoint="https://test-client"),
)


def test_client_dump_json():
    assert (
        TEST_CLIENT.dump_json()
        == """{
    "apiVersion": "jumpstarter.dev/v1alpha1",
    "kind": "Client",
    "metadata": {
        "creationTimestamp": "2021-10-01T00:00:00Z",
        "generation": 1,
        "name": "test-client",
        "namespace": "default",
        "resourceVersion": "1",
        "uid": "7a25eb81-6443-47ec-a62f-50165bffede8"
    },
    "status": {
        "credential": null,
        "endpoint": "https://test-client"
    }
}"""
    )


def test_client_dump_yaml():
    assert (
        TEST_CLIENT.dump_yaml()
        == """apiVersion: jumpstarter.dev/v1alpha1
kind: Client
metadata:
  creationTimestamp: '2021-10-01T00:00:00Z'
  generation: 1
  name: test-client
  namespace: default
  resourceVersion: '1'
  uid: 7a25eb81-6443-47ec-a62f-50165bffede8
status:
  credential: null
  endpoint: https://test-client
"""
    )


def test_client_from_dict_with_credential():
    """Test V1Alpha1Client.from_dict with credential"""

    test_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Client",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-client",
            "namespace": "default",
            "resourceVersion": "1",
            "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
        },
        "status": {"credential": {"name": "test-credential"}, "endpoint": "https://test-client"},
    }
    client = V1Alpha1Client.from_dict(test_dict)
    assert client.metadata.name == "test-client"
    assert client.status.endpoint == "https://test-client"
    assert client.status.credential.name == "test-credential"


def test_client_from_dict_without_credential():
    """Test V1Alpha1Client.from_dict without credential"""
    test_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Client",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-client",
            "namespace": "default",
            "resourceVersion": "1",
            "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
        },
        "status": {"endpoint": "https://test-client"},
    }
    client = V1Alpha1Client.from_dict(test_dict)
    assert client.metadata.name == "test-client"
    assert client.status.endpoint == "https://test-client"
    assert client.status.credential is None


def test_client_from_dict_without_status():
    """Test V1Alpha1Client.from_dict without status"""
    test_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Client",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-client",
            "namespace": "default",
            "resourceVersion": "1",
            "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
        },
    }
    client = V1Alpha1Client.from_dict(test_dict)
    assert client.metadata.name == "test-client"
    assert client.status is None


def test_client_rich_add_columns():
    """Test V1Alpha1Client.rich_add_columns"""

    mock_table = MagicMock()
    V1Alpha1Client.rich_add_columns(mock_table)
    assert mock_table.add_column.call_count == 2
    mock_table.add_column.assert_any_call("NAME", no_wrap=True)
    mock_table.add_column.assert_any_call("ENDPOINT")


def test_client_rich_add_rows_with_status():
    """Test V1Alpha1Client.rich_add_rows with status"""

    mock_table = MagicMock()
    TEST_CLIENT.rich_add_rows(mock_table)
    mock_table.add_row.assert_called_once_with("test-client", "https://test-client")


def test_client_rich_add_rows_without_status():
    """Test V1Alpha1Client.rich_add_rows without status"""

    client = V1Alpha1Client(
        api_version="jumpstarter.dev/v1alpha1",
        kind="Client",
        metadata=V1ObjectMeta(name="test-client", namespace="default"),
        status=None,
    )
    mock_table = MagicMock()
    client.rich_add_rows(mock_table)
    mock_table.add_row.assert_called_once_with("test-client", "")


def test_client_rich_add_names():
    """Test V1Alpha1Client.rich_add_names"""
    names = []
    TEST_CLIENT.rich_add_names(names)
    assert names == ["client.jumpstarter.dev/test-client"]


def test_client_list_from_dict():
    """Test V1Alpha1ClientList.from_dict"""
    from jumpstarter_kubernetes import V1Alpha1ClientList

    test_dict = {
        "items": [
            {
                "apiVersion": "jumpstarter.dev/v1alpha1",
                "kind": "Client",
                "metadata": {
                    "creationTimestamp": "2021-10-01T00:00:00Z",
                    "generation": 1,
                    "name": "client1",
                    "namespace": "default",
                    "resourceVersion": "1",
                    "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
                },
                "status": {"endpoint": "https://client1"},
            },
            {
                "apiVersion": "jumpstarter.dev/v1alpha1",
                "kind": "Client",
                "metadata": {
                    "creationTimestamp": "2021-10-01T00:00:00Z",
                    "generation": 1,
                    "name": "client2",
                    "namespace": "default",
                    "resourceVersion": "1",
                    "uid": "8b36fc92-7554-48fd-b73g-61276cggfef9",
                },
                "status": {"endpoint": "https://client2"},
            },
        ]
    }
    client_list = V1Alpha1ClientList.from_dict(test_dict)
    assert len(client_list.items) == 2
    assert client_list.items[0].metadata.name == "client1"
    assert client_list.items[1].metadata.name == "client2"


def test_client_list_rich_add_columns():
    """Test V1Alpha1ClientList.rich_add_columns"""

    from jumpstarter_kubernetes import V1Alpha1ClientList

    mock_table = MagicMock()
    V1Alpha1ClientList.rich_add_columns(mock_table)
    assert mock_table.add_column.call_count == 2


def test_client_list_rich_add_rows():
    """Test V1Alpha1ClientList.rich_add_rows"""

    from jumpstarter_kubernetes import V1Alpha1ClientList

    client_list = V1Alpha1ClientList(items=[TEST_CLIENT])
    mock_table = MagicMock()
    client_list.rich_add_rows(mock_table)
    assert mock_table.add_row.call_count == 1


def test_client_list_rich_add_names():
    """Test V1Alpha1ClientList.rich_add_names"""
    from jumpstarter_kubernetes import V1Alpha1ClientList

    client_list = V1Alpha1ClientList(items=[TEST_CLIENT])
    names = []
    client_list.rich_add_names(names)
    assert names == ["client.jumpstarter.dev/test-client"]


# Tests for get_ca_bundle and get_client_config


@pytest.mark.asyncio
async def test_get_ca_bundle_with_ca_cert():
    """Test get_ca_bundle returns base64-encoded CA certificate"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.core_api = AsyncMock()

    # Mock ConfigMap with CA certificate
    ca_cert_pem = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----"
    mock_configmap = V1ConfigMap(data={"ca.crt": ca_cert_pem})
    api.core_api.read_namespaced_config_map = AsyncMock(return_value=mock_configmap)

    result = await api.get_ca_bundle()

    # Verify it's base64-encoded
    expected = base64.b64encode(ca_cert_pem.encode("utf-8")).decode("utf-8")
    assert result == expected
    api.core_api.read_namespaced_config_map.assert_called_once_with("jumpstarter-service-ca-cert", "test-namespace")


@pytest.mark.asyncio
async def test_get_ca_bundle_empty_ca_cert():
    """Test get_ca_bundle returns empty string when ca.crt is empty"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.core_api = AsyncMock()

    # Mock ConfigMap with empty CA certificate
    mock_configmap = V1ConfigMap(data={"ca.crt": ""})
    api.core_api.read_namespaced_config_map = AsyncMock(return_value=mock_configmap)

    result = await api.get_ca_bundle()

    assert result == ""


@pytest.mark.asyncio
async def test_get_ca_bundle_missing_ca_crt_key():
    """Test get_ca_bundle returns empty string when ca.crt key is missing"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.core_api = AsyncMock()

    # Mock ConfigMap without ca.crt key
    mock_configmap = V1ConfigMap(data={"other-key": "value"})
    api.core_api.read_namespaced_config_map = AsyncMock(return_value=mock_configmap)

    result = await api.get_ca_bundle()

    assert result == ""


@pytest.mark.asyncio
async def test_get_ca_bundle_configmap_not_found():
    """Test get_ca_bundle returns empty string when ConfigMap doesn't exist"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.core_api = AsyncMock()

    # Mock 404 error
    api.core_api.read_namespaced_config_map = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))

    result = await api.get_ca_bundle()

    assert result == ""


@pytest.mark.asyncio
async def test_get_ca_bundle_other_api_error():
    """Test get_ca_bundle raises exception for non-404 errors"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.core_api = AsyncMock()

    # Mock 403 error
    api.core_api.read_namespaced_config_map = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))

    with pytest.raises(ApiException) as exc_info:
        await api.get_ca_bundle()

    assert exc_info.value.status == 403


@pytest.mark.asyncio
async def test_get_client_config_includes_ca_bundle():
    """Test get_client_config includes CA bundle from ConfigMap"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.api = AsyncMock()
    api.core_api = AsyncMock()

    # Mock client response
    client_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Client",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-client",
            "namespace": "test-namespace",
            "resourceVersion": "1",
            "uid": "test-uid",
        },
        "status": {
            "credential": {"name": "test-secret"},
            "endpoint": "https://test-endpoint:8082",
        },
    }
    api.api.get_namespaced_custom_object = AsyncMock(return_value=client_dict)

    # Mock secret with token
    token = "test-token-value"
    mock_secret = V1Secret(data={"token": base64.b64encode(token.encode()).decode()})
    api.core_api.read_namespaced_secret = AsyncMock(return_value=mock_secret)

    # Mock ConfigMap with CA certificate
    ca_cert_pem = "-----BEGIN CERTIFICATE-----\nMIIBtest\n-----END CERTIFICATE-----"
    mock_configmap = V1ConfigMap(data={"ca.crt": ca_cert_pem})
    api.core_api.read_namespaced_config_map = AsyncMock(return_value=mock_configmap)

    config = await api.get_client_config("test-client", allow=[], unsafe=False)

    # Verify CA bundle is included and base64-encoded
    expected_ca = base64.b64encode(ca_cert_pem.encode("utf-8")).decode("utf-8")
    assert config.tls.ca == expected_ca
    assert config.endpoint == "https://test-endpoint:8082"
    assert config.token == token


@pytest.mark.asyncio
async def test_get_client_config_without_ca_bundle():
    """Test get_client_config works when CA ConfigMap doesn't exist"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.api = AsyncMock()
    api.core_api = AsyncMock()

    # Mock client response
    client_dict = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Client",
        "metadata": {
            "creationTimestamp": "2021-10-01T00:00:00Z",
            "generation": 1,
            "name": "test-client",
            "namespace": "test-namespace",
            "resourceVersion": "1",
            "uid": "test-uid",
        },
        "status": {
            "credential": {"name": "test-secret"},
            "endpoint": "https://test-endpoint:8082",
        },
    }
    api.api.get_namespaced_custom_object = AsyncMock(return_value=client_dict)

    # Mock secret with token
    token = "test-token-value"
    mock_secret = V1Secret(data={"token": base64.b64encode(token.encode()).decode()})
    api.core_api.read_namespaced_secret = AsyncMock(return_value=mock_secret)

    # Mock ConfigMap not found
    api.core_api.read_namespaced_config_map = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))

    config = await api.get_client_config("test-client", allow=[], unsafe=False)

    # Verify CA bundle is empty
    assert config.tls.ca == ""
    assert config.endpoint == "https://test-endpoint:8082"
    assert config.token == token


def test_client_from_dict_keeps_labels():
    """Labels are how clients are grouped, so from_dict must not drop them"""
    client = V1Alpha1Client.from_dict(
        {
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Client",
            "metadata": {
                "creationTimestamp": "2021-10-01T00:00:00Z",
                "generation": 1,
                "labels": {"team": "platform"},
                "name": "test-client",
                "namespace": "default",
                "resourceVersion": "1",
                "uid": "7a25eb81-6443-47ec-a62f-50165bffede8",
            },
            "status": {"endpoint": "https://test-client"},
        }
    )
    assert client.metadata.labels == {"team": "platform"}
    assert '"team": "platform"' in client.dump_json()


@pytest.mark.asyncio
async def test_get_client_config_without_credentials():
    """A client whose credentials the controller has not issued yet is reported, not crashed on"""
    api = ClientsV1Alpha1Api(namespace="test-namespace")
    api.api = AsyncMock()
    api.core_api = AsyncMock()
    api.api.get_namespaced_custom_object = AsyncMock(
        return_value={
            "apiVersion": "jumpstarter.dev/v1alpha1",
            "kind": "Client",
            "metadata": {
                "creationTimestamp": "2021-10-01T00:00:00Z",
                "generation": 1,
                "name": "fresh-client",
                "namespace": "test-namespace",
                "resourceVersion": "1",
                "uid": "test-uid",
            },
        }
    )

    with pytest.raises(CredentialNotReadyError, match="fresh-client"):
        await api.get_client_config("fresh-client", allow=[], unsafe=False)

    api.core_api.read_namespaced_secret.assert_not_awaited()
