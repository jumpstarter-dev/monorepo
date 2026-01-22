"""Tests for NanoKVM driver"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from .driver import NanoKVM, NanoKVMHID, NanoKVMVideo
from jumpstarter.common.utils import serve


@pytest.fixture
def mock_nanokvm_client():
    """Create a mock NanoKVM API client"""
    with patch("jumpstarter_driver_nanokvm.driver.NanoKVMAPIClient") as mock_client_class:
        mock_client = AsyncMock()

        # Mock authentication
        mock_client.authenticate = AsyncMock()
        mock_client.logout = AsyncMock()
        mock_client.close = AsyncMock()

        # Mock info
        mock_info = MagicMock()
        mock_info.ips = []
        mock_info.mdns = "nanokvm-test.local"
        mock_info.image = "1.0.0"
        mock_info.application = "1.0.0"
        mock_info.device_key = "test-key"
        mock_client.get_info = AsyncMock(return_value=mock_info)

        # Mock video streaming
        test_image = Image.new("RGB", (640, 480), color="red")

        async def mock_stream():
            # Yield several frames - first ones are buffered/old, later ones are fresh
            yield test_image
            yield test_image
            yield test_image
            yield test_image

        mock_client.mjpeg_stream = mock_stream

        # Mock HID functions
        mock_client.paste_text = AsyncMock()
        mock_client.reset_hid = AsyncMock()
        mock_client.mouse_move_abs = AsyncMock()
        mock_client.mouse_click = AsyncMock()

        # Mock reboot
        mock_client.reboot_system = AsyncMock()

        # Mock image management
        mock_images = MagicMock()
        mock_images.files = ["/data/alpine-standard-3.23.2-x86_64.iso", "/data/cs10-js.iso"]
        mock_client.get_images = AsyncMock(return_value=mock_images)

        # Mock context manager behavior
        mock_context = AsyncMock()
        mock_context.__aenter__ = AsyncMock(return_value=mock_client)
        mock_context.__aexit__ = AsyncMock(return_value=None)
        mock_client_class.return_value = mock_context

        yield mock_client


@pytest.fixture
def mock_aiohttp_session():
    """Create a mock aiohttp ClientSession"""
    with patch("aiohttp.ClientSession") as mock_session_class:
        mock_session = AsyncMock()
        mock_session.close = AsyncMock()
        mock_session_class.return_value = mock_session
        yield mock_session


def test_nanokvm_video_snapshot(mock_nanokvm_client, mock_aiohttp_session):
    """Test video snapshot functionality"""
    video = NanoKVMVideo(host="test.local", username="admin", password="admin")

    with serve(video) as client:
        # Get a snapshot
        image = client.snapshot()

        # Verify it's a PIL Image
        assert isinstance(image, Image.Image)
        assert image.size == (640, 480)


def test_nanokvm_hid_paste(mock_nanokvm_client, mock_aiohttp_session):
    """Test HID paste text functionality"""
    hid = NanoKVMHID(host="test.local", username="admin", password="admin")

    with serve(hid) as client:
        # Paste some text
        client.paste_text("Hello, World!")

        # Verify the mock was called
        mock_nanokvm_client.paste_text.assert_called_once_with("Hello, World!")


def test_nanokvm_hid_reset(mock_nanokvm_client, mock_aiohttp_session):
    """Test HID reset functionality"""
    hid = NanoKVMHID(host="test.local", username="admin", password="admin")

    with serve(hid) as client:
        # Reset HID
        client.reset_hid()

        # Verify the mock was called
        mock_nanokvm_client.reset_hid.assert_called_once()


def test_nanokvm_hid_press_key(mock_nanokvm_client, mock_aiohttp_session):
    """Test key press functionality"""
    hid = NanoKVMHID(host="test.local", username="admin", password="admin")

    with serve(hid) as client:
        # Press a key
        client.press_key("a")

        # Verify paste_text was called with the character
        mock_nanokvm_client.paste_text.assert_called_with("a")


def test_nanokvm_composite(mock_nanokvm_client, mock_aiohttp_session):
    """Test composite NanoKVM driver"""
    driver = NanoKVM(
        host="test.local",
        username="admin",
        password="admin",
    )

    with serve(driver) as client:
        # Test that children are accessible
        assert hasattr(client, "video")
        assert hasattr(client, "hid")

        # Test video snapshot through composite
        image = client.video.snapshot()
        assert isinstance(image, Image.Image)

        # Test HID paste through composite
        client.hid.paste_text("Test")
        mock_nanokvm_client.paste_text.assert_called_with("Test")

        # Test get_info
        info = client.get_info()
        assert "mdns" in info
        assert info["mdns"] == "nanokvm-test.local"


def test_nanokvm_reboot(mock_nanokvm_client, mock_aiohttp_session):
    """Test NanoKVM reboot functionality"""
    driver = NanoKVM(
        host="test.local",
        username="admin",
        password="admin",
    )

    with serve(driver) as client:
        # Test reboot
        client.reboot()
        mock_nanokvm_client.reboot_system.assert_called_once()


def test_nanokvm_video_client_creation():
    """Test that NanoKVMVideo returns correct client class"""
    assert NanoKVMVideo.client() == "jumpstarter_driver_nanokvm.client.NanoKVMVideoClient"


def test_nanokvm_hid_client_creation():
    """Test that NanoKVMHID returns correct client class"""
    assert NanoKVMHID.client() == "jumpstarter_driver_nanokvm.client.NanoKVMHIDClient"


def test_nanokvm_client_creation():
    """Test that NanoKVM returns correct client class"""
    assert NanoKVM.client() == "jumpstarter_driver_nanokvm.client.NanoKVMClient"


def test_nanokvm_mouse_move_abs(mock_nanokvm_client, mock_aiohttp_session):
    """Test mouse absolute movement"""
    hid = NanoKVMHID(host="test.local", username="admin", password="admin")

    with serve(hid) as client:
        # Move mouse to absolute position (normalized 0.0-1.0 coordinates)
        client.mouse_move_abs(0.5, 0.5)

        # Verify the mock was called
        mock_nanokvm_client.mouse_move_abs.assert_called_once_with(0.5, 0.5)


def test_nanokvm_mouse_click(mock_nanokvm_client, mock_aiohttp_session):
    """Test mouse click"""
    from nanokvm.models import MouseButton

    hid = NanoKVMHID(host="test.local", username="admin", password="admin")

    with serve(hid) as client:
        # Click left button
        client.mouse_click("left")

        # Verify the mock was called
        mock_nanokvm_client.mouse_click.assert_called_once_with(MouseButton.LEFT, None, None)


def test_nanokvm_get_images(mock_nanokvm_client, mock_aiohttp_session):
    """Test getting list of available images"""
    driver = NanoKVM(
        host="test.local",
        username="admin",
        password="admin",
    )

    with serve(driver) as client:
        # Get list of images
        images = client.get_images()

        # Verify the result
        assert isinstance(images, list)
        assert len(images) == 2
        assert "/data/alpine-standard-3.23.2-x86_64.iso" in images
        assert "/data/cs10-js.iso" in images

        # Verify the mock was called
        mock_nanokvm_client.get_images.assert_called_once()
