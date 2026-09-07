# RideSX Driver

`jumpstarter-driver-ridesx` provides functionality for Qualcomm automotive platforms:

- **RideSX** fastboot partition flashing
- **QDL platform flashing** (`QualcommFlasher`) for full firmware/bootloader updates on SA8775P, SA8650P, and related SoCs

RideSX support includes automatic compression handling (`.gz`, `.gzip`, `.xz`), built-in storage
for firmware images with upload/download capabilities, and direct access to the
underlying serial interface for custom commands.

This is mainly tailored towards images that were produced using [automotive-image-builder](https://sigs.centos.org/automotive/latest/getting-started/about-automotive-image-builder.html):

```{code-block} console
automotive-image-builder build --target ridesx4 --export aboot.simg --mode package manifest.aib.yml ridesx.img
```

## Installation

```{code-block} console
:substitutions:
$ pip3 install --extra-index-url {{index_url}} jumpstarter-driver-ridesx
```

The QDL platform flasher (`QualcommFlasher`) is included in this package. The exporter host must provide `qdl` and `fastboot`.

## Configuration

The RideSX driver supports two main components:

### Storage and Flashing Configuration

Example configuration for the RideSX driver:

```yaml
  storage:
    type: "jumpstarter_driver_ridesx.driver.RideSXDriver"
    config:
    children:
      # fastboot management serial port
      serial:
        type: "jumpstarter_driver_pyserial.driver.PySerial"
        config:
          url: "/dev/serial/by-id/usb-QUALCOMM_Inc._Embedded_Power_Measurement__EPM__device_98000205101B0224-if01"
          baudrate: 115200
  power:
    type: "jumpstarter_driver_ridesx.driver.RideSXPowerDriver"
    config:
    children:
      serial:
        type: "jumpstarter_driver_pyserial.driver.PySerial"
        config:
          url: "/dev/serial/by-id/usb-QUALCOMM_Inc._Embedded_Power_Measurement__EPM__device_98000205101B0224-if01"
          baudrate: 115200
  serial:
    type: "jumpstarter_driver_pyserial.driver.PySerial"
    config:
      url: "/dev/serial/by-id/usb-FTDI_Qualcomm_AIR_8775_AI208U7YXA-if01-port01"
      baudrate: 115200

```

### CLI usage

```console
$ jmp shell -l board=qc-ridesx4
# Flash the device using the artifacts from automotive-image-builder, this uses 3 partition file systems
$$ j storage flash --target system_a:rootfs.simg --target system_b:qm_var.simg --target boot_a:aboot.img
$$ j storage erase recoveryinfo
$$ j power on
$$ j serial start-console
```

By default the device is powered off after flashing. Use ``--no-power-off`` to
leave it on.

### Config parameters

#### RideSXDriver

| Parameter   | Description                                           | Type | Required | Default                     |
| ----------- | ----------------------------------------------------- | ---- | -------- | --------------------------- |
| storage_dir | Directory to store firmware images and temporary files | str  | no       | /var/lib/jumpstarter/ridesx |

#### RideSXPowerDriver

The power driver requires a `serial` child instance for communication.

### Required Children

Both drivers require:

| Child  | Description                                                  | Required |
| ------ | ------------------------------------------------------------ | -------- |
| serial | PySerial driver instance for communicating with the device  | yes      |

## Usage

### Flash Single Partition

```{code-block} python
# Flash a single partition (paths must exist; flash runs fastboot on the exporter)
ridesx_client.flash("/path/to/boot.img", target="boot")
```

### Flash Multiple Partitions

```{code-block} python
# Flash multiple partitions
partitions = {
    "boot": "/path/to/boot.img",
    "system": "/path/to/system.img",
    "userdata": "/path/to/userdata.img"
}
ridesx_client.flash(partitions)
```

### Flash with Compressed Images

The driver automatically handles compressed images (`.gz`, `.gzip`, `.xz`):

```{code-block} python
# Flash compressed images - decompression is automatic
ridesx_client.flash("/path/to/boot.img.gz", target="boot")
```

### Erase Partition

```{code-block} python
# Erase a partition (boots to fastboot, erases, leaves device in fastboot)
ridesx_client.erase_partition("recoveryinfo")
```

### Power Control

```{code-block} python
# Turn device power on
power_client.on()

# Turn device power off
power_client.off()

# Power cycle the device
power_client.cycle(wait=5)  # Wait 5 seconds between off/on
```

## API Reference

### RideSXClient

```{eval-rst}
.. autoclass:: jumpstarter_driver_ridesx.client.RideSXClient()
    :members: flash, flash_images, erase_partition, boot_to_fastboot, cli
```

### RideSXPowerClient

```{eval-rst}
.. autoclass:: jumpstarter_driver_ridesx.client.RideSXPowerClient()
    :members: on, off, cycle, rescue, serial
```

## QDL platform flashing (`QualcommFlasher`)

Manifest-driven QDL/fastboot flashing for vendor firmware packages (ES13, ES21, ES22, CS4, CS5, …).
See `examples/exporter-platform.yaml` and reference manifests in
`jumpstarter_driver_ridesx/qdl/examples/manifests/`.

**driver**: `jumpstarter_driver_ridesx.qdl.driver.QualcommFlasher`

TAC serial handles power on/off and mode switching (EDL/fastboot). Export as `firmware` with
`tac`, `serial`, and `sail` children for identification.

### Example exporter configuration

```yaml
apiVersion: jumpstarter.dev/v1alpha1
kind: ExporterConfig
metadata:
  namespace: jumpstarter-lab
  name: qualcomm-sa8775p
endpoint:
token:
export:
  firmware:
    type: "jumpstarter_driver_ridesx.qdl.driver.QualcommFlasher"
    config:
      soc_type: sa8775p
      work_dir: /var/lib/jumpstarter/qualcomm
      board_revision: v3
      power_cycle_delay: 2.0
    children:
      tac:
        ref: tac
      serial:
        ref: serial
      sail:
        ref: sail
  tac:
    type: "jumpstarter_driver_pyserial.driver.PySerial"
    config:
      url: "/dev/ttyACM0"
      baudrate: 115200
  serial:
    type: "jumpstarter_driver_pyserial.driver.PySerial"
    config:
      url: "/dev/ttyUSB1"
      baudrate: 115200
  sail:
    type: "jumpstarter_driver_pyserial.driver.PySerial"
    config:
      url: "/dev/ttyUSB2"
      baudrate: 115200
```

### Config parameters

| Parameter            | Description                                          | Type  | Required | Default                        |
| -------------------- | ---------------------------------------------------- | ----- | -------- | ------------------------------ |
| soc_type             | SoC profile (`sa8775p`, `sa8540p1`, `sa8540p2`)      | str   | no       | sa8775p                        |
| work_dir             | Base directory for firmware extraction                | str   | no       | /var/lib/jumpstarter/qualcomm  |
| board_revision       | Board revision for CDT image selection (`v1`–`v4`)   | str   | no       |                                |
| qdl_timeout          | Timeout for QDL subprocess steps (seconds)           | int   | no       | 1800                           |
| fastboot_timeout     | Timeout for fastboot subprocess steps (seconds)      | int   | no       | 600                            |
| power_cycle_delay    | Delay between power off/on (seconds)                 | float | no       | 2.0                            |
| tac_command_timeout  | Timeout for TAC command acknowledgement (seconds)    | float | no       | 10.0                           |

### Required children

| Child  | Description                            | Required for flash | Required for `id` |
| ------ | -------------------------------------- | ------------------ | ----------------- |
| tac    | TAC serial for power and mode control  | Yes                | Yes               |
| serial | Main boot serial console               | No                 | Yes               |
| sail   | SAIL boot serial console               | No                 | Yes               |

### CLI

Both the firmware archive and `--manifest` accept local paths or `http://` / `https://` URLs.
Firmware URLs are downloaded on the exporter. Manifest URLs are fetched by the client.

```bash
# Flash firmware (manifest auto-discovered from archive)
j firmware flash https://example.com/firmware/sx4-r00021.1a.tar.xz

# Flash with explicit manifest
j firmware flash https://example.com/firmware/sx4-r00021.1a.tar.xz --manifest ./es22.yaml

# Cache firmware on the exporter for faster re-flashing
j firmware flash ./sx4-r00021.1a.tar.xz --cached

# Force re-download when cache is incomplete or corrupted
j firmware flash ./sx4-r00021.1a.tar.xz --cached --force-download

# Identify running firmware
j firmware id -v

# Check firmware matches expected variant
j firmware check ES22 --hypervisor prod --sail-fw-version 1.3.0

# Boot into specific modes
j firmware boot-to-edl
j firmware boot-to-fastboot
```

Use `--cached` to keep extracted firmware on the exporter and reuse it on subsequent
flashes. Each source URL gets its own cache directory (namespaced by a hash of the URL)
under `work_dir/`, so different firmware archives never overwrite each other.

If a download is interrupted, the cache may be left in an incomplete state. Use
`--force-download` with `--cached` to clear the existing cache and re-download.

Archives may embed `jumpstarter_manifest.yaml`. See `examples/exporter-platform.yaml` for
exporter configuration and the QDL module for manifest schema details.

### Board revision and CDT flash

Fastboot flash operations in the manifest can include a `revision` field to conditionally
flash based on the board hardware revision. The board revision is set via the
`board_revision` field in the exporter driver config.

When a flash operation has `revision` set and no board revision is configured, the
flash command fails with an error.

### Manifest example

ABL and CDT flashing are regular fastboot steps in the manifest, giving full control over
ordering, retries, and device mode switching:

```yaml
name: "SA8650P CS4 Firmware"
data:
  folder: "r00010.1"
steps:
  - set_mode: edl
    check_dmesg: "qcserial"
  - sleep: 5
  - name: "UFS provisioning"
    retry_mode: edl
    qdl:
      storage: ufs
      programmer: prog_firehose_ddr.elf
      files:
        - provision_default.xml
  - sleep: 10
  - set_mode: edl
    check_dmesg: "qcserial"
  - name: "Flash UFS"
    retry_mode: edl
    qdl:
      storage: ufs
      programmer: prog_firehose_ddr.elf
      files:
        - "rawprogram*.xml"
        - "patch*.xml"
  - sleep: 30
  - set_mode: fastboot
    check_dmesg: "Product: Android"
  - name: "Flash ABL"
    fastboot:
      flash:
        - partition: abl_a
          file: qam8650p_abl_signed.elf
        - partition: abl_b
          file: qam8650p_abl_signed.elf
  - name: "Flash CDT"
    fastboot:
      flash:
        - partition: cdt
          file: ufs/LEMANSAU_QAM_1.1.0.bin
          revision: v1
        - partition: cdt
          file: ufs/LEMANSAU_QAM_1.1.0.bin
          revision: v2
        - partition: cdt
          file: ufs/LEMANSAU_QAM_1.2.0.bin
          revision: v3
        - partition: cdt
          file: ufs/LEMANSAU_QAM_1.2.0.bin
          revision: v4
      continue: true
```

The `revision` field on flash operations filters by board revision — only the matching
entry is flashed, the rest are skipped. The `continue: true` on the last fastboot step
tells the device to boot after flashing.

### Requirements on exporter host

- `qdl` (Qualcomm download tool)
- `fastboot`
- USB access to the DUT in EDL/fastboot modes
- TAC serial device for mode switching
