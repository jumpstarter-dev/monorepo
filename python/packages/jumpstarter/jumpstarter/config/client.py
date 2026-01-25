from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Optional, Self

import grpc
import yaml
from anyio.from_thread import BlockingPortal, start_blocking_portal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .common import CONFIG_PATH, ObjectMeta
from .env import JMP_LEASE
from .grpc import call_credentials
from .shell import ShellConfigV1Alpha1
from .tls import TLSConfigV1Alpha1
from jumpstarter.client.grpc import ClientService, Exporter
from jumpstarter.common.exceptions import (
    ConfigurationError,
    ConnectionError,
    FileNotFoundError,
)
from jumpstarter.common.grpc import aio_secure_channel, ssl_channel_credentials


def _blocking_compat(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(f(*args, **kwargs))
        else:
            return f(*args, **kwargs)

    return wrapper


def _handle_connection_error(f):
    @wraps(f)
    async def wrapper(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except ConnectionError as e:
            if "token is expired" in str(e):
                # args[0] should be self for instance methods
                e.set_config(args[0])
            raise e
        except Exception:
            raise

    return wrapper


class ClientConfigV1Alpha1Drivers(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JMP_DRIVERS_")

    allow: Annotated[list[str], NoDecode] = Field(default_factory=list)
    unsafe: bool = Field(default=False)

    @field_validator("allow", mode="before")
    @classmethod
    def decode_allow(cls, v: str | list[str]) -> list[str]:
        if not isinstance(v, list):
            return list(v.split(","))
        else:
            return v

    @model_validator(mode="after")
    def decode_unsafe(self) -> Self:
        if "UNSAFE" in self.allow:
            self.unsafe = True

        return self


class ClientConfigV1Alpha1Lease(BaseSettings):
    """Configuration for lease operations."""

    acquisition_timeout: int = Field(
        default=7200,
        description="Timeout in seconds for lease acquisition",
        ge=5,  # Must be at least 5 seconds (polling interval)
    )
    dial_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for Dial retry loop when exporter not ready",
        gt=0,
        exclude=True,  # Internal field, not serialized to config files
    )


class ClientConfigV1Alpha1(BaseSettings):
    CLIENT_CONFIGS_PATH: ClassVar[Path] = CONFIG_PATH / "clients"

    model_config = SettingsConfigDict(env_prefix="JMP_")

    alias: str = Field(default="default")
    path: Path | None = Field(default=None)

    apiVersion: Literal["jumpstarter.dev/v1alpha1"] = Field(default="jumpstarter.dev/v1alpha1")
    kind: Literal["ClientConfig"] = Field(default="ClientConfig")

    metadata: ObjectMeta = Field(default_factory=ObjectMeta)

    endpoint: str | None = Field(default=None)
    tls: TLSConfigV1Alpha1 = Field(default_factory=TLSConfigV1Alpha1)
    token: str | None = Field(default=None)
    grpcOptions: dict[str, str | int] | None = Field(default_factory=dict)

    drivers: ClientConfigV1Alpha1Drivers = Field(default_factory=ClientConfigV1Alpha1Drivers)

    shell: ShellConfigV1Alpha1 = Field(default_factory=ShellConfigV1Alpha1)

    leases: ClientConfigV1Alpha1Lease = Field(default_factory=ClientConfigV1Alpha1Lease)

    async def channel(self) -> grpc.aio.Channel:
        if self.endpoint is None or self.token is None:
            raise ConfigurationError("endpoint or token not set in client config")

        credentials = grpc.composite_channel_credentials(
            await ssl_channel_credentials(self.endpoint, self.tls),
            call_credentials("Client", self.metadata, self.token),
        )

        return aio_secure_channel(self.endpoint, credentials, self.grpcOptions)

    @contextmanager
    def lease(
        self,
        selector: str | None = None,
        lease_name: str | None = None,
        duration: timedelta = timedelta(minutes=30),
    ):
        with start_blocking_portal() as portal:
            with portal.wrap_async_context_manager(self.lease_async(selector, lease_name, duration, portal)) as lease:
                yield lease

    @_blocking_compat
    @_handle_connection_error
    async def get_exporter(
        self,
        name: str,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        return await svc.GetExporter(name=name)

    @_blocking_compat
    @_handle_connection_error
    async def list_exporters(
        self,
        page_size: int | None = None,
        page_token: str | None = None,
        filter: str | None = None,
        include_leases: bool = False,
        include_online: bool = False,
        include_status: bool = False,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        exporters_response = await svc.ListExporters(page_size=page_size, page_token=page_token, filter=filter)

        # Set the include flags for display purposes
        exporters_response.include_online = include_online
        exporters_response.include_status = include_status

        if not include_leases:
            return exporters_response

        leases_response = await svc.ListLeases()
        lease_map = {}
        for lease in leases_response.leases:
            if lease.exporter and lease.effective_begin_time:
                if lease.conditions:
                    latest_condition = lease.conditions[-1]
                    if latest_condition.type == "Ready" and latest_condition.status == "True":
                        lease_map[lease.exporter] = lease

        exporters_with_leases = []
        for exporter in exporters_response.exporters:
            lease = lease_map.get(exporter.name)
            exporter_with_lease = Exporter(
                namespace=exporter.namespace,
                name=exporter.name,
                labels=exporter.labels,
                online=exporter.online,
                lease=lease,
            )
            exporters_with_leases.append(exporter_with_lease)
        exporters_response.include_leases = True
        exporters_response.exporters = exporters_with_leases
        return exporters_response

    @_blocking_compat
    @_handle_connection_error
    async def create_lease(
        self,
        selector: str,
        duration: timedelta,
        begin_time: datetime | None = None,
        lease_id: str | None = None,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        return await svc.CreateLease(
            selector=selector,
            duration=duration,
            begin_time=begin_time,
            lease_id=lease_id,
        )

    @_blocking_compat
    @_handle_connection_error
    async def delete_lease(
        self,
        name: str,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        await svc.DeleteLease(
            name=name,
        )

    @_blocking_compat
    @_handle_connection_error
    async def list_leases(
        self,
        page_size: int | None = None,
        page_token: str | None = None,
        filter: str | None = None,
        only_active: bool = True,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        return await svc.ListLeases(
            page_size=page_size,
            page_token=page_token,
            filter=filter,
            only_active=only_active,
        )

    @_blocking_compat
    @_handle_connection_error
    async def update_lease(
        self,
        name: str,
        duration: timedelta | None = None,
        begin_time: datetime | None = None,
    ):
        svc = ClientService(channel=await self.channel(), namespace=self.metadata.namespace)
        return await svc.UpdateLease(name=name, duration=duration, begin_time=begin_time)

    @asynccontextmanager
    async def lease_async(
        self,
        selector: str,
        lease_name: str | None,
        duration: timedelta,
        portal: BlockingPortal,
        acquisition_timeout: timedelta | None = None,
    ):
        from jumpstarter.client import Lease

        # if no lease_name provided, check if it is set in the environment
        lease_name = lease_name or os.environ.get(JMP_LEASE, "")
        # when no lease name is provided, release the lease on exit
        release_lease = lease_name == ""
        try:
            # Convert timedelta to seconds for acquisition_timeout
            acquisition_timeout_seconds = (
                int(acquisition_timeout.total_seconds())
                if acquisition_timeout is not None
                else self.leases.acquisition_timeout
            )
            async with Lease(
                channel=await self.channel(),
                namespace=self.metadata.namespace,
                name=lease_name,
                selector=selector,
                duration=duration,
                portal=portal,
                allow=self.drivers.allow,
                unsafe=self.drivers.unsafe,
                release=release_lease,
                tls_config=self.tls,
                grpc_options=self.grpcOptions,
                acquisition_timeout=acquisition_timeout_seconds,
                dial_timeout=self.leases.dial_timeout,
            ) as lease:
                yield lease

        # this replicates _handle_connection_error, the decorator doesn't work with asynccontextmanager
        except ConnectionError as e:
            if "token is expired" in str(e):
                # args[0] should be self for instance methods
                e.set_config(self)
            raise e
        except Exception:
            raise

    @classmethod
    def from_file(cls, path: os.PathLike):
        with open(path) as f:
            v = cls.model_validate(yaml.safe_load(f))
            v.alias = os.path.basename(path).split(".")[0]
            v.path = Path(path)
            return v

    @classmethod
    def ensure_exists(cls):
        """Check if the clients config dir exists, otherwise create it."""
        os.makedirs(cls.CLIENT_CONFIGS_PATH, exist_ok=True)

    @classmethod
    def try_from_env(cls):
        try:
            return cls.from_env()
        except ValidationError:
            return None

    @classmethod
    def from_env(cls):
        return cls()

    @classmethod
    def _get_path(cls, alias: str) -> Path:
        """Get the regular path of a client config given an alias."""
        return (cls.CLIENT_CONFIGS_PATH / alias).with_suffix(".yaml")

    @classmethod
    def load(cls, alias: str) -> Self:
        """Load a client config by alias."""
        path = cls._get_path(alias)
        if path.exists() is False:
            raise FileNotFoundError(f"Client config '{path}' does not exist.")
        return cls.from_file(path)

    @classmethod
    def save(cls, config: Self, path: Optional[os.PathLike] = None) -> Path:
        """Saves a client config as YAML."""
        # Ensure the clients dir exists
        if path is None:
            cls.ensure_exists()
            # Set the config path before saving
            config.path = cls._get_path(config.alias)
        else:
            config.path = Path(path)
        with config.path.open(mode="w") as f:
            yaml.safe_dump(config.model_dump(mode="json", exclude={"path", "alias"}), f, sort_keys=False)
        return config.path

    @classmethod
    def dump_yaml(cls, config: Self) -> str:
        return yaml.safe_dump(config.model_dump(mode="json", exclude={"path", "alias"}), sort_keys=False)

    @classmethod
    def exists(cls, alias: str) -> bool:
        """Check if a client config exists by alias."""
        return cls._get_path(alias).exists()

    @classmethod
    def list(cls) -> ClientConfigListV1Alpha1:
        """List the available client configs."""
        from .user import UserConfigV1Alpha1

        if cls.CLIENT_CONFIGS_PATH.exists() is False:
            # Return an empty list if the dir does not exist
            return ClientConfigListV1Alpha1(
                current_config=None,
                items=[],
            )

        results = os.listdir(cls.CLIENT_CONFIGS_PATH)
        # Only accept YAML files in the list
        files = filter(lambda x: x.endswith(".yaml"), results)

        def make_config(file: str):
            path = cls.CLIENT_CONFIGS_PATH / file
            return cls.from_file(path)

        current_config = None
        if UserConfigV1Alpha1.exists():
            current_client = UserConfigV1Alpha1.load().config.current_client
            current_config = current_client.alias if current_client is not None else None

        return ClientConfigListV1Alpha1(
            current_config=current_config,
            items=list(map(make_config, files)),
        )

    @classmethod
    def delete(cls, alias: str) -> Path:
        """Delete a client config by alias."""
        path = cls._get_path(alias)
        if path.exists() is False:
            raise FileNotFoundError(f"Client config '{path}' does not exist.")
        path.unlink()
        return path


class ClientConfigListV1Alpha1(BaseModel):
    api_version: Literal["jumpstarter.dev/v1alpha1"] = Field(alias="apiVersion", default="jumpstarter.dev/v1alpha1")
    current_config: Optional[str] = Field(alias="currentConfig")
    items: list[ClientConfigV1Alpha1]
    kind: Literal["ClientConfigList"] = Field(default="ClientConfigList")

    def dump_json(self):
        return self.model_dump_json(indent=4, by_alias=True)

    def dump_yaml(self):
        return yaml.safe_dump(self.model_dump(mode="json", by_alias=True), indent=2)

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    @classmethod
    def rich_add_columns(cls, table):
        table.add_column("CURRENT")
        table.add_column("ALIAS")
        table.add_column("ENDPOINT")
        table.add_column("PATH")

    def rich_add_rows(self, table):
        for client in self.items:
            table.add_row(
                "*" if self.current_config == client.alias else "",
                client.alias,
                client.endpoint,
                str(client.path),
            )

    def rich_add_names(self, names):
        for client in self.items:
            names.append(client.alias)
