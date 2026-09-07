from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from jumpstarter.common.oci import OciCredentials

import click
from jumpstarter_driver_composite.client import CompositeClient
from jumpstarter_driver_opendal.client import FlasherClient, clean_filename, operator_for_path
from jumpstarter_driver_power.client import PowerClient
from opendal import Operator

from jumpstarter.client.decorators import driver_click_group
from jumpstarter.common.exceptions import JumpstarterException


class RideSXFlashError(JumpstarterException):
    """User-facing flash operation failure."""


@dataclass(kw_only=True)
class RideSXClient(FlasherClient, CompositeClient):
    """Client for RideSX"""

    def __post_init__(self):
        super().__post_init__()

    def boot_to_fastboot(self):
        return self.call("boot_to_fastboot")

    def erase_partition(self, partition: str) -> dict[str, str]:
        """Erase a partition on the device via fastboot.

        Args:
            partition: The partition name to erase (e.g., 'recoveryinfo')
        """
        self.logger.info(f"Starting erase operation for partition '{partition}'")
        self.boot_to_fastboot()

        detection_result = self.call("detect_fastboot_device", 5, 2.0)
        if detection_result["status"] != "device_found":
            raise click.ClickException("No fastboot devices found. Make sure device is in fastboot mode.")

        device_id = detection_result["device_id"]
        self.logger.info(f"Found fastboot device: {device_id}")
        return self.call("erase_partition", device_id, partition)

    def _upload_file_if_needed(self, file_path: str, operator: Operator | None = None) -> str:
        if not file_path or not file_path.strip():
            raise ValueError("File path cannot be empty. Please provide a valid file path.")

        if operator is None:
            path_buf, operator, operator_scheme = operator_for_path(file_path)
        else:
            path_buf = Path(file_path)
            operator_scheme = "unknown"

        filename = clean_filename(path_buf)

        if self._should_upload_file(self.storage, filename, path_buf, operator, operator_scheme):
            if operator_scheme == "http":
                self.logger.info(f"Downloading {file_path} to storage as {filename}")
            else:
                self.logger.info(f"Uploading {file_path} to storage as {filename}")

            try:
                self.storage.write_from_path(filename, path_buf, operator=operator)
            except Exception as e:
                raise RideSXFlashError(
                    f"Failed to transfer '{file_path}' to exporter storage as '{filename}' (scheme={operator_scheme})"
                ) from e
        else:
            self.logger.info(f"File {filename} already exists in storage with matching hash, skipping upload")

        return filename

    def flash_images(self, partitions: Dict[str, str], operators: Optional[Dict[str, Operator]] = None):
        """Flash images to specified partitions

        Args:
            partitions: Dictionary mapping partition names to file paths
            operators: Optional dictionary mapping partition names to operators
        """
        if not partitions:
            raise ValueError("At least one partition must be provided")

        operators = operators or {}
        remote_files = {}

        for partition, file_path in partitions.items():
            self.logger.info(f"Processing {partition} image: {file_path}")
            operator = operators.get(partition)
            try:
                remote_files[partition] = self._upload_file_if_needed(file_path, operator)
            except Exception as e:
                raise RideSXFlashError(f"Failed preparing image for partition '{partition}': {file_path}") from e

        self.logger.info("Checking for fastboot devices on Exporter...")
        detection_result = self.call("detect_fastboot_device", 5, 2.0)

        if detection_result["status"] != "device_found":
            raise click.ClickException("No fastboot devices found. Make sure device is in fastboot mode.")

        device_id = detection_result["device_id"]
        self.logger.info(f"found fastboot device: {device_id}")

        flash_result = self.call("flash_with_fastboot", device_id, remote_files)

        return flash_result

    def _validate_partition_mappings(self, partitions: Dict[str, str] | None) -> None:
        """Validate partition mappings; raise ValueError if any path is empty."""
        if partitions is None:
            return
        for partition_name, file_path in partitions.items():
            if not file_path or not file_path.strip():
                raise ValueError(
                    f"Partition '{partition_name}' has an empty file path. "
                    f"Please provide a valid file path (e.g., -t {partition_name}:/path/to/image)"
                )

    def _power_off_if_available(self, power_off: bool = True) -> None:
        """Power off device if power child is present and power_off is True."""
        if not power_off:
            self.logger.info("leaving device powered on")
            return
        if "power" in self.children:
            self.power.off()
            self.logger.info("device powered off")
        else:
            self.logger.info("device left running")

    def _execute_flash_operation(self, operation_func, *args, power_off: bool = True, **kwargs):
        """Common wrapper for flash operations with logging and power management."""
        self.logger.info("Starting RideSX flash operation")
        self.boot_to_fastboot()

        try:
            result = operation_func(*args, **kwargs)
            self.logger.info("flash operation completed successfully")
        except Exception as flash_error:
            # Capture failure details before best-effort power-off cleanup.
            self.logger.error(
                "flash error details: type=%s repr=%r args=%r",
                type(flash_error).__name__,
                flash_error,
                getattr(flash_error, "args", ()),
            )

            wrapped_error = None
            if not str(flash_error).strip():
                wrapped_error = RideSXFlashError(
                    "Flash operation failed with an empty error message "
                    f"(type={type(flash_error).__name__}, repr={flash_error!r})"
                )

            try:
                self._power_off_if_available(power_off)
            except Exception as power_error:
                self.logger.exception("power-off cleanup failed after flash operation error: %s", power_error)

            if wrapped_error is not None:
                raise wrapped_error from flash_error
            raise

        try:
            self._power_off_if_available(power_off)
        except Exception as power_error:
            # Keep successful flashes successful, but make cleanup failures visible.
            self.logger.exception("power-off cleanup failed after successful flash operation: %s", power_error)

        return result

    def flash(
        self,
        path: str | Dict[str, str],
        *,
        target: str | None = None,
        operator: Operator | Dict[str, Operator] | None = None,
        compression=None,
        power_off: bool = True,
    ):
        """Flash image to DUT - supports both OCI and traditional paths.

        Args:
            path: File path, URL, or OCI image reference (or dict of partition->path mappings)
            target: Target partition (for single file mode)
            operator: Optional operator for file access (usually auto-detected)
            compression: Compression type
            power_off: Whether to power off the device after flashing (default: True)
        """
        # Auto-detect flash mode based on path type
        if isinstance(path, dict):
            # Dictionary mode: {partition: file_path, ...}
            operators_dict = operator if isinstance(operator, dict) else None
            return self.flash_local(path, operators_dict, power_off=power_off)

        elif isinstance(path, str) and path.startswith("oci://"):
            # OCI mode: auto-detect partitions or use target as partition->filename mapping
            if target and ":" in target:
                # Target is "partition:filename" format for OCI explicit mapping
                partition_name, filename = target.split(":", 1)
                partitions = {partition_name: filename}
                return self.flash_with_targets(path, partitions, power_off=power_off)
            else:
                # OCI auto-detection mode
                return self.flash_oci_auto(path, None, power_off=power_off)

        else:
            # Traditional single file mode
            if target is None:
                if isinstance(path, str) and ":" in path:
                    before_colon, after_colon = path.split(":", 1)
                    if "/" in before_colon:
                        # registry/path:tag - likely an OCI ref missing the oci:// prefix
                        raise click.ClickException(
                            f"OCI URLs must start with oci://, got: {path}\nUsage: j storage flash oci://{path}"
                        )
                    # partition:something - likely a partition:path mapping
                    raise click.ClickException(
                        f"'{path}' looks like a partition:path mapping.\n"
                        f"Use the -t flag: j storage flash -t {path}\n"
                        f"Or use the API: client.flash('{after_colon}', target='{before_colon}')"
                    )
                raise click.ClickException(
                    "This driver requires a target partition for non-OCI paths.\n"
                    "Usage: client.flash('/path/to/file.img', target='boot_a')\n"
                    "For OCI: client.flash('oci://registry.com/image:tag')\n"
                    "For dict: client.flash({'boot_a': '/path/to/file.img'})"
                )

            # Use operator if provided, otherwise auto-detect
            if operator is not None:
                operators = {target: operator} if isinstance(operator, Operator) else operator
            else:
                operators = None

            partitions = {target: path}
            return self.flash_local(partitions, operators, power_off=power_off)

    def flash_with_targets(
        self,
        oci_url: str,
        partitions: Dict[str, str],
        *,
        power_off: bool = True,
    ):
        """Flash OCI image with explicit partition mappings.

        Args:
            oci_url: OCI image URL (must start with oci://)
            partitions: Mapping of partition name -> filename in OCI image
            power_off: Whether to power off the device after flashing (default: True)

        Raises:
            ValueError: If partitions is empty or None
        """
        if not partitions:
            raise ValueError(
                "flash_with_targets requires a non-empty mapping of partition name -> filename. "
                "Use flash() for auto-detection mode."
            )
        self._validate_partition_mappings(partitions)

        self.logger.info(f"Using FLS OCI flash with explicit mapping for image: {oci_url}")

        def _flash_operation():
            return self._flash_oci_auto_impl(oci_url, partitions)

        return self._execute_flash_operation(_flash_operation, power_off=power_off)

    def flash_local(
        self,
        partitions: Dict[str, str],
        operators: Dict[str, Operator] | None = None,
        *,
        power_off: bool = True,
    ):
        """Flash local files or URLs to partitions.

        Args:
            partitions: Mapping of partition name -> file path or URL
            operators: Optional mapping of partition name -> operator
            power_off: Whether to power off the device after flashing (default: True)
        """
        self._validate_partition_mappings(partitions)

        self.logger.info(f"Flashing local files: {list(partitions.keys())}")

        def _flash_operation():
            return self.flash_images(partitions, operators)

        return self._execute_flash_operation(_flash_operation, power_off=power_off)

    def _read_oci_credentials(self, oci_url: str) -> OciCredentials:
        """Read OCI registry credentials from environment variables or auth files."""
        from jumpstarter.common.oci import resolve_oci_credentials

        return resolve_oci_credentials(oci_url)

    def _flash_oci_auto_impl(
        self,
        oci_url: str,
        partitions: Dict[str, str] | None = None,
    ):
        """Core implementation of OCI flash without wrapper logic."""
        creds = self._read_oci_credentials(oci_url)

        self.logger.info("Checking for fastboot devices on Exporter...")
        detection_result = self.call("detect_fastboot_device", 5, 2.0)

        if detection_result["status"] != "device_found":
            raise click.ClickException("No fastboot devices found. Make sure device is in fastboot mode.")

        device_id = detection_result["device_id"]
        self.logger.info(f"Found fastboot device: {device_id}")

        flash_result = self.call(
            "flash_oci_image",
            oci_url,
            partitions,
            creds.username,
            creds.plain_password,
        )

        # Display FLS output to user
        if flash_result.get("status") == "success" and flash_result.get("output"):
            self.logger.info("FLS fastboot completed successfully")
            # Log the detailed output for user visibility
            for line in flash_result["output"].strip().split("\n"):
                if line.strip():
                    self.logger.info(f"FLS: {line.strip()}")

        return flash_result

    def flash_oci_auto(
        self,
        oci_url: str,
        partitions: Dict[str, str] | None = None,
        *,
        power_off: bool = True,
    ):
        """Flash OCI image using auto-detection or explicit partition mapping

        Args:
            oci_url: OCI image reference (e.g., "oci://registry.com/image:latest")
            partitions: Optional mapping of partition -> filename inside OCI image
            power_off: Whether to power off the device after flashing (default: True)
        """
        if not oci_url.startswith("oci://"):
            raise ValueError(f"OCI URL must start with oci://, got: {oci_url}\nUsage: j storage flash oci://{oci_url}")

        if partitions:
            self.logger.info(f"Flashing OCI image with explicit mapping: {list(partitions.keys())}")
        else:
            self.logger.info(f"Auto-detecting partitions for OCI image: {oci_url}")

        def _flash_operation():
            return self._flash_oci_auto_impl(oci_url, partitions)

        return self._execute_flash_operation(_flash_operation, power_off=power_off)

    def _parse_target_specs(self, target_specs: tuple[str, ...]) -> dict[str, str]:
        """Parse -t target specs into a partition->path mapping."""
        mapping: dict[str, str] = {}
        for spec in target_specs:
            if ":" not in spec:
                raise click.ClickException(f"Invalid target spec '{spec}'. Expected format: partition:path")
            name, path = spec.split(":", 1)
            mapping[name] = path
        return mapping

    def _parse_and_validate_targets(self, target_specs: tuple[str, ...]):
        """Parse and validate target specifications, returning (mapping, single_target)."""
        mapping = {}
        single_target = None

        for spec in target_specs:
            if ":" in spec:
                # Multi-partition format: partition:path
                partition, file_path = spec.split(":", 1)
                mapping[partition] = file_path
            else:
                # Single partition format: just partition name
                if single_target is not None:
                    raise click.ClickException("Cannot mix single-partition and multi-partition target specs")
                single_target = spec

        if mapping and single_target:
            raise click.ClickException("Cannot mix single-partition and multi-partition target specs")

        return mapping, single_target

    def _execute_flash_command(self, path, target_specs, power_off: bool = True):
        """Execute flash command logic with proper argument handling."""
        # Parse target specifications
        if target_specs:
            mapping, single_target = self._parse_and_validate_targets(target_specs)

            if mapping:
                if path:
                    # In multi-target mode, positional path must be an oci:// reference.
                    if not path.startswith("oci://"):
                        t_flags = " ".join(f"-t {s}" for s in target_specs)
                        raise click.ClickException(
                            f"'{path}' is missing the -t flag.\n"
                            f"Each partition mapping needs its own -t flag.\n\n"
                            f"Example:\n"
                            f"  j storage flash -t {path} {t_flags}\n\n"
                            f"For OCI images, use the oci:// prefix:\n"
                            f"  j storage flash oci://registry.com/image:tag {t_flags}"
                        )
                    # Multi-partition mode with path: extract specific files from OCI image
                    self.flash_with_targets(path, mapping, power_off=power_off)
                else:
                    # Multi-partition mode: use mapping as dict for local files
                    self.flash(mapping, power_off=power_off)
            else:
                # Single partition mode: use path with target
                if not path:
                    raise click.ClickException("Path argument required when using single-partition target")
                self.flash(path, target=single_target, power_off=power_off)
        elif path:
            # Path only - should be OCI for auto-detection
            self.flash(path, power_off=power_off)
        else:
            raise click.ClickException("Provide a path or use -t to specify partition mappings")

    def cli(self):
        generic_cli = FlasherClient.cli(self)

        @driver_click_group(self)
        def base():
            """RideSX storage operations"""
            pass

        # Add all generic commands except 'flash' (we override it)
        for name, cmd in generic_cli.commands.items():
            if name != "flash":
                base.add_command(cmd, name=name)

        @base.command()
        @click.argument("path", required=False)
        @click.option(
            "-t",
            "--target",
            "target_specs",
            multiple=True,
            help="Target spec as partition:path for multi-partition or just partition for single file",
        )
        @click.option(
            "--power-off/--no-power-off",
            "power_off",
            default=True,
            help="Power off device after flashing (default)",
        )
        def flash(path, target_specs, power_off):
            """Flash image to device.

            \b
            Examples:
              # OCI auto-detection
              j storage flash oci://registry.com/image:tag

              # OCI with explicit partition->filename mapping
              j storage flash -t boot_a:boot.img oci://registry.com/image:tag

              # OCI with registry credentials (via env vars)
              OCI_USERNAME=user OCI_PASSWORD=pass j storage flash oci://registry.com/image:tag

              # Single file to partition
              j storage flash /local/boot.img --target boot_a

              # Multiple files
              j storage flash -t boot_a:/local/boot.img -t system_a:/local/system.img

              # HTTP URLs
              j storage flash -t boot_a:https://example.com/boot.img

            \b
            Environment variables:
              OCI_USERNAME  Registry username for private OCI images
              OCI_PASSWORD  Registry password for private OCI images
            """
            self._execute_flash_command(path, target_specs, power_off=power_off)

        @base.command()
        def boot_to_fastboot():
            """Boot to fastboot"""
            self.boot_to_fastboot()

        @base.command()
        @click.argument("partition")
        def erase(partition):
            """Erase a partition using fastboot.

            \b
            Examples:
              j storage erase recoveryinfo
              j storage erase userdata
            """
            result = self.erase_partition(partition)
            click.echo(f"Erased partition '{partition}': {result.get('status')}")

        return base


@dataclass(kw_only=True)
class RideSXPowerClient(PowerClient):
    """Power control client for RideSX"""

    def __post_init__(self):
        super().__post_init__()

    @property
    def serial(self):
        return self.children["serial"]

    def on(self) -> None:
        """Turn device power on"""
        self.call("on")

    def off(self) -> None:
        """Turn device power off"""
        self.call("off")

    def cycle(self, wait: int = 2):
        """Power cycle the device"""
        self.call("cycle", wait)

    def rescue(self) -> None:
        """Rescue mode"""
        self.call("rescue")
