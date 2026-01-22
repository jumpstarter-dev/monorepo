# NanoKVM Driver

`jumpstarter-driver-nanokvm` provides comprehensive support for [NanoKVM](https://github.com/sipeed/NanoKVM) devices thanks to the amazing [python-nanokvm](https://github.com/puddly/python-nanokvm) library, enabling remote KVM (Keyboard, Video, Mouse) control over the network.

## Features

- **Video Streaming**: Access live video feed from the connected device
- **Snapshot Capture**: Take screenshots of the video stream
- **Keyboard Control**: Send text and keystrokes via HID emulation
- **Mouse Control**: Full mouse support via WebSocket
  - Absolute positioning (0.0-1.0 normalized coordinates)
  - Relative movement (0.0-1.0 normalized, where 1.0 = full screen)
  - Left/right/middle button clicks
  - Mouse wheel scrolling
- **Image Management**: Virtual disk and CD-ROM control
  - Mount/unmount disk and CD-ROM images
  - Download images from URLs
  - Check mounted image status
  - Monitor download progress
- **Device Management**: Get device info, reboot the NanoKVM
- **Composite Driver**: Access all functionality through a unified interface

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-nanokvm
```

## Configuration

### Basic Configuration

```yaml
export:
  nanokvm:
    type: jumpstarter_driver_nanokvm.driver.NanoKVM
    config:
      host: "nanokvm.local"  # Hostname or IP address
      username: "admin"      # Default NanoKVM web interface username
      password: "admin"      # Default NanoKVM web interface password
```

### Advanced Configuration

```yaml
export:
  nanokvm:
    type: jumpstarter_driver_nanokvm.driver.NanoKVM
    config:
      host: "192.168.1.100"
      username: "admin"
      password: "your-password"
      # Optional: SSH access for serial console (future feature)
      enable_serial: false
      ssh_username: "root"
      ssh_password: "root"
      ssh_port: 22
```

### Config Parameters

| Parameter      | Description                                | Type  | Required | Default |
| -------------- | ------------------------------------------ | ----- | -------- | ------- |
| host           | NanoKVM hostname or IP address             | str   | yes      |         |
| username       | Web interface username                     | str   | no       | "admin" |
| password       | Web interface password                     | str   | no       | "admin" |
| enable_serial  | Enable serial console access via SSH       | bool  | no       | false   |
| ssh_username   | SSH username for serial console            | str   | no       | "root"  |
| ssh_password   | SSH password for serial console            | str   | no       | "root"  |
| ssh_port       | SSH port for serial console                | int   | no       | 22      |

## Architecture

The NanoKVM driver is a composite driver that provides three main interfaces:

1. **video**: Video streaming and snapshot capture
2. **hid**: Keyboard and mouse HID control
3. **serial**: Serial console access (optional, future feature)

## API Reference

### NanoKVMClient

```{eval-rst}
.. autoclass:: jumpstarter_driver_nanokvm.client.NanoKVMClient()
    :members: get_info, reboot, mount_image, download_image, get_mounted_image, get_cdrom_status, is_image_download_enabled, get_image_download_status
```

### NanoKVMVideoClient

```{eval-rst}
.. autoclass:: jumpstarter_driver_nanokvm.client.NanoKVMVideoClient()
    :members: snapshot
```

### NanoKVMHIDClient

```{eval-rst}
.. autoclass:: jumpstarter_driver_nanokvm.client.NanoKVMHIDClient()
    :members: paste_text, press_key, reset_hid, mouse_move_abs, mouse_move_rel, mouse_click, mouse_scroll
```

## CLI Usage

The NanoKVM driver provides CLI commands accessible through the `jmp shell` command:

### Main Commands

```bash
# Get device information
j nanokvm info

# Reboot the NanoKVM device (with confirmation)
j nanokvm reboot
```

### Video Commands

```bash
# Take a snapshot (saves to snapshot.jpg by default)
j nanokvm video snapshot

# Take a snapshot with custom filename
j nanokvm video snapshot my_screenshot.jpg
```

### HID Commands

#### Keyboard Commands

```bash
# Paste text via keyboard HID
j nanokvm hid paste "Hello, World!"

# Send commands with newline (use $'...' syntax in bash for escape sequences)
j nanokvm hid paste $'root\n'

# Or use double backslash
j nanokvm hid paste "root\\n"

# Send multiple lines
j nanokvm hid paste $'ls -la\ndate\n'

# Press a single key
j nanokvm hid press "a"

# Press special keys
j nanokvm hid press $'\n'    # Enter
j nanokvm hid press $'\t'    # Tab

# Reset HID subsystem if it's not responding
j nanokvm hid reset
```

#### Mouse Commands

```bash
# Move mouse to absolute coordinates (0.0-1.0, where 0.0=top/left, 1.0=bottom/right)
j nanokvm hid mouse move 0.5 0.5     # Center of screen
j nanokvm hid mouse move 0.0 0.0     # Top-left corner
j nanokvm hid mouse move 1.0 1.0     # Bottom-right corner

# Move mouse relatively (-1.0 to 1.0, where 1.0 = full screen width/height)
j nanokvm hid mouse move-rel 0.1 0.1   # Move right and down by 10% of screen
j nanokvm hid mouse move-rel -0.2 0.0  # Move left by 20% of screen width

# Click at current position (default: left button)
j nanokvm hid mouse click

# Click with specific button
j nanokvm hid mouse click --button right

# Click at specific coordinates
j nanokvm hid mouse click --x 0.5 --y 0.5 --button left

# Scroll (default: down 5 units)
j nanokvm hid mouse scroll

# Scroll up
j nanokvm hid mouse scroll --dy 5

# Scroll down
j nanokvm hid mouse scroll --dy -5
```

### Image Management Commands

```bash
# Mount a disk image
j nanokvm image mount /data/myimage.img

# Mount a CD-ROM image
j nanokvm image mount /data/installer.iso --cdrom

# Unmount current image
j nanokvm image unmount

# Check mounted image status
j nanokvm image status

# Check if mounted image is in CD-ROM mode
j nanokvm image cdrom-status

# Download an image from URL
j nanokvm image download https://example.com/image.iso

# Check if image downloads are enabled
j nanokvm image download-enabled

# Check download progress
j nanokvm image download-status
```

### Example Session

```bash
# Connect to the exporter
jmp shell -l my=device

# Inside the shell, use the commands
j nanokvm info
j nanokvm video snapshot my_screen.jpg

# Mount a CD-ROM image
j nanokvm image mount /data/installer.iso --cdrom
j nanokvm image status

# Control the mouse and keyboard
j nanokvm hid mouse move 0.5 0.5
j nanokvm hid mouse click
j nanokvm hid paste "echo 'Hello from NanoKVM'\n"
```

## Usage Examples

### Basic Setup

```python
image = nanokvm.video.snapshot()
image.save("snapshot.jpg")
print(f"Snapshot size: {image.size}")
```

### Keyboard Control

```python
# Paste text to the connected device
nanokvm.hid.paste_text("Hello from Jumpstarter!\n")

# Send commands
nanokvm.hid.paste_text("ls -la\n")

# Press individual keys
nanokvm.hid.press_key("a")
nanokvm.hid.press_key("\n")  # Enter
nanokvm.hid.press_key("\t")  # Tab
```

### Mouse Control

```python
# Move mouse to center of screen (normalized 0.0-1.0 coordinates)
nanokvm.hid.mouse_move_abs(0.5, 0.5)

# Move to top-left corner
nanokvm.hid.mouse_move_abs(0.0, 0.0)

# Move to bottom-right corner
nanokvm.hid.mouse_move_abs(1.0, 1.0)

# Click left button
nanokvm.hid.mouse_click("left")

# Click at specific coordinates
nanokvm.hid.mouse_click("left", x=0.5, y=0.25)

# Move mouse relatively (normalized -1.0 to 1.0, where 1.0 = full screen)
nanokvm.hid.mouse_move_rel(0.1, 0.1)   # Move right/down by 10% of screen
nanokvm.hid.mouse_move_rel(-0.2, 0.0)  # Move left by 20% of screen width

# Scroll up
nanokvm.hid.mouse_scroll(0, 5)

# Scroll down
nanokvm.hid.mouse_scroll(0, -5)
```

### Image Management

```python
# Mount a disk image
nanokvm.mount_image("/data/myimage.img", cdrom=False)

# Mount a CD-ROM image
nanokvm.mount_image("/data/installer.iso", cdrom=True)

# Unmount current image
nanokvm.mount_image("")

# Get mounted image info
file = nanokvm.get_mounted_image()
if file:
    print(f"Mounted: {file}")
    is_cdrom = nanokvm.get_cdrom_status()
    print(f"Mode: {'CD-ROM' if is_cdrom else 'Disk'}")

# Download an image
status = nanokvm.download_image("https://example.com/image.iso")
print(f"Download: {status['status']}, File: {status['file']}")

# Check if downloads are enabled
if nanokvm.is_image_download_enabled():
    print("Downloads are available")

# Monitor download progress
status = nanokvm.get_image_download_status()
print(f"Status: {status['status']}, Progress: {status['percentage']}")
```

### Device Management

```python
# Get device info
info = nanokvm.get_info()
print(f"Device: {info['mdns']}")
print(f"IPs: {info['ips']}")
print(f"Application version: {info['application']}")

# Reset HID
nanokvm.hid.reset_hid()
```


## Character Support for paste_text()

The `paste_text()` method supports a limited character set due to HID keyboard constraints:

- Alphanumeric: `A-Z`, `a-z`, `0-9`
- Punctuation: `` `~!@#$%^&*()-_=+[]{}\|;:'",.<>/? ``
- Whitespace: Tab (`\t`), Newline (`\n`), Space
- Not supported: Extended Unicode, emoji, special control characters


## Related Documentation

- [NanoKVM GitHub](https://github.com/sipeed/NanoKVM)
- [python-nanokvm Library](https://github.com/puddly/python-nanokvm)
- [Jumpstarter Documentation](https://jumpstarter.dev)
