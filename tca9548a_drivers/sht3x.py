"""Recoverable SHT3X driver for TCA9548A."""

import logging

from .. import sht3x, tca9548a
from .recovery import EnvironmentRecoveryMixin

SHT3X_FETCH_COMMAND = [0xE0, 0x00]


def _sht3x_crc8(data):
    """Calculate the SHT3X CRC for exactly one 16-bit measurement word."""
    crc = 0xFF
    for byte in ((data >> 8) & 0xFF, data & 0xFF):
        crc ^= byte
        for ignored in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


class SHT3XMeasurementError(Exception):
    """The SHT3X returned an incomplete or unusable measurement."""


class SHT3XTCA9548A(EnvironmentRecoveryMixin, sht3x.SHT3X):
    """Use Klipper's SHT3X implementation with a recoverable mux transport."""

    model = "sht3x_tca9548a"
    recovery_sensor_type = "SHT3X"

    def __init__(self, config):
        if tca9548a._has_option(config, "sht3x_report_time"):
            raise config.error(
                "%s: sht3x_report_time is not supported on TCA9548A SHT3X "
                "sensors; set environment_report_time in the [tca9548a] "
                "mux section" % (config.get_name(),))
        for option in ("zero_temperature_on_error",
                       "zero_humidity_on_error"):
            if tca9548a._has_option(config, option):
                raise config.error(
                    "%s: %s is not supported in TCA9548A SHT3X sensors; "
                    "set it in the [tca9548a] mux section" % (
                        config.get_name(), option))

        self._mux = None
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % (mux_name,)
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % (
                mux_section,))
        mux = config.get_printer().load_object(config, mux_section)
        mux_config = config.getsection(mux_section)
        super(SHT3XTCA9548A, self).__init__(
            tca9548a.MuxedSensorConfig(config, mux_config))

        self._mux = mux
        self.report_time = mux.environment_report_time
        self.i2c = tca9548a.RecoverableMuxedI2C(
            mux, self._mux_channel, self.i2c)
        self._initialized = False
        self._init_environment_recovery()
        mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        if (self._sampling_stopped()
                or self._stop_sampling_if_printer_shutdown()):
            return
        with self._mux.session(close_on_exit=True):
            if self._initialize_sensor():
                self._sample_initialized(self.reactor.monotonic())
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

    def _initialize_sensor(self):
        self.i2c.clear_error()
        try:
            super(SHT3XTCA9548A, self)._init_sht3x()
        except Exception as exc:
            self._record_failure("initialization", exc)
            return False
        self._initialized = True
        return True

    def _on_environment_failure(self):
        self._initialized = False

    def _sample_sht3x(self, eventtime):
        if (self._sampling_stopped()
                or self._stop_sampling_if_printer_shutdown()):
            return self.reactor.NEVER
        if self._mux.should_pause_environment_sampling(eventtime):
            return eventtime + self.report_time
        if self._mux.is_busy():
            return eventtime + self.report_time
        with self._mux.session(close_on_exit=True):
            if not self._initialized and not self._initialize_sensor():
                return eventtime + self.report_time
            success = self._sample_initialized(eventtime)
        if not success:
            return eventtime + self.report_time
        return self.reactor.monotonic() + self.report_time

    def _sample_initialized(self, eventtime):
        previous_values = (self.temp, self.humidity)
        self.i2c.clear_error()
        try:
            params = self.i2c.i2c_read(SHT3X_FETCH_COMMAND, 6)
            if params is None:
                raise SHT3XMeasurementError("empty I2C read response")
            response = bytearray(params.get("response", []))
            if len(response) != 6:
                raise SHT3XMeasurementError(
                    "expected 6 measurement bytes, received %d" % (
                        len(response),))

            raw_temperature = (response[0] << 8) | response[1]
            raw_humidity = (response[3] << 8) | response[4]
            if _sht3x_crc8(raw_temperature) != response[2]:
                raise SHT3XMeasurementError("temperature checksum error")
            if _sht3x_crc8(raw_humidity) != response[5]:
                raise SHT3XMeasurementError("humidity checksum error")

            self.temp = -45. + (175. * raw_temperature / 65535.)
            self.humidity = 100. * raw_humidity / 65535.
        except Exception as exc:
            self.temp, self.humidity = previous_values
            self._record_failure("measurement", exc)
            return False

        if self.temp < self.min_temp or self.temp > self.max_temp:
            self.printer.invoke_shutdown(
                "SHT3X temperature %0.1f outside range of %0.1f:%.01f" % (
                    self.temp, self.min_temp, self.max_temp))
        measured_time = self.reactor.monotonic()
        print_time = self.i2c.get_mcu().estimated_print_time(measured_time)
        self._callback(print_time, self.temp)
        self._record_success()
        return True

    def get_status(self, eventtime):
        status = super(SHT3XTCA9548A, self).get_status(eventtime)
        status.update(self._get_environment_recovery_status())
        return status


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("SHT3X_TCA9548A", SHT3XTCA9548A)
