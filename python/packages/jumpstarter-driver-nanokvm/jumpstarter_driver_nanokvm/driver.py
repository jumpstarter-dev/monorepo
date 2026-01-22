import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from io import BytesIO

import anyio
from aiohttp import ClientResponseError
from jumpstarter_driver_composite.driver import Composite
from jumpstarter_driver_pyserial.driver import PySerial
from nanokvm.client import NanoKVMClient as NanoKVMAPIClient
from nanokvm.models import MouseButton

from jumpstarter.driver import Driver, export, exportstream

# Re-export MouseButton for convenience
__all__ = ["NanoKVMVideo", "NanoKVMHID", "NanoKVMSerial", "NanoKVM", "MouseButton"]


def _is_unauthorized_error(error: Exception) -> bool:
    """Check if an error is a 401 Unauthorized error"""
    if isinstance(error, ClientResponseError):
        return error.status == 401
    # Also check for string representation in case error is wrapped
    error_str = str(error)
    return "401" in error_str and ("Unauthorized" in error_str or "unauthorized" in error_str.lower())


def with_reauth(func):
    """Decorator to automatically re-authenticate on 401 errors"""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except Exception as e:
            if _is_unauthorized_error(e):
                self.logger.warning("Received 401 Unauthorized, re-authenticating...")
                await self._reset_client()
                # Retry once after re-authentication
                return await func(self, *args, **kwargs)
            raise
    return wrapper


@dataclass(kw_only=True)
class NanoKVMVideo(Driver):
    """NanoKVM Video Streaming driver"""

    host: str
    username: str = "admin"
    password: str = "admin"

    _client: NanoKVMAPIClient | None = field(init=False, repr=False, default=None)
    _client_ctx: object = field(init=False, repr=False, default=None)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_nanokvm.client.NanoKVMVideoClient"

    async def _reset_client(self):
        """Reset the client, forcing re-authentication"""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                self.logger.debug(f"Error closing client during reset: {e}")
        self._client = None
        self._client_ctx = None

    async def _get_client(self) -> NanoKVMAPIClient:
        """Get or create the NanoKVM API client using context manager"""
        if self._client is None:
            # Create a new client context manager
            self._client_ctx = NanoKVMAPIClient(f"http://{self.host}/api/")
            # Enter the context manager
            self._client = await self._client_ctx.__aenter__()
            # Authenticate
            await self._client.authenticate(self.username, self.password)
        return self._client

    def close(self):
        """Clean up resources"""
        # Schedule cleanup of client
        if self._client_ctx is not None:
            try:
                anyio.from_thread.run(self._client_ctx.__aexit__(None, None, None))
            except Exception as e:
                self.logger.debug(f"Error closing client: {e}")

    @export
    @with_reauth
    async def snapshot(self, skip_frames: int = 3) -> str:
        """
        Take a snapshot from the video stream

        Returns:
            Base64 encoded JPEG image data
        """
        client = await self._get_client()
        frame_count = 0
        async for frame in client.mjpeg_stream():
            frame_count += 1
            # Skip the first frames as it's normally stale
            if frame_count < skip_frames:
                continue
            # Return the second (fresh) frame
            buffer = BytesIO()
            frame.save(buffer, format="JPEG")
            data = buffer.getvalue()
            self.logger.debug(f"snapshot: {len(data)} bytes")
            return b64encode(data).decode("ascii")
        raise RuntimeError("No frames available from video stream")

    @exportstream
    @asynccontextmanager
    async def stream(self):
        """
        Stream video frames as JPEG images

        Yields a stream that provides JPEG image data
        """
        self.logger.debug("Starting video stream")
        client = await self._get_client()

        # Create a pair of connected streams
        send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=10)

        async def stream_video():
            try:
                async with send_stream:
                    async for frame in client.mjpeg_stream():
                        buffer = BytesIO()
                        frame.save(buffer, format="JPEG")
                        data = buffer.getvalue()
                        # TODO(mangelajo): this needs to be tested
                        await send_stream.send(data)
            except Exception as e:
                if _is_unauthorized_error(e):
                    self.logger.warning("Received 401 Unauthorized during stream, re-authenticating...")
                    await self._reset_client()
                    # Retry with new client
                    new_client = await self._get_client()
                    async for frame in new_client.mjpeg_stream():
                        buffer = BytesIO()
                        frame.save(buffer, format="JPEG")
                        data = buffer.getvalue()
                        await send_stream.send(data)
                else:
                    self.logger.error(f"Error streaming video: {e}")
                    raise

        # Start the video streaming task
        task = asyncio.create_task(stream_video())

        try:
            yield receive_stream
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@dataclass(kw_only=True)
class NanoKVMHID(Driver):
    """NanoKVM HID (Keyboard/Mouse) driver"""

    host: str
    username: str = "admin"
    password: str = "admin"

    _client: NanoKVMAPIClient | None = field(init=False, repr=False, default=None)
    _client_ctx: object = field(init=False, repr=False, default=None)

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_nanokvm.client.NanoKVMHIDClient"

    async def _reset_client(self):
        """Reset the client, forcing re-authentication"""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                self.logger.debug(f"Error closing client during reset: {e}")
        self._client = None
        self._client_ctx = None

    async def _get_client(self) -> NanoKVMAPIClient:
        """Get or create the NanoKVM API client using context manager"""
        if self._client is None:
            # Create a new client context manager
            self._client_ctx = NanoKVMAPIClient(f"http://{self.host}/api/")
            # Enter the context manager
            self._client = await self._client_ctx.__aenter__()
            # Authenticate
            await self._client.authenticate(self.username, self.password)
        return self._client

    def close(self):
        """Clean up resources"""
        # Schedule cleanup of client
        if self._client_ctx is not None:
            try:
                anyio.from_thread.run(self._client_ctx.__aexit__(None, None, None))
            except Exception as e:
                self.logger.debug(f"Error closing client: {e}")

    @export
    @with_reauth
    async def paste_text(self, text: str):
        """
        Paste text via keyboard HID simulation

        Args:
            text: Text to paste (limited character set supported)
        """
        client = await self._get_client()
        await client.paste_text(text)
        self.logger.info(f"Pasted text: {text}")

    @export
    @with_reauth
    async def press_key(self, key: str):
        """
        Press a key by pasting a single character

        Args:
            key: Single character or escape sequence to press (e.g., 'a', 'A', '\\n', '\\t')

        Note:
            This uses paste_text under the hood, so it supports the same character set.
            For special keys like Enter, use '\\n'. For Tab, use '\\t'.
        """
        if len(key) > 2:  # Allow for escape sequences like \n, \t
            self.logger.warning(f"press_key should be used with single characters, got: {key}")

        client = await self._get_client()
        await client.paste_text(key)
        self.logger.debug(f"Pressed key: {repr(key)}")

    @export
    @with_reauth
    async def reset_hid(self):
        """Reset the HID subsystem"""
        client = await self._get_client()
        await client.reset_hid()
        self.logger.info("HID subsystem reset")

    @export
    @with_reauth
    async def mouse_move_abs(self, x: float, y: float):
        """
        Move mouse to absolute coordinates

        Args:
            x: X coordinate (0.0 to 1.0, where 0.0 is left/top and 1.0 is right/bottom)
            y: Y coordinate (0.0 to 1.0, where 0.0 is left/top and 1.0 is right/bottom)
        """
        client = await self._get_client()
        await client.mouse_move_abs(x, y)
        self.logger.debug(f"Mouse moved to absolute position: ({x}, {y})")

    @export
    @with_reauth
    async def mouse_move_rel(self, dx: float, dy: float):
        """
        Move mouse relative to current position

        Args:
            dx: X movement delta (-1.0 to 1.0, where 1.0 is full screen width)
            dy: Y movement delta (-1.0 to 1.0, where 1.0 is full screen height)
        """
        client = await self._get_client()
        await client.mouse_move_rel(dx, dy)
        self.logger.debug(f"Mouse moved by relative offset: ({dx}, {dy})")

    @export
    @with_reauth
    async def mouse_click(self, button: MouseButton | str = "left", x: float | None = None, y: float | None = None):
        """
        Click a mouse button at current position or specified coordinates

        Args:
            button: Mouse button to click (MouseButton enum or "left", "right", "middle" string)
            x: Optional X coordinate (0.0 to 1.0) for absolute positioning before click
            y: Optional Y coordinate (0.0 to 1.0) for absolute positioning before click
        """
        # Convert string to MouseButton enum for backward compatibility
        if isinstance(button, str):
            button_map = {
                "left": MouseButton.LEFT,
                "right": MouseButton.RIGHT,
                "middle": MouseButton.MIDDLE,
            }
            button = button_map.get(button.lower(), MouseButton.LEFT)

        client = await self._get_client()
        await client.mouse_click(button, x, y)
        self.logger.info(f"Mouse {button.name} clicked")

    @export
    @with_reauth
    async def mouse_scroll(self, dx: int, dy: int):
        """
        Scroll the mouse wheel

        Args:
            dx: Horizontal scroll amount
            dy: Vertical scroll amount (positive=up, negative=down)
        """
        client = await self._get_client()
        await client.mouse_scroll(dx, dy)
        self.logger.debug(f"Mouse scrolled: ({dx}, {dy})")


@dataclass(kw_only=True)
class NanoKVMSerial(PySerial):
    """NanoKVM Serial console access via SSH tunnel"""

    nanokvm_host: str
    nanokvm_username: str = "root"
    nanokvm_password: str = "root"
    nanokvm_ssh_port: int = 22

    # PySerial will use the SSH tunnel
    url: str = field(init=False)

    def __post_init__(self):
        # Create an RFC2217 URL that will connect via SSH
        # For now, we'll use a simple approach with a localhost tunnel
        # This requires the user to set up SSH port forwarding manually
        # or we can use paramiko to create the tunnel
        self.url = "rfc2217://localhost:2217"
        super().__post_init__()

    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_pyserial.client.PySerialClient"


@dataclass(kw_only=True)
class NanoKVM(Composite):
    """
    Composite driver for NanoKVM devices

    This driver provides:
    - Video streaming via the 'video' child driver
    - HID (Keyboard/Mouse) control via the 'hid' child driver
    - Serial console access via SSH tunnel (optional)
    """

    host: str
    username: str = "admin"
    password: str = "admin"

    # SSH access for serial console (optional)
    ssh_username: str = "root"
    ssh_password: str = "root"
    ssh_port: int = 22

    # Optional: provide serial console access
    enable_serial: bool = False

    def __post_init__(self):
        # Create child drivers
        self.children = {
            "video": NanoKVMVideo(
                host=self.host,
                username=self.username,
                password=self.password,
            ),
            "hid": NanoKVMHID(
                host=self.host,
                username=self.username,
                password=self.password,
            ),
        }

        super().__post_init__()

        # Optionally add serial console access
        if self.enable_serial:
            # Note: This is a placeholder - actual serial console access via SSH
            # would require additional implementation in the nanokvm library
            self.logger.warning("Serial console access not yet fully implemented")


    @classmethod
    def client(cls) -> str:
        return "jumpstarter_driver_nanokvm.client.NanoKVMClient"

    @export
    async def get_info(self):
        """Get device information"""
        # Get info from the video driver's client
        video_driver = self.children["video"]

        def _format_info(info):
            """Format device info into a dictionary"""
            return {
                "ips": [
                    {"name": ip.name, "addr": ip.addr, "version": ip.version, "type": ip.type}
                    for ip in info.ips
                ],
                "mdns": info.mdns,
                "image": info.image,
                "application": info.application,
                "device_key": info.device_key,
            }

        try:
            client = await video_driver._get_client()
            info = await client.get_info()
            return _format_info(info)
        except Exception as e:
            if _is_unauthorized_error(e):
                self.logger.warning("Received 401 Unauthorized, re-authenticating...")
                await video_driver._reset_client()
                # Retry once after re-authentication
                client = await video_driver._get_client()
                info = await client.get_info()
                return _format_info(info)
            raise

    @export
    async def reboot(self):
        """Reboot the NanoKVM device"""
        video_driver = self.children["video"]

        @with_reauth
        async def _reboot_impl(driver):
            client = await driver._get_client()
            await client.reboot_system()

        await _reboot_impl(video_driver)
        self.logger.info("NanoKVM device rebooted")

    @export
    async def mount_image(self, file: str = "", cdrom: bool = False):
        """
        Mount an image file or unmount if file is empty string

        Args:
            file: Path to image file on the NanoKVM device, or empty string to unmount
            cdrom: Whether to mount as CD-ROM (True) or disk (False)
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _mount_impl(driver):
            client = await driver._get_client()
            # Pass empty string or None for unmount - API expects empty string
            mount_file = file if file else ""
            # When unmounting, we need to pass the file as empty string or None
            await client.mount_image(file=mount_file or None, cdrom=cdrom if mount_file else False)

        await _mount_impl(video_driver)
        if file:
            self.logger.info(f"Mounted image: {file} (cdrom={cdrom})")
        else:
            self.logger.info("Unmounted image")

    @export
    async def download_image(self, url: str):
        """
        Start downloading an image from a URL

        Args:
            url: URL of the image to download

        Returns:
            Dictionary with download status information
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _download_impl(driver):
            client = await driver._get_client()
            status = await client.download_image(url=url)
            return {
                "status": status.status,
                "file": status.file,
                "percentage": status.percentage,
            }

        result = await _download_impl(video_driver)
        self.logger.info(f"Started download from {url}")
        return result

    @export
    async def get_mounted_image(self):
        """
        Get information about mounted image

        Returns:
            String with mounted image file path, or None if no image mounted
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _get_mounted_impl(driver):
            client = await driver._get_client()
            info = await client.get_mounted_image()
            return info.file

        return await _get_mounted_impl(video_driver)

    @export
    async def get_cdrom_status(self):
        """
        Check if the mounted image is in CD-ROM mode

        Returns:
            Boolean indicating if CD-ROM mode is active (True=CD-ROM, False=disk)
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _get_cdrom_status_impl(driver):
            client = await driver._get_client()
            status = await client.get_cdrom_status()
            return bool(status.cdrom)

        return await _get_cdrom_status_impl(video_driver)

    @export
    async def is_image_download_enabled(self):
        """
        Check if the /data partition allows image downloads

        Returns:
            Boolean indicating if image downloads are enabled
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _is_download_enabled_impl(driver):
            client = await driver._get_client()
            status = await client.is_image_download_enabled()
            return status.enabled

        return await _is_download_enabled_impl(video_driver)

    @export
    async def get_image_download_status(self):
        """
        Get the status of an ongoing image download

        Returns:
            Dictionary with download status, file, and percentage complete
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _get_download_status_impl(driver):
            client = await driver._get_client()
            status = await client.get_image_download_status()
            return {
                "status": status.status,
                "file": status.file,
                "percentage": status.percentage,
            }

        return await _get_download_status_impl(video_driver)

    @export
    async def get_images(self):
        """
        Get the list of available image files

        Returns:
            List of image file paths available on the NanoKVM device
        """
        video_driver = self.children["video"]

        @with_reauth
        async def _get_images_impl(driver):
            client = await driver._get_client()
            images = await client.get_images()
            return images.files

        return await _get_images_impl(video_driver)

