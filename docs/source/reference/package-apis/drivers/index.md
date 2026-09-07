# Drivers

This section documents the drivers from the Jumpstarter packages directory. Each
driver is contained in a separate package in the form of
`jumpstarter-driver-{name}` and provides specific functionality for interacting
with different hardware components and systems.

## Types of Drivers

Jumpstarter includes several types of drivers organized by their primary
function:

### System Control

Drivers that control the power state and basic operation of devices:

- {doc}`Power <power>` (`jumpstarter-driver-power`) - Power control for devices
- {doc}`gpiod <gpiod>` (`jumpstarter-driver-gpiod`) - GPIO hardware control via libgpiod
- {doc}`Yepkit <yepkit>` (`jumpstarter-driver-yepkit`) - Yepkit USB hub hardware control
- {doc}`DUT Link <dutlink>` (`jumpstarter-driver-dutlink`) - [DUT Link Board](https://github.com/jumpstarter-dev/dutlink-board) hardware control
- {doc}`Energenie PDU <energenie>` (`jumpstarter-driver-energenie`) - Energenie PDU control
- {doc}`Tasmota <tasmota>` (`jumpstarter-driver-tasmota`) - Tasmota device control
- {doc}`HTTP Power <http-power>` (`jumpstarter-driver-http-power`) - HTTP-based power control for smart sockets
- {doc}`Noyito Relay <noyito-relay>` (`jumpstarter-driver-noyito-relay`) - NOYITO USB relay board control

### Communication

Drivers that provide various communication interfaces:

- {doc}`ADB <adb>` (`jumpstarter-driver-adb`) - Android Debug Bridge tunneling
- {doc}`BLE <ble>` (`jumpstarter-driver-ble`) - Bluetooth Low Energy communication
- {doc}`BT Peer <bt-peer>` (`jumpstarter-driver-bt-peer`) - Bluetooth peer device powered by bumble
- {doc}`CAN <can>` (`jumpstarter-driver-can`) - Controller Area Network communication
- {doc}`HTTP <http>` (`jumpstarter-driver-http`) - HTTP communication
- {doc}`mitmproxy <mitmproxy>` (`jumpstarter-driver-mitmproxy`) - HTTP/HTTPS interception, mocking, and traffic recording
- {doc}`DUT Network <dut-network>` (`jumpstarter-driver-dut-network`) - DUT network isolation with bridge, DHCP, DNS, and NAT
- {doc}`Network <network>` (`jumpstarter-driver-network`) - Network interfaces and configuration
- {doc}`PySerial <pyserial>` (`jumpstarter-driver-pyserial`) - Serial port communication
- {doc}`SNMP <snmp>` (`jumpstarter-driver-snmp`) - Simple Network Management Protocol
- {doc}`SSH <ssh>` (`jumpstarter-driver-ssh`) - SSH wrapper driver
- {doc}`SSH MITM <ssh-mitm>` (`jumpstarter-driver-ssh-mitm`) - SSH proxy with server-side private key storage
- {doc}`TFTP <tftp>` (`jumpstarter-driver-tftp`) - Trivial File Transfer Protocol
- {doc}`VNC <vnc>` (`jumpstarter-driver-vnc`) - Virtual Network Computing remote desktop
- {doc}`XCP <xcp>` (`jumpstarter-driver-xcp`) - Universal Measurement and Calibration Protocol

### Storage and Data

Drivers that control storage devices and manage data:

- {doc}`OpenDAL <opendal>` (`jumpstarter-driver-opendal`) - Open Data Access Layer
- {doc}`SD Wire <sdwire>` (`jumpstarter-driver-sdwire`) - SD card switching
- {doc}`iSCSI <iscsi>` (`jumpstarter-driver-iscsi`) - iSCSI target server for LUN export

### Media

Drivers that handle media streams:

- {doc}`uStreamer <ustreamer>` (`jumpstarter-driver-ustreamer`) - Video streaming
- {doc}`Video <video>` (`jumpstarter-driver-video`) - Video interface and HTTP/MJPEG camera sources

### Automotive Diagnostics

Drivers for automotive diagnostic protocols:

- {doc}`DoIP <doip>` (`jumpstarter-driver-doip`) - Diagnostics over Internet Protocol (ISO 13400)
- {doc}`UDS <uds>` (`jumpstarter-driver-uds`) - Unified Diagnostic Services (ISO 14229)
- {doc}`UDS over DoIP <uds-doip>` (`jumpstarter-driver-uds-doip`) - UDS diagnostics over DoIP transport
- {doc}`UDS over CAN <uds-can>` (`jumpstarter-driver-uds-can`) - UDS diagnostics over CAN/ISO-TP transport
- {doc}`OBD-II <obd>` (`jumpstarter-driver-obd`) - OBD-II vehicle diagnostics via ELM327
- {doc}`SOME/IP <someip>` (`jumpstarter-driver-someip`) - SOME/IP protocol operations via opensomeip

### Flashing and Programming

Drivers for flashing firmware and programming devices:

- {doc}`ESP32 <esp32>` (`jumpstarter-driver-esp32`) - ESP32 flashing via esptool
- {doc}`Flashers <flashers>` (`jumpstarter-driver-flashers`) - Flash memory programming tools
- {doc}`Pi Pico <pi-pico>` (`jumpstarter-driver-pi-pico`) - Raspberry Pi Pico UF2 flashing via BOOTSEL
- {doc}`Probe-RS <probe-rs>` (`jumpstarter-driver-probe-rs`) - Debug probe support
- {doc}`ST-LINK MSD <stlink-msd>` (`jumpstarter-driver-stlink-msd`) - ST-LINK mass storage flasher for STM32
- {doc}`U-Boot <uboot>` (`jumpstarter-driver-uboot`) - Universal Bootloader interface
- {doc}`RideSX <ridesx>` (`jumpstarter-driver-ridesx`) - RideSX fastboot flashing, QDL platform updates, and power management for Qualcomm automotive SoCs

### Emulation

Drivers for virtual and emulated targets:

- {doc}`Android Emulator <androidemulator>` (`jumpstarter-driver-androidemulator`) - Android emulator lifecycle management with ADB tunneling
- {doc}`QEMU <qemu>` (`jumpstarter-driver-qemu`) - QEMU virtual machine management
- {doc}`Renode <renode>` (`jumpstarter-driver-renode`) - Renode embedded systems emulation
- {doc}`Corellium <corellium>` (`jumpstarter-driver-corellium`) - Corellium virtualization platform
- {doc}`Cuttlefish <cuttlefish>` (`jumpstarter-driver-cuttlefish`) - Android Cuttlefish virtual device management
- {doc}`Netsim <netsim>` (`jumpstarter-driver-netsim`) - Android netsim virtual radio control (Bluetooth, WiFi, UWB)

### Utility

General-purpose utility drivers:

- {doc}`Shell <shell>` (`jumpstarter-driver-shell`) - Shell command execution
- {doc}`SSH Mount <ssh-mount>` (`jumpstarter-driver-ssh-mount`) - SSHFS remote filesystem mounting
- {doc}`TMT <tmt>` (`jumpstarter-driver-tmt`) - Test Management Tool wrapper

```{toctree}
:hidden:
adb.md
androidemulator.md
ble.md
bt-peer.md
can.md
corellium.md
cuttlefish.md
doip.md
dut-network.md
dutlink.md
energenie.md
esp32.md
flashers.md
gpiod.md
http.md
http-power.md
iscsi.md
mitmproxy.md
netsim.md
network.md
noyito-relay.md
obd.md
opendal.md
pi-pico.md
power.md
probe-rs.md
pyserial.md
qemu.md
renode.md
ridesx.md
sdwire.md
shell.md
snmp.md
someip.md
ssh.md
ssh-mount.md
ssh-mitm.md
stlink-msd.md
tasmota.md
tftp.md
tmt.md
uboot.md
uds.md
uds-can.md
uds-doip.md
ustreamer.md
video.md
vnc.md
xcp.md
yepkit.md
```
