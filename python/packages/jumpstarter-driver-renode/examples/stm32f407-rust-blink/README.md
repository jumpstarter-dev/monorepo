# STM32F407 Rust blink (Renode)

This folder holds a **Jumpstarter exporter** plus a **prebuilt firmware** so you can run an STM32F407 blink demo inside **Renode** and drive it from the shell.

## Files

| File | Role |
|------|------|
| `exporter-stm32f4.yaml` | Exporter config: Renode platform (`stm32f4_discovery-kit.repl`), UART on `sysbus.usart2`, and the composite device label `ecu`. |
| `.cargo/config.toml` | Rust embedded target (`thumbv7em-none-eabihf`) and link script flags when you build firmware for this board. |
| `Cargo.toml`, `src/main.rs` | Firmware: toggles the Discovery user LED on **PD12**, prints **`ON` / `OFF`** on **USART2** (PA2 @ 115200 8N1), matching Renode’s `UserLED` and `usart2`. |
| `memory.x`, `build.rs` | Linker memory map for the STM32F407 and `cortex-m-rt` (`link.x`). |
| `rust-toolchain.toml` | Pins **stable** and the **`thumbv7em-none-eabihf`** target via rustup. |
| `stm32f407-rust-blink.elf` | Optional copy of the release ELF for flashing; the canonical build artifact is under `target/…/release/`. |
| `target/` | Rust build output (if you build here). |

## Build the firmware

Install the [rustup](https://rustup.rs/) toolchain and the embedded target (`rust-toolchain.toml` requests it). From this directory:

```bash
PATH="$HOME/.cargo/bin:$PATH" cargo build --release
```

The ELF is `target/thumbv7em-none-eabihf/release/stm32f407-rust-blink`. Copy it if you want a stable name for Jumpstarter:

```bash
cp target/thumbv7em-none-eabihf/release/stm32f407-rust-blink stm32f407-rust-blink.elf
```

## Workflow

1. **Start a Jumpstarter shell** wired to this exporter (from this directory):

   ```bash
   jmp shell --exporter-config exporter-stm32f4.yaml
   ```

2. Inside that shell, `j` is the client for the exported **composite device**. The Renode stack is grouped under **`j ecu`** (not a top-level `j serial`).

3. **Flash** the firmware:

   ```bash
   j ecu flasher flash stm32f407-rust-blink.elf
   ```

4. **Turn simulation on** and **open the serial console** (USART2 in this config):

   ```bash
   j ecu power on && j ecu console start-console
   ```

   To reset the simulation and attach the console again:

   ```bash
   j ecu power cycle && j ecu console start-console
   ```

5. **Leave the console**: press **Ctrl+B three times** (as printed when the console starts).

## `j ecu` commands (reference)

Under `j ecu` you typically use:

- **`flasher`** — load an ELF into the target.
- **`power`** — `on`, `off`, `cycle`, etc. (starts/controls the Renode simulation for this exporter).
- **`console`** — serial client (e.g. `start-console`) to the UART defined in the YAML.
- **`monitor`** — send commands to the Renode monitor.

Run `j ecu --help` for the full list.

## Requirements

- **Renode** installed and on `PATH` (the driver spawns it for power/simulation).
- Jumpstarter CLI (`jmp`) and the **Renode driver** available to the exporter.
