# TCA9548A Klipper Add-on

[中文文档](README.zh-CN.md)

Experimental Klipper add-on for testing AHT sensors behind a TCA9548A I2C
multiplexer.

The first goal is a small feasibility test: keep Klipper changes minimal, add a
small set of AHT temperature sensor types, and select the mux channel before
each AHT I2C operation.

## Install

Copy `tca9548a.py` into Klipper's `klippy/extras/` directory:

```text
~/klipper/klippy/extras/tca9548a.py
```

Then add a config like `tca9548a_aht2x_example.cfg` to `printer.cfg`.

### Happy-Hare RFID PN532 Integration

The TCA9548A mux core is always installed as a **separate Klipper Extra** at
`klippy/extras/tca9548a.py`. The Happy-Hare-RFID-Reader PN532 mux adapter
lives only in `nfc_gates/pn532_tca9548a_driver.py` and references that
installed mux core.

The RFID project and its installer must not bundle, copy, download, or
overwrite `tca9548a.py`. Some users may already have this add-on installed.
The NFC mux configuration should detect the separate installation and tell the
user to install it when absent; when present, it must retain the existing file
and version. Generic TCA9548A behavior is maintained only in this repository.

Supported sensor types:

```text
AHT1X_TCA9548A
AHT2X_TCA9548A
AHT3X_TCA9548A
BME280_TCA9548A
SHT3X_TCA9548A
```

## Example

```ini
[tca9548a mux1]
i2c_mcu: mmu
i2c_bus: i2c2_PB10_PB11
i2c_address: 112 # 0x70, A0/A1/A2 all low; use 113-119 for 0x71-0x77
environment_report_time: 30

[temperature_sensor Lane_0]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 0
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_1]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 1
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_2]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 2
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_3]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 3
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Lane_4]
sensor_type: AHT2X_TCA9548A
tca9548a: mux1
tca9548a_channel: 4
i2c_address: 56
min_temp: -20
max_temp: 80

[temperature_sensor Chamber_BME]
sensor_type: BME280_TCA9548A
tca9548a: mux1
tca9548a_channel: 5
i2c_address: 118
min_temp: -20
max_temp: 80

[temperature_sensor Chamber_SHT]
sensor_type: SHT3X_TCA9548A
tca9548a: mux1
tca9548a_channel: 6
i2c_address: 68
min_temp: -20
max_temp: 80
```

The `[tca9548a mux1]` section both defines the mux and loads the plugin, so a
separate `[tca9548a]` section is not required. Put the mux section before any
`[temperature_sensor]` section that uses an `*_TCA9548A` sensor type.

Use the Klipper MCU name and I2C bus name for your board in `i2c_mcu` and
`i2c_bus` on the `[tca9548a mux1]` section. Sensors using that mux inherit
`i2c_mcu`, `i2c_bus`, and `i2c_speed` from the mux unless they override those
options. The example uses `mmu` and `i2c2_PB10_PB11`.

`environment_report_time` sets the polling interval, in seconds, for
environment sensors on that mux. It defaults to `30`. TCA9548A AHT sensor
sections intentionally do not support `aht10_report_time`; set the shared
polling interval on the mux so all lanes can be scheduled together.
`BME280_TCA9548A` follows the same rule and does not support per-sensor
`bme280_report_time`. `SHT3X_TCA9548A` also uses the mux interval and does not
support per-sensor `sht3x_report_time`.

At Klipper startup, each mux logs its environment scheduler plan. Sensors on the
same mux are spread evenly across `environment_report_time` so their periodic
polls do not all run at the same instant.

If your TCA9548A has A0/A1/A2 pulled high or low differently, adjust the mux
`i2c_address` from the default `112` (`0x70`). Klipper expects I2C addresses in
decimal. AHT20's `0x38` address is written as `56` in each sensor section.
SHT3X's default `0x44` address is written as `68`.

## Debug

For mux-only testing, enable:

```ini
[tca9548a mux1]
debug_no_disable: True
# select_delay: 0.01
# verify_select: True

[temperature_sensor Lane_0]
debug_skip_init: True
```

This prevents startup from automatically writing to the mux or initializing the
AHT2X sensor. After Klipper reaches ready state, manually test the mux with:

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_SELECT MUX=mux1
```

The first command selects channel 0. `TCA_STATUS` reads back the TCA9548A
control register. The last command disables all channels.

`select_delay` waits after each mux write. It defaults to `0`, because the
TCA9548A normally does not need a command processing delay. `verify_select`
reads the control register after each write and only reports success if the
read-back byte matches the requested channel mask.

`debug_no_disable` defaults to `False`. When it is `False`, the plugin writes
`0x00` to the TCA9548A during Klipper startup to disable all mux channels before
normal sensor initialization. Setting it to `True` skips that startup disable
write, which is useful only when debugging mux hardware state.

`debug_skip_init` defaults to `False`. On AHT mux sensors, setting it to `True`
skips the sensor initialization sequence so Klipper can reach ready state while
you test the mux manually. Normal configurations should leave both debug options
unset.

## Notes

This prototype deliberately supports only a narrow set of environment sensors.
It wraps existing Klipper sensor drivers and re-selects the TCA9548A channel
before every I2C write/read, which avoids relying on mux state across reactor
pauses.
