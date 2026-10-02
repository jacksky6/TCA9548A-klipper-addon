"""Recoverable AHTxx temperature and humidity drivers for TCA9548A."""

import logging

from .. import bus, tca9548a
from .recovery import EnvironmentRecoveryMixin

I2C_ADDR = 0x38

CMD_MEASURE = [0xAC, 0x33, 0x00]
CMD_RESET = [0xBA]
CMD_INIT_AHT1X = [0xE1, 0x08, 0x00]
CMD_INIT_AHT2X = [0xBE, 0x08, 0x00]

STATUS_BUSY = 0x80
STATUS_CALIBRATED = 0x08
MAX_BUSY_CYCLES = 5


class AHTMeasurementError(Exception):
    """The AHT device returned an incomplete or unusable measurement."""


class AHTBase(EnvironmentRecoveryMixin):
    model = None
    recovery_sensor_type = "AHT"

    def __init__(self, config):
        if tca9548a._has_option(config, "aht10_report_time"):
            raise config.error(
                "%s: aht10_report_time is not supported on TCA9548A AHT "
                "sensors; set environment_report_time in the [tca9548a] "
                "mux section" % (config.get_name(),))
        for option in ("zero_temperature_on_error",
                       "zero_humidity_on_error"):
            if not tca9548a._has_option(config, option):
                continue
            raise config.error(
                "%s: %s is not supported in TCA9548A AHT sensors; set it "
                "in the [tca9548a] mux section" % (
                    config.get_name(), option))

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
        self.is_calibrated = False
        self.init_sent = False
        self._callback = None
        self._init_environment_recovery()

        # Preserve Klipper's standard AHT object name for consumers that look
        # up humidity independently of temperature_sensor.
        self.printer.add_object("aht10 " + self.name, self)
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self._mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        if self._sampling_stopped():
            return
        if self._debug_skip_init:
            logging.info("%s %s: debug_skip_init enabled, skipping sensor init",
                         self.model, self.name)
            return
        with self._mux.session(close_on_exit=True):
            initialized = self._initialize_sensor()
        if initialized:
            self._publish_sample()
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

    def _send_init(self):
        raise NotImplementedError("Subclass must implement _send_init")

    def _on_environment_failure(self):
        self.init_sent = False

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

    def _sample_aht(self, eventtime):
        if self._sampling_stopped():
            return self.reactor.NEVER
        if self._mux.is_busy():
            return eventtime + self.report_time
        with self._mux.session(close_on_exit=True):
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
        self._publish_temperature(measured_time)
        return measured_time + self.report_time

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, callback):
        self._callback = callback

    def get_report_time_delta(self):
        return self.report_time

    def get_status(self, eventtime):
        status = {
            "temperature": round(self.temp, 2),
            "humidity": self.humidity,
        }
        status.update(self._get_environment_recovery_status())
        return status


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
