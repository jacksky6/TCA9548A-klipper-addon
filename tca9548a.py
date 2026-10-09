"""TCA9548A I2C multiplexer support for Klipper.

This module selects TCA9548A channels and provides MuxedI2C, an I2C wrapper
for devices behind the mux. Sensor implementations live in the
tca9548a_drivers package.

Typical temperature-sensor configuration::

    [tca9548a mux0]
    i2c_mcu: EMU_1
    i2c_bus: i2c1_PB6_PB7
    i2c_address: 112              # 0x70
    environment_report_time: 60
    # zero_temperature_on_error: False
    # zero_humidity_on_error: False
    # Optional hardware reset control:
    # reset_pin: EMU_1:PC12
    # reset_active_high: True      # True for an N-MOS pull-down circuit
    # Board N-MOS circuit: PC12 high pulls TCA RST low; PC12 low runs normally.

    [temperature_sensor chamber]
    sensor_type: AHT2X_TCA9548A
    # Also: AHT1X_TCA9548A, AHT3X_TCA9548A, BME280_TCA9548A,
    #       SHT3X_TCA9548A
    tca9548a: mux0
    tca9548a_channel: 0
    i2c_address: 56               # 0x38 for AHT2x

The mux section is the only source of i2c_mcu, i2c_bus, i2c_speed, and
software-I2C pin settings. Downstream settings for those options are ignored.

The mux core is deliberately reader-agnostic.  PN532 support belongs in the
Happy-Hare-RFID-Reader addon, where pn532_tca9548a_driver.py wraps a PN532
driver with MuxedI2C and holds a mux session for each complete PN532 operation.
"""

# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from contextlib import contextmanager
import logging
import greenlet
from . import bus

TCA9548A_I2C_ADDR = 0x70
DEFAULT_RESET_PULSE_TIME = .010
DEFAULT_RESET_SETTLE_TIME = .010
DEFAULT_RESET_RECOVERY_COOLDOWN = 30.
# Automatic mux reset stops after one failure group. Environment sensors use
# three groups before stopping their own sampling.
RECOVERY_FAILURE_GROUP_SIZE = 5
MAX_CONSECUTIVE_AUTO_RESET_FAILURES = RECOVERY_FAILURE_GROUP_SIZE
INHERITED_I2C_OPTIONS = set([
    "i2c_mcu", "i2c_bus", "i2c_speed",
    "i2c_software_scl_pin", "i2c_software_sda_pin",
])
INHERITED_OPTION_ALIASES = {
    "aht10_report_time": "environment_report_time",
}


def _has_option(config, option):
    return config.fileconfig.has_option(config.get_name(), option)


class I2CStatusError(Exception):
    """An I2C failure reported by the modern i2c_transfer protocol."""

    def __init__(self, i2c, status, operation, write_len, read_len):
        self.status = status
        self.operation = operation
        self.write_len = write_len
        self.read_len = read_len
        self.i2c_address = i2c.get_i2c_address()
        self.mcu_name = i2c.get_mcu().get_name()
        Exception.__init__(
            self, "MCU '%s' I2C request to addr %i reports error %s "
            "during %s" % (self.mcu_name, self.i2c_address, status,
                             operation))


class I2CResponseError(I2CStatusError):
    """The MCU accepted an I2C query but did not return its response."""

    def __init__(self, i2c, operation, write_len, read_len, cause):
        self.cause = cause
        self.status = "NO_RESPONSE"
        self.operation = operation
        self.write_len = write_len
        self.read_len = read_len
        self.i2c_address = i2c.get_i2c_address()
        self.mcu_name = i2c.get_mcu().get_name()
        Exception.__init__(
            self, "MCU '%s' did not return i2c_response during %s: %s" % (
                self.mcu_name, operation, cause))


class MuxSelectionError(Exception):
    """A downstream operation could not select its TCA9548A channel."""

    def __init__(self, mux_name, channel):
        self.mux_name = mux_name
        self.channel = channel
        Exception.__init__(
            self, "TCA9548A '%s' could not select channel %d" % (
                mux_name, channel))


def i2c_status_supported(i2c):
    """Return whether an MCU I2C object reports transfer status to the host."""
    command = getattr(i2c, "i2c_transfer_cmd", None)
    if command is None:
        return False
    # File output replaces the query command with a write-only command, so it
    # cannot provide a status response.
    return not i2c.get_mcu().is_fileoutput()


def _legacy_i2c_write(i2c, data, minclock, reqclock, retry):
    try:
        return i2c.i2c_write(data, minclock=minclock, reqclock=reqclock,
                             retry=retry)
    except TypeError as exc:
        # Older Klipper releases do not expose retry on i2c_write().
        if "unexpected keyword argument 'retry'" not in str(exc):
            raise
        return i2c.i2c_write(data, minclock=minclock, reqclock=reqclock)


def i2c_transfer_recoverable(i2c, write, read_len=0, minclock=0,
                             reqclock=0, retry=True, operation="transfer"):
    """Perform one transfer without bus.py escalating a reported error.

    Modern MCU firmware returns i2c_bus_status in i2c_response. Klipper's
    public MCU_I2C methods convert a non-success status into shutdown, so
    callers that can recover must issue the query command directly. Legacy
    firmware has no recoverable status path and retains its native behavior.
    """
    write = list(write)
    if not i2c_status_supported(i2c):
        if read_len:
            return i2c.i2c_read(write, read_len, retry=retry)
        _legacy_i2c_write(i2c, write, minclock, reqclock, retry)
        return None

    try:
        params = i2c.i2c_transfer_cmd.send(
            [i2c.get_oid(), write, read_len], minclock=minclock,
            reqclock=reqclock, retry=retry)
    except Exception as exc:
        if "Unable to obtain 'i2c_response' response" not in str(exc):
            raise
        raise I2CResponseError(i2c, operation, len(write), read_len, exc)
    if params is None:
        raise I2CStatusError(i2c, "MALFORMED_RESPONSE", operation,
                             len(write), read_len)
    status = params.get("i2c_bus_status", "MALFORMED_RESPONSE")
    if status != "SUCCESS":
        raise I2CStatusError(i2c, status, operation, len(write), read_len)
    return params


class MuxedSensorConfig:
    # Reuse Klipper's native sensor drivers while letting the mux section
    # supply the shared I2C settings.  I2C transport settings in a downstream
    # sensor section are intentionally ignored; all other settings resolve
    # from the original sensor section.
    def __init__(self, sensor_config, mux_config):
        self.sensor_config = sensor_config
        self.mux_config = mux_config

    def __getattr__(self, name):
        return getattr(self.sensor_config, name)

    def _get_config_for_option(self, option):
        if option in INHERITED_I2C_OPTIONS:
            return self.mux_config
        if option in INHERITED_OPTION_ALIASES and not _has_option(
                self.sensor_config, option):
            return self.mux_config
        return self.sensor_config

    def _get_option_name(self, option):
        if option in INHERITED_OPTION_ALIASES and not _has_option(
                self.sensor_config, option):
            return INHERITED_OPTION_ALIASES[option]
        return option

    def get(self, option, *args, **kwargs):
        config = self._get_config_for_option(option)
        return config.get(self._get_option_name(option), *args, **kwargs)

    def getint(self, option, *args, **kwargs):
        config = self._get_config_for_option(option)
        return config.getint(self._get_option_name(option), *args, **kwargs)


class TCA9548A:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.debug_no_disable = config.getboolean("debug_no_disable", False)
        self.select_delay = config.getfloat("select_delay", 0.,
                                            minval=0.)
        self.verify_select = config.getboolean("verify_select", False)
        self.config_mcu = config.get("i2c_mcu", "mcu")
        self.config_bus = config.get("i2c_bus", None)
        self.config_address = config.getint("i2c_address",
                                            TCA9548A_I2C_ADDR,
                                            minval=0, maxval=127)
        self.environment_report_time = config.getint(
            "environment_report_time", 60, minval=5)
        self.zero_temperature_on_error = config.getboolean(
            "zero_temperature_on_error", False)
        self.zero_humidity_on_error = config.getboolean(
            "zero_humidity_on_error", False)
        self.reset_pin = None
        self.reset_active_high = True
        self.reset_pulse_time = DEFAULT_RESET_PULSE_TIME
        self.reset_settle_time = DEFAULT_RESET_SETTLE_TIME
        self.reset_recovery_cooldown = DEFAULT_RESET_RECOVERY_COOLDOWN
        self.reset_count = 0
        self.auto_reset_count = 0
        self.auto_reset_failure_count = 0
        self.last_reset_time = None
        self.last_reset_result = None
        self.last_auto_reset_time = None
        self._i2c_pause_until = None
        reset_pin_desc = config.get("reset_pin", None)
        if reset_pin_desc is None:
            for option in ("reset_active_high", "reset_pulse_time",
                           "reset_settle_time", "reset_recovery_cooldown"):
                if _has_option(config, option):
                    raise config.error(
                        "%s requires reset_pin" % (option,))
        else:
            if reset_pin_desc.lstrip().startswith("!"):
                raise config.error(
                    "reset_pin must not use '!'; set reset_active_high "
                    "instead")
            self.reset_active_high = config.getboolean(
                "reset_active_high", True)
            self.reset_pulse_time = config.getfloat(
                "reset_pulse_time", DEFAULT_RESET_PULSE_TIME, above=0.)
            self.reset_settle_time = config.getfloat(
                "reset_settle_time", DEFAULT_RESET_SETTLE_TIME, minval=0.)
            self.reset_recovery_cooldown = config.getfloat(
                "reset_recovery_cooldown", DEFAULT_RESET_RECOVERY_COOLDOWN,
                minval=0.)
            ppins = self.printer.lookup_object("pins")
            self.reset_pin = ppins.setup_pin("digital_out", reset_pin_desc)
            self.reset_pin.setup_max_duration(0.)
            # The TCA RESET# pin must remain inactive while Klipper starts,
            # restarts, or shuts down. reset_active_high describes the GPIO
            # level that asserts reset, not the TCA's active-low pin itself.
            reset_release_value = not self.reset_active_high
            self.reset_pin.setup_start_value(reset_release_value,
                                             reset_release_value)
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=TCA9548A_I2C_ADDR, default_speed=100000)
        self.mutex = self.reactor.mutex()
        logging.info("TCA9548A '%s': configured on mcu '%s', bus '%s', "
                     "address %d",
                     self.name, self.config_mcu, self.config_bus,
                     self.config_address)
        self.last_channel = None
        self.last_control = None
        self._reported_select_failures = set()
        self._reported_session_access_failures = set()
        self._reported_i2c_failures = set()
        self._session_owner = None
        self._session_depth = 0
        self.environment_sensors = []
        self.environment_schedule = {}
        self.environment_schedule_ready = False
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_mux_command("TCA_SELECT", "MUX", self.name,
                                        self.cmd_TCA_SELECT,
                                        desc=self.cmd_TCA_SELECT_help)
        self.gcode.register_mux_command("TCA_STATUS", "MUX", self.name,
                                        self.cmd_TCA_STATUS,
                                        desc=self.cmd_TCA_STATUS_help)
        self.gcode.register_mux_command("TCA_RESET", "MUX", self.name,
                                        self.cmd_TCA_RESET,
                                        desc=self.cmd_TCA_RESET_help)

    def _handle_connect(self):
        if not self.debug_no_disable:
            self.disable_all()
        self._build_environment_schedule()

    def register_environment_sensor(self, sensor, channel):
        self.environment_sensors.append((channel, sensor.name, sensor))
        self.environment_schedule_ready = False

    def _build_environment_schedule(self):
        if self.environment_schedule_ready:
            return
        self.environment_schedule.clear()
        # A stable sort keeps the staggered schedule predictable across
        # restarts and spreads devices uniformly over one report interval.
        sensors = sorted(self.environment_sensors, key=lambda s: (s[0], s[1]))
        sensor_count = len(sensors)
        if not sensor_count:
            logging.info("TCA9548A '%s': environment scheduler disabled "
                         "(no registered sensors)", self.name)
            self.environment_schedule_ready = True
            return
        slot_width = self.environment_report_time / float(sensor_count)
        logging.info("TCA9548A '%s': environment scheduler report_time=%ds "
                     "sensors=%d slot_width=%.3fs",
                     self.name, self.environment_report_time, sensor_count,
                     slot_width)
        for index, (channel, sensor_name, sensor) in enumerate(sensors):
            offset = slot_width * (index + 1)
            self.environment_schedule[sensor] = offset
            logging.info("TCA9548A '%s': environment schedule %s channel=%d "
                         "initial_delay=%.3fs",
                         self.name, sensor_name, channel, offset)
        self.environment_schedule_ready = True

    def get_environment_waketime(self, sensor):
        self._build_environment_schedule()
        offset = self.environment_schedule.get(sensor, 0.)
        return self.reactor.monotonic() + offset

    def _write_control_locked(self, value, allow_auto_reset=True):
        if self._automatic_recovery_stopped():
            self.last_control = self.last_channel = None
            return False
        if self._get_i2c_pause_remaining() > 0.:
            self.last_control = self.last_channel = None
            return False
        if self.last_control == value:
            return True
        # Wait for the mux write to complete before a downstream device can
        # submit its first I2C transaction on the selected channel.
        try:
            i2c_transfer_recoverable(
                self.i2c, [value], operation="TCA9548A control write")
        except I2CStatusError as exc:
            self.last_control = self.last_channel = None
            if allow_auto_reset and self._attempt_auto_reset_locked(exc):
                return self._write_control_locked(value, allow_auto_reset=False)
            self._report_i2c_failure(exc)
            return False
        self._clear_i2c_pause_locked()
        self._clear_auto_reset_failures_locked()
        self._reported_i2c_failures.clear()
        if self.select_delay:
            self.reactor.pause(self.reactor.monotonic() + self.select_delay)
        if self.verify_select:
            control, reset_performed = self._read_control_result_locked()
            if reset_performed:
                # The hardware reset intentionally cleared this selection.
                # Repeat the original control write once, without recursion.
                return self._write_control_locked(value, allow_auto_reset=False)
            if control != value:
                return False
        self.last_control = value
        if value == 0:
            self.last_channel = None
        return True

    def _write_control(self, value):
        with self.mutex:
            return self._write_control_locked(value)

    def _set_reset_result(self, result):
        self.last_reset_result = result

    def _automatic_recovery_stopped(self):
        return (self.auto_reset_failure_count >=
                MAX_CONSECUTIVE_AUTO_RESET_FAILURES)

    def _clear_auto_reset_failures_locked(self):
        self.auto_reset_failure_count = 0

    def _record_auto_reset_failure_locked(self):
        self.auto_reset_failure_count += 1
        return self._automatic_recovery_stopped()

    def _report_automatic_recovery_stopped(self):
        reason = (self.last_reset_result or "unknown").replace(
            "verification failed: ", "", 1)
        message = (
            "TCA9548A %s: automatic recovery stopped after %d failed "
            "resets (%s). Check RESET# wiring and TCA power; repair, then "
            "run TCA_RESET or power-cycle." %
            (self.name, MAX_CONSECUTIVE_AUTO_RESET_FAILURES,
             reason))
        logging.error(message)
        self._respond_error(message)

    def _get_reset_cooldown_remaining(self, eventtime=None):
        if self.last_reset_time is None:
            return 0.
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return max(0., self.last_reset_time
                   + self.reset_recovery_cooldown - eventtime)

    def _pause_i2c_locked(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        self._i2c_pause_until = eventtime + self.environment_report_time
        self.last_control = self.last_channel = None
        return self.environment_report_time

    def _get_i2c_pause_remaining(self, eventtime=None):
        if self._i2c_pause_until is None:
            return 0.
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return max(0., self._i2c_pause_until - eventtime)

    def _clear_i2c_pause_locked(self):
        self._i2c_pause_until = None

    def _respond_error(self, message):
        respond_raw = getattr(self.gcode, "respond_raw", None)
        if respond_raw is not None:
            respond_raw("!! " + message)
        else:
            self.gcode.respond_info(message)

    def _attempt_auto_reset_locked(self, error):
        if self.reset_pin is None or not i2c_status_supported(self.i2c):
            return False
        if self._automatic_recovery_stopped():
            return False
        now = self.reactor.monotonic()
        remaining = self._get_reset_cooldown_remaining(now)
        if remaining > 0.:
            self._pause_i2c_locked(now)
            return False
        self.last_auto_reset_time = now
        self.auto_reset_count += 1
        message = "TCA9548A %s: %s; hardware reset" % (
            self.name, error.status)
        logging.warning(message)
        self._respond_error(message)
        if self._reset_locked():
            logging.info("TCA9548A '%s': hardware reset verified; retrying "
                         "I2C control operation", self.name)
            self.gcode.respond_info("TCA9548A %s: reset verified; retrying" % (
                self.name,))
            return True
        if self._record_auto_reset_failure_locked():
            self._clear_i2c_pause_locked()
            self._report_automatic_recovery_stopped()
            return False
        remaining = self._get_i2c_pause_remaining()
        message = "TCA9548A %s: reset pulse sent; %s; I2C paused for %.0fs" % (
            self.name, self.last_reset_result, remaining)
        logging.error(message)
        self._respond_error(message)
        return False

    def _verify_reset_locked(self):
        if not i2c_status_supported(self.i2c):
            # Old MCU firmware shuts down before returning an I2C failure to
            # Python. Do not turn an optional manual reset into that shutdown.
            self.last_control = self.last_channel = None
            self._set_reset_result("pulsed (not verified on legacy I2C)")
            return True
        try:
            params = i2c_transfer_recoverable(
                self.i2c, [], 1, operation="TCA9548A reset verify")
        except I2CStatusError as exc:
            self.last_control = self.last_channel = None
            self._set_reset_result("verification failed: %s" % (exc.status,))
            self._pause_i2c_locked()
            logging.error("TCA9548A '%s': reset verification failed: %s",
                          self.name, exc)
            return False
        response = params.get("response") if params is not None else None
        if not response or response[0] != 0:
            self.last_control = self.last_channel = None
            self._set_reset_result("verification failed: control is not 0x00")
            self._pause_i2c_locked()
            logging.error("TCA9548A '%s': reset verification read %r, "
                          "expected 0x00", self.name, response)
            return False
        self.last_control = 0
        self.last_channel = None
        self._set_reset_result("verified")
        self._clear_auto_reset_failures_locked()
        return True

    def _reset_locked(self):
        if self.reset_pin is None:
            self._set_reset_result("not configured")
            return False
        now = self.reactor.monotonic()
        mcu = self.reset_pin.get_mcu()
        # queue_digital_out needs enough lead time to reach the MCU. This is
        # especially important for a manual reset on an idle CAN MCU.
        schedule_delay = mcu.min_schedule_time()
        print_time = mcu.estimated_print_time(now + schedule_delay)
        reset_release_value = not self.reset_active_high
        logging.info("TCA9548A '%s': pulsing hardware reset", self.name)
        self.last_control = self.last_channel = None
        self._clear_i2c_pause_locked()
        self.last_reset_time = now
        self.last_reset_result = "verification pending"
        self.reset_pin.set_digital(print_time, self.reset_active_high)
        self.reset_pin.set_digital(print_time + self.reset_pulse_time,
                                   reset_release_value)
        self.reactor.pause(now + schedule_delay + self.reset_pulse_time +
                           self.reset_settle_time)
        self.reset_count += 1
        return self._verify_reset_locked()

    def reset(self):
        with self.mutex:
            return self._reset_locked()

    @contextmanager
    def session(self, close_on_exit=False):
        # ReactorMutex is not reentrant.  Make nested use by the same logical
        # operation safe while retaining a concrete owner for MuxedI2C checks.
        # Callers that set close_on_exit keep a channel selected throughout a
        # complete device operation, then isolate every downstream branch.
        # Only the outermost session owns that cleanup policy.
        owner = greenlet.getcurrent()
        if self._session_owner is owner:
            self._session_depth += 1
            try:
                yield
            finally:
                self._session_depth -= 1
            return
        with self.mutex:
            self._session_owner = owner
            self._session_depth = 1
            try:
                yield
            finally:
                try:
                    if close_on_exit:
                        self._disable_all_locked()
                finally:
                    self._session_depth = 0
                    self._session_owner = None

    def is_session_owner(self):
        return self._session_owner is greenlet.getcurrent()

    def check_session_access(self, channel):
        if self.is_session_owner():
            self._reported_session_access_failures.discard(channel)
            return True
        message = ("TCA9548A '%s': channel %d accessed outside mux.session(); "
                   "the downstream I2C operation was skipped" % (
                       self.name, channel))
        if channel in self._reported_session_access_failures:
            return False
        self._reported_session_access_failures.add(channel)
        logging.error(message)
        self.gcode.respond_info(message)
        return False

    def _read_control_result_locked(self, allow_auto_reset=True):
        if self._automatic_recovery_stopped():
            self.last_control = self.last_channel = None
            return None, False
        if self._get_i2c_pause_remaining() > 0.:
            self.last_control = self.last_channel = None
            return None, False
        try:
            params = i2c_transfer_recoverable(
                self.i2c, [], 1, operation="TCA9548A control read")
        except I2CStatusError as exc:
            self.last_control = self.last_channel = None
            if allow_auto_reset and self._attempt_auto_reset_locked(exc):
                value, ignored = self._read_control_result_locked(
                    allow_auto_reset=False)
                return value, True
            self._report_i2c_failure(exc)
            return None, False
        self._clear_i2c_pause_locked()
        self._clear_auto_reset_failures_locked()
        self._reported_i2c_failures.clear()
        if params is None:
            return None, False
        response = params.get("response")
        if not response:
            return None, False
        return response[0], False

    def _read_control_locked(self, allow_auto_reset=True):
        value, ignored = self._read_control_result_locked(allow_auto_reset)
        return value

    def _read_control(self):
        with self.mutex:
            value = self._read_control_locked()
            self.last_control = value
            return value

    def _select_channel_locked(self, channel):
        value = 1 << channel
        if self.last_channel == channel:
            return True
        if not self._write_control_locked(value):
            # The physical mux state is unknown after a failed verification.
            # Do not retain a cached channel or let downstream I2C continue.
            self.last_control = None
            self.last_channel = None
            self._report_select_failure(channel)
            return False
        self.last_channel = channel
        self._reported_select_failures.discard(channel)
        return True

    def _report_select_failure(self, channel):
        message = ("TCA9548A '%s': failed to verify selection of channel %d; "
                   "the downstream I2C operation was skipped" % (
                       self.name, channel))
        # A recurring sensor timer must not flood the console or klippy.log.
        # A successful later selection clears this latch for the channel.
        if channel in self._reported_select_failures:
            return
        self._reported_select_failures.add(channel)
        logging.error(message)
        self.gcode.respond_info(message)

    def _report_i2c_failure(self, error):
        key = (error.operation, error.status)
        if key in self._reported_i2c_failures:
            return
        self._reported_i2c_failures.add(key)
        message = "TCA9548A '%s': %s; channel state is unknown" % (
            self.name, error)
        logging.error(message)
        self.gcode.respond_info(message)

    def select_channel(self, channel):
        with self.mutex:
            return self._select_channel_locked(channel)

    def _disable_all_locked(self):
        if self.last_control == 0:
            return True
        if not self._write_control_locked(0x00):
            # A failed control write or verification leaves the physical mux
            # state unknown.  Never let a later selection trust stale cache.
            self.last_control = self.last_channel = None
            return False
        self.last_control = 0
        self.last_channel = None
        return True

    def disable_all(self):
        with self.mutex:
            return self._disable_all_locked()

    def is_busy(self):
        # Sensor timers call this before acquiring the lock.  With no reactor
        # pause between this test and `with session`, it is a safe try-acquire
        # pattern: skip one low-priority sample instead of queueing behind a
        # long multi-transaction operation such as PN532.
        return self.mutex.test()

    def get_status(self, eventtime):
        return {
            "channel": self.last_channel,
            "i2c_mcu": self.config_mcu,
            "i2c_bus": self.config_bus,
            "i2c_address": self.config_address,
            "i2c_status_supported": i2c_status_supported(self.i2c),
            "environment_report_time": self.environment_report_time,
            "environment_sensor_count": len(self.environment_sensors),
            "reset_configured": self.reset_pin is not None,
            "reset_active_high": self.reset_active_high,
            "reset_pulse_time": self.reset_pulse_time,
            "reset_settle_time": self.reset_settle_time,
            "reset_recovery_cooldown": self.reset_recovery_cooldown,
            "reset_count": self.reset_count,
            "auto_reset_count": self.auto_reset_count,
            "auto_reset_failure_count": self.auto_reset_failure_count,
            "automatic_recovery_stopped": self._automatic_recovery_stopped(),
            "last_reset_time": self.last_reset_time,
            "last_reset_result": self.last_reset_result,
            "last_auto_reset_time": self.last_auto_reset_time,
            "i2c_recovery_paused": (
                self._get_i2c_pause_remaining(eventtime) > 0.),
            "i2c_recovery_pause_remaining": (
                self._get_i2c_pause_remaining(eventtime)),
        }

    cmd_TCA_SELECT_help = "Select or disable a TCA9548A mux channel"
    def cmd_TCA_SELECT(self, gcmd):
        channel = gcmd.get_int("CHANNEL", None, minval=0, maxval=7)
        if channel is None:
            if not self.disable_all():
                return
            gcmd.respond_info("TCA9548A '%s': all channels disabled" % (
                self.name,))
            return
        if not self.select_channel(channel):
            return
        gcmd.respond_info("TCA9548A '%s': selected channel %d" % (
            self.name, channel))

    cmd_TCA_STATUS_help = "Read the TCA9548A mux control register"
    def cmd_TCA_STATUS(self, gcmd):
        value = self._read_control()
        if value is None:
            return
        self.last_channel = None
        channels = []
        for channel in range(8):
            if value & (1 << channel):
                channels.append(str(channel))
        if len(channels) == 1:
            self.last_channel = int(channels[0])
        channel_text = ",".join(channels) if channels else "none"
        gcmd.respond_info(
            "TCA9548A '%s': control=0x%02x active_channels=%s "
            "i2c_status=%s" % (
                self.name, value, channel_text,
                "supported" if i2c_status_supported(self.i2c)
                else "legacy"))

    cmd_TCA_RESET_help = "Pulse a TCA9548A hardware reset pin"
    def cmd_TCA_RESET(self, gcmd):
        if self.reset_pin is None:
            gcmd.respond_info(
                "TCA9548A '%s': reset_pin is not configured" % (
                    self.name,))
            return
        if self.reset():
            gcmd.respond_info("TCA9548A '%s': reset %s" % (
                self.name, self.last_reset_result))
            return
        remaining = self._get_i2c_pause_remaining()
        gcmd.respond_info("TCA9548A '%s': reset pulse sent; %s; "
                          "I2C paused for %.0fs" % (
                              self.name,
                              self.last_reset_result,
                              remaining))


class MuxedI2C:
    # I2C wrapper that selects the mux channel before each transaction.
    #
    # The caller must use mux.session() for the whole logical operation - a
    # full sensor sample, or one PN532 command/ack/response sequence.  This
    # class never takes the mutex itself.  Taking it per transaction would let
    # another channel switch the mux inside a multi-transaction sequence,
    # because Klipper i2c helpers pause the reactor while waiting on the mcu.
    # Directly nesting ReactorMutex is not an option because it is not
    # reentrant.  mux.session() handles nesting by the same greenlet without
    # reacquiring the underlying mutex.
    def __init__(self, mux, channel, i2c):
        self.mux = mux
        self.channel = channel
        self.i2c = i2c
        self.i2c_address = i2c.get_i2c_address()

    def _select_locked(self):
        if not self.mux.check_session_access(self.channel):
            return False
        return self.mux._select_channel_locked(self.channel)

    def get_oid(self):
        return self.i2c.get_oid()

    def get_mcu(self):
        return self.i2c.get_mcu()

    def get_i2c_address(self):
        return self.i2c.get_i2c_address()

    def get_command_queue(self):
        return self.i2c.get_command_queue()

    def i2c_write_noack(self, data, minclock=0, reqclock=0):
        if not self._select_locked():
            return None
        return self.i2c.i2c_write_noack(data, minclock=minclock,
                                        reqclock=reqclock)

    def i2c_write(self, data, minclock=0, reqclock=0, retry=True):
        if not self._select_locked():
            return None
        try:
            return self.i2c.i2c_write(data, minclock=minclock,
                                      reqclock=reqclock, retry=retry)
        except TypeError as exc:
            # Older Klipper releases do not expose retry on i2c_write().
            if "unexpected keyword argument 'retry'" not in str(exc):
                raise
            return self.i2c.i2c_write(data, minclock=minclock,
                                      reqclock=reqclock)

    def i2c_read(self, write, read_len, retry=True):
        if not self._select_locked():
            return None
        return self.i2c.i2c_read(write, read_len, retry=retry)

    def i2c_transfer(self, write, read_len=0, minclock=0, reqclock=0,
                     retry=True):
        if not self._select_locked():
            return None
        return self.i2c.i2c_transfer(write, read_len=read_len,
                                     minclock=minclock, reqclock=reqclock,
                                     retry=retry)


class RecoverableMuxedI2C(MuxedI2C):
    """Muxed I2C wrapper for a device that can recover from bus errors."""

    def __init__(self, mux, channel, i2c):
        MuxedI2C.__init__(self, mux, channel, i2c)
        self.last_error = None

    @property
    def status_supported(self):
        return i2c_status_supported(self.i2c)

    def clear_error(self):
        self.last_error = None

    def _transfer(self, write, read_len=0, minclock=0, reqclock=0,
                  retry=True, operation="transfer"):
        if not self._select_locked():
            error = MuxSelectionError(self.mux.name, self.channel)
            self.last_error = error
            raise error
        try:
            return i2c_transfer_recoverable(
                self.i2c, write, read_len, minclock, reqclock, retry,
                operation)
        except Exception as exc:
            self.last_error = exc
            raise

    def i2c_write(self, data, minclock=0, reqclock=0, retry=True):
        self._transfer(data, minclock=minclock, reqclock=reqclock,
                       retry=retry, operation="write")

    def i2c_read(self, write, read_len, retry=True):
        return self._transfer(write, read_len, retry=retry,
                              operation="read")

    def i2c_transfer(self, write, read_len=0, minclock=0, reqclock=0,
                     retry=True):
        return self._transfer(write, read_len, minclock, reqclock, retry)


def _register_sensor_factory(config):
    # Import lazily so tca9548a_drivers can import this module's public mux
    # helpers without a module-load cycle.
    from .tca9548a_drivers import aht, bme280, sht3x
    pheaters = config.get_printer().load_object(config, "heaters")
    aht.register_sensor_factories(pheaters)
    bme280.register_sensor_factories(pheaters)
    sht3x.register_sensor_factories(pheaters)


def load_config(config):
    _register_sensor_factory(config)


def load_config_prefix(config):
    _register_sensor_factory(config)
    return TCA9548A(config)
