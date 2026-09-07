from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from secrets import randbits
from subprocess import PIPE, CalledProcessError, Popen, TimeoutExpired
from tempfile import TemporaryDirectory
from typing import Literal

import yaml
from anyio import fail_after, run_process, sleep
from anyio.streams.file import FileReadStream, FileWriteStream
from jumpstarter_driver_network.driver import TcpNetwork, UnixNetwork, VsockNetwork
from jumpstarter_driver_power.driver import PowerInterface, PowerReading
from jumpstarter_driver_pyserial.driver import PySerial
from pydantic import BaseModel, ByteSize, Field, TypeAdapter, ValidationError, validate_call
from qemu.qmp import QMPClient
from qemu.qmp.protocol import ConnectError, Runstate

from jumpstarter.common.fls import get_fls_binary
from jumpstarter.driver import Driver, FlasherInterface, export
from jumpstarter.streams.encoding import AutoDecompressIterator


def _vsock_available(socket_path: str = "/dev/vhost-vsock") -> bool:
    if platform.system() != "Linux":
        return False

    if not os.path.exists(socket_path):
        return False

    return os.access(socket_path, os.R_OK | os.W_OK)


class QmpLogFilter(logging.Filter):
    def filter(self, record):
        return False


async def _read_pipe(stream: asyncio.StreamReader, name: str, queue: asyncio.Queue):
    while True:
        line = await stream.readline()
        if not line:
            break
        await queue.put((name, line.decode("utf-8", errors="replace")))
    await queue.put((name, None))


@dataclass(kw_only=True)
class QemuFlasher(FlasherInterface, Driver):
    parent: Qemu

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_qemu.client.QemuFlasherClient"

    @export
    async def flash(self, source, partition: str | None = None):
        """Flash an image to the specified partition.

        Accepts OCI image references (oci://...) or streamed image data.
        Supports transparent decompression of gzip, xz, bz2, and zstd compressed images.
        Compression format is auto-detected from file signature.
        """
        if isinstance(source, str) and source.startswith("oci://"):
            async for _ in self.flash_oci(source, partition):
                pass
            return

        async with await FileWriteStream.from_path(self.parent.validate_partition(partition)) as stream:
            async with self.resource(source) as res:
                # Wrap with auto-decompression to handle .gz, .xz, .bz2, .zstd files
                async for chunk in AutoDecompressIterator(source=res):
                    await stream.send(chunk)

    @export
    async def flash_oci(
        self,
        oci_url: str,
        partition: str | None = None,
        oci_username: str | None = None,
        oci_password: str | None = None,
    ) -> AsyncGenerator[tuple[str, str, int | None], None]:
        """Flash an OCI image to the specified partition using fls.

        Streams subprocess output back to the caller as it arrives.
        Yields (stdout_chunk, stderr_chunk, returncode) tuples.
        returncode is None until the process completes.

        Args:
            oci_url: OCI image reference (must start with oci://)
            partition: Target partition name (default: root)
            oci_username: Registry username for OCI authentication
            oci_password: Registry password for OCI authentication
        """
        if not oci_url.startswith("oci://"):
            raise ValueError(f"OCI URL must start with oci://, got: {oci_url}")

        from jumpstarter.common.oci import resolve_oci_credentials

        creds = resolve_oci_credentials(oci_url, username=oci_username, password=oci_password)

        target_path = str(self.parent.validate_partition(partition))

        fls_binary = get_fls_binary(
            fls_version=self.parent.fls_version,
            fls_binary_url=self.parent.fls_custom_binary_url,
            allow_custom_binaries=self.parent.fls_allow_custom_binaries,
        )

        fls_cmd = [fls_binary, "from-url", oci_url, target_path]

        fls_env = None
        if creds.is_authenticated:
            fls_env = os.environ.copy()
            fls_env["FLS_REGISTRY_USERNAME"] = creds.username
            fls_env["FLS_REGISTRY_PASSWORD"] = creds.plain_password

        self.logger.info(f"Running fls: {' '.join(fls_cmd)}")

        try:
            async for chunk in self._stream_subprocess(fls_cmd, fls_env):
                yield chunk
        except FileNotFoundError:
            raise RuntimeError("fls command not found. Install fls or configure fls_version in the driver.") from None

    async def _stream_subprocess(
        self, cmd: list[str], env: dict[str, str] | None
    ) -> AsyncGenerator[tuple[str, str, int | None], None]:
        """Run a subprocess and yield (stdout, stderr, returncode) tuples as output arrives."""
        process = await asyncio.create_subprocess_exec(  # ty: ignore[missing-argument]
            *cmd,
            stdout=asyncio.subprocess.PIPE,  # ty: ignore[unresolved-attribute]
            stderr=asyncio.subprocess.PIPE,  # ty: ignore[unresolved-attribute]
            env=env,
        )

        output_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        tasks = [
            asyncio.create_task(_read_pipe(process.stdout, "stdout", output_queue)),
            asyncio.create_task(_read_pipe(process.stderr, "stderr", output_queue)),
        ]

        finished_streams = 0
        start_time = asyncio.get_running_loop().time()

        try:
            while finished_streams < 2:
                elapsed = asyncio.get_running_loop().time() - start_time
                if elapsed >= self.parent.flash_timeout:
                    process.kill()
                    await process.wait()
                    raise RuntimeError(f"fls flash timed out after {self.parent.flash_timeout}s")

                remaining = self.parent.flash_timeout - elapsed
                try:
                    name, text = await asyncio.wait_for(output_queue.get(), timeout=min(remaining, 30))
                except asyncio.TimeoutError:
                    continue

                if text is None:
                    finished_streams += 1
                    continue

                stdout_chunk = text if name == "stdout" else ""
                stderr_chunk = text if name == "stderr" else ""
                yield stdout_chunk, stderr_chunk, None

            await process.wait()
            returncode = process.returncode

            if returncode != 0:
                self.logger.error(f"fls failed - return code: {returncode}")
                raise RuntimeError(f"fls flash failed (return code {returncode})")

            self.logger.info("OCI flash completed successfully")
            yield "", "", returncode
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.returncode is None:
                process.kill()
                await process.wait()

    @export
    async def dump(self, target, partition: str | None = None):
        async with await FileReadStream.from_path(
            self.parent.validate_partition(partition, use_default_partitions=True)
        ) as stream:
            async with self.resource(target) as res:
                async for chunk in stream:
                    await res.send(chunk)


@dataclass(kw_only=True)
class QemuPower(PowerInterface, Driver):
    parent: Qemu

    @export
    async def on(self) -> None:  # noqa: C901
        if hasattr(self, "_process"):
            self.logger.warning("already powered on, ignoring request")
            return

        root = self.parent.validate_partition("root", use_default_partitions=True)
        bios = self.parent.validate_partition("bios", use_default_partitions=True)
        ovmf_code = self.parent.validate_partition("OVMF_CODE.fd", use_default_partitions=True)
        ovmf_vars = self.parent.validate_partition("OVMF_VARS.fd", use_default_partitions=True)

        cpu = self.parent.cpu

        cmdline = [
            f"qemu-system-{self.parent.arch}",
            "-nodefaults",
            "-nographic",
        ]

        if self.parent.arch == platform.machine() and os.access("/dev/kvm", os.R_OK | os.W_OK):
            cmdline += [
                "-accel",
                "kvm",
            ]

            if cpu is None:
                cpu = "host"

        match self.parent.arch:
            case "aarch64":
                cmdline += ["-machine", "virt"]
            case "x86_64":
                cmdline += ["-machine", "q35"]

        if cpu is None:
            match self.parent.arch:
                case "aarch64":
                    cpu = "cortex-a57"
                case "x86_64":
                    cpu = "qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt"

        cmdline += [
            "-cpu",
            cpu,
            "-qmp",
            f"unix:{self.parent._qmp},server=on,wait=off",
            "-smp",
            str(self.parent.smp),
            "-m",
            self.parent.mem,
            "-vnc",
            f"unix:{self.parent._vnc}",
            "-vga",
            "none",
            "-serial",
            f"unix:{self.parent._serial_socket},server=on,wait=off"
            if self.parent.launcher_socket
            else "pty",
            "-netdev",
            ",".join(
                ["user", "id=eth0"]
                + [
                    "hostfwd={}:{}:{}-:{}".format(v.protocol, v.hostaddr, v.hostport, v.guestport)
                    for k, v in self.parent.hostfwd.items()
                ]
            ),
        ]

        devices = [
            "virtio-net-pci,netdev=eth0",
            "virtio-gpu-pci",
        ]

        if _vsock_available():
            devices.append("vhost-vsock-pci,guest-cid={}".format(self.parent._cid))

        for device in devices:
            cmdline += ["-device", device]

        if bios.exists() or self.parent._runtime_firmware_path(bios):
            cmdline += [
                "-bios",
                str(bios),
            ]

        if (ovmf_code.exists() or self.parent._runtime_firmware_path(ovmf_code)) and (
            ovmf_vars.exists() or self.parent._runtime_firmware_path(ovmf_vars)
        ):
            cmdline += [
                "-drive",
                f"file={ovmf_code},if=pflash,format=raw,unit=0,readonly=on",
                "-drive",
                f"file={ovmf_vars},if=pflash,format=raw,unit=1,snapshot=on,readonly=off",
            ]

        if root.exists():
            proc = await run_process(
                self.parent._wrap_command(["qemu-img", "info", "--output=json", str(root)]),
                stdout=PIPE,
                stderr=PIPE,
            )
            try:
                proc.check_returncode()
                info = json.loads(proc.stdout)
                image_format = info.get("format", "raw")
                current_virtual_size = info.get("virtual-size") or root.stat().st_size
                match image_format:
                    case "raw" | "qcow2" | "qcow" | "vmdk":
                        image_driver = image_format
                    case _:
                        raise ValueError(f"unsupported image format: {image_format}")
            except CalledProcessError:
                self.logger.warning("unable to detect image format, assuming raw")
                image_driver = "raw"
                current_virtual_size = root.stat().st_size

            # Resize disk if configured
            if self.parent.disk_size:
                requested = self.parent._parse_size(self.parent.disk_size)

                if requested < current_virtual_size:
                    raise RuntimeError(
                        f"Shrinking disk is not supported: current {ByteSize(current_virtual_size).human_readable()}, "
                        f"requested {self.parent.disk_size}"
                    )

                available = shutil.disk_usage(root.parent).free
                if requested > available:
                    raise RuntimeError(
                        f"Not enough disk space: need {ByteSize(requested).human_readable()}, "
                        f"only {ByteSize(available).human_readable()} available"
                    )

                if requested > current_virtual_size:
                    self.logger.info(f"Resizing disk to {ByteSize(requested).human_readable()}")
                    proc = await run_process(
                        self.parent._wrap_command(["qemu-img", "resize", str(root), str(requested)]),
                        stdout=PIPE,
                        stderr=PIPE,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(f"Failed to resize disk: {proc.stderr.decode()}")

            cmdline += [
                "-blockdev",
                f"driver={image_driver},node-name=rootfs,file.driver=file,file.filename={root}",
                "-device",
                "virtio-blk-pci,drive=rootfs,bootindex=1",
            ]

        self._cidata = self.parent.cidata()

        cmdline += [
            "-blockdev",
            f"driver=vvfat,node-name=cidata,read-only=on,dir={self._cidata.name},label=CIDATA",
            "-device",
            "virtio-blk-pci,drive=cidata",
        ]

        self._process = Popen(self.parent._wrap_command(cmdline), stdin=PIPE)

        qmp = QMPClient(self.parent.hostname)

        logging.getLogger(
            "qemu.qmp.protocol.{}".format(self.parent.hostname),
        ).addFilter(QmpLogFilter())

        with fail_after(10):
            while qmp.runstate != Runstate.RUNNING:
                try:
                    await qmp.connect(self.parent._qmp)
                except ConnectError:
                    await sleep(0.5)

        if not self.parent.launcher_socket:
            chardevs = await qmp.execute("query-chardev")
            pty = next(c for c in chardevs if c["label"] == "serial0")["filename"].lstrip("pty:")
            Path(self.parent._pty).unlink(missing_ok=True)
            Path(self.parent._pty).symlink_to(pty)

        await qmp.execute("system_reset")
        await qmp.disconnect()

    @export
    def off(self) -> None:
        if hasattr(self, "_process"):
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except TimeoutExpired:
                self._process.kill()
            del self._process
        else:
            self.logger.warning("already powered off, ignoring request")

        if hasattr(self, "_cidata"):
            del self._cidata

    @export
    async def read(self) -> AsyncGenerator[PowerReading, None]:
        raise NotImplementedError

    def close(self):
        self.off()


class Hostfwd(BaseModel):
    protocol: Literal["tcp"] = "tcp"
    hostaddr: str = "127.0.0.1"
    hostport: int = Field(ge=1, le=65535)
    guestport: int = Field(ge=1, le=65535)


@dataclass(kw_only=True)
class Qemu(Driver):
    """QEMU virtual machine management driver."""

    driver_type = "composite"

    arch: str = field(default_factory=platform.machine)
    cpu: str | None = None

    smp: int = 2
    mem: str = "512M"
    disk_size: str | None = None  # e.g., "20G" (resize disk before boot)

    hostname: str = "demo"
    username: str = "jumpstarter"
    password: str = "password"

    default_partitions: dict[str, Path] = field(default_factory=dict)

    hostfwd: dict[str, Hostfwd] = field(default_factory=dict)

    # FLS configuration for OCI flashing
    fls_version: str | None = field(default=None)
    fls_allow_custom_binaries: bool = field(default=False)
    fls_custom_binary_url: str | None = field(default=None)
    flash_timeout: int = field(default=30 * 60)  # 30 minutes

    # Sidecar mode: path to the jumpstarter-exec launcher socket.
    # When set, commands are executed remotely in the runtime
    # container via jumpstarter-exec over this Unix socket.
    launcher_socket: str | None = None

    _tmp_dir: TemporaryDirectory = field(init=False, default_factory=TemporaryDirectory)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_qemu.client.QemuClient"

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        self.hostfwd = {k: Hostfwd.model_validate(v) for k, v in self.hostfwd.items()}
        self.default_partitions = {k: Path(v) for k, v in self.default_partitions.items()}

        self.children["power"] = QemuPower(parent=self)
        self.children["flasher"] = QemuFlasher(parent=self)

        if self.launcher_socket:
            self.children["console"] = UnixNetwork(path=self._serial_socket)
        else:
            self.children["console"] = PySerial(url=self._pty, check_present=False)

        self.children["vnc"] = UnixNetwork(path=self._vnc)

        if _vsock_available():
            self.children["ssh"] = VsockNetwork(cid=self._cid, port=22)

        for k, v in self.hostfwd.items():
            match v.protocol:
                case "tcp":
                    self.children[k] = TcpNetwork(host=v.hostaddr, port=v.hostport)

    @property
    def _work_dir(self) -> str:
        """Directory for sockets and jumpstarter-exec in sidecar mode."""
        if self.launcher_socket:
            # Sidecar: QEMU only sees the shared volume. Derive from the
            # socket path so production (/shared/launcher.sock) and tests
            # (tmpdir/shared/launcher.sock) both place cidata correctly.
            return str(Path(self.launcher_socket).parent)
        return self._tmp_dir.name

    @property
    def _disk_dir(self) -> str:
        """Directory for flashable guest disk images (root, bios, …)."""
        if self.launcher_socket:
            work = Path(self._work_dir)
            # Production sidecar: shared volume for sockets, separate mount for guest disk.
            # Keep in sync with Go constants in exporterset/provisioners/qemu and disk packages.
            if work == Path("/shared"):
                return "/disk"
            return str(work)
        return self._tmp_dir.name

    @property
    def _pty(self) -> str:
        return str(Path(self._work_dir) / "pty")

    @property
    def _serial_socket(self) -> str:
        return str(Path(self._work_dir) / "serial.sock")

    @property
    def _vnc(self) -> str:
        return str(Path(self._work_dir) / "vnc")

    @property
    def _qmp(self) -> str:
        return str(Path(self._work_dir) / "qmp")

    def _wrap_command(self, cmd: list[str]) -> list[str]:
        """Wrap a command with jumpstarter-exec for remote execution in sidecar mode."""
        if self.launcher_socket:
            return [
                str(Path(self._work_dir) / "jumpstarter-exec"),
                "exec", "--socket", self.launcher_socket, "--",
            ] + cmd
        return cmd

    @cached_property
    def _cid(self) -> int:
        return randbits(32)

    def _runtime_firmware_path(self, path: Path) -> bool:
        """True when path is a default firmware path that lives in the runtime image."""
        return self.launcher_socket is not None and path in self.default_partitions.values()

    def validate_partition(
        self,
        partition: str | None = None,
        use_default_partitions: bool = False,
    ) -> Path:
        match partition:
            case "root" | None:
                path = Path(self._disk_dir) / "root"
            case "OVMF_CODE.fd":
                path = Path(self._disk_dir) / "OVMF_CODE.fd"
            case "OVMF_VARS.fd":
                path = Path(self._disk_dir) / "OVMF_VARS.fd"
            case "bios":
                path = Path(self._disk_dir) / "bios"
            case _:
                raise ValueError(f"invalid partition name: {partition}")

        if not path.exists() and partition in self.default_partitions and use_default_partitions:
            return self.default_partitions[partition]

        return path

    def cidata(self) -> TemporaryDirectory:
        # In sidecar mode QEMU runs in the runtime container and can only
        # see paths on the shared volume — never the exporter's /tmp.
        # Runtime runs as root (pod securityContext); TemporaryDirectory's
        # default 0o700 is fine for cross-container read access.
        tmp = TemporaryDirectory(dir=self._work_dir)
        path = Path(tmp.name)

        (path / "meta-data").write_text(
            yaml.safe_dump(
                {
                    "instance-id": str(self.uuid),
                    "local-hostname": self.hostname,
                }
            )
        )
        (path / "user-data").write_text(
            "#cloud-config\n"
            + yaml.safe_dump(
                {
                    "ssh_pwauth": True,
                    "users": [
                        {
                            "name": self.username,
                            "plain_text_passwd": self.password,
                            "lock_passwd": False,
                            "sudo": "ALL=(ALL) NOPASSWD:ALL",
                        }
                    ],
                }
            )
        )

        return tmp

    @export
    @validate_call(validate_return=True)
    def get_hostname(self) -> str:
        return self.hostname

    @export
    @validate_call(validate_return=True)
    def get_username(self) -> str:
        return self.username

    @export
    @validate_call(validate_return=True)
    def get_password(self) -> str:
        return self.password

    def _parse_size(self, size: str) -> int:
        """Parse size string (e.g., '20G') to bytes."""
        try:
            return int(TypeAdapter(ByteSize).validate_python(size + "iB" if size[-1] in "kmgtKMGT" else size))
        except (ValidationError, IndexError):
            raise ValueError(f"Invalid size: '{size}'. Use e.g. '20G', '512M', '2T'") from None

    @export
    @validate_call(validate_return=True)
    def set_disk_size(self, size: str) -> None:
        """Set the disk size for resizing before boot."""
        self._parse_size(size)  # Validate
        self.disk_size = size

    @export
    @validate_call(validate_return=True)
    def set_memory_size(self, size: str) -> None:
        """Set the memory size for next boot."""
        self._parse_size(size)  # Validate
        self.mem = size
