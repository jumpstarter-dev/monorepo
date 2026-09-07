from __future__ import annotations

import errno
import logging
import os
import tempfile
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, Self

import grpc
import yaml
from anyio.from_thread import start_blocking_portal
from pydantic import BaseModel, ConfigDict, Field, RootModel

from .common import CONFIG_PATH, ObjectMeta
from .grpc import call_credentials
from .tls import TLSConfigV1Alpha1
from jumpstarter.common.exceptions import ConfigurationError, MissingDriverError
from jumpstarter.common.grpc import aio_secure_channel, ssl_channel_credentials
from jumpstarter.common.importlib import import_class

if TYPE_CHECKING:
    from jumpstarter.driver import Driver

logger = logging.getLogger(__name__)


class HookInstanceConfigV1Alpha1(BaseModel):
    """Configuration for a specific lifecycle hook."""

    model_config = ConfigDict(populate_by_name=True)

    exec_: str | None = Field(
        default=None,
        alias="exec",
        description=(
            "Interpreter used to execute the script (e.g. /bin/bash, python3). "
            "When not set, auto-detected from the script file extension "
            "(.py uses the exporter's Python, .sh uses /bin/sh) or defaults to /bin/sh for inline scripts."
        ),
    )
    script: str = Field(alias="script", description="The j script to execute for this hook")
    timeout: int = Field(default=120, description="The hook execution timeout in seconds (default: 120s)")
    on_failure: Literal[
        "warn",
        "endLease",
        "exit",
    ] = Field(
        default="warn",
        alias="onFailure",
        description=(
            "Action to take when the expected exit code is not returned: 'endLease' to end the lease, "
            "'exit' takes the exporter offline and ends the lease, 'warn' continues and prints a warning"
        ),
    )


class HookConfigV1Alpha1(BaseModel):
    """Configuration for lifecycle hooks."""

    model_config = ConfigDict(populate_by_name=True)

    before_lease: HookInstanceConfigV1Alpha1 | None = Field(default=None, alias="beforeLease")
    after_lease: HookInstanceConfigV1Alpha1 | None = Field(default=None, alias="afterLease")


class FailureDetectionConfigV1Alpha1(BaseModel):
    """Configuration for rapid failure detection in the exporter restart loop.

    If the child process fails within ``rapid_failure_window`` seconds of
    starting, it is counted as a "rapid failure".  After
    ``max_rapid_failures`` consecutive rapid failures the main process exits
    with code 1 so that systemd / the container orchestrator can recreate the
    container.
    """

    model_config = ConfigDict(populate_by_name=True)

    max_rapid_failures: int = Field(
        default=5,
        alias="maxRapidFailures",
        description="Number of consecutive rapid failures before the exporter exits.",
    )
    rapid_failure_window: int = Field(
        default=60,
        alias="rapidFailureWindow",
        description="Seconds - a child that exits faster than this counts as a rapid failure.",
    )


class ExporterConfigV1Alpha1DriverInstanceProxy(BaseModel):
    ref: str


class ExporterConfigV1Alpha1DriverInstanceComposite(BaseModel):
    children: dict[str, ExporterConfigV1Alpha1DriverInstance] = Field(default_factory=dict)


class ExporterConfigV1Alpha1DriverInstanceBase(BaseModel):
    type: str
    description: str | None = None
    methods_description: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    children: dict[str, ExporterConfigV1Alpha1DriverInstance] = Field(default_factory=dict)


class ExporterConfigV1Alpha1DriverInstance(RootModel):
    root: (
        ExporterConfigV1Alpha1DriverInstanceBase
        | ExporterConfigV1Alpha1DriverInstanceComposite
        | ExporterConfigV1Alpha1DriverInstanceProxy
    )

    def instantiate(self) -> "Driver":
        match self.root:
            case ExporterConfigV1Alpha1DriverInstanceBase():
                try:
                    driver_class = import_class(self.root.type, [], True)
                except MissingDriverError:
                    raise ConfigurationError(
                        f"Driver '{self.root.type}' is not installed. Please check exporter configuration."
                    ) from None

                children = {name: child.instantiate() for name, child in self.root.children.items()}

                return driver_class(
                    description=self.root.description,
                    methods_description=self.root.methods_description,
                    children=children,
                    **self.root.config,
                )

            case ExporterConfigV1Alpha1DriverInstanceComposite():
                from jumpstarter_driver_composite.driver import Composite

                children = {name: child.instantiate() for name, child in self.root.children.items()}

                return Composite(children=children)

            case ExporterConfigV1Alpha1DriverInstanceProxy():
                from jumpstarter_driver_composite.driver import Proxy

                return Proxy(ref=self.root.ref)

    @classmethod
    def from_path(cls, path: str) -> ExporterConfigV1Alpha1DriverInstance:
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))

    @classmethod
    def from_str(cls, config: str) -> ExporterConfigV1Alpha1DriverInstance:
        return cls.model_validate(yaml.safe_load(config))


class ExporterConfigV1Alpha1(BaseModel):
    """Exporter configuration (jumpstarter.dev/v1alpha1 ExporterConfig).

    Stores credentials, endpoint, driver tree, hooks, and failure-detection
    settings for a single exporter instance.  The CLI writes new configs to
    ``BASE_PATH`` (user config dir); ``SYSTEM_CONFIG_PATH`` is kept as a
    read fallback for production deployments (systemd units, containers).
    """

    # Default location for exporter configs created via the CLI. Lives under the
    # user config dir (e.g. ~/.config/jumpstarter/exporters), consistent with clients.
    BASE_PATH: ClassVar[Path] = CONFIG_PATH / "exporters"
    # System-wide location, kept as a read fallback so production deployments
    # (systemd units, containers mounting /etc/jumpstarter) keep working.
    SYSTEM_CONFIG_PATH: ClassVar[Path] = Path("/etc/jumpstarter/exporters")

    alias: str = Field(default="default")

    apiVersion: Literal["jumpstarter.dev/v1alpha1"] = Field(default="jumpstarter.dev/v1alpha1")
    kind: Literal["ExporterConfig"] = Field(default="ExporterConfig")
    metadata: ObjectMeta

    endpoint: str | None = Field(default=None)
    tls: TLSConfigV1Alpha1 = Field(default_factory=TLSConfigV1Alpha1)
    token: str | None = Field(default=None)
    grpcOptions: dict[str, str | int] | None = Field(default_factory=dict)

    description: str | None = None
    motd: str | None = None
    export: dict[str, ExporterConfigV1Alpha1DriverInstance] = Field(default_factory=dict)
    hooks: HookConfigV1Alpha1 = Field(default_factory=HookConfigV1Alpha1)
    failure_detection: FailureDetectionConfigV1Alpha1 = Field(
        default_factory=FailureDetectionConfigV1Alpha1,
        alias="failureDetection",
    )
    exit_on_lease_end: bool = Field(
        default=False,
        alias="exitOnLeaseEnd",
    )

    path: Path | None = Field(default=None)

    @classmethod
    def validate_alias(cls, alias: str) -> None:
        if not alias or alias in (".", "..") or any(sep in alias for sep in ("/", "\\")):
            raise ConfigurationError(
                f"Invalid exporter alias '{alias}': must not contain path separators or be '.' / '..'"
            )

    @classmethod
    def _get_path(cls, alias: str) -> Path:
        cls.validate_alias(alias)
        return cls.BASE_PATH / f"{alias}.yaml"

    @classmethod
    def _resolve_path(cls, alias: str) -> Path:
        """Return an alias's config path, preferring the user dir over the production system location.

        Falls back to the system location only when the user-dir file does not exist. When neither
        exists, the user-dir path is returned so callers raise a ``FileNotFoundError`` pointing at
        the current default location.
        """
        user_path = cls._get_path(alias)
        if user_path.exists():
            return user_path
        system_path = cls.SYSTEM_CONFIG_PATH / f"{alias}.yaml"
        if system_path.exists():
            return system_path
        return user_path

    @classmethod
    def user_config_exists(cls, alias: str) -> bool:
        return cls._get_path(alias).exists()

    @classmethod
    def resolve_path(cls, alias: str) -> Path:
        return cls._resolve_path(alias)

    @classmethod
    def exists(cls, alias: str) -> bool:
        """Return True if a config for the alias exists in either the user or system location."""
        return cls._resolve_path(alias).exists()

    @classmethod
    def load_path(cls, path: Path) -> Self:
        with path.open() as f:
            config = cls.model_validate(yaml.safe_load(f))
            config.path = path
            return config

    @classmethod
    def load(cls, alias: str) -> Self:
        """Load the config for an alias, searching the user dir then the system fallback."""
        config = cls.load_path(cls._resolve_path(alias))
        config.alias = alias
        return config

    @classmethod
    def list(cls) -> ExporterConfigListV1Alpha1:
        """List exporter configs from the user and system locations (user takes precedence)."""
        # Aliases in the user config dir take precedence over the production system location.
        aliases: dict[str, None] = {}
        for base in (cls.BASE_PATH, cls.SYSTEM_CONFIG_PATH):
            with suppress(FileNotFoundError):
                for entry in base.iterdir():
                    if entry.suffix == ".yaml":
                        aliases.setdefault(entry.stem, None)
        exporters = []
        for alias in aliases:
            with suppress(FileNotFoundError):
                exporters.append(cls.load(alias))
        return ExporterConfigListV1Alpha1(items=exporters)

    _EXCLUDE_FROM_YAML: ClassVar[set[str]] = {"alias", "path"}

    @classmethod
    def dump_yaml(self, config: Self) -> str:
        """Serialize a config to a YAML string, omitting internal fields (alias, path)."""
        return yaml.safe_dump(
            config.model_dump(mode="json", by_alias=True, exclude=self._EXCLUDE_FROM_YAML),
            sort_keys=False,
        )

    @classmethod
    def save(cls, config: Self, path: Optional[str] = None) -> Path:
        """Save the config to disk, defaulting to the user config dir when no path is given."""
        # Set the config path before saving
        if path is None:
            config.path = cls._get_path(config.alias)
        else:
            config.path = Path(path)
        config.path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(prefix=f".{config.path.name}.", dir=config.path.parent)
        try:
            os.fchmod(temp_fd, 0o600)
            with os.fdopen(temp_fd, "w") as f:
                yaml.safe_dump(
                    config.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude=cls._EXCLUDE_FROM_YAML,
                    ),
                    f,
                    sort_keys=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, config.path)
            os.chmod(config.path, 0o600)
        finally:
            try:
                os.unlink(temp_path)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
        return config.path

    @classmethod
    def delete(cls, alias: str) -> Path:
        """Delete the user-dir config file for an alias and return its path."""
        path = cls._get_path(alias)
        if not path.exists():
            system_path = cls.SYSTEM_CONFIG_PATH / f"{alias}.yaml"
            if system_path.exists():
                raise ConfigurationError(
                    f"Exporter config '{alias}' exists only in the system location"
                    f" '{system_path}' and cannot be deleted."
                )
            return path
        path.unlink()
        return path

    @asynccontextmanager
    async def serve_unix_async(self):
        # dynamic import to avoid circular imports
        from jumpstarter.common import ExporterStatus
        from jumpstarter.exporter import Session

        with Session(
            root_device=ExporterConfigV1Alpha1DriverInstance(
                type="jumpstarter_driver_composite.driver.Composite",
                description=self.description,
                children=self.export,
            ).instantiate(),
            motd=self.motd,
        ) as session:
            async with session.serve_unix_async() as path:
                # For local usage, set status to LEASE_READY since there's no lease/hook flow
                session.update_status(ExporterStatus.LEASE_READY)
                yield path

    @contextmanager
    def serve_unix(self):
        with start_blocking_portal() as portal:
            with portal.wrap_async_context_manager(self.serve_unix_async()) as path:
                yield path

    @asynccontextmanager
    async def create_exporter(self, *, standalone: bool = False):
        """Create and manage an exporter instance with proper lifecycle.

        When standalone is True, channel_factory is a no-op (never used);
        use exporter.serve_standalone_tcp() instead of exporter.serve().
        """
        # dynamic import to avoid circular imports
        from anyio import CancelScope

        from jumpstarter.exporter import Exporter

        async def channel_factory() -> grpc.aio.Channel:
            if self.endpoint is None or self.token is None:
                raise ConfigurationError("endpoint or token not set in exporter config")
            credentials = grpc.composite_channel_credentials(
                await ssl_channel_credentials(self.endpoint, self.tls),
                call_credentials("Exporter", self.metadata, self.token),
            )
            return aio_secure_channel(self.endpoint, credentials, self.grpcOptions)

        async def dummy_channel_factory() -> grpc.aio.Channel:
            raise RuntimeError("channel_factory must not be called in standalone mode")

        # Create hook executor if hooks are configured
        hook_executor = None
        if self.hooks.before_lease or self.hooks.after_lease:
            from jumpstarter.exporter.hooks import HookExecutor

            hook_executor = HookExecutor(
                config=self.hooks,
            )

        async def on_token_rotated(new_token: str):
            self.token = new_token
            if self.path is not None:
                try:
                    self.save(self, path=str(self.path))
                    logger.info("Saved rotated exporter token to %s", self.path)
                except OSError as e:
                    logger.warning("Could not save rotated token to %s: %s", self.path, e)

        exporter = None
        entered = False
        try:
            exporter = Exporter(
                token=self.token or "",
                exporter_name=self.metadata.name,
                channel_factory=dummy_channel_factory if standalone else channel_factory,
                device_factory=ExporterConfigV1Alpha1DriverInstance(
                    type="jumpstarter_driver_composite.driver.Composite",
                    description=self.description,
                    children=self.export,
                ).instantiate,
                tls=self.tls,
                grpc_options=self.grpcOptions,
                hook_executor=hook_executor,
                motd=self.motd,
                exit_on_lease_end=self.exit_on_lease_end,
                on_token_rotated=on_token_rotated,
            )
            # Initialize the exporter (registration, etc.)
            await exporter.__aenter__()
            entered = True
            yield exporter
        finally:
            # Shield all cleanup operations from abrupt cancellation for clean shutdown
            if exporter and entered:
                with CancelScope(shield=True):
                    await exporter.__aexit__(None, None, None)

    async def rotate_token(self) -> str:
        """Rotate the exporter authentication token with the controller."""
        async with self.create_exporter() as exporter:
            return await exporter.rotate_token()

    async def serve(self):
        async with self.create_exporter() as exporter:
            await exporter.serve()


class ExporterConfigListV1Alpha1(BaseModel):
    api_version: Literal["jumpstarter.dev/v1alpha1"] = Field(alias="apiVersion", default="jumpstarter.dev/v1alpha1")
    items: list[ExporterConfigV1Alpha1]
    kind: Literal["ExporterConfigList"] = Field(default="ExporterConfigList")

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    @classmethod
    def rich_add_columns(cls, table):
        table.add_column("ALIAS")
        table.add_column("PATH")

    def rich_add_rows(self, table):
        for exporter in self.items:
            table.add_row(
                exporter.alias,
                str(exporter.path),
            )

    def rich_add_names(self, names):
        for exporter in self.items:
            names.append(exporter.alias)
