from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from google.protobuf import duration_pb2, field_mask_pb2, json_format, timestamp_pb2
from grpc import ChannelConnectivity
from grpc.aio import Channel
from jumpstarter_protocol import client_pb2, client_pb2_grpc, jumpstarter_pb2_grpc, kubernetes_pb2, router_pb2_grpc
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from jumpstarter.common import ExporterStatus
from jumpstarter.common.grpc import translate_grpc_exceptions


@dataclass
class WithOptions:
    show_online: bool = False
    show_leases: bool = False
    show_status: bool = False


def add_display_columns(table, options: WithOptions = None):
    if options is None:
        options = WithOptions()
    table.add_column("NAME")
    if options.show_online:
        table.add_column("ONLINE")
    if options.show_status:
        table.add_column("STATUS")
    table.add_column("LABELS")
    if options.show_leases:
        table.add_column("LEASED BY")
        table.add_column("LEASE STATUS")
        table.add_column("RELEASE TIME")


def add_exporter_row(table, exporter, options: WithOptions = None, lease_info: tuple[str, str, str] | None = None):
    if options is None:
        options = WithOptions()
    row_data = []
    row_data.append(exporter.name)
    if options.show_online:
        row_data.append("yes" if exporter.online else "no")
    if options.show_status:
        status_str = str(exporter.status) if exporter.status else "UNKNOWN"
        row_data.append(status_str)
    row_data.append(",".join(("{}={}".format(k, v) for k, v in sorted(exporter.labels.items()))))
    if options.show_leases:
        if lease_info:
            lease_client, lease_status, expected_release = lease_info
        else:
            lease_client, lease_status, expected_release = "", "Available", ""
        row_data.extend([lease_client, lease_status, expected_release])

    table.add_row(*row_data)


def parse_identifier(identifier: str, kind: str) -> tuple[str, str]:
    segments = identifier.split("/")
    if len(segments) != 4:
        raise ValueError("incorrect number of segments in identifier, expecting 4, got {}".format(len(segments)))
    if segments[0] != "namespaces":
        raise ValueError("incorrect first segment in identifier, expecting namespaces, got {}".format(segments[0]))
    if segments[2] != kind:
        raise ValueError("incorrect third segment in identifier, expecting {}, got {}".format(kind, segments[2]))
    return segments[1], segments[3]


def parse_client_identifier(identifier: str) -> tuple[str, str]:
    return parse_identifier(identifier, "clients")


def parse_exporter_identifier(identifier: str) -> tuple[str, str]:
    return parse_identifier(identifier, "exporters")


def parse_lease_identifier(identifier: str) -> tuple[str, str]:
    return parse_identifier(identifier, "leases")


class Exporter(BaseModel):
    namespace: str
    name: str
    labels: dict[str, str]
    online: bool = False
    status: ExporterStatus | None = None
    lease: Lease | None = None

    @classmethod
    def from_protobuf(cls, data: client_pb2.Exporter) -> Exporter:
        namespace, name = parse_exporter_identifier(data.name)
        status = None
        if hasattr(data, "status") and data.status:
            status = ExporterStatus.from_proto(data.status)
        return cls(namespace=namespace, name=name, labels=data.labels, online=data.online, status=status)

    @classmethod
    def rich_add_columns(cls, table, options: WithOptions = None):
        add_display_columns(table, options)

    def rich_add_rows(self, table, options: WithOptions = None):
        lease_info = None
        if options and options.show_leases and self.lease:
            lease_client = self.lease.client
            lease_status = self.lease.get_status()
            release_time = ""
            if self.lease.effective_end_time:
                # Ended: use actual end time
                release_time = self.lease.effective_end_time.strftime("%Y-%m-%d %H:%M:%S")
            elif self.lease.effective_begin_time:
                # Active: calculate expected end
                release_time = self.lease.effective_begin_time + self.lease.duration
                release_time = release_time.strftime("%Y-%m-%d %H:%M:%S")
            elif self.lease.begin_time:
                # Scheduled: calculate expected end
                release_time = self.lease.begin_time + self.lease.duration
                release_time = release_time.strftime("%Y-%m-%d %H:%M:%S")
            lease_info = (lease_client, lease_status, release_time)
        elif options and options.show_leases:
            lease_info = ("", "Available", "")
        add_exporter_row(table, self, options, lease_info)

    def rich_add_names(self, names):
        names.append(self.name)


class Lease(BaseModel):
    namespace: str
    name: str
    selector: str
    duration: timedelta
    effective_duration: timedelta | None = None
    begin_time: datetime | None = None
    client: str
    exporter: str
    conditions: list[kubernetes_pb2.Condition]
    effective_begin_time: datetime | None = None
    effective_end_time: datetime | None = None

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        ser_json_timedelta="float",
    )

    @field_serializer("conditions")
    def serialize_conditions(self, conditions: list[kubernetes_pb2.Condition], _info):
        return [json_format.MessageToDict(condition) for condition in conditions]

    @classmethod
    def from_protobuf(cls, data: client_pb2.Lease) -> Lease:
        namespace, name = parse_lease_identifier(data.name)

        _, client = parse_client_identifier(data.client)
        if data.exporter != "":
            _, exporter = parse_exporter_identifier(data.exporter)
        else:
            exporter = ""

        effective_duration = None
        if data.HasField("effective_duration"):
            effective_duration = data.effective_duration.ToTimedelta()

        begin_time = None
        if data.HasField("begin_time"):
            begin_time = data.begin_time.ToDatetime(
                tzinfo=datetime.now().astimezone().tzinfo,
            )

        effective_begin_time = None
        if data.HasField("effective_begin_time"):
            effective_begin_time = data.effective_begin_time.ToDatetime(
                tzinfo=datetime.now().astimezone().tzinfo,
            )

        effective_end_time = None
        if data.HasField("effective_end_time"):
            effective_end_time = data.effective_end_time.ToDatetime(
                tzinfo=datetime.now().astimezone().tzinfo,
            )

        return cls(
            namespace=namespace,
            name=name,
            selector=data.selector,
            duration=data.duration.ToTimedelta(),
            effective_duration=effective_duration,
            begin_time=begin_time,
            client=client,
            exporter=exporter,
            effective_begin_time=effective_begin_time,
            effective_end_time=effective_end_time,
            conditions=data.conditions,
        )

    @classmethod
    def rich_add_columns(cls, table):
        table.add_column("NAME", no_wrap=True)
        table.add_column("SELECTOR")
        table.add_column("BEGIN TIME")
        table.add_column("DURATION")
        table.add_column("CLIENT")
        table.add_column("EXPORTER")

    def rich_add_rows(self, table):
        # Show effective_begin_time if active, otherwise show scheduled begin_time
        begin_time = ""
        if self.effective_begin_time:
            begin_time = self.effective_begin_time.strftime("%Y-%m-%d %H:%M:%S")
        elif self.begin_time:
            begin_time = self.begin_time.strftime("%Y-%m-%d %H:%M:%S")

        # Show actual duration for ended leases, requested duration otherwise
        duration = str(self.effective_duration if self.effective_end_time else self.duration or "")

        table.add_row(
            self.name,
            self.selector,
            begin_time,
            duration,
            self.client,
            self.exporter,
        )

    def rich_add_names(self, names):
        names.append(self.name)

    def get_status(self) -> str:
        """Get the lease status based on conditions"""
        # Check if lease has ended (effective_end_time is set)
        if self.effective_end_time:
            return "Ended"

        if not self.conditions:
            return "Unknown"

        latest_condition = self.conditions[-1]

        if latest_condition.type == "Ready" and latest_condition.status == "True":
            return "In-Use"
        elif latest_condition.type == "Ready" and latest_condition.status == "False":
            return "Waiting"
        elif latest_condition.type == "Expired":
            return "Expired"
        else:
            return latest_condition.reason if latest_condition.reason else "Unknown"


class ExporterList(BaseModel):
    exporters: list[Exporter]
    next_page_token: str | None = Field(exclude=True)
    include_online: bool = Field(default=False, exclude=True)
    include_leases: bool = Field(default=False, exclude=True)
    include_status: bool = Field(default=False, exclude=True)

    @classmethod
    def from_protobuf(cls, data: client_pb2.ListExportersResponse) -> ExporterList:
        return cls(
            exporters=list(map(Exporter.from_protobuf, data.exporters)),
            next_page_token=data.next_page_token,
        )

    def rich_add_columns(self, table):
        options = WithOptions(
            show_online=self.include_online, show_leases=self.include_leases, show_status=self.include_status
        )
        Exporter.rich_add_columns(table, options)

    def rich_add_rows(self, table):
        options = WithOptions(
            show_online=self.include_online, show_leases=self.include_leases, show_status=self.include_status
        )
        for exporter in self.exporters:
            exporter.rich_add_rows(table, options)

    def rich_add_names(self, names):
        for exporter in self.exporters:
            exporter.rich_add_names(names)

    def model_dump_json(self, **kwargs):
        json_kwargs = {k: v for k, v in kwargs.items() if k in {"indent", "separators", "sort_keys", "ensure_ascii"}}

        # Determine which fields to exclude
        exclude_fields = set()
        if not self.include_leases:
            exclude_fields.add("lease")
        if not self.include_online:
            exclude_fields.add("online")
        if not self.include_status:
            exclude_fields.add("status")

        data = {"exporters": [exporter.model_dump(mode="json", exclude=exclude_fields) for exporter in self.exporters]}
        return json.dumps(data, **json_kwargs)

    def model_dump(self, **kwargs):
        exclude_fields = set()
        if not self.include_leases:
            exclude_fields.add("lease")
        if not self.include_online:
            exclude_fields.add("online")
        if not self.include_status:
            exclude_fields.add("status")

        return {"exporters": [exporter.model_dump(mode="json", exclude=exclude_fields) for exporter in self.exporters]}


class LeaseList(BaseModel):
    leases: list[Lease]
    next_page_token: str | None = Field(exclude=True)

    @classmethod
    def from_protobuf(cls, data: client_pb2.ListLeasesResponse) -> LeaseList:
        return cls(
            leases=list(map(Lease.from_protobuf, data.leases)),
            next_page_token=data.next_page_token,
        )

    @classmethod
    def rich_add_columns(cls, table):
        Lease.rich_add_columns(table)

    def rich_add_rows(self, table):
        for lease in self.leases:
            lease.rich_add_rows(table)

    def rich_add_names(self, names):
        for lease in self.leases:
            lease.rich_add_names(names)


@dataclass(kw_only=True, slots=True)
class ClientService:
    channel: Channel
    namespace: str
    stub: client_pb2_grpc.ClientServiceStub = field(init=False)

    def __post_init__(self):
        self.stub = client_pb2_grpc.ClientServiceStub(channel=self.channel)

    async def GetExporter(self, *, name: str):
        with translate_grpc_exceptions():
            exporter = await self.stub.GetExporter(
                client_pb2.GetExporterRequest(
                    name="namespaces/{}/exporters/{}".format(self.namespace, name),
                )
            )
        return Exporter.from_protobuf(exporter)

    async def ListExporters(
        self,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
        filter: str | None = None,
    ):
        with translate_grpc_exceptions():
            exporters = await self.stub.ListExporters(
                client_pb2.ListExportersRequest(
                    parent="namespaces/{}".format(self.namespace),
                    page_size=page_size,
                    page_token=page_token,
                    filter=filter,
                )
            )
        return ExporterList.from_protobuf(exporters)

    async def GetLease(self, *, name: str):
        with translate_grpc_exceptions():
            lease = await self.stub.GetLease(
                client_pb2.GetLeaseRequest(
                    name="namespaces/{}/leases/{}".format(self.namespace, name),
                )
            )
        return Lease.from_protobuf(lease)

    async def ListLeases(
        self,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
        filter: str | None = None,
        only_active: bool = True,
    ):
        with translate_grpc_exceptions():
            leases = await self.stub.ListLeases(
                client_pb2.ListLeasesRequest(
                    parent="namespaces/{}".format(self.namespace),
                    page_size=page_size,
                    page_token=page_token,
                    filter=filter,
                    only_active=only_active,
                )
            )
        return LeaseList.from_protobuf(leases)

    async def CreateLease(
        self,
        *,
        selector: str,
        duration: timedelta,
        begin_time: datetime | None = None,
        lease_id: str | None = None,
    ):
        duration_pb = duration_pb2.Duration()
        duration_pb.FromTimedelta(duration)

        lease_pb = client_pb2.Lease(
            duration=duration_pb,
            selector=selector,
        )

        if begin_time:
            timestamp_pb = timestamp_pb2.Timestamp()
            timestamp_pb.FromDatetime(begin_time)
            lease_pb.begin_time.CopyFrom(timestamp_pb)

        with translate_grpc_exceptions():
            lease = await self.stub.CreateLease(
                client_pb2.CreateLeaseRequest(
                    parent="namespaces/{}".format(self.namespace),
                    lease=lease_pb,
                    lease_id=lease_id or "",
                )
            )
        return Lease.from_protobuf(lease)

    async def UpdateLease(
        self,
        *,
        name: str,
        duration: timedelta | None = None,
        begin_time: datetime | None = None,
    ):
        lease_pb = client_pb2.Lease(
            name="namespaces/{}/leases/{}".format(self.namespace, name),
        )

        update_fields = []

        if duration is not None:
            duration_pb = duration_pb2.Duration()
            duration_pb.FromTimedelta(duration)
            lease_pb.duration.CopyFrom(duration_pb)
            update_fields.append("duration")

        if begin_time is not None:
            timestamp_pb = timestamp_pb2.Timestamp()
            timestamp_pb.FromDatetime(begin_time)
            lease_pb.begin_time.CopyFrom(timestamp_pb)
            update_fields.append("begin_time")

        if not update_fields:
            raise ValueError("At least one of duration or begin_time must be provided")

        update_mask = field_mask_pb2.FieldMask()
        update_mask.FromJsonString(",".join(update_fields))

        with translate_grpc_exceptions():
            lease = await self.stub.UpdateLease(
                client_pb2.UpdateLeaseRequest(
                    lease=lease_pb,
                    update_mask=update_mask,
                )
            )
        return Lease.from_protobuf(lease)

    async def DeleteLease(self, *, name: str):
        with translate_grpc_exceptions():
            await self.stub.DeleteLease(
                client_pb2.DeleteLeaseRequest(
                    name="namespaces/{}/leases/{}".format(self.namespace, name),
                )
            )


@dataclass(frozen=True, slots=True)
class MultipathExporterStub:
    """
    Multipath ExporterServiceStub

    Connecting to exporter service using multiple channels.
    All channels are tried in sequence, and the first one ready
    is used, prioritizing channels in the front.
    """

    channels: InitVar[list[Channel]]

    __stubs: dict[Channel, Any] = field(init=False, default_factory=OrderedDict)

    def __post_init__(self, channels):
        for channel in channels:
            stub = SimpleNamespace()
            jumpstarter_pb2_grpc.ExporterServiceStub.__init__(stub, channel)
            router_pb2_grpc.RouterServiceStub.__init__(stub, channel)
            self.__stubs[channel] = stub

    def __getattr__(self, name):
        for channel, stub in self.__stubs.items():
            # find the first channel that's ready
            if channel.get_state(try_to_connect=True) == ChannelConnectivity.READY:
                return getattr(stub, name)
        # or fallback to the last channel (via router)
        return getattr(next(reversed(self.__stubs.values())), name)
