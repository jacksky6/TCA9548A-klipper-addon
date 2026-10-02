"""Recoverable AHTxx temperature and humidity drivers for TCA9548A."""

import logging

from .. import bus, tca9548a

I2C_ADDR = 0x38

CMD_MEASURE = [0xAC, 0x33, 0x00]
CMD_RESET = [0xBA]
CMD_INIT_AHT1X = [0xE1, 0x08, 0x00]
CMD_INIT_AHT2X = [0xBE, 0x08, 0x00]

STATUS_BUSY = 0x80
STATUS_CALIBRATED = 0x08
MAX_BUSY_CYCLES = 5
LOG_FAILURE_NOTICE_INTERVAL = 10
WEB_FAILURE_NOTICE_INTERVAL = 120
WEB_CONSOLE_READY_DELAY = 1.


class AHTMeasurementError(Exception):
    """The AHT device returned an incomplete or unusable measurement."""


class AHTBase:
    model = None

    def __init__(self, config):
        if tca9548a._has_option(config, "aht10_report_time"):
            raise config.error(
                "%s: aht10_report_time is not supported on TCA9548A AHT "
                "sensors; set environment_report_time in the [tca9548a] "
                "mux section" % (config.get_name(),))

        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        self._debug_skip_init = config.getboolean("debug_skip_init", False)
        self._status_patched = False
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % (mux_name,)
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % (
                mux_section,))
        self._mux = self.printer.load_object(config, mux_section)
        mux_config = config.getsection(mux_section)
        sensor_config = tca9548a.MuxedSensorConfig(config, mux_config)
        raw_i2c = bus.MCU_I2C_from_config(
            sensor_config, default_addr=I2C_ADDR, default_speed=100000)
        self.i2c = tca9548a.RecoverableMuxedI2C(
            self._mux, self._mux_channel, raw_i2c)
        self.report_time = self._mux.environment_report_time
        self.temp = self.min_temp = self.max_temp = self.humidity = 0.
        self.sample_timer = self.reactor.register_timer(self._sample_aht)
        self._pending_web_notification_timer = self.reactor.register_timer(
            self._emit_pending_web_notification)
        self.is_calibrated = False
        self.init_sent = False
        self._callback = None

        self.valid = False
        self.communication_ok = False
        self.last_error = None
        self.last_error_time = None
        self.last_success_time = None
        self.i2c_error_count = 0
        self.error_count = 0
        self._last_log_error_key = None
        self._suppressed_log_errors = 0
        self._last_web_error_key = None
        self._suppressed_web_errors = 0
        self._klippy_ready = False
        self._pending_web_notification = None

        # Preserve Klipper's standard AHT object name for consumers that look
        # up humidity independently of temperature_sensor.
        self.printer.add_object("aht10 " + self.name, self)
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self._mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        if self._debug_skip_init:
            logging.info("%s %s: debug_skip_init enabled, skipping sensor init",
                         self.model, self.name)
            return
        with self._mux.session():
            initialized = self._initialize_sensor()
        if initialized:
            self._publish_sample()
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

    def handle_ready(self):
        self._klippy_ready = True
        if self._pending_web_notification is not None:
            self.reactor.update_timer(
                self._pending_web_notification_timer,
                self.reactor.monotonic() + WEB_CONSOLE_READY_DELAY)

    def _send_init(self):
        raise NotImplementedError("Subclass must implement _send_init")

    def _initialize_sensor(self):
        self.init_sent = False
        self.i2c.clear_error()
        try:
            self._send_init()
        except Exception as exc:
            self._record_failure("initialization", exc)
            return False
        self.init_sent = True
        if not self._make_measurement("initialization"):
            return False
        if not self.is_calibrated:
            logging.warning("%s %s: not calibrated, possible OTP fault",
                            self.model, self.name)
        logging.info("%s %s: successfully initialized, initial temp: %.3f, "
                     "humidity: %.3f", self.model, self.name, self.temp,
                     self.humidity)
        return True

    def _soft_reset(self):
        logging.info("%s %s: performing soft reset", self.model, self.name)
        self.i2c.i2c_write(CMD_RESET)
        self.reactor.pause(self.reactor.monotonic() + .020)

    def _make_measurement(self, stage):
        if not self.init_sent:
            self._record_failure(
                stage, AHTMeasurementError("sensor is not initialized"))
            return False

        try:
            for cycle in range(MAX_BUSY_CYCLES + 1):
                self.i2c.i2c_write(CMD_MEASURE)
                # AHTxx needs at least 75ms; retain Klipper's 110ms delay.
                self.reactor.pause(self.reactor.monotonic() + .110)
                params = self.i2c.i2c_read([], 6)
                if params is None:
                    raise AHTMeasurementError("empty I2C read response")
                data = bytearray(params.get("response", []))
                if len(data) != 6:
                    raise AHTMeasurementError(
                        "expected 6 measurement bytes, received %d" % (
                            len(data),))

                self.is_calibrated = bool(data[0] & STATUS_CALIBRATED)
                if data[0] & STATUS_BUSY:
                    continue

                temp_raw = ((data[3] & 0x0F) << 16) | \
                    (data[4] << 8) | data[5]
                humidity_raw = (data[1] << 12) | (data[2] << 4) | \
                    (data[3] >> 4)
                self.temp = ((temp_raw * 200.0) / 1048576.0) - 50.0
                self.humidity = min(
                    100., max(0., (humidity_raw * 100.0) / 1048576.0))
                self._record_success()
                return True
            self._soft_reset()
            raise AHTMeasurementError(
                "device remained busy after %d measurements" % (
                    MAX_BUSY_CYCLES + 1,))
        except Exception as exc:
            self._record_failure(stage, exc)
            return False

    def _record_failure(self, stage, error):
        now = self.reactor.monotonic()
        self.valid = False
        self.communication_ok = False
        self.init_sent = False
        self.error_count += 1
        details = {
            "stage": stage,
            "type": error.__class__.__name__,
            "message": str(error) or error.__class__.__name__,
        }
        if isinstance(error, tca9548a.I2CStatusError):
            self.i2c_error_count += 1
            details["i2c_bus_status"] = error.status
            details["i2c_address"] = error.i2c_address
        self.last_error = details
        self.last_error_time = now

        key = (stage, details.get("i2c_bus_status"), details["type"],
               details["message"])
        if key != self._last_log_error_key:
            logging.warning("%s %s: communication failed during %s: %s; "
                            "retrying in %ds", self.model, self.name,
                            stage, details["message"], self.report_time)
            self._last_log_error_key = key
            self._suppressed_log_errors = 0
        else:
            self._suppressed_log_errors += 1
            if self._suppressed_log_errors >= LOG_FAILURE_NOTICE_INTERVAL:
                logging.warning("%s %s: communication failure persists; "
                                "%d repeated failure(s) over %ds",
                                self.model, self.name,
                                self._suppressed_log_errors,
                                self._suppressed_log_errors *
                                self.report_time)
                self._suppressed_log_errors = 0

        self._record_web_failure(key, stage, details)

    def _record_web_failure(self, key, stage, details):
        message = (
            "TCA9548A AHT %s: I2C communication failed during %s: %s; "
            "retrying in %ds" % (
                self.name, stage, details["message"], self.report_time))
        if key != self._last_web_error_key:
            self._suppressed_web_errors = 0
            if not self._klippy_ready:
                # Moonraker subscribes to G-code output after klippy:connect.
                # Retain startup failures until the Console can receive them.
                self._pending_web_notification = (key, message)
            elif self._pending_web_notification is not None:
                self._pending_web_notification = (key, message)
            else:
                self._respond_info(message)
                self._last_web_error_key = key
        else:
            self._suppressed_web_errors += 1
            if self._suppressed_web_errors >= WEB_FAILURE_NOTICE_INTERVAL:
                self._respond_info(
                    "TCA9548A AHT %s: I2C communication still failing; "
                    "%d repeated failed attempts since last notice" % (
                        self.name, self._suppressed_web_errors))
                self._suppressed_web_errors = 0

    def _emit_pending_web_notification(self, eventtime):
        pending = self._pending_web_notification
        self._pending_web_notification = None
        if pending is not None and self.communication_ok is False:
            key, message = pending
            self._respond_info(message)
            self._last_web_error_key = key
        return self.reactor.NEVER

    def _record_success(self):
        if not self.communication_ok and self.last_success_time is not None:
            logging.info("%s %s: I2C communication recovered",
                         self.model, self.name)
            if self._last_web_error_key is not None:
                self._respond_info(
                    "TCA9548A AHT %s: I2C communication recovered" % (
                        self.name,))
        self.valid = True
        self.communication_ok = True
        self.last_success_time = self.reactor.monotonic()
        self._last_log_error_key = None
        self._suppressed_log_errors = 0
        self._last_web_error_key = None
        self._suppressed_web_errors = 0
        self._pending_web_notification = None

    def _respond_info(self, message):
        try:
            gcode = self.printer.lookup_object("gcode", None)
            if gcode is not None:
                gcode.respond_info(message)
        except Exception:
            logging.exception("%s %s: unable to send Console notification",
                              self.model, self.name)

    def _sample_aht(self, eventtime):
        if self._mux.is_busy():
            return eventtime + self.report_time
        with self._mux.session():
            if self.init_sent:
                self.i2c.clear_error()
                success = self._make_measurement("measurement")
            else:
                success = self._initialize_sensor()
        if not success:
            return eventtime + self.report_time

        if self.temp < self.min_temp or self.temp > self.max_temp:
            self.printer.invoke_shutdown(
                "%s temperature %.1f outside range of %.1f:%.1f" % (
                    self.model.upper(), self.temp, self.min_temp,
                    self.max_temp))
        return self._publish_sample()

    def _publish_sample(self):
        measured_time = self.reactor.monotonic()
        if self._callback is not None:
            print_time = self.i2c.get_mcu().estimated_print_time(measured_time)
            self._callback(print_time, self.temp)
        return measured_time + self.report_time

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, callback):
        self._callback = callback

    def get_report_time_delta(self):
        return self.report_time

    def _patch_temperature_sensor_status(self):
        if self._status_patched:
            return
        tsensor_name = "temperature_sensor %s" % (self.name,)
        tsensor = self.printer.lookup_object(tsensor_name, None)
        if tsensor is None:
            return
        original_get_status = tsensor.get_status
        sensor = self

        def get_status_with_environment(eventtime):
            status = original_get_status(eventtime)
            sensor_status = sensor.get_status(eventtime)
            for key in ("humidity", "valid", "communication_ok",
                        "last_error", "last_error_time", "last_success_time",
                        "i2c_error_count", "error_count",
                        "i2c_status_supported"):
                status[key] = sensor_status[key]
            status["tca9548a_channel"] = sensor._mux_channel
            return status

        tsensor.get_status = get_status_with_environment
        self._status_patched = True
        logging.info("%s %s: exposed environment data on '%s'",
                     self.model, self.name, tsensor_name)

    def get_status(self, eventtime):
        return {
            "temperature": round(self.temp, 2),
            "humidity": self.humidity,
            "valid": self.valid,
            "communication_ok": self.communication_ok,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "last_success_time": self.last_success_time,
            "i2c_error_count": self.i2c_error_count,
            "error_count": self.error_count,
            "i2c_status_supported": self.i2c.status_supported,
            "tca9548a_channel": self._mux_channel,
        }


class AHT1x(AHTBase):
    model = "aht1x_tca9548a"

    def _send_init(self):
        self.i2c.i2c_write(CMD_INIT_AHT1X)
        self.reactor.pause(self.reactor.monotonic() + .040)


class AHT2x(AHTBase):
    model = "aht2x_tca9548a"

    def _send_init(self):
        self.i2c.i2c_write(CMD_INIT_AHT2X)
        self.reactor.pause(self.reactor.monotonic() + .100)


class AHT3x(AHTBase):
    model = "aht3x_tca9548a"

    def _send_init(self):
        # AHT3x calibrates automatically after power-on.
        self.reactor.pause(self.reactor.monotonic() + .100)


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("AHT1X_TCA9548A", AHT1x)
    pheaters.add_sensor_factory("AHT2X_TCA9548A", AHT2x)
    pheaters.add_sensor_factory("AHT3X_TCA9548A", AHT3x)
