"""Apply Jumpstarter manifests to a cluster.

This is the write half of ``jmp admin get``: it takes the YAML for a Client,
Exporter, ExporterSet, VirtualTargetClass or any other Jumpstarter custom
resource and sends it to the cluster with a server-side apply, so a manifest
can be created and later re-applied without the caller tracking what changed.

Only resources in the ``jumpstarter.dev`` API groups are accepted. This is a
Jumpstarter admin tool, not a general-purpose ``kubectl apply``.
"""

import logging
from typing import Literal, Optional

import yaml
from kubernetes_asyncio.client.exceptions import ApiException
from pydantic import Field

from .exceptions import JumpstarterKubernetesError
from .json import JsonBaseModel
from .list import V1Alpha1List
from .util import AbstractAsyncCustomObjectApi

logger = logging.getLogger(__name__)

CORE_API_GROUP = "jumpstarter.dev"

# Identifies this tool as the owner of the fields it applies, so a later apply
# of the same manifest can remove a field it no longer sets.
FIELD_MANAGER = "jumpstarter-admin"

APPLY_PATCH_CONTENT_TYPE = "application/apply-patch+yaml"


class ManifestError(JumpstarterKubernetesError):
    """Raised when a manifest cannot be applied as written."""


def is_jumpstarter_group(group: str) -> bool:
    """True for the Jumpstarter API group and its subgroups."""
    return group == CORE_API_GROUP or group.endswith("." + CORE_API_GROUP)


def validate_manifest(document: object, origin: str) -> dict:
    """Check that a parsed YAML document is a Jumpstarter resource we can apply."""
    if not isinstance(document, dict):
        raise ManifestError(f"{origin}: expected a resource, got {type(document).__name__}")

    api_version = document.get("apiVersion")
    if not isinstance(api_version, str) or api_version == "":
        raise ManifestError(f"{origin}: missing apiVersion")
    group, _, version = api_version.partition("/")
    if version == "":
        raise ManifestError(
            f"{origin}: apiVersion '{api_version}' is a core Kubernetes resource, "
            "only Jumpstarter resources can be applied"
        )
    if not is_jumpstarter_group(group):
        raise ManifestError(
            f"{origin}: apiVersion '{api_version}' is not a Jumpstarter API group, "
            f"expected {CORE_API_GROUP} or a subgroup of it"
        )

    kind = document.get("kind")
    if not isinstance(kind, str) or kind == "":
        raise ManifestError(f"{origin}: missing kind")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ManifestError(f"{origin}: missing metadata")
    name = metadata.get("name")
    if not isinstance(name, str) or name == "":
        raise ManifestError(f"{origin}: missing metadata.name")

    return document


def load_manifests(source: str, origin: str = "manifest") -> list[dict]:
    """Parse a YAML stream of Jumpstarter resources, empty documents skipped."""
    try:
        documents = list(yaml.safe_load_all(source))
    except yaml.YAMLError as e:
        raise ManifestError(f"{origin} is not valid YAML: {e}") from e

    resources = []
    for index, document in enumerate(documents):
        if document is None:
            continue
        # Only number the documents when there is more than one to point at.
        label = origin if len(documents) == 1 else f"{origin} (document {index + 1})"
        resources.append(validate_manifest(document, label))

    if len(resources) == 0:
        raise ManifestError(f"{origin} contains no resources")
    return resources


class V1Alpha1AppliedResource(JsonBaseModel):
    """One resource as the cluster stored it, and what applying it did."""

    api_version: str = Field(alias="apiVersion")
    kind: str
    name: str
    namespace: Optional[str] = None
    action: Literal["created", "configured", "unchanged"]
    resource: dict

    @property
    def qualified_name(self) -> str:
        group = self.api_version.partition("/")[0]
        return f"{self.kind.lower()}.{group}/{self.name}"

    @classmethod
    def rich_add_columns(cls, table, **kwargs):
        table.add_column("NAME", no_wrap=True)
        table.add_column("KIND")
        table.add_column("NAMESPACE")
        table.add_column("ACTION")

    def rich_add_rows(self, table, **kwargs):
        table.add_row(self.name, self.kind, self.namespace or "", self.action)

    def rich_add_names(self, names):
        names.append(self.qualified_name)


class V1Alpha1AppliedResourceList(V1Alpha1List[V1Alpha1AppliedResource]):
    kind: Literal["List"] = Field(default="List")

    @classmethod
    def rich_add_columns(cls, table, **kwargs):
        V1Alpha1AppliedResource.rich_add_columns(table, **kwargs)

    def rich_add_rows(self, table, **kwargs):
        for applied in self.items:
            applied.rich_add_rows(table, **kwargs)

    def rich_add_names(self, names):
        for applied in self.items:
            applied.rich_add_names(names)


class ApplyV1Alpha1Api(AbstractAsyncCustomObjectApi):
    """Apply Jumpstarter manifests of any kind the cluster serves."""

    def __init__(self, namespace: str, config_file: Optional[str] = None, context: Optional[str] = None):
        super().__init__(namespace, config_file, context)
        self._resources: dict[tuple[str, str, str], tuple[str, bool]] = {}

    async def _resolve_resource(self, group: str, version: str, kind: str) -> tuple[str, bool]:
        """Look up a kind's plural name and scope in the cluster's discovery data.

        Discovery keeps this working for kinds this client was never taught,
        which is what lets one command apply Clients, ExporterSets and whatever
        the operator adds next.
        """
        cached = self._resources.get((group, version, kind))
        if cached is not None:
            return cached

        try:
            listing = await self._client.call_api(
                f"/apis/{group}/{version}",
                "GET",
                auth_settings=["BearerToken"],
                header_params={"Accept": "application/json"},
                response_types_map={200: "object"},
                _return_http_data_only=True,
            )
        except ApiException as e:
            if e.status == 404:
                raise ManifestError(
                    f"the cluster does not serve {group}/{version}, check that the Jumpstarter CRDs are installed"
                ) from e
            raise

        for resource in listing.get("resources", []):
            # Subresources ("exportersets/status") are not kinds of their own.
            if "/" in resource.get("name", ""):
                continue
            if resource.get("kind") == kind:
                found = (resource["name"], bool(resource.get("namespaced", True)))
                self._resources[(group, version, kind)] = found
                return found

        raise ManifestError(f"the cluster does not serve kind {kind} in {group}/{version}")

    async def _read(self, group, version, plural, name, namespace) -> Optional[dict]:
        try:
            if namespace is None:
                return await self.api.get_cluster_custom_object(group=group, version=version, plural=plural, name=name)
            return await self.api.get_namespaced_custom_object(
                group=group, version=version, plural=plural, name=name, namespace=namespace
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def _server_side_apply(
        self, group, version, plural, name, namespace, manifest: dict, *, dry_run: bool, force: bool
    ) -> dict:
        """PATCH a resource with a server-side apply, creating it if needed.

        The request is built by hand because the generated custom object client
        only deserializes a 200 response, and an apply that creates a resource
        answers 201.
        """
        if namespace is None:
            path = "/apis/{group}/{version}/{plural}/{name}"
            path_params = {"group": group, "version": version, "plural": plural, "name": name}
        else:
            path = "/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}"
            path_params = {
                "group": group,
                "version": version,
                "namespace": namespace,
                "plural": plural,
                "name": name,
            }

        query_params = [("fieldManager", FIELD_MANAGER)]
        if force:
            # Take ownership of fields another manager holds. Off by default,
            # as in kubectl: a conflict means something else is managing the
            # field, which the caller should see rather than silently win.
            query_params.append(("force", "true"))
        if dry_run:
            query_params.append(("dryRun", "All"))

        return await self._client.call_api(
            path,
            "PATCH",
            path_params=path_params,
            query_params=query_params,
            header_params={"Accept": "application/json", "Content-Type": APPLY_PATCH_CONTENT_TYPE},
            body=manifest,
            auth_settings=["BearerToken"],
            response_types_map={200: "object", 201: "object"},
            _return_http_data_only=True,
        )

    async def apply(
        self, manifest: dict, *, dry_run: bool = False, force_conflicts: bool = False
    ) -> V1Alpha1AppliedResource:
        """Server-side apply one resource and report what it did."""
        validate_manifest(manifest, f"{manifest.get('kind', 'resource')} manifest")
        api_version = manifest["apiVersion"]
        group, _, version = api_version.partition("/")
        kind = manifest["kind"]
        name = manifest["metadata"]["name"]

        plural, namespaced = await self._resolve_resource(group, version, kind)
        namespace = None
        if namespaced:
            namespace = manifest["metadata"].get("namespace") or self.namespace
            # The body has to agree with the URL the request is sent to.
            manifest = {**manifest, "metadata": {**manifest["metadata"], "namespace": namespace}}
        elif manifest["metadata"].get("namespace"):
            raise ManifestError(f"{kind} is cluster scoped, remove metadata.namespace")

        existing = await self._read(group, version, plural, name, namespace)
        applied = await self._server_side_apply(
            group, version, plural, name, namespace, manifest, dry_run=dry_run, force=force_conflicts
        )

        return V1Alpha1AppliedResource(
            apiVersion=api_version,
            kind=kind,
            name=name,
            namespace=namespace,
            action=_action_taken(existing, applied, dry_run=dry_run),
            resource=applied,
        )

    async def apply_all(
        self, manifests: list[dict], *, dry_run: bool = False, force_conflicts: bool = False
    ) -> V1Alpha1AppliedResourceList:
        """Apply resources in the order they were written."""
        applied = []
        for manifest in manifests:
            applied.append(await self.apply(manifest, dry_run=dry_run, force_conflicts=force_conflicts))
        return V1Alpha1AppliedResourceList(items=applied)


def _action_taken(
    existing: Optional[dict], applied: dict, *, dry_run: bool = False
) -> Literal["created", "configured", "unchanged"]:
    """Describe an apply by what the cluster did, or would do, with it.

    A persisted apply that changes nothing leaves the resource version alone,
    which is how an unchanged re-apply is told apart from a real update. A dry
    run is never persisted, so its resource version never moves either — there
    the answer has to come from comparing what the server says the object would
    become against what it is now.
    """
    if existing is None:
        return "created"
    if dry_run:
        return "unchanged" if _same_content(existing, applied) else "configured"
    before = existing.get("metadata", {}).get("resourceVersion")
    after = applied.get("metadata", {}).get("resourceVersion")
    if before is not None and before == after:
        return "unchanged"
    return "configured"


# Set by the server on every write, so they say nothing about whether the
# manifest changed anything.
_SERVER_OWNED_METADATA = frozenset(
    {"resourceVersion", "generation", "managedFields", "creationTimestamp", "uid", "selfLink"}
)


def _same_content(before: dict, after: dict) -> bool:
    """Whether two versions of a resource differ in anything the user wrote."""
    return _without_server_fields(before) == _without_server_fields(after)


def _without_server_fields(obj: dict) -> dict:
    trimmed = {key: value for key, value in obj.items() if key != "status"}
    trimmed["metadata"] = {
        key: value
        for key, value in (obj.get("metadata") or {}).items()
        if key not in _SERVER_OWNED_METADATA
    }
    return trimmed
