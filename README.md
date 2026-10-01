# TCA9548A Klipper Add-on

Klipper add-on providing TCA9548A I2C multiplexer support. It manages channel
selection and serialized access to devices behind the mux, and provides Klipper
temperature-sensor adapters for AHT1x, AHT2x, AHT3x, BME280, and SHT3X sensors.

The project was originally created for [EMU](https://github.com/DW-Tas/EMU)
deployments where MMB/AFC control boards do not provide enough I2C interfaces
for all required devices. A TCA9548A allows multiple downstream devices to
share one hardware I2C bus while remaining independently addressable by mux
channel.

The mux implementation is kept independent of reader-specific code. PN532
support is integrated by the Happy-Hare-RFID-Reader project, which uses this
repository's mux and I2C infrastructure from its own PN532 adapter.

## Hardware and Wiring

<p align="center">
  <img src="images/TCA9548A.jpg" alt="TCA9548A HW-617 breakout board" width="50%">
</p>

The image shows the commonly available HW-617 TCA9548A breakout board. Connect
its upstream bus pins to the MCU or controller board as follows:

| TCA9548A pin | Connection |
| --- | --- |
| `VIN` | 3.3V or 5V supply |
| `GND` | Controller ground |
| `SDA` | Controller I2C SDA |
| `SCL` | Controller I2C SCL |
| `RST` | Pull up to `VIN`/VCC through a resistor; do not leave floating. An optional open-drain MCU GPIO may pull it low for reset. |
| `A0`, `A1`, `A2` | Leave unconnected for the default I2C address `0x70` (decimal `112`) |

Connect each downstream device's SDA/SCL pair to one matching mux channel:
`SD0`/`SC0` for channel 0 through `SD7`/`SC7` for channel 7. Supply power and
ground to downstream devices separately. Devices with the same I2C address can
be used on different mux channels, because only the selected channel is
connected to the upstream I2C bus.

## Install

Run the following on the Klipper or Kalico host:

```bash
cd ~
git clone https://github.com/jacksky6/TCA9548A-klipper-addon.git
cd TCA9548A-klipper-addon
./install.sh
```

The installer displays the current Git branch, attempts a fast-forward Git
update when the directory is a Git checkout, detects Klipper or Kalico, then
creates these symbolic links in its extras directory:

```text
tca9548a.py
tca9548a_drivers/
```

For upgrade compatibility, the installer removes an existing symbolic link,
regular file, or directory at either of these two exact paths, then recreates
the current symbolic links. This migrates old one-file installations and old
driver directories automatically.

It does not restart Klipper or Kalico; use Fluidd/Mainsail's Restart Klipper
action after installation.

### AHT I2C Recovery Check

During installation, the script checks the target Klipper or Kalico `bus.py`
for the modern `i2c_transfer` protocol with `i2c_bus_status` responses.
It displays the detected firmware path and current Git version. This protocol
was introduced by Klipper commit `8965958a8b6c` on 2026-02-07, which is
reported by `git describe` as `v0.13.0-525-g8965958`; use that version or a
newer matching host and MCU firmware. The result has two distinct levels:

- **Supported host source:** the standalone AHT driver can receive I2C
  `NACK`, `START_NACK`, `START_READ_NACK`, and `BUS_TIMEOUT` statuses in the
  host instead of using Klipper's public I2C helper that turns those statuses
  into a host shutdown. The relevant MCU must still be rebuilt and flashed
  from matching modern firmware. The driver's `i2c_status_supported` status
  field shows the final runtime result for the configured MCU.
- **Legacy host source:** legacy MCU firmware calls its own `shutdown()` for
  these I2C failures before any Python driver can handle them. The add-on is
  compatible with that source, but cannot guarantee that a missing or failed
  AHT sensor will not stop Klipper/Kalico. The installer explains this and
  requests confirmation before continuing.

For a deliberate non-interactive legacy installation, use:

```bash
./install.sh --allow-legacy-i2c
```

This flag only acknowledges the limitation; it does not enable recovery on an
old host or MCU firmware.

## Uninstall

Remove the installed symbolic link with either command:

```bash
./install.sh --uninstall
# Or: ./install.sh -u
```

The repository directory is retained. If you configured Moonraker updates,
manually remove the `[update_manager tca9548a]` section from `moonraker.conf`,
restart Moonraker, then use Fluidd/Mainsail's Restart Klipper action.

## Updates in Fluidd/Mainsail

Fluidd and Mainsail show update status through Moonraker's Update Manager. Add
the following section to `~/printer_data/config/moonraker.conf` after the
initial installation:

```ini
[update_manager tca9548a]
type: git_repo
path: ~/TCA9548A-klipper-addon
origin: https://github.com/jacksky6/TCA9548A-klipper-addon.git
primary_branch: main
install_script: install.sh
is_system_service: False
```

Restart Moonraker after saving the configuration. The update page will then
show this repository and run `install.sh` after each update to keep the
symbolic links in place. The script attempts a fast-forward Git update and
replaces any existing file, directory, or symbolic link at its two add-on
paths before recreating the current symbolic links.
On a legacy I2C target, Moonraker runs non-interactively and installation will
stop at the recovery confirmation. Upgrade Klipper/Kalico and the MCU firmware,
or explicitly acknowledge the limitation in `moonraker.conf`:

```ini
install_script: install.sh --allow-legacy-i2c
```

This configuration intentionally omits
`managed_services`, so updating does not restart Klipper. The
`is_system_service: False` setting also tells Moonraker that `tca9548a` is not a
restartable system service. Use Fluidd/Mainsail's Restart Klipper action when
you are ready to load the updated add-on.

Configure the `[tca9548a]` and `[temperature_sensor]` sections in your own
`printer.cfg` for the sensors, I2C addresses, and mux channels actually
connected to your hardware. Do not copy an example configuration unchanged.

### Happy-Hare RFID PN532 Integration (Planned)

This integration is not implemented yet. When it is added, the TCA9548A mux
core will remain installed as a **separate Klipper Extra** at
`klippy/extras/tca9548a.py`. The Happy-Hare-RFID-Reader PN532 mux adapter
will live only in `nfc_gates/pn532_tca9548a_driver.py` and reference that
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

## Configuration Reference

The following is a reference showing several supported sensor types. Select
only the sections that match your installed hardware, then adjust the mux
settings, channel numbers, I2C addresses, and temperature limits accordingly.

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
`i2c_bus` on the `[tca9548a mux1]` section. That section is the sole source
of `i2c_mcu`, `i2c_bus`, `i2c_speed`, and software-I2C pins for every device
behind the mux. Any of those options in a downstream sensor section are
ignored. The example uses `mmu` and `i2c2_PB10_PB11`.

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

## AHT Communication Recovery

`AHT1X_TCA9548A`, `AHT2X_TCA9548A`, and `AHT3X_TCA9548A` are maintained by the
add-on's standalone `tca9548a_drivers/aht.py`, rather than inheriting the
installed Klipper/Kalico `aht10.py` driver. This lets the add-on handle
recoverable I2C status responses without changing Klipper/Kalico itself.

With a modern host and matching MCU firmware, a failed AHT initialization or
sample does not stop its timer. The driver records the failure, retains the
last valid temperature and humidity, marks the reading invalid, and retries a
full AHT initialization after the shared `environment_report_time`. A
successful retry marks the reading valid again. Repeated identical errors are
rate-limited in `klippy.log`.

The AHT object's status includes `valid`, `communication_ok`, `last_error`,
`last_error_time`, `last_success_time`, `i2c_error_count`, `error_count`, and
`i2c_status_supported`, as well as `tca9548a_channel`. The same fields are
added to the linked `temperature_sensor` status when it is available.

This recovery applies only to I2C transport errors and only to the AHT sensor
types above in the current release. It does not change BME280, SHT3X, PN532, or
other downstream device error semantics. A configured AHT `min_temp` or
`max_temp` violation remains a normal Klipper safety shutdown. An I2C error
does not toggle the TCA9548A `RST` pin, issue a downstream reset, or remove
power from any downstream device.

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
The AHT family is a standalone driver in `tca9548a_drivers/aht.py`; BME280 and
SHT3X currently wrap their installed Klipper/Kalico drivers. All adapters
re-select the TCA9548A channel before every I2C write/read, which avoids
relying on mux state across reactor pauses.

## License

This project is licensed under the GNU General Public License v3.0 or later.
See [LICENSE](LICENSE).
