import asyncio
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from jumpstarter_driver_opendal.driver import Opendal

from .tac import PROMPT, send_power_commands_sequence
from jumpstarter.common.exceptions import ConfigurationError
from jumpstarter.common.fls import get_fls_binary
from jumpstarter.driver import Driver, export


@dataclass(kw_only=True)
class RideSXDriver(Driver):
    """RideSX Driver"""

    driver_type = "automotive"

    decompression_timeout: int = field(default=15 * 60)  # 15 minutes
    flash_timeout: int = field(default=30 * 60)  # 30 minutes
    continue_timeout: int = field(default=20 * 60)  # 20 minutes
    erase_timeout: int = field(default=120)  # 2 minutes
    storage_dir: str = field(default="/var/lib/jumpstarter/ridesx")

    # FLS configuration
    fls_version: str | None = field(default=None)
    fls_allow_custom_binaries: bool = field(
        default=False,
        metadata={
            "help": "⚠️  SECURITY WARNING: Enables downloading custom FLS binaries. Only use in trusted environments."
        }
    )
    fls_custom_binary_url: str | None = field(
        default=None,
        metadata={"help": "Custom URL for FLS binary download. Requires fls_allow_custom_binaries=True."}
    )

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        if "serial" not in self.children:
            raise ConfigurationError("'serial' instance is required")

        # Security warning for custom binary downloads
        if self.fls_allow_custom_binaries:
            self.logger.warning(
                "⚠️  SECURITY WARNING: Custom FLS binary downloads are enabled. "
                "This allows arbitrary code execution on the exporter host. "
                "Only use this in trusted environments with verified binary sources."
            )

        Path(self.storage_dir).mkdir(parents=True, exist_ok=True)
        self.children["storage"] = Opendal(
            scheme="fs",
            kwargs={"root": self.storage_dir},
            remove_created_on_close=True,  # Clean up temporary firmware files on close
        )

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_ridesx.client.RideSXClient"

    def _get_decompression_command(self, filename: str) -> str:
        if filename.endswith((".gz", ".gzip")):
            return "zcat"
        elif filename.endswith(".xz"):
            return "xzcat"
        else:
            return "cat"

    def _needs_decompression(self, filename: str) -> bool:
        return filename.endswith((".gz", ".gzip", ".xz"))

    def _decompress_file(self, compressed_file: Path) -> Path:
        if compressed_file.name.endswith(".xz"):
            decompressed_name = compressed_file.name[:-3]
        elif compressed_file.name.endswith(".gz"):
            decompressed_name = compressed_file.name[:-3]
        elif compressed_file.name.endswith(".gzip"):
            decompressed_name = compressed_file.name[:-5]
        else:
            return compressed_file

        decompressed_file = compressed_file.parent / decompressed_name

        self.logger.info(f"decompressing {compressed_file.name} to {decompressed_name}")

        decompress_cmd = self._get_decompression_command(compressed_file.name)

        try:
            cmd = f"{decompress_cmd} '{compressed_file}' > '{decompressed_file}'"
            self.logger.debug(f"running decompression command: {cmd}")

            with open(decompressed_file, "wb") as output_file:
                self.logger.debug(f"running decompression command: {decompress_cmd} {compressed_file}")
                result = subprocess.run(
                    [decompress_cmd, str(compressed_file)],
                    stdout=output_file,
                    stderr=subprocess.PIPE,
                    text=False,
                    check=True,
                    timeout=self.decompression_timeout,
                )

            if result.stderr:
                self.logger.debug(f"decompression stderr: {result.stderr}")

            if not decompressed_file.exists() or decompressed_file.stat().st_size == 0:
                raise RuntimeError("decompression failed: output file is missing or empty")

            self.logger.info(f"successfully decompressed {compressed_file.name}")

            # Register with Opendal for automatic cleanup on close
            storage = self.children["storage"]
            relative_path = decompressed_file.relative_to(Path(self.storage_dir))
            storage.register_path(str(relative_path))

            return decompressed_file

        except subprocess.CalledProcessError as e:
            self.logger.error(f"decompression failed - return code: {e.returncode}")
            self.logger.error(f"stdout: {e.stdout}")
            self.logger.error(f"stderr: {e.stderr}")
            raise RuntimeError(f"failed to decompress {compressed_file.name}: {e}") from e
        except subprocess.TimeoutExpired:
            self.logger.error(f"decompression timed out for {compressed_file.name}")
            raise RuntimeError(f"decompression timeout for {compressed_file.name}") from None

    @export
    def detect_fastboot_device(self, max_attempts: int = 5, delay: float = 2.0):
        max_attempts = int(max_attempts)
        delay = float(delay)

        self.logger.info("checking for fastboot devices...")

        for attempt in range(max_attempts):
            try:
                self.logger.debug(f"running: fastboot devices -l (attempt {attempt + 1}/{max_attempts})")
                result = subprocess.run(
                    ["fastboot", "devices", "-l"], capture_output=True, text=True, check=True, timeout=10
                )

                self.logger.debug(f"fastboot devices output: {result.stdout.strip()}")
                self.logger.debug(f"fastboot devices return code: {result.returncode}")

                if result.stdout.strip():
                    device_id = result.stdout.strip().split()[0]
                    self.logger.info(f"Found fastboot device: {device_id}")
                    return {"status": "device_found", "device_id": device_id}
                else:
                    self.logger.warning(f"No fastboot devices found on attempt {attempt + 1}/{max_attempts}")
                    if attempt < max_attempts - 1:
                        time.sleep(delay)
            except subprocess.TimeoutExpired:
                self.logger.warning(f"Fastboot command timed out on attempt {attempt + 1}/{max_attempts}")
                if attempt < max_attempts - 1:
                    time.sleep(delay)
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Fastboot command failed with return code {e.returncode}")
                raise RuntimeError(f"Fastboot command failed: {e}") from e
            except FileNotFoundError:
                raise RuntimeError("fastboot command not found") from None

        self.logger.error("No fastboot devices found after all attempts")
        try:
            self.logger.info("Final attempt with verbose fastboot output...")
            result = subprocess.run(["fastboot", "devices", "-l"], capture_output=True, text=True, timeout=10)
            self.logger.error(f"Final fastboot stdout: '{result.stdout}'")
            self.logger.error(f"Final fastboot stderr: '{result.stderr}'")
        except Exception as e:
            self.logger.error(f"Final fastboot check failed: {e}")

        return {"status": "no_device_found", "device_id": None}

    @export
    def flash_with_fastboot(self, device_id: str, partitions: Dict[str, str]):
        """Flash partitions using fastboot

        Args:
            device_id: The fastboot device ID
            partitions: Dictionary mapping partition names to filenames
        """
        if not partitions:
            raise ValueError("At least one partition must be provided")

        self.logger.info(f"Flashing device {device_id} with partitions: {list(partitions.keys())}")

        for partition_name, filename in partitions.items():
            file_path = Path(self.storage_dir) / filename
            if not file_path.exists():
                raise FileNotFoundError(f"Image not found in storage: {filename}")

            if self._needs_decompression(filename):
                file_path = self._decompress_file(file_path)

            self.logger.info(f"Flashing {partition_name}: {file_path.name}")

            cmd = ["fastboot", "-s", device_id, "flash", partition_name, str(file_path)]
            self.logger.debug(f"Running command: {' '.join(cmd)}")

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=self.flash_timeout)
                self.logger.info(f"Successfully flashed {partition_name}")
                self.logger.debug(f"Flash stdout: {result.stdout}")
                if result.stderr:
                    self.logger.debug(f"Flash stderr: {result.stderr}")
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Failed to flash {partition_name} - return code: {e.returncode}")
                self.logger.error(f"stdout: {e.stdout}")
                self.logger.error(f"stderr: {e.stderr}")
                raise RuntimeError(f"Failed to flash {partition_name}: {e}") from e
            except subprocess.TimeoutExpired:
                self.logger.error(f"timeout while flashing {partition_name}")
                raise RuntimeError(f"timeout while flashing {partition_name}") from None

        self.logger.info("Running fastboot continue...")
        cmd = ["fastboot", "-s", device_id, "continue"]
        self.logger.debug(f"Running command: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=self.continue_timeout)
            self.logger.debug(f"Fastboot continue stdout: {result.stdout}")
            self.logger.debug(f"Fastboot continue stderr: {result.stderr}")
            self.logger.info("Fastboot continue completed successfully")
        except subprocess.CalledProcessError as e:
            self.logger.warning(f"Fastboot continue failed - return code: {e.returncode}")
            self.logger.warning(f"stdout: {e.stdout}")
            self.logger.warning(f"stderr: {e.stderr}")

    @staticmethod
    def _validate_oci_url(oci_url: str):
        """Validate that the URL is a proper OCI reference."""
        if oci_url.startswith("oci://"):
            return
        hint = ""
        if ":" in oci_url:
            _, after = oci_url.split(":", 1)
            if after.startswith(("/", "./", "../", "~")):
                hint = (
                    f"\n\nIt looks like '{oci_url}' is a partition:path mapping, not an OCI reference.\n"
                    f"For local files, use: j storage flash -t {oci_url}"
                )
        raise ValueError(
            f"OCI URL must start with oci://, got: {oci_url}"
            f"{hint}"
        )

    def _build_fls_command(self, oci_url, partitions):
        """Build FLS fastboot command and environment."""
        fls_binary = get_fls_binary(
            fls_version=self.fls_version,
            fls_binary_url=self.fls_custom_binary_url,
            allow_custom_binaries=self.fls_allow_custom_binaries,
        )
        fls_cmd = [fls_binary, "fastboot", oci_url]

        if partitions:
            for partition_name, filename in sorted(partitions.items()):
                if not filename or not filename.strip():
                    raise ValueError(
                        f"Partition '{partition_name}' has an empty filename. "
                        "Each partition must have a non-empty filename."
                    )
                fls_cmd.extend(["-t", f"{partition_name}:{filename}"])

        fls_cmd.extend(["--timeout", str(self.flash_timeout)])
        return fls_cmd

    @export
    def flash_oci_image(
        self,
        oci_url: str,
        partitions: Dict[str, str] | None = None,
        oci_username: str | None = None,
        oci_password: str | None = None,
    ):
        """Flash OCI image using FLS fastboot CLI

        Args:
            oci_url: OCI image reference (e.g., "quay.io/bzlotnik/ridesx-image:latest")
            partitions: Optional mapping of partition -> filename inside OCI image
            oci_username: Registry username for OCI authentication
            oci_password: Registry password for OCI authentication
        """
        self._validate_oci_url(oci_url)

        if bool(oci_username) != bool(oci_password):
            raise ValueError("OCI authentication requires both --username and --password")

        fls_cmd = self._build_fls_command(oci_url, partitions)

        fls_env = os.environ.copy()
        if oci_username and oci_password:
            fls_env["FLS_REGISTRY_USERNAME"] = oci_username
            fls_env["FLS_REGISTRY_PASSWORD"] = oci_password

        self.logger.info(f"Running FLS fastboot: {' '.join(fls_cmd)}")
        if oci_username:
            self.logger.info("Using OCI registry credentials from environment")

        try:
            result = subprocess.run(
                fls_cmd, capture_output=True, text=True,
                check=True, timeout=self.flash_timeout + 30, env=fls_env,
            )

            self.logger.info("FLS fastboot auto-detection completed successfully")
            self.logger.debug(f"FLS stdout: {result.stdout}")
            if result.stderr:
                self.logger.debug(f"FLS stderr: {result.stderr}")

            return {"status": "success", "output": result.stdout}

        except subprocess.CalledProcessError as e:
            self.logger.error(f"FLS fastboot failed - return code: {e.returncode}")
            self.logger.error(f"stdout: {e.stdout}")
            self.logger.error(f"stderr: {e.stderr}")
            output = (e.stderr or e.stdout or "").strip()
            raise RuntimeError(f"FLS fastboot failed: {output}") from e

        except subprocess.TimeoutExpired:
            self.logger.error("FLS fastboot auto-detection timed out")
            raise RuntimeError("FLS fastboot auto-detection timeout") from None

        except FileNotFoundError:
            self.logger.error("FLS command not found - ensure FLS is installed and in PATH")
            raise RuntimeError("FLS command not found") from None

    @export
    def erase_partition(self, device_id: str, partition: str) -> dict[str, str]:
        """Erase a partition using fastboot

        Args:
            device_id: The fastboot device ID
            partition: The partition name to erase (e.g., 'recoveryinfo')
        """
        if not partition or not partition.strip():
            raise ValueError("Partition name cannot be empty")

        self.logger.info(f"Erasing partition '{partition}' on device {device_id}")

        cmd = ["fastboot", "-s", device_id, "erase", partition]
        self.logger.debug(f"Running command: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=self.erase_timeout)
            self.logger.info(f"Successfully erased partition '{partition}'")
            self.logger.debug(f"Erase stdout: {result.stdout}")
            if result.stderr:
                self.logger.debug(f"Erase stderr: {result.stderr}")
            return {"status": "success", "partition": partition, "output": result.stdout}
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to erase partition '{partition}' - return code: {e.returncode}")
            self.logger.error(f"stdout: {e.stdout}")
            self.logger.error(f"stderr: {e.stderr}")
            raise RuntimeError(f"Failed to erase partition '{partition}': {e.stderr or e.stdout}") from e
        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout while erasing partition '{partition}'")
            raise RuntimeError(f"Timeout while erasing partition '{partition}'") from None
        except FileNotFoundError:
            raise RuntimeError("fastboot command not found") from None

    @export
    async def boot_to_fastboot(self):
        """Boot device to fastboot mode"""
        self.logger.info("Booting device to fastboot mode")
        commands = [
            # power off
            ("devicePower 0", 0),
            ("gpio vbusdis1 0", 0),
            ("usbDevicePower 1", 0),
            ("ttl outputBit 1 0", 0),
            ("gpio volup 0", 0),
            ("ttl outputBit 2 1", 0),
            ("ttl outputBit 4 0", 0.5),
            # power on
            ("devicePower 1", 0.9),
            ("usbDevicePower 1", 0),
            ("gpio vbusdis1 0", 0.03),
            ("ttl outputBit 1 1", 0.8),
            ("ttl outputBit 1 0", 8),
            ("ttl outputBit 2 0", 0.5),
        ]
        serial = self.children["serial"]
        async with serial.connect() as stream:
            for command, delay in commands:
                self.logger.info(f"Executing {command}")
                await stream.send(f"{command}\r".encode())
                data = b""
                while b"ok" not in data:
                    chunk = await stream.receive()
                    data += chunk
                self.logger.debug(f"Command {command} acknowledged with 'ok'")
                prompt = PROMPT
                while prompt not in data:
                    chunk = await stream.receive()
                    data += chunk
                self.logger.debug(f"prompt returned after command: {command}")
                await asyncio.sleep(delay)
        self.logger.info("device should now be in fastboot mode")


@dataclass(kw_only=True)
class RideSXPowerDriver(Driver):
    """RideSX Power Driver"""

    driver_type = "power"

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        if "serial" not in self.children:
            raise ConfigurationError("'serial' instance is required")

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_ridesx.client.RideSXPowerClient"

    @export
    async def on(self):
        """Turn device power on"""
        self.logger.info("Turning device power on")
        commands = [
            ("devicePower 1", 0.9),
            ("usbDevicePower 1", 0),
            ("gpio vbusdis1 0", 0.03),
        ]
        await send_power_commands_sequence(self.children["serial"], self.logger, commands)

    @export
    async def off(self):
        """Turn device power off"""
        self.logger.info("Turning device power off")
        commands = [
            ("gpio vbusdis1 0", 0),
            ("usbDevicePower 1", 0),
            ("devicePower 0", 0.5),
        ]
        await send_power_commands_sequence(self.children["serial"], self.logger, commands)

    @export
    async def cycle(self, delay: float = 2):
        """Power cycle the device"""
        self.logger.info(f"Power cycling device with {delay}s delay")
        await self.off()
        await asyncio.sleep(delay)
        await self.on()

    @export
    async def rescue(self):
        """Rescue mode - not implemented for RideSX"""
        raise NotImplementedError("Rescue mode not available for RideSX")
