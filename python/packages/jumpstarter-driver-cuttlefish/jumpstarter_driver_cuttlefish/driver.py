import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass, field

import httpx
import requests
from anyio import EndOfStream, sleep, to_thread
from anyio.streams.file import FileReadStream, FileWriteStream
from jumpstarter_driver_adb.driver import AdbServer
from jumpstarter_driver_power.driver import PowerReading, VirtualPowerInterface

from jumpstarter.driver import Driver, export
from jumpstarter.driver.flasher import FlasherInterface


class CuttlefishError(Exception):
    """Raised when a Host Orchestrator API call fails."""


class CuttlefishTimeout(CuttlefishError):
    """Raised when an operation doesn't complete in time."""


@dataclass
class ImageDirGeneration:
    """A Cuttlefish image directory and the artifacts populated into it."""

    dir_id: str
    artifacts: dict[str, str] = field(default_factory=dict)


@dataclass(kw_only=True)
class Cuttlefish(Driver):
    """Cuttlefish Host Orchestrator driver for managing Android virtual devices.

    Composite driver with children: power, storage, adb.
    """

    driver_type = "composite"

    scheme: str = "http"
    host: str = "localhost"
    port: int = 2080
    group: str = "cvd"
    name: str = "1"
    instance_num: int = 1
    adb_server_port: int = 15037
    boot_timeout: int = 300
    env_config: dict = field(default_factory=dict)
    webrtc_url: str = ""
    upload_chunk_size: int = 16 * 1024 * 1024
    upload_max_retries: int = 3
    _cvd_group: str | None = field(default=None, init=False, repr=False)
    _cvd_name: str | None = field(default=None, init=False, repr=False)
    # Both generations are in-memory session state. The active directory remains
    # immutable while a CVD references it; a partial flash creates a new
    # generation and repopulates it from the active checksums.
    _staged: ImageDirGeneration | None = field(default=None, init=False, repr=False)
    _active: ImageDirGeneration | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()
        self.children["power"] = CvdPower(parent=self)
        self.children["storage"] = CvdFlasher(parent=self)
        self.children["adb"] = AdbServer(host="127.0.0.1", port=self.adb_server_port)

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_cuttlefish.client.CuttlefishClient"

    @property
    def _base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def _expected_adb_port(self) -> int:
        return 6520 + (self.instance_num - 1)

    @property
    def _cvd_path(self) -> str:
        return f"/cvds/{self._cvd_group or self.group}/{self._cvd_name or self.name}"

    def _fmt(self, result) -> str:
        return json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)

    def _request_response(
        self, method: str, path: str, data: dict | None = None, timeout: float = 10
    ) -> requests.Response:
        try:
            return requests.request(method, f"{self._base_url}{path}", json=data, timeout=timeout)
        except requests.ConnectionError as e:
            raise CuttlefishError(f"not connected to Host Orchestrator at {self.host}:{self.port}") from e
        except requests.Timeout as e:
            raise CuttlefishError(f"{method} {path} timed out after {timeout}s") from e

    def _raise_for_status(self, response: requests.Response, context: str) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise CuttlefishError(f"{context} failed: {e}") from e

    def _response_body(self, response: requests.Response) -> dict | list | str | None:
        if not response.content:
            return None
        try:
            return response.json()
        except requests.JSONDecodeError:
            return response.text

    def _request(
        self, method: str, path: str, data: dict | None = None, timeout: float = 10
    ) -> dict | list | str | None:
        response = self._request_response(method, path, data, timeout)
        self._raise_for_status(response, f"{method} {path}")
        return self._response_body(response)

    def _wait_for_operation(
        self,
        op_name: str,
        timeout: float = 300,
        accepted_statuses: tuple[int, ...] = (),
    ) -> dict | list | str | None:
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            elapsed = int(time.monotonic() - start)
            self.logger.info("operation %s: waiting (%ds elapsed, %ds remaining)", op_name, elapsed, int(remaining))
            try:
                r = requests.post(
                    f"{self._base_url}/operations/{op_name}/:wait",
                    timeout=min(130, max(1, remaining)),
                )
            except requests.ConnectionError as e:
                raise CuttlefishError(f"lost connection during operation {op_name}") from e
            except requests.Timeout:
                self.logger.info("operation %s: poll timeout after %ds, retrying", op_name, elapsed)
                time.sleep(2)
                continue
            if r.status_code in (503, 504):
                self.logger.info("operation %s: server busy (%d), retrying in 2s", op_name, r.status_code)
                time.sleep(2)
                continue
            return self._operation_result(op_name, r, accepted_statuses)
        raise CuttlefishTimeout(f"operation {op_name} timed out after {timeout}s")

    def _operation_result(
        self,
        op_name: str,
        response: requests.Response,
        accepted_statuses: tuple[int, ...],
    ) -> dict | list | str | None:
        if response.status_code == 500:
            body = self._response_body(response)
            if isinstance(body, dict):
                msg = body.get("error", "unknown error")
                details = body.get("details", "")
                raise CuttlefishError(f"operation failed: {msg}\n{details}")
            raise CuttlefishError(f"operation failed with status 500: {response.text}")
        if response.status_code in accepted_statuses:
            self.logger.info("operation %s completed with accepted status %d", op_name, response.status_code)
            return None
        self._raise_for_status(response, f"operation {op_name}")
        return self._response_body(response)

    def _do_operation(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        timeout: float = 300,
    ) -> dict | list | str | None:
        result = self._request(method, path, data)
        if isinstance(result, dict) and "done" in result:
            op_name = result.get("name")
            if not op_name:
                raise CuttlefishError(f"operation response missing 'name': {result}")
            self.logger.info(f"Waiting for operation {op_name}")
            return self._wait_for_operation(str(op_name), timeout)
        return result

    def _artifact_exists(self, checksum: str) -> bool:
        """Return True if a user artifact with this SHA-256 is already uploaded.

        HO addresses artifacts by checksum, so an existing upload can be reused
        across sessions and hosts — a re-flash of the same image skips the
        (potentially multi-GB) transfer.
        """
        r = self._request_response("GET", f"/v1/userartifacts/{checksum}")
        if r.status_code == 404:
            return False
        self._raise_for_status(r, "stat user artifact")
        return True

    def _extract_artifact(self, checksum: str) -> None:
        """Extract an uploaded artifact server-side (async operation).

        HO dispatches on the *stored file's suffix* (``.zip`` → unzip,
        ``.tar.gz`` → untar), so the upload must have preserved the real
        extension. Extracting an already-extracted checksum returns 409
        Conflict, which we treat as success — this is the idempotent re-flash
        path (upload skipped because the artifact was already present).
        """
        r = self._request_response("POST", f"/v1/userartifacts/{checksum}/:extract", timeout=130)
        if r.status_code == 409:
            self.logger.info("artifact %s already extracted", checksum[:12])
            return
        self._raise_for_status(r, "extract artifact")
        result = self._response_body(r)
        if result is None:
            return
        if isinstance(result, dict) and "done" in result:
            op_name = result.get("name")
            if not op_name:
                raise CuttlefishError(f"extract operation missing 'name': {result}")
            self._wait_for_operation(str(op_name), accepted_statuses=(409,))

    def _delete_image_dir_quietly(self, dir_id: str) -> bool:
        """Best-effort DELETE of an image dir; log but don't raise on failure.

        HO refuses to delete a dir a running CVD still references, so callers
        must be able to tolerate failure (e.g. teardown while a CVD is up).
        """
        try:
            self._do_operation("DELETE", f"/cvd_imgs_dirs/{dir_id}")
        except CuttlefishError as e:
            self.logger.warning("failed to delete image dir %s; it may need manual cleanup: %s", dir_id, e)
            return False
        return True

    def _create_image_dir(self) -> str:
        """Create an empty image directory, returning its id (async operation)."""
        result = self._do_operation("POST", "/cvd_imgs_dirs")
        dir_id = None
        if isinstance(result, dict):
            # HO returns CreateImageDirectoryResponse{id}; the id may be at the
            # top level of the operation-wait result or nested under "result".
            dir_id = result.get("id") or (result.get("result") or {}).get("id")
        if not dir_id:
            raise CuttlefishError(f"create image dir: no 'id' in response: {result!r}")
        return str(dir_id)

    def _populate_image_dir(self, dir_id: str, checksum: str) -> dict | list | str | None:
        """Fill an image directory from a previously-uploaded artifact (async operation)."""
        return self._do_operation("PUT", f"/cvd_imgs_dirs/{dir_id}", {"user_artifact_checksum": checksum})

    def _stage_artifact(self, checksum: str, roles: set[str]) -> ImageDirGeneration:
        """Populate a new staged generation and carry forward missing active roles."""
        generation = self._staged
        if generation is None:
            generation = ImageDirGeneration(dir_id=self._create_image_dir())
            self._staged = generation

        self._populate_image_dir(generation.dir_id, checksum)
        for role in roles:
            generation.artifacts[role] = checksum

        if self._active is not None:
            for role, active_checksum in self._active.artifacts.items():
                if role not in generation.artifacts:
                    self._populate_image_dir(generation.dir_id, active_checksum)
                    generation.artifacts[role] = active_checksum

        return generation

    def _promote_staged(self) -> None:
        """Make the staged generation active after its CVD is created."""
        staged = self._staged
        if staged is None:
            return

        previous = self._active
        self._active = staged
        self._staged = None
        if previous is not None:
            self._delete_image_dir_quietly(previous.dir_id)

    def _release_active(self) -> None:
        """Delete the active generation after its CVD has been destroyed."""
        active = self._active
        if active is not None and self._delete_image_dir_quietly(active.dir_id):
            self._active = None

    def _env_config_for_create(self) -> dict:
        """Build the env_config for POST /cvds, injecting the flashed image dir.

        The dir to boot from is the freshly-staged one if a flash is pending,
        otherwise the dir the previous CVD used — so ``power off --destroy;
        power on`` recreates the same flashed images without re-flashing. With
        neither set, the configured env_config is used verbatim (e.g.
        fetch-from-branch).

        HO's populate step symlinks each artifact's contents into a single image
        dir (a per-filename merge), so one dir holds both the device images and
        the host package even when they arrive as separate ``flash()`` calls.
        For a complete bundle both path-valued fields point at the same
        ``@image_dirs/{id}`` token — ``common.host_package`` (host tools) and
        every ``instances[].disk.default_build`` (device images). A targeted
        partial flash carries forward the active generation's checksums into
        the new directory, so both fields continue to use the flashed
        generation. HO rewrites the token to the on-host path
        (``strings.ReplaceAll(config, "@image_dirs/", ...)``) before running
        ``cvd load``.
        """
        config = copy.deepcopy(self.env_config)
        generation = self._staged or self._active
        if generation is None:
            return config

        token = f"@image_dirs/{generation.dir_id}"
        if "host_package" in generation.artifacts:
            config.setdefault("common", {})["host_package"] = token
        if "images" in generation.artifacts:
            instances = config.setdefault("instances", [])
            if not instances:
                instances.append({})
            for inst in instances:
                inst.setdefault("disk", {})["default_build"] = token
        return config

    def _get_existing_cvds(self) -> list[dict]:
        """Return CVDs belonging to this driver's group.

        Raises CuttlefishError on connection/timeout/server failures so callers
        don't mistake a failed query for "no CVDs exist".
        """
        result = self._request("GET", "/cvds")
        if not isinstance(result, dict):
            raise CuttlefishError(f"unexpected response from GET /cvds: {result!r}")
        all_cvds = result.get("cvds", [])
        own_group = self._cvd_group or self.group
        return [c for c in all_cvds if c.get("group") == own_group]

    @property
    def _cvd_device(self) -> str:
        """Pinned ADB address derived from config, never queried from HO."""
        return f"{self.host}:{self._expected_adb_port}"

    def _auto_connect_adb(self) -> str:
        adb = self.children.get("adb")
        if not adb:
            return self._cvd_device
        device = self._cvd_device
        self.logger.info(f"Auto-connecting ADB to {device}")
        try:
            adb.connect_device(device)
        except Exception:
            self.logger.warning("ADB connect to %s failed, will retry during boot wait", device)
        return device

    def _auto_disconnect_adb(self):
        adb = self.children.get("adb")
        if not adb:
            return
        device = self._cvd_device
        self.logger.info(f"Disconnecting ADB from {device}")
        try:
            adb.disconnect_device(device)
        except Exception:
            pass

    def _wait_boot(self, timeout: float = 300):
        """Wait for CVD to be ADB-reachable and fully booted."""
        adb = self.children.get("adb")
        if not adb:
            return

        device = self._cvd_device

        deadline = time.monotonic() + timeout
        adb_path = adb.adb_path
        adb_env = adb.adb_env()

        self.logger.info("Waiting for %s to come online", device)
        while time.monotonic() < deadline:
            try:
                subprocess.run(
                    [adb_path, "connect", device],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=adb_env,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            try:
                r = subprocess.run(
                    [adb_path, "devices"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=adb_env,
                )
                for line in r.stdout.splitlines():
                    if device in line and "\tdevice" in line:
                        self.logger.info("%s is online", device)
                        break
                else:
                    time.sleep(3)
                    continue
                break
            except (subprocess.TimeoutExpired, OSError):
                time.sleep(3)
        else:
            raise CuttlefishTimeout(f"{device} did not come online within {timeout}s")

        self.logger.info("Waiting for boot to complete on %s", device)
        while time.monotonic() < deadline:
            try:
                r = subprocess.run(
                    [adb_path, "-s", device, "shell", "getprop", "sys.boot_completed"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=adb_env,
                )
                if r.stdout.strip() == "1":
                    self.logger.info("Boot completed on %s", device)
                    return
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(5)

        raise CuttlefishTimeout(f"boot did not complete on {device} within {timeout}s")

    @export
    def get_host(self) -> str:
        return self.host

    @export
    def get_webrtc_url(self) -> str:
        if self.webrtc_url:
            return self.webrtc_url
        return f"{self.scheme}://{self.host}:1080"

    @export
    def list_cvds(self) -> str:
        return self._fmt(self._request("GET", "/cvds"))

    @export
    def get_cvd(self) -> str:
        return self._fmt(self._request("GET", self._cvd_path))

    @export
    def restart_cvd(self) -> str:
        self.logger.info(f"Restarting CVD {self.group}/{self.name}")
        return self._fmt(self._do_operation("POST", f"{self._cvd_path}/:restart"))

    @export
    def powerwash_cvd(self) -> str:
        self.logger.info(f"Powerwashing CVD {self.group}/{self.name}")
        return self._fmt(self._do_operation("POST", f"{self._cvd_path}/:powerwash"))

    @export
    def powerbtn_cvd(self) -> str:
        self.logger.info(f"Power button on CVD {self.group}/{self.name}")
        return self._fmt(self._do_operation("POST", f"{self._cvd_path}/:powerbtn"))

    @export
    def status(self) -> str:
        """Check that nginx and Host Orchestrator are both reachable."""
        self._request("GET", "/_debug/statusz")
        return "OK"

    @export
    def create_cvd(self, config_json: str) -> str:
        try:
            config = json.loads(config_json)
        except json.JSONDecodeError as e:
            raise CuttlefishError(f"invalid JSON: {e}") from e
        return self._fmt(self._do_operation("POST", "/cvds", config, timeout=600))

    @export
    def start_cvd(self) -> str:
        return self._fmt(self._do_operation("POST", f"{self._cvd_path}/:start"))

    @export
    def stop_cvd(self) -> str:
        return self._fmt(self._do_operation("POST", f"{self._cvd_path}/:stop"))

    @export
    def delete_cvd(self) -> str:
        return self._fmt(self._do_operation("DELETE", self._cvd_path))

    @export
    def get_adb_port(self) -> str:
        result = self._request("GET", self._cvd_path)
        if isinstance(result, dict):
            for cvd in result.get("cvds", []):
                port = cvd.get("adb_port")
                if port is not None:
                    return str(port)
        raise CuttlefishError(f"no ADB port found for {self.group}/{self.name}")

    @export
    def list_operations(self) -> str:
        return self._fmt(self._request("GET", "/operations"))

    @export
    def wait_boot(self, timeout: int = 0) -> str:
        """Wait for CVD to finish booting. Uses boot_timeout config if timeout=0."""
        t = timeout or self.boot_timeout
        if t:
            self._wait_boot(t)
        return "OK"

    @export
    def reset_host(self) -> str:
        """Forcefully delete all CVDs and clean host state via HO reset endpoint.

        Kills orphaned processes, removes stale files, and resets HO tracking.
        """
        self.logger.warning("Resetting host orchestrator")
        self._auto_disconnect_adb()
        result = self._do_operation("POST", "/reset", timeout=60)
        self._cvd_group = None
        self._cvd_name = None
        return self._fmt(result)


@dataclass(kw_only=True)
class CvdPower(VirtualPowerInterface, Driver):
    """Virtual power control for Cuttlefish devices.

    on() creates a CVD if none exists, or starts an existing one.
    If multiple CVDs exist in the configured group, all are deleted before
    creating a fresh one (assumes single-tenant host orchestrator).
    off() stops the CVD; off(destroy=True) deletes it entirely.
    """

    parent: Cuttlefish

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_cuttlefish.client.CvdPowerClient"

    def _delete_stale_cvds(self, cvds: list[dict]) -> None:
        group_name = self.parent._cvd_group or self.parent.group
        self.logger.warning("Found %d stale CVDs in group %s, deleting", len(cvds), group_name)
        failed = []
        for cvd in cvds:
            group = cvd.get("group", self.parent.group)
            name = cvd.get("name", self.parent.name)
            try:
                self.parent._do_operation("DELETE", f"/cvds/{group}/{name}")
            except CuttlefishError:
                self.logger.warning("Failed to delete stale CVD %s/%s", group, name)
                failed.append(f"{group}/{name}")
        if failed:
            raise CuttlefishError(
                f"cannot create CVD - failed to delete stale CVDs: {', '.join(failed)}. "
                f"Run 'j cuttlefish reset' then retry."
            )

    def _use_existing_cvd(self, cvd: dict) -> None:
        self.parent._cvd_group = cvd.get("group")
        self.parent._cvd_name = cvd.get("name")
        self.logger.info(
            "Found existing CVD %s/%s (status: %s)",
            self.parent._cvd_group,
            self.parent._cvd_name,
            cvd.get("status"),
        )
        if cvd.get("status") != "Running":
            self.parent._do_operation("POST", f"{self.parent._cvd_path}/:start")

    def _create_cvd(self) -> None:
        self.logger.info("Creating CVD from env_config")
        try:
            result = self.parent._do_operation(
                "POST",
                "/cvds",
                {"env_config": self.parent._env_config_for_create()},
                timeout=600,
            )
        except CuttlefishError as e:
            msg = str(e)
            if "in use" in msg or "already running" in msg or "ValidateTapDevices" in msg:
                raise CuttlefishError(
                    f"CVD creation failed - orphaned processes from a previous session. "
                    f"Run 'j cuttlefish reset' then retry. Original error: {msg}"
                ) from e
            raise

        if isinstance(result, dict):
            for cvd in result.get("cvds", []):
                self.parent._cvd_group = cvd.get("group")
                self.parent._cvd_name = cvd.get("name")
                actual_port = cvd.get("adb_port")
                if actual_port and actual_port != self.parent._expected_adb_port:
                    try:
                        self.parent._do_operation("DELETE", self.parent._cvd_path)
                    except CuttlefishError:
                        self.logger.warning("Failed to clean up CVD after port mismatch")
                    self.parent._cvd_group = None
                    self.parent._cvd_name = None
                    raise CuttlefishError(
                        f"HO assigned adb_port {actual_port} but expected "
                        f"{self.parent._expected_adb_port} — stale state may have leaked. "
                        f"Run 'j cuttlefish reset' then retry."
                    )
                break

        self.parent._promote_staged()

    @export
    def on(self) -> None:
        existing = self.parent._get_existing_cvds()

        if len(existing) > 1:
            self._delete_stale_cvds(existing)
            existing = []

        if existing:
            self._use_existing_cvd(existing[0])
        else:
            self._create_cvd()

        self.parent._auto_connect_adb()
        if self.parent.boot_timeout:
            self.parent._wait_boot(self.parent.boot_timeout)

    @export
    def off(self, destroy: bool = False) -> None:
        p = self.parent
        cvd_id = f"{p._cvd_group or p.group}/{p._cvd_name or p.name}"
        if destroy:
            p._auto_disconnect_adb()
            self.logger.info(f"Deleting CVD {cvd_id}")
            p._do_operation("DELETE", p._cvd_path)
            p._cvd_group = None
            p._cvd_name = None
            p._release_active()
        else:
            self.logger.info(f"Stopping CVD {cvd_id}")
            p._do_operation("POST", f"{p._cvd_path}/:stop")

    @export
    def read(self) -> Generator[PowerReading, None, None]:
        raise NotImplementedError("no power telemetry for virtual devices")


@dataclass(kw_only=True)
class CvdFlasher(FlasherInterface, Driver):
    """Flasher for Cuttlefish devices, backed by the HO user-artifact store.

    Each ``flash(source, target)`` streams one archive to the exporter, hashes
    it (SHA-256), uploads it to HO's user-artifact store, extracts it, and
    symlinks its contents into an image directory. HO's populate is a
    *per-filename merge*, so several artifacts accumulate into one dir — the
    dict form of the client API turns into one ``flash()`` call per entry, all
    landing in the same staged dir::

        client.storage.flash({
            "images": "aosp_cf_x86_64_auto-img.zip",       # device images
            "host_package": "cvd-host_package.tar.gz",      # host tools
        })

    ``target`` must be either ``images`` (a ``.zip``) or ``host_package`` (a
    ``.tar.gz``). An untargeted flash is treated as a complete bundle and
    supplies both paths. The archive format is sniffed from magic bytes, since
    HO's extractor dispatches on the upload's suffix and the resource stream
    carries no name.

    A targeted flash may supply only one role; when an active generation exists,
    the missing role is carried forward by checksum into the new immutable
    generation. For a fresh CVD, provide both targeted artifacts (or one
    complete untargeted bundle).

    Flash only STAGES — images are selected at CVD *creation*, so a plain
    stop/start keeps the old images. The staged dir is consumed by the next
    ``power on`` that creates a CVD::

        j storage flash <bundle-or-artifacts>
        j power off --destroy    # stop/start would keep the old images
        j power on               # creates the CVD from the staged images
    """

    parent: Cuttlefish
    _VALID_TARGETS = {"images", "host_package"}

    def _validate_target(self, target: str | None) -> None:
        if target is not None and target not in self._VALID_TARGETS:
            raise CuttlefishError(
                f"unsupported Cuttlefish storage target {target!r}; expected 'images' or 'host_package'"
            )

    def _roles_for_flash(self, target: str | None, suffix: str) -> set[str]:
        """Validate a target label and return the roles this artifact supplies."""
        if target is None:
            # An untargeted flash is the complete-bundle form and supplies both
            # paths. Targeted calls are available for a split images/package
            # upload or for preserving one path from env_config.
            return set(self._VALID_TARGETS)
        expected_suffix = ".zip" if target == "images" else ".tar.gz"
        if suffix != expected_suffix:
            raise CuttlefishError(f"storage target {target!r} requires {expected_suffix}, got {suffix}")
        return {target}

    @export
    async def flash(self, source, target: str | None = None) -> None:
        self._validate_target(target)
        tmp_path, checksum, size, suffix = await self._spool(source)
        try:
            roles = self._roles_for_flash(target, suffix)
            label = target or "bundle"
            if await to_thread.run_sync(self.parent._artifact_exists, checksum):
                self.logger.info("artifact %s (%s) already present, skipping upload", checksum[:12], label)
            else:
                self.logger.info("uploading %s artifact %s (%d bytes)", label, checksum[:12], size)
                await self._upload(checksum, tmp_path, size, suffix)

            self.logger.info("extracting artifact %s", checksum[:12])
            await to_thread.run_sync(self.parent._extract_artifact, checksum)

            had_staged = self.parent._staged is not None
            generation = await to_thread.run_sync(self.parent._stage_artifact, checksum, roles)
            if not had_staged:
                self.logger.info("created image dir %s", generation.dir_id)
        finally:
            await to_thread.run_sync(os.unlink, tmp_path)

        self.logger.info(
            "staged %s into image dir %s; create the CVD (power off --destroy; power on) to boot it",
            label,
            generation.dir_id,
        )

    def close(self):
        """Reclaim pending staged image dirs at teardown.

        The active image dir is intentionally retained: the exporter can be
        stopped while its CVD is still running, and deleting a directory that
        backs that CVD can corrupt Host Orchestrator state. It is released by
        ``power off --destroy`` after the CVD is deleted. HO has no delete route
        for the underlying user artifacts, so those persist server-side by
        design.
        """
        p = self.parent
        if p._staged:
            p._delete_image_dir_quietly(p._staged.dir_id)
        if p._active:
            self.logger.warning(
                "leaving active image dir %s at exporter teardown; destroy the CVD before manual cleanup",
                p._active.dir_id,
            )
        p._staged = None
        p._active = None
        super().close()

    # HO's extractor accepts only these two formats; both are detectable by the
    # archive's leading magic bytes.
    _GZIP_MAGIC = b"\x1f\x8b"
    _ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")  # normal, empty, spanned

    async def _spool(self, source) -> tuple[str, str, int, str]:
        """Stream `source` to a temp file, returning (path, sha256_hex, size, suffix).

        HO addresses uploads by checksum, so the full digest must be known
        before the upload exists — hence spool-to-disk-then-upload rather than a
        single streaming pass. The archive format is sniffed from the leading
        magic bytes (the resource stream has no filename) so the upload can carry
        the suffix HO's extractor dispatches on.
        """
        fd, tmp_path = tempfile.mkstemp(prefix="cvd-flash-")
        os.close(fd)
        hasher = hashlib.sha256()
        size = 0
        head = b""
        try:
            async with await FileWriteStream.from_path(tmp_path) as out:
                async with self.resource(source) as res:
                    async for chunk in res:
                        if len(head) < 4:
                            head += bytes(chunk[: 4 - len(head)])
                        hasher.update(chunk)
                        size += len(chunk)
                        await out.send(chunk)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        suffix = self._sniff_suffix(head, tmp_path)
        return tmp_path, hasher.hexdigest(), size, suffix

    def _sniff_suffix(self, head: bytes, tmp_path: str) -> str:
        """Map an archive's magic bytes to the suffix HO's extractor requires."""
        if head.startswith(self._GZIP_MAGIC):
            return ".tar.gz"
        if head.startswith(self._ZIP_MAGIC):
            return ".zip"
        # Unusable spool — remove it here since flash()'s finally never runs when
        # _spool raises, and fail clearly (HO would reject it anyway).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise CuttlefishError(
            f"unsupported archive format: expected a .zip (device images) or a "
            f".tar.gz (host package); leading bytes were {head[:4]!r}"
        )

    async def _upload(self, checksum: str, path: str, size: int, suffix: str) -> None:
        """Upload a spooled artifact via HO's chunked multipart PUT.

        Each PUT carries one multipart ``file`` part plus ``chunk_offset_bytes``
        and ``file_size_bytes`` form fields; HO reassembles by offset. Uses
        httpx (async) so a multi-GB transfer never blocks the event loop.

        A failed chunk is retried at the same offset — since HO reassembles by
        offset, re-PUTting an offset is idempotent, so per-chunk retry doubles
        as resume and a mid-transfer blip doesn't abort several GB of upload.
        """
        url = f"{self.parent._base_url}/v1/userartifacts/{checksum}"
        chunk_size = self.parent.upload_chunk_size
        # The stored filename's suffix is what HO's extractor dispatches on, so
        # it must reflect the real archive format (see _sniff_suffix).
        filename = f"{checksum}{suffix}"
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=None, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with await FileReadStream.from_path(path) as f:
                offset = 0
                while offset < size:
                    buf = bytearray()
                    while len(buf) < chunk_size:
                        try:
                            buf += await f.receive(chunk_size - len(buf))
                        except EndOfStream:
                            break
                    if not buf:
                        break
                    await self._put_chunk(client, url, filename, bytes(buf), offset, size)
                    offset += len(buf)

    async def _put_chunk(
        self, client: "httpx.AsyncClient", url: str, filename: str, chunk: bytes, offset: int, size: int
    ) -> None:
        """PUT a single chunk, retrying at the same offset on transient failure."""
        attempts = max(1, self.parent.upload_max_retries)
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.put(
                    url,
                    files={"file": (filename, chunk, "application/octet-stream")},
                    data={"chunk_offset_bytes": str(offset), "file_size_bytes": str(size)},
                )
                resp.raise_for_status()
                return
            except httpx.HTTPError as e:
                if attempt >= attempts:
                    raise CuttlefishError(f"upload failed at offset {offset} after {attempts} attempts: {e}") from e
                self.logger.warning(
                    "upload chunk at offset %d failed (attempt %d/%d): %s; retrying", offset, attempt, attempts, e
                )
                await sleep(2 * attempt)

    @export
    def dump(self, target, partition: str | None = None) -> None:
        raise NotImplementedError("dump not supported for Cuttlefish devices")
