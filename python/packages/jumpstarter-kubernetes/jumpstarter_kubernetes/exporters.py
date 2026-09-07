import asyncio
import base64
from typing import Literal, Optional

from kubernetes_asyncio.client.models import V1ObjectMeta, V1ObjectReference
from pydantic import Field

from .datetime import time_since
from .exceptions import CredentialNotReadyError
from .json import JsonBaseModel
from .list import V1Alpha1List
from .serialize import SerializeV1ObjectMeta, SerializeV1ObjectReference
from .util import AbstractAsyncCustomObjectApi
from jumpstarter.config.common import ObjectMeta
from jumpstarter.config.exporter import ExporterConfigV1Alpha1
from jumpstarter.config.tls import TLSConfigV1Alpha1

CREATE_EXPORTER_DELAY = 1
CREATE_EXPORTER_COUNT = 10


class V1Alpha1ExporterDevice(JsonBaseModel):
    labels: dict[str, str]
    uuid: str


class V1Alpha1ExporterStatus(JsonBaseModel):
    # The controller fills these in after it reconciles the exporter, so a
    # freshly created one has a status with nothing in it yet.
    credential: Optional[SerializeV1ObjectReference] = None
    devices: list[V1Alpha1ExporterDevice] = []
    endpoint: str = ""
    exporter_status: str | None = Field(alias="exporterStatus", default=None)
    status_message: str | None = Field(alias="statusMessage", default=None)


class V1Alpha1Exporter(JsonBaseModel):
    api_version: Literal["jumpstarter.dev/v1alpha1"] = Field(alias="apiVersion", default="jumpstarter.dev/v1alpha1")
    kind: Literal["Exporter"] = Field(default="Exporter")
    metadata: SerializeV1ObjectMeta
    status: Optional[V1Alpha1ExporterStatus] = None

    @staticmethod
    def from_dict(dict: dict):
        return V1Alpha1Exporter(
            api_version=dict["apiVersion"],
            kind=dict["kind"],
            metadata=V1ObjectMeta(
                creation_timestamp=dict["metadata"]["creationTimestamp"],
                generation=dict["metadata"]["generation"],
                # Labels are what a client selects an exporter by, so they are
                # part of what an admin needs to see about one.
                labels=dict["metadata"].get("labels"),
                name=dict["metadata"]["name"],
                namespace=dict["metadata"]["namespace"],
                resource_version=dict["metadata"]["resourceVersion"],
                uid=dict["metadata"]["uid"],
            ),
            status=V1Alpha1ExporterStatus(
                credential=V1ObjectReference(name=dict["status"]["credential"]["name"])
                if "credential" in dict["status"]
                else None,
                endpoint=dict["status"].get("endpoint", ""),
                devices=[V1Alpha1ExporterDevice(labels=d["labels"], uuid=d["uuid"]) for d in dict["status"]["devices"]]
                if "devices" in dict["status"]
                else [],
                exporter_status=dict["status"].get("exporterStatus"),
                status_message=dict["status"].get("statusMessage"),
            )
            # An exporter the controller has not reconciled yet has no status.
            if "status" in dict
            else None,
        )

    @classmethod
    def rich_add_columns(cls, table, devices: bool = False):
        if devices:
            table.add_column("NAME", no_wrap=True)
            table.add_column("STATUS")
            table.add_column("ENDPOINT")
            table.add_column("AGE")
            table.add_column("LABELS")
            table.add_column("UUID")
        else:
            table.add_column("NAME", no_wrap=True)
            table.add_column("STATUS")
            table.add_column("ENDPOINT")
            table.add_column("DEVICES")
            table.add_column("AGE")

    def rich_add_rows(self, table, devices: bool = False):
        status = self.status.exporter_status if self.status else "Unknown"
        if devices:
            if not (self.status and self.status.devices):
                # An exporter with no devices to enumerate — never run, or not
                # reconciled yet — still exists, so it still gets a row. Without
                # this it disappears from the listing entirely.
                table.add_row(
                    self.metadata.name,
                    status or "Unknown",
                    self.status.endpoint if self.status else "",
                    time_since(self.metadata.creation_timestamp),
                    "",
                    "",
                )
                return
            for d in self.status.devices:
                labels = []
                if d.labels is not None:
                    for label in d.labels:
                        labels.append(f"{label}:{str(d.labels[label])}")
                table.add_row(
                    self.metadata.name,
                    status or "Unknown",
                    self.status.endpoint,
                    time_since(self.metadata.creation_timestamp),
                    ",".join(labels),
                    d.uuid,
                )

        else:
            table.add_row(
                self.metadata.name,
                status or "Unknown",
                self.status.endpoint if self.status else "",
                str(len(self.status.devices) if self.status and self.status.devices else 0),
                time_since(self.metadata.creation_timestamp),
            )

    def rich_add_names(self, names):
        names.append(f"exporter.jumpstarter.dev/{self.metadata.name}")


class V1Alpha1ExporterList(V1Alpha1List[V1Alpha1Exporter]):
    kind: Literal["ExporterList"] = Field(default="ExporterList")

    @staticmethod
    def from_dict(dict: dict):
        return V1Alpha1ExporterList(items=[V1Alpha1Exporter.from_dict(c) for c in dict["items"]])

    @classmethod
    def rich_add_columns(cls, table, **kwargs):
        V1Alpha1Exporter.rich_add_columns(table, **kwargs)

    def rich_add_rows(self, table, **kwargs):
        for exporter in self.items:
            exporter.rich_add_rows(table, **kwargs)

    def rich_add_names(self, names):
        for exporter in self.items:
            exporter.rich_add_names(names)


class ExportersV1Alpha1Api(AbstractAsyncCustomObjectApi):
    """Interact with the exporters custom resource API"""

    async def list_exporters(self) -> V1Alpha1List[V1Alpha1Exporter]:
        """List the exporter objects in the cluster"""
        res = await self.api.list_namespaced_custom_object(
            namespace=self.namespace, group="jumpstarter.dev", plural="exporters", version="v1alpha1"
        )
        return V1Alpha1ExporterList.from_dict(res)

    async def get_exporter(self, name: str) -> V1Alpha1Exporter:
        """Get a single exporter object from the cluster"""
        result = await self.api.get_namespaced_custom_object(
            namespace=self.namespace, group="jumpstarter.dev", plural="exporters", version="v1alpha1", name=name
        )
        return V1Alpha1Exporter.from_dict(result)

    async def create_exporter(
        self, name: str, labels: dict[str, str] | None = None, oidc_username: str | None = None
    ) -> V1Alpha1Exporter:
        """Create an exporter in the cluster"""
        # Create the namespaced exporter object
        await self.api.create_namespaced_custom_object(
            namespace=self.namespace,
            group="jumpstarter.dev",
            plural="exporters",
            version="v1alpha1",
            body={
                "apiVersion": "jumpstarter.dev/v1alpha1",
                "kind": "Exporter",
                "metadata": {"name": name} | {"labels": labels} if labels is not None else {},
                "spec": {"username": oidc_username} if oidc_username is not None else {},
            },
        )
        # Wait for the credentials to become available
        # NOTE: Watch is not working here with the Python kubernetes library
        count = 0
        updated_exporter = {}
        # Retry for a maximum of 10s
        while count < CREATE_EXPORTER_COUNT:
            # Try to get the updated client resource
            updated_exporter = await self.api.get_namespaced_custom_object(
                namespace=self.namespace, group="jumpstarter.dev", plural="exporters", version="v1alpha1", name=name
            )
            # check if the client status is updated with the credentials
            if "status" in updated_exporter:
                if "credential" in updated_exporter["status"]:
                    return V1Alpha1Exporter.from_dict(updated_exporter)
            count += 1
            await asyncio.sleep(CREATE_EXPORTER_DELAY)
        raise Exception("Timeout waiting for exporter credentials")

    async def get_exporter_config(self, name: str) -> ExporterConfigV1Alpha1:
        """Get an exporter config for a specified exporter name"""
        exporter = await self.get_exporter(name)
        if exporter.status is None or exporter.status.credential is None:
            raise CredentialNotReadyError("exporter", name)
        secret = await self.core_api.read_namespaced_secret(exporter.status.credential.name, self.namespace)
        endpoint = exporter.status.endpoint
        token = base64.b64decode(secret.data["token"]).decode("utf8")
        # Get CA bundle from ConfigMap (base64-encoded)
        ca_bundle = await self.get_ca_bundle()
        return ExporterConfigV1Alpha1(
            alias=name,
            metadata=ObjectMeta(
                namespace=exporter.metadata.namespace,
                name=exporter.metadata.name,
            ),
            endpoint=endpoint,
            token=token,
            export={},
            tls=TLSConfigV1Alpha1(ca=ca_bundle),
        )

    async def delete_exporter(self, name: str):
        """Delete an exporter object"""
        await self.api.delete_namespaced_custom_object(
            namespace=self.namespace,
            name=name,
            group="jumpstarter.dev",
            plural="exporters",
            version="v1alpha1",
        )
