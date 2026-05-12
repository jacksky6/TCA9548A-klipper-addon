# TCA9548A Klipper Add-on

Experimental Klipper add-on for testing AHT2X sensors behind a TCA9548A I2C
multiplexer.

The first goal is a small feasibility test: keep Klipper changes minimal, add a
new `AHT2X_TCA9548A` temperature sensor type, and select the mux channel before
each AHT2X I2C operation.

## Install

Copy `tca9548a.py` into Klipper's `klippy/extras/` directory:

```text
~/klipper/klippy/extras/tca9548a.py
```

Then add a config like `tca9548a_aht2x_example.cfg` to `printer.cfg`.

## Example

```ini
[tca9548a mux1]
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 112

[temperature_sensor Lane_0]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 0
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_1]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 1
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_2]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 2
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_3]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 3
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_4]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 4
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 56
min_temp: -20
max_temp: 80
```

The `[tca9548a mux1]` section both defines the mux and loads the plugin, so a
separate `[tca9548a]` section is not required. Put the mux section before any
`[temperature_sensor]` section that uses `sensor_type: AHT2X_TCA9548A`.

Use the Klipper MCU name and I2C bus name for your board in `i2c_mcu` and
`i2c_bus`. The example uses `mmu` and `i2c2_PB10_PB11`. If your TCA9548A has
A0/A1/A2 pulled high or low differently, adjust the mux `i2c_address` from the
default `112` (`0x70`). Klipper expects I2C addresses in decimal, so AHT20's
`0x38` address is written as `56`.

## Notes

This prototype deliberately supports only AHT2X sensors. It wraps the existing
Klipper AHT2X driver and re-selects the TCA9548A channel before every I2C
write/read, which avoids relying on mux state across reactor pauses.
