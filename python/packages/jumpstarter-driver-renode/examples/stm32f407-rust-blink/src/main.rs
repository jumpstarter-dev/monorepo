//! Blinks the STM32F407 Discovery user LED (PD12) and prints `ON` / `OFF` on USART2.
//!
//! Matches Renode `platforms/boards/stm32f4_discovery-kit.repl`: USART2 (`sysbus.usart2`),
//! green LED on `gpioPortD` pin 12. Console is typically wired to PA2 (TX) at 115200 8N1.

#![no_std]
#![no_main]

use core::fmt::Write;

use cortex_m_rt::entry;
use panic_halt as _;
use stm32f4xx_hal::rcc::Config;
use stm32f4xx_hal::{pac, prelude::*};

#[entry]
fn main() -> ! {
    let dp = pac::Peripherals::take().unwrap();

    // STM32F407G-DISC1: 8 MHz HSE
    let mut rcc = dp.RCC.freeze(Config::hse(8.MHz()).sysclk(168.MHz()));

    let gpioa = dp.GPIOA.split(&mut rcc);
    let gpiod = dp.GPIOD.split(&mut rcc);

    let mut tx = dp
        .USART2
        .tx(gpioa.pa2, 115200.bps(), &mut rcc)
        .expect("USART2");

    let mut led = gpiod.pd12.into_push_pull_output();

    let mut delay = dp.TIM5.delay_ms(&mut rcc);

    loop {
        led.set_high();
        writeln!(tx, "ON").ok();

        delay.delay(200.millis());

        led.set_low();
        writeln!(tx, "OFF").ok();

        delay.delay(200.millis());
    }
}
