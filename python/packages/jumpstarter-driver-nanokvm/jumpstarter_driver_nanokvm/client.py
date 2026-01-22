import io
from base64 import b64decode
from dataclasses import dataclass

import click
from jumpstarter_driver_composite.client import CompositeClient
from nanokvm.models import MouseButton
from PIL import Image

from jumpstarter.client import DriverClient
from jumpstarter.client.decorators import driver_click_group

# Re-export MouseButton for convenience
__all__ = ["NanoKVMVideoClient", "NanoKVMHIDClient", "NanoKVMClient", "MouseButton"]


@dataclass(kw_only=True)
class NanoKVMVideoClient(DriverClient):
    """
    Client interface for NanoKVM video streaming

    This client provides methods to access video stream and snapshots
    from the NanoKVM device.
    """

    def snapshot(self, skip_frames: int = 3) -> Image.Image:
        """
        Get a snapshot image from the video stream

        Returns:
            PIL Image object of the snapshot
        """
        input_jpg_data = b64decode(self.call("snapshot", skip_frames))
        return Image.open(io.BytesIO(input_jpg_data))

    def cli(self):
        @driver_click_group(self)
        def base():
            """NanoKVM Video commands"""
            pass

        @base.command()
        @click.argument("output", type=click.Path(), default="snapshot.jpg")
        def snapshot(output):
            """Take a snapshot and save to file"""
            image = self.snapshot()
            image.save(output)
            click.echo(f"Snapshot saved to {output}")
            click.echo(f"Image size: {image.size[0]}x{image.size[1]}")

        return base


@dataclass(kw_only=True)
class NanoKVMHIDClient(DriverClient):
    """
    Client interface for NanoKVM HID (Keyboard/Mouse) control

    This client provides methods to send keyboard and mouse events
    to the device connected to the NanoKVM.
    """

    def paste_text(self, text: str):
        """
        Paste text via keyboard HID simulation

        Args:
            text: Text to paste. Supports limited character set:
                  alphanumeric, basic punctuation, and common symbols

        Example::

            hid.paste_text("Hello, World!")
            hid.paste_text("ls -la\\n")
        """
        self.call("paste_text", text)

    def press_key(self, key: str):
        """
        Press a key by pasting a single character

        Args:
            key: Single character or escape sequence to press

        Example::

            hid.press_key("a")      # Type 'a'
            hid.press_key("A")      # Type 'A'
            hid.press_key("\\n")    # Press Enter
            hid.press_key("\\t")    # Press Tab
        """
        self.call("press_key", key)

    def reset_hid(self):
        """
        Reset the HID subsystem

        This can help recover from HID communication issues.
        """
        self.call("reset_hid")

    def mouse_move_abs(self, x: float, y: float):
        """
        Move mouse to absolute coordinates

        Args:
            x: X coordinate (0.0 to 1.0, where 0.0 is left and 1.0 is right)
            y: Y coordinate (0.0 to 1.0, where 0.0 is top and 1.0 is bottom)

        Example::

            # Move to center of screen
            hid.mouse_move_abs(0.5, 0.5)

            # Move to top-left corner
            hid.mouse_move_abs(0.0, 0.0)

            # Move to bottom-right corner
            hid.mouse_move_abs(1.0, 1.0)
        """
        self.call("mouse_move_abs", x, y)

    def mouse_move_rel(self, dx: float, dy: float):
        """
        Move mouse relative to current position

        Args:
            dx: X movement delta (-1.0 to 1.0, where 1.0 is full screen width)
            dy: Y movement delta (-1.0 to 1.0, where 1.0 is full screen height)

        Example::

            # Move right by 10% of screen width and down by 10%
            hid.mouse_move_rel(0.1, 0.1)

            # Move left by 20%
            hid.mouse_move_rel(-0.2, 0.0)
        """
        self.call("mouse_move_rel", dx, dy)

    def mouse_click(self, button: MouseButton | str = "left", x: float | None = None, y: float | None = None):
        """
        Click a mouse button

        Args:
            button: Mouse button to click (MouseButton enum or "left", "right", "middle" string)
            x: Optional X coordinate (0.0 to 1.0) for absolute positioning before click
            y: Optional Y coordinate (0.0 to 1.0) for absolute positioning before click

        Example::

            # Using string (backward compatible)
            hid.mouse_click("left")
            hid.mouse_click("left", 0.5, 0.5)

            # Using MouseButton enum (recommended)
            hid.mouse_click(MouseButton.LEFT)
            hid.mouse_click(MouseButton.RIGHT, 0.75, 0.25)
        """
        if x is not None and y is not None:
            self.call("mouse_click", button, x, y)
        else:
            self.call("mouse_click", button, None, None)

    def mouse_scroll(self, dx: int, dy: int):
        """
        Scroll the mouse wheel

        Args:
            dx: Horizontal scroll amount
            dy: Vertical scroll amount (positive=up, negative=down)

        Example::

            # Scroll up
            hid.mouse_scroll(0, 5)

            # Scroll down
            hid.mouse_scroll(0, -5)
        """
        self.call("mouse_scroll", dx, dy)

    def cli(self):  # noqa: C901
        @driver_click_group(self)
        def base():
            """NanoKVM HID (Keyboard/Mouse) commands"""
            pass

        @base.command()
        @click.argument("text")
        def paste(text):
            """Paste text via keyboard HID (supports \\n for newline, \\t for tab)"""
            # Decode escape sequences like \n, \t, etc.
            decoded_text = text.encode().decode("unicode_escape")
            self.paste_text(decoded_text)
            click.echo(f"Pasted: {repr(decoded_text)}")

        @base.command()
        @click.argument("key")
        def press(key):
            """Press a single key (supports \\n for Enter, \\t for Tab)"""
            # Decode escape sequences
            decoded_key = key.encode().decode("unicode_escape")
            self.press_key(decoded_key)
            click.echo(f"Pressed: {repr(decoded_key)}")

        @base.command()
        def reset():
            """Reset the HID subsystem"""
            self.reset_hid()
            click.echo("HID subsystem reset")

        @base.group()
        def mouse():
            """Mouse control commands"""
            pass

        @mouse.command()
        @click.argument("x", type=float)
        @click.argument("y", type=float)
        def move(x, y):
            """Move mouse to absolute coordinates (0.0-1.0)"""
            self.mouse_move_abs(x, y)
            click.echo(f"Mouse moved to ({x}, {y})")

        @mouse.command()
        @click.argument("dx", type=float)
        @click.argument("dy", type=float)
        def move_rel(dx, dy):
            """Move mouse by relative offset (-1.0 to 1.0, where 1.0 is full screen)"""
            self.mouse_move_rel(dx, dy)
            click.echo(f"Mouse moved by ({dx}, {dy})")

        @mouse.command(name="click")
        @click.option("--button", "-b", default="left", type=click.Choice(["left", "right", "middle"]))
        @click.option("--x", type=float, default=None, help="Optional X coordinate (0.0-1.0)")
        @click.option("--y", type=float, default=None, help="Optional Y coordinate (0.0-1.0)")
        def mouse_click_cmd(button, x, y):
            """Click a mouse button"""
            # Convert string to MouseButton enum
            button_map = {
                "left": MouseButton.LEFT,
                "right": MouseButton.RIGHT,
                "middle": MouseButton.MIDDLE,
            }
            button_enum = button_map[button]
            self.mouse_click(button_enum, x, y)
            if x is not None and y is not None:
                click.echo(f"Clicked {button} button at ({x}, {y})")
            else:
                click.echo(f"Clicked {button} button")

        @mouse.command()
        @click.option("--dx", type=int, default=0, help="Horizontal scroll")
        @click.option("--dy", type=int, default=-5, help="Vertical scroll")
        def scroll(dx, dy):
            """Scroll the mouse wheel"""
            self.mouse_scroll(dx, dy)
            click.echo(f"Scrolled ({dx}, {dy})")

        return base


@dataclass(kw_only=True)
class NanoKVMClient(CompositeClient):
    """
    Client interface for NanoKVM devices

    This composite client provides access to all NanoKVM functionality:
    - video: Video streaming and snapshots
    - hid: Keyboard and mouse control
    - serial: Serial console access (if enabled)

    Example::

        # Get a snapshot
        image = nanokvm.video.snapshot()

        # Paste text
        nanokvm.hid.paste_text("Hello from Jumpstarter!")

        # Get device info
        info = nanokvm.get_info()
        print(f"Device: {info['mdns']}")
    """

    def get_info(self) -> dict:
        """
        Get device information

        Returns:
            Dictionary containing device information:
            - ips: List of IP addresses
            - mdns: mDNS hostname
            - image: Image version
            - application: Application version
            - device_key: Device key
        """
        return self.call("get_info")

    def reboot(self):
        """
        Reboot the NanoKVM device

        Warning:
            This will reboot the NanoKVM itself, not the connected device.
            The connection will be lost during reboot.
        """
        self.call("reboot")

    def mount_image(self, file: str = "", cdrom: bool = False):
        """
        Mount an image file or unmount if file is empty string

        Args:
            file: Path to image file on the NanoKVM device, or empty string to unmount
            cdrom: Whether to mount as CD-ROM (True) or disk (False)

        Note:
            Unmounting may fail if image is currently in use. If unmount fails,
            you may need to power cycle the connected device first.

        Example::

            # Mount a disk image
            nanokvm.mount_image("/path/to/disk.img", cdrom=False)

            # Mount a CD-ROM image
            nanokvm.mount_image("/path/to/cdrom.iso", cdrom=True)

            # Unmount
            nanokvm.mount_image("") or nanokvm.mount_image()
        """
        self.call("mount_image", file, cdrom)

    def download_image(self, url: str) -> dict:
        """
        Start downloading an image from a URL

        Args:
            url: URL of the image to download

        Returns:
            Dictionary with download status, file, and percentage

        Example::

            status = nanokvm.download_image("https://example.com/image.iso")
            print(f"Download: {status['status']}, File: {status['file']}, {status['percentage']}%")
        """
        return self.call("download_image", url)

    def get_mounted_image(self) -> str | None:
        """
        Get information about mounted image

        Returns:
            String with mounted image file path, or None if no image mounted

        Example::

            file = nanokvm.get_mounted_image()
            if file:
                print(f"Mounted: {file}")
        """
        return self.call("get_mounted_image")

    def get_cdrom_status(self) -> bool:
        """
        Check if the mounted image is in CD-ROM mode

        Returns:
            Boolean indicating if CD-ROM mode is active (True=CD-ROM, False=disk)

        Example::

            if nanokvm.get_cdrom_status():
                print("CD-ROM mode is enabled")
        """
        return self.call("get_cdrom_status")

    def is_image_download_enabled(self) -> bool:
        """
        Check if the /data partition allows image downloads

        Returns:
            Boolean indicating if image downloads are enabled

        Example::

            if nanokvm.is_image_download_enabled():
                print("Image downloads are available")
        """
        return self.call("is_image_download_enabled")

    def get_image_download_status(self) -> dict:
        """
        Get the status of an ongoing image download

        Returns:
            Dictionary with download status, file, and percentage complete

        Example::

            status = nanokvm.get_image_download_status()
            print(f"Status: {status['status']}, File: {status['file']}, {status['percentage']}%")
        """
        return self.call("get_image_download_status")

    def get_images(self) -> list[str]:
        """
        Get the list of available image files

        Returns:
            List of image file paths available on the NanoKVM device

        Example::

            images = nanokvm.get_images()
            for image in images:
                print(f"Available: {image}")
        """
        return self.call("get_images")

    def cli(self):  # noqa: C901
        """Create CLI interface with device management and child commands"""
        base = super().cli()

        @base.command()
        def info():
            """Get device information"""
            info = self.get_info()
            click.echo("NanoKVM Device Information:")
            click.echo(f"  mDNS: {info['mdns']}")
            click.echo(f"  Image version: {info['image']}")
            click.echo(f"  Application version: {info['application']}")
            click.echo(f"  Device key: {info['device_key']}")
            if info['ips']:
                click.echo("  IP Addresses:")
                for ip in info['ips']:
                    click.echo(f"    - {ip['name']}: {ip['addr']} ({ip['type']}, {ip['version']})")

        @base.command()
        @click.confirmation_option(prompt="Are you sure you want to reboot the NanoKVM device?")
        def reboot():
            """Reboot the NanoKVM device"""
            self.reboot()
            click.echo("NanoKVM device is rebooting...")

        @base.group()
        def image():
            """Image management commands"""
            pass

        @image.command()
        @click.argument("file")
        @click.option("--cdrom", is_flag=True, help="Mount as CD-ROM instead of disk")
        def mount(file, cdrom):
            """Mount an image file"""
            self.mount_image(file, cdrom)
            image_type = "CD-ROM" if cdrom else "disk"
            click.echo(f"Mounted {file} as {image_type}")

        @image.command()
        def unmount():
            """Unmount the currently mounted image

            Note: Unmount may fail if image is in use by the connected device.
            Power cycle the device first if unmount fails.
            """
            try:
                self.mount_image("")
                click.echo("Image unmounted successfully")
            except Exception as e:
                click.echo(f"Failed to unmount image: {e}", err=True)
                click.echo("Note: Image may be in use. Try power cycling the connected device first.", err=True)
                raise

        @image.command()
        @click.argument("url")
        def download(url):
            """Download an image from URL"""
            status = self.download_image(url)
            click.echo(f"Download started: {status['status']}")
            if status['file']:
                click.echo(f"File: {status['file']}")
            if status['percentage']:
                click.echo(f"Progress: {status['percentage']}%")

        @image.command()
        def status():
            """Show mounted image status"""
            file = self.get_mounted_image()
            if file:
                is_cdrom = self.get_cdrom_status()
                mode = "CD-ROM" if is_cdrom else "Disk"
                click.echo(f"Mounted: {file}")
                click.echo(f"Mode: {mode}")
            else:
                click.echo("No image mounted")

        @image.command()
        def cdrom_status():
            """Check if mounted image is in CD-ROM mode"""
            is_cdrom = self.get_cdrom_status()
            mode = "CD-ROM" if is_cdrom else "Disk"
            click.echo(f"Current mode: {mode}")

        @image.command()
        def download_enabled():
            """Check if image downloads are enabled"""
            enabled = self.is_image_download_enabled()
            status = "enabled" if enabled else "disabled"
            click.echo(f"Image downloads: {status}")

        @image.command()
        def download_status():
            """Get current image download status"""
            status = self.get_image_download_status()
            click.echo(f"Status: {status['status']}")
            if status['file']:
                click.echo(f"File: {status['file']}")
            if status['percentage']:
                click.echo(f"Progress: {status['percentage']}")

        @image.command()
        def list():
            """List available image files"""
            images = self.get_images()
            if images:
                click.echo("Available images:")
                for img in images:
                    click.echo(f"  - {img}")
            else:
                click.echo("No images available")

        return base
