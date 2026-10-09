# TCA9548A Klipper Add-on

[English](README.md) | [简体中文](README_CN.md)

Klipper add-on providing TCA9548A I2C multiplexer support. It manages channel
selection and serialized access to devices behind the mux, and provides Klipper
temperature-sensor adapters for AHT1x, AHT2x, AHT3x, BME280, and SHT3X sensors.

The project was originally created for [EMU](https://github.com/DW-Tas/EMU)
deployments where MMB/AFC control boards do not provide enough I2C interfaces
for all required devices. A TCA9548A allows multiple downstream devices to
share one hardware I2C bus while remaining independently addressable by mux
channel.

Each supported environment sensor initialization or measurement selects its
channel only for the duration of that complete operation, then disables all
TCA9548A channels. This keeps an idle or disconnected downstream branch
isolated from the shared upstream I2C bus. Future multi-transfer drivers such
as PN532 must hold one `mux.session(close_on_exit=True)` around their complete
command, ACK, and response exchange; they must not open a separate session for
each transfer.

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

### Optional Hardware Reset

Set `reset_pin` in the mux section to let the add-on reset the TCA9548A without
using I2C. `reset_active_high` describes the **MCU GPIO level that requests a
reset**. Do not use Klipper's `!` pin inversion prefix with `reset_pin`; the
add-on rejects it so the configuration has one unambiguous polarity setting.
When `reset_pin` is set and this option is omitted, it defaults to `True` for
the board's N-MOS pull-down reset circuit.

| Hardware connection | `reset_active_high` | Normal MCU output | Reset MCU output |
| --- | --- | --- | --- |
| GPIO directly to TCA `RESET#` | `False` | High | Low |
| GPIO to an N-MOS gate; N-MOS pulls `RESET#` low | `True` | Low | High |

For a TCA9548A powered from 5V, use the N-MOS pull-down arrangement. Connect a
10k resistor from `RESET#` to the TCA's 5V supply, connect the MOS drain to
`RESET#`, source to GND, and gate to the MCU GPIO with a 10k gate pull-down to
GND. This lets the 5V pull-up produce a valid high level while the MCU only
drives the MOS gate. A 3.3V push-pull GPIO connected directly to a 5V
`RESET#` cannot release the pin to 5V and is not recommended.

Default board configuration for the N-MOS arrangement shown above. Remove the
comment marker from both reset lines to enable the hardware reset:

```ini
[tca9548a mux1]
i2c_mcu: EMU_1
i2c_bus: i2c1_PB6_PB7
i2c_address: 112
environment_report_time: 60
# pause_env_on_toolchange: True  # Optional: skip samples during AFC/Happy Hare actions.
# zero_temperature_on_error: False
# zero_humidity_on_error: False

# reset_pin: EMU_1:PC12
# reset_active_high: True
# Board N-MOS circuit: PC12 high pulls TCA RST low; PC12 low runs normally.
# reset_pulse_time: 0.010          # default: 10 ms
# reset_settle_time: 0.010         # default: 10 ms
# reset_recovery_cooldown: 30      # default: 30 s
```

At Klipper startup, MCU restart, and shutdown, the pin is set to its normal
non-reset level. A reset pulse clears the TCA9548A control register and
disconnects all downstream I2C channels; it does **not** remove 3.3V/5V power
from downstream devices.

Use this command to test the wiring after restarting Klipper:

```text
TCA_RESET MUX=mux1
```

On modern I2C firmware the command reads the TCA control register after the
pulse to verify that the hardware reset cleared every channel. On legacy I2C
firmware it sends the hardware pulse without an I2C verification, because an
unsuccessful legacy verification can shut down the MCU firmware.

When `reset_pin` is configured and the modern I2C protocol returns an error
while accessing the TCA control register itself, or the host cannot obtain its
`i2c_response`, the add-on automatically pulses the reset pin, verifies the
all-channels-disabled state, and retries the original TCA control operation
once. A missing response is reported as `NO_RESPONSE`; it means the MCU did
not return the query result, not that it returned an I2C bus status. Automatic
attempts are limited to one per 30 seconds by default. A downstream device
error, such as a sensor
`START_NACK`, does not directly reset the TCA; it remains a sensor retry. If
that fault later
prevents access to the TCA control register, the resulting TCA error triggers
the hardware recovery. If post-reset verification fails, no further TCA
control or downstream I2C transfers are submitted for one
`environment_report_time` interval. This avoids repeatedly submitting timeouts
to a stuck bus.

After five consecutive automatic reset attempts fail verification, the mux
stops all automatic TCA and downstream I2C activity. The Console identifies
the final verification failure and advises checking `RESET#` wiring and TCA
power. A reset pulse may recover an I2C controller stuck by a downstream
fault only while the MCU command channel can still accept the GPIO command. It
cannot restore a disconnected or shutdown MCU. After repairing the fault, run
`TCA_RESET MUX=mux1`; power-cycle the printer if that reset still cannot be
verified. A verified reset or successful TCA control access clears the
consecutive-failure count. A downstream sensor error alone does not count
toward this limit.

The Console reports an automatic attempt as a short red error line, for
example `TCA9548A mux1: BUS_TIMEOUT; hardware reset`, followed by a normal
`reset verified; retrying` line when verification succeeds. A verification
failure instead says that the reset pulse was sent and that I2C is paused; it
does not imply that the GPIO pulse itself failed. The mux status also includes
reset configuration, counts, pulse time, verification result, and recovery
pause state.

## Install

Run the following on the Klipper or Kalico host:

```bash
cd ~
git clone https://github.com/jacksky6/TCA9548A-klipper-addon.git
cd TCA9548A-klipper-addon
./install.sh
```

The installer displays the current Git branch, checks that branch's upstream
for updates with a 10-second timeout, and asks before updating. It then detects
Klipper or Kalico and creates these symbolic links in its extras directory:

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

Use `-b` or `--branch` without a name to list local and `origin` branches, then
select one interactively. Use `-b BRANCH` or `--branch BRANCH` to switch
directly; for example, `./install.sh -b dev`. If a requested branch is not
local, the installer fetches it from `origin` with the same 10-second timeout.
Use `-s` or `--skip-update` to skip the remote update check and install from
the current local files; with `-b BRANCH`, the requested branch must already
exist locally or in the local `origin` cache. In a non-interactive run, an
available update is reported but not installed; the installer continues with
local files.

### I2C Recovery Feature Check

During installation, the script checks the target Klipper or Kalico `bus.py`
for the modern `i2c_transfer` protocol with `i2c_bus_status` responses.
It displays the detected firmware type and current Git version. This protocol
was introduced by Klipper commit `8965958a8b6c` on 2026-02-07, which is
reported by `git describe` as `v0.13.0-525-g8965958`; use that version or a
newer matching host and MCU firmware. The result has two distinct levels:

- **Supported host source:** the add-on's recoverable environment sensor
  drivers can receive I2C
  `NACK`, `START_NACK`, `START_READ_NACK`, and `BUS_TIMEOUT` statuses in the
  host instead of using Klipper's public I2C helper that turns those statuses
  into a host shutdown. A query that receives no `i2c_response` is also
  contained by the add-on and reported as `NO_RESPONSE`; it is not a returned
  I2C bus status. The relevant MCU must still be rebuilt and flashed from
  matching modern firmware. The driver's `i2c_status_supported` status field
  shows the final runtime result for the configured MCU.
- **Legacy host source:** legacy MCU firmware calls its own `shutdown()` for
  these I2C failures before any Python driver can handle them. The add-on is
  compatible with that source, but cannot guarantee that a missing or failed
  environment sensor will not stop Klipper/Kalico. The installer explains this
  and requests confirmation before continuing.

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

The adapter should wrap each complete PN532 command exchange in
`mux.session(close_on_exit=True)`. This retains one selected channel during
the command, ACK, and response transfers, then isolates all downstream
channels when the exchange finishes.

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
i2c_mcu: EMU_1
i2c_bus: i2c1_PB6_PB7
i2c_address: 112 # 0x70, A0/A1/A2 all low; use 113-119 for 0x71-0x77
environment_report_time: 60
# Optional: skip environment samples while AFC or Happy Hare changes filament
# during a print. Detection runs once 30 seconds after Klipper is ready.
# pause_env_on_toolchange: True
# On a failed supported environment sensor transfer, show zero instead of the
# last valid value.
# Set these only here, not in a [temperature_sensor] section.
# zero_temperature_on_error: False
# zero_humidity_on_error: False
# Optional TCA RST control. See "Optional Hardware Reset" above.
# reset_pin: EMU_1:PC12
# reset_active_high: True
# Board N-MOS circuit: PC12 high pulls TCA RST low; PC12 low runs normally.

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
ignored. The example uses `EMU_1` and `i2c1_PB6_PB7`.

`environment_report_time` sets the polling interval, in seconds, for
environment sensors on that mux. It defaults to `60`. TCA9548A AHT sensor
sections intentionally do not support `aht10_report_time`; set the shared
polling interval on the mux so all lanes can be scheduled together.
`BME280_TCA9548A` follows the same rule and does not support per-sensor
`bme280_report_time`. `SHT3X_TCA9548A` also uses the mux interval and does not
support per-sensor `sht3x_report_time`.

`pause_env_on_toolchange` defaults to `False`. When set to `True`, the add-on
waits 30 seconds after Klipper is ready, then detects loaded AFC and Happy Hare
objects once and records the result in `klippy.log`. While a print is active,
an AFC `current_state` or Happy Hare `action` other than `Idle` skips that
environment sensor interval before selecting a TCA channel or submitting I2C.
Normal printing with the multicolor system idle, and all non-print activity,
continue sampling. AFC versions without `current_state`, Happy Hare versions
without `action`, or no detected multicolor system continue sampling.

`zero_temperature_on_error` and `zero_humidity_on_error` are optional Boolean
settings for AHT, BME280, and SHT3X communication failures. Both default to
`False`, which keeps the respective last valid value. Set either option to
`True` to report `0` for only that value after a failure. Set these options only
in the `[tca9548a ...]` mux section; individual `[temperature_sensor ...]`
sections do not support them. A failure-generated zero is not checked against
`min_temp` or `max_temp`.

At Klipper startup, each mux logs its environment scheduler plan. Sensors on the
same mux are spread evenly across `environment_report_time` so their periodic
polls do not all run at the same instant.

## Runtime Commands

All commands use the name from `[tca9548a <name>]`. For example, the commands
below use a mux named `mux1`.

### Environment Sampling

Use these commands around a manually invoked purge or another operation where
environment sensor I2C traffic should be deferred:

```gcode
TCA_PAUSE_ENV_SAMPLING MUX=mux1
# Run the purge or other high-load operation.
TCA_RESUME_ENV_SAMPLING MUX=mux1
```

`TCA_PAUSE_ENV_SAMPLING` sets a manual pause for all supported AHT, BME280, and
SHT3X environment sensors behind that mux. It does not stop or recreate their
timers, change the selected TCA channel, reset the mux, or block other I2C
devices such as a PN532. A scheduled environment sample simply skips its I2C
work while the pause is set.

`TCA_RESUME_ENV_SAMPLING` clears only this manual pause. Sampling resumes on
each sensor's next normally scheduled, staggered interval; it does not force
all sensors to sample at once. If `pause_env_on_toolchange: True` is enabled,
an active AFC or Happy Hare toolchange still keeps sampling paused after the
manual pause is cleared.

### Mux Control and Reset

Use these commands for manual mux checks while the printer is idle and no
downstream device is expected to be communicating:

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_SELECT MUX=mux1
```

`TCA_SELECT` selects one channel from `0` through `7`. Omitting `CHANNEL`
disables every mux channel. `TCA_STATUS` reads the TCA control register and
reports the selected channels and whether the installed I2C interface supports
modern status responses.

With `reset_pin` configured, this sequence tests a hardware reset:

```gcode
TCA_SELECT MUX=mux1 CHANNEL=0
TCA_STATUS MUX=mux1
TCA_RESET MUX=mux1
TCA_STATUS MUX=mux1
```

First confirm that the selected channel is reported, then run the reset. On
modern I2C firmware, `TCA_RESET` reports `reset verified` only after the reset
pulse is sent and a read-back confirms the TCA control register is `0x00`. The
final status command should therefore report `active_channels=none`. This
verifies that the selected channel was cleared by the reset. On legacy I2C
firmware, the command reports
`pulsed (not verified on legacy I2C)` because a failed verification could stop
the MCU; the GPIO pulse is sent, but software cannot safely prove the result.
The reset disconnects downstream I2C channels only. It does not remove power
from downstream devices.

## Environment Sensor I2C Recovery

`AHT1X_TCA9548A`, `AHT2X_TCA9548A`, and `AHT3X_TCA9548A` use the add-on's
standalone `tca9548a_drivers/aht.py` driver. `BME280_TCA9548A` and
`SHT3X_TCA9548A` use the installed Klipper/Kalico setup paths where applicable,
with add-on-controlled sampling, recovery, and the same recoverable mux
transport. No Klipper/Kalico source changes are required.

With a modern host and matching MCU firmware, a failed initialization or sample
from any supported type records the failure, marks the reading invalid, and
retries a full sensor initialization after the shared
`environment_report_time`. A successful retry marks the reading valid again.
Each recoverable environment I2C operation is issued once with host-side retry
disabled. AHT and SHT3X make one measurement attempt per scheduled sample; a
busy AHT response or a failed SHT3X fetch ends that sample without repeating
the command or sending a soft reset. The next normal environment interval
starts a fresh attempt.
After 15 consecutive failures, that sensor stops sampling for the rest of the
Klipper process. It no longer submits I2C traffic or affects other sensors on
the mux. The Console and `klippy.log` identify the final error and advise
checking its wiring and sensor. Repair the fault, then restart Klipper to
resume sampling. By default a failed sensor retains the last valid temperature
and humidity; the two mux-level `zero_*_on_error` options can independently
report zero for either value. BME280 pressure sampling and compensation are
disabled, so it presents the same temperature and humidity fields as AHT.
`klippy.log` records continued failures after the fifth and tenth consecutive
attempt; the fifteenth failure is the final stop notification.
If Klipper is already shutdown, supported environment sensors stop sampling
immediately without submitting I2C traffic. New and queued Console failure
notifications are suppressed; `klippy.log` records the stop once. A
`FIRMWARE_RESTART` creates a new sensor session and resumes normal startup
initialization.
Repeated identical errors are rate-limited in `klippy.log`. The Fluidd/Mainsail
Console shows failure stage, I2C status, and retry interval, for example
`TCA9548A BME280 chamber: measurement failed: START_NACK; retry in 60s`, as
Klipper error lines, so they use the frontend's error color. A later retry may
report `initialization failed` after a measurement failure; it is
reinitializing the sensor before trying another sample. Detailed MCU, address,
operation, and exception information remains in `klippy.log`.

After a failure has been shown in the Console, the first successful retry emits
for example `TCA9548A SHT3X chamber: recovered` as a normal Console line. This
state is kept only for the current Klipper process: a Klipper restart starts a
new sensor session and does not emit a delayed recovery message. A startup
failure that recovers before it is shown in the Console also remains silent.

Each recoverable environment sensor object's status includes `valid`,
`communication_ok`, `last_error`, `last_error_time`, `last_success_time`,
`i2c_error_count`, `error_count`, `consecutive_failure_count`,
`sampling_stopped`, and `i2c_status_supported`, as well as `tca9548a_channel`.
The same fields are added to the linked
`temperature_sensor` status when it is available.

This recovery applies only to I2C transport and malformed-measurement errors
for AHT, BME280, and SHT3X types above in the current release. It does not
change PN532 or other downstream device error semantics. A configured sensor
`min_temp` or `max_temp` violation remains a normal Klipper safety shutdown.
An I2C error does not itself toggle the TCA9548A `RST` pin, issue a downstream
reset, or remove power from any downstream device. With an optional configured
`reset_pin`, a later error while accessing the TCA control register can trigger
the mux hardware recovery described above.

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
AHT2X sensor. After Klipper reaches ready state, use the commands in
[Runtime Commands](#runtime-commands) to test the mux manually.

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
