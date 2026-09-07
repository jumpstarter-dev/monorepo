from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from .apply import (
    APPLY_PATCH_CONTENT_TYPE,
    FIELD_MANAGER,
    ApplyV1Alpha1Api,
    ManifestError,
    load_manifests,
    validate_manifest,
)

CLIENT_MANIFEST = """
apiVersion: jumpstarter.dev/v1alpha1
kind: Client
metadata:
  name: hello
"""

EXPORTER_SET_MANIFEST = """
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: pool
spec:
  minReplicas: 1
"""


def test_load_manifests_reads_every_document():
    manifests = load_manifests(f"{CLIENT_MANIFEST}\n---\n{EXPORTER_SET_MANIFEST}")
    assert [m["kind"] for m in manifests] == ["Client", "ExporterSet"]


def test_load_manifests_skips_empty_documents():
    manifests = load_manifests(f"---\n\n---\n{CLIENT_MANIFEST}\n---\n# just a comment\n")
    assert [m["kind"] for m in manifests] == ["Client"]


def test_load_manifests_rejects_an_empty_stream():
    with pytest.raises(ManifestError, match="contains no resources"):
        load_manifests("\n# nothing here\n", "empty.yaml")


def test_load_manifests_rejects_invalid_yaml():
    with pytest.raises(ManifestError, match="not valid YAML"):
        load_manifests("kind: [unterminated", "broken.yaml")


def test_load_manifests_names_the_document_that_failed():
    with pytest.raises(ManifestError, match=r"pair\.yaml \(document 2\): missing kind"):
        load_manifests(f"{CLIENT_MANIFEST}\n---\napiVersion: jumpstarter.dev/v1alpha1\n", "pair.yaml")


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("a string", "expected a resource"),
        ({"kind": "Client", "metadata": {"name": "x"}}, "missing apiVersion"),
        ({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x"}}, "core Kubernetes resource"),
        ({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "x"}}, "not a Jumpstarter API group"),
        (
            # A group that merely ends in the same letters is not a subgroup.
            {"apiVersion": "notjumpstarter.dev/v1", "kind": "Client", "metadata": {"name": "x"}},
            "not a Jumpstarter API group",
        ),
        ({"apiVersion": "jumpstarter.dev/v1alpha1", "metadata": {"name": "x"}}, "missing kind"),
        ({"apiVersion": "jumpstarter.dev/v1alpha1", "kind": "Client"}, "missing metadata"),
        ({"apiVersion": "jumpstarter.dev/v1alpha1", "kind": "Client", "metadata": {}}, "missing metadata.name"),
    ],
)
def test_validate_manifest_rejects(document, message):
    with pytest.raises(ManifestError, match=message):
        validate_manifest(document, "manifest")


def test_validate_manifest_accepts_a_subgroup():
    document = {
        "apiVersion": "virtualtarget.jumpstarter.dev/v1alpha1",
        "kind": "ExporterSet",
        "metadata": {"name": "p"},
    }
    assert validate_manifest(document, "manifest") is document


DISCOVERY = {
    "resources": [
        {"name": "clients/status", "kind": "Client", "namespaced": True},
        {"name": "clients", "kind": "Client", "namespaced": True},
        {"name": "virtualtargetclasses", "kind": "VirtualTargetClass", "namespaced": False},
    ]
}


def make_api(*, discovery=None, existing=None, applied=None) -> ApplyV1Alpha1Api:
    """An apply API wired to a fake cluster instead of a real one."""
    api = ApplyV1Alpha1Api("default")
    api._client = Mock()

    async def call_api(path, method, **kwargs):
        if method == "GET":
            return discovery if discovery is not None else DISCOVERY
        return applied or {"metadata": {"name": "hello", "resourceVersion": "2"}}

    api._client.call_api = AsyncMock(side_effect=call_api)
    api.api = Mock()
    api.api.get_namespaced_custom_object = AsyncMock(return_value=existing)
    api.api.get_cluster_custom_object = AsyncMock(return_value=existing)
    if existing is None:
        api.api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        api.api.get_cluster_custom_object.side_effect = ApiException(status=404)
    return api


def patch_call(api: ApplyV1Alpha1Api):
    """The PATCH the API sent, as (path, path_params, query_params, body)."""
    for call in api._client.call_api.await_args_list:
        if call.args[1] == "PATCH":
            return call
    raise AssertionError("no apply request was sent")


@pytest.mark.asyncio
async def test_apply_creates_a_resource_the_cluster_does_not_have():
    api = make_api()

    applied = await api.apply(load_manifests(CLIENT_MANIFEST)[0])

    assert applied.action == "created"
    assert applied.qualified_name == "client.jumpstarter.dev/hello"
    assert applied.namespace == "default"
    call = patch_call(api)
    assert call.args[0] == "/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}"
    assert call.kwargs["path_params"] == {
        "group": "jumpstarter.dev",
        "version": "v1alpha1",
        "namespace": "default",
        "plural": "clients",
        "name": "hello",
    }
    assert call.kwargs["header_params"]["Content-Type"] == APPLY_PATCH_CONTENT_TYPE
    assert ("fieldManager", FIELD_MANAGER) in call.kwargs["query_params"]
    # Conflicts are reported, not won, unless the caller asks for it.
    assert not any(param == "force" for param, _ in call.kwargs["query_params"])
    assert not any(param == "dryRun" for param, _ in call.kwargs["query_params"])
    # The body has to carry the namespace it is being sent to.
    assert call.kwargs["body"]["metadata"]["namespace"] == "default"
    # An apply that creates a resource answers 201, which has to deserialize.
    assert call.kwargs["response_types_map"] == {200: "object", 201: "object"}


@pytest.mark.asyncio
async def test_apply_reports_a_resource_it_changed_as_configured():
    api = make_api(existing={"metadata": {"name": "hello", "resourceVersion": "1"}})

    applied = await api.apply(load_manifests(CLIENT_MANIFEST)[0])

    assert applied.action == "configured"


@pytest.mark.asyncio
async def test_apply_reports_an_unchanged_resource_as_unchanged():
    # A server-side apply that changes nothing leaves the resource version be.
    api = make_api(
        existing={"metadata": {"name": "hello", "resourceVersion": "7"}},
        applied={"metadata": {"name": "hello", "resourceVersion": "7"}},
    )

    applied = await api.apply(load_manifests(CLIENT_MANIFEST)[0])

    assert applied.action == "unchanged"


@pytest.mark.asyncio
async def test_apply_keeps_the_namespace_the_manifest_asks_for():
    api = make_api()
    manifest = load_manifests(CLIENT_MANIFEST)[0]
    manifest["metadata"]["namespace"] = "lab"

    applied = await api.apply(manifest)

    assert applied.namespace == "lab"
    assert patch_call(api).kwargs["path_params"]["namespace"] == "lab"


@pytest.mark.asyncio
async def test_apply_takes_ownership_of_conflicting_fields_when_asked():
    api = make_api()

    await api.apply(load_manifests(CLIENT_MANIFEST)[0], force_conflicts=True)

    assert ("force", "true") in patch_call(api).kwargs["query_params"]


@pytest.mark.asyncio
async def test_apply_passes_a_dry_run_through_to_the_server():
    api = make_api()

    await api.apply(load_manifests(CLIENT_MANIFEST)[0], dry_run=True)

    assert ("dryRun", "All") in patch_call(api).kwargs["query_params"]


@pytest.mark.asyncio
async def test_a_dry_run_reports_a_change_the_resource_version_cannot_show():
    # Nothing is persisted, so the resource version stays put even though the
    # manifest would change the labels. The answer has to come from the content.
    api = make_api(
        existing={"metadata": {"name": "hello", "resourceVersion": "7", "labels": {"env": "dev"}}},
        applied={"metadata": {"name": "hello", "resourceVersion": "7", "labels": {"env": "prod"}}},
    )

    applied = await api.apply(load_manifests(CLIENT_MANIFEST)[0], dry_run=True)

    assert applied.action == "configured"


@pytest.mark.asyncio
async def test_a_dry_run_ignores_fields_the_server_owns():
    api = make_api(
        existing={
            "metadata": {"name": "hello", "resourceVersion": "7", "generation": 3, "managedFields": []},
            "spec": {"username": "hello"},
            "status": {"credential": {"name": "hello-client"}},
        },
        applied={
            "metadata": {"name": "hello", "resourceVersion": "7", "generation": 4, "managedFields": [{"a": 1}]},
            "spec": {"username": "hello"},
            "status": {},
        },
    )

    applied = await api.apply(load_manifests(CLIENT_MANIFEST)[0], dry_run=True)

    assert applied.action == "unchanged"


@pytest.mark.asyncio
async def test_apply_uses_the_cluster_scoped_endpoint_for_a_cluster_scoped_kind():
    api = make_api()
    manifest = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "VirtualTargetClass",
        "metadata": {"name": "qemu"},
    }

    applied = await api.apply(manifest)

    assert applied.namespace is None
    call = patch_call(api)
    assert call.args[0] == "/apis/{group}/{version}/{plural}/{name}"
    assert call.kwargs["path_params"] == {
        "group": "jumpstarter.dev",
        "version": "v1alpha1",
        "plural": "virtualtargetclasses",
        "name": "qemu",
    }


@pytest.mark.asyncio
async def test_apply_rejects_a_namespace_on_a_cluster_scoped_kind():
    api = make_api()
    manifest = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "VirtualTargetClass",
        "metadata": {"name": "qemu", "namespace": "lab"},
    }

    with pytest.raises(ManifestError, match="cluster scoped"):
        await api.apply(manifest)


@pytest.mark.asyncio
async def test_apply_rejects_a_kind_the_cluster_does_not_serve():
    api = make_api()
    manifest = {
        "apiVersion": "jumpstarter.dev/v1alpha1",
        "kind": "Imaginary",
        "metadata": {"name": "nope"},
    }

    with pytest.raises(ManifestError, match="does not serve kind Imaginary"):
        await api.apply(manifest)


@pytest.mark.asyncio
async def test_apply_explains_a_missing_api_group():
    api = make_api()
    api._client.call_api.side_effect = ApiException(status=404)

    with pytest.raises(ManifestError, match="CRDs are installed"):
        await api.apply(load_manifests(CLIENT_MANIFEST)[0])


@pytest.mark.asyncio
async def test_apply_all_looks_up_each_kind_once():
    api = make_api()

    applied = await api.apply_all(load_manifests(f"{CLIENT_MANIFEST}\n---\n{CLIENT_MANIFEST}"))

    assert [item.action for item in applied.items] == ["created", "created"]
    discoveries = [call for call in api._client.call_api.await_args_list if call.args[1] == "GET"]
    assert len(discoveries) == 1
