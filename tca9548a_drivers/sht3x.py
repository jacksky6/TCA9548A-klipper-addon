"""Recoverable SHT3X driver for TCA9548A."""

import logging

from .. import sht3x, tca9548a
from .recovery import EnvironmentRecoveryMixin


class SHT3XMeasurementError(Exception):
    """The installed SHT3X driver stopped a sample without an I2C error."""


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
            result = super(SHT3XTCA9548A, self)._sample_sht3x(eventtime)
        except Exception as exc:
            self.temp, self.humidity = previous_values
            self._record_failure("measurement", exc)
            return False
        if result == self.reactor.NEVER:
            # The native driver clears both values when an exception escapes.
            # Restore them before applying the shared per-value error policy.
            self.temp, self.humidity = previous_values
            error = self.i2c.last_error or SHT3XMeasurementError(
                "measurement did not complete")
            self._record_failure("measurement", error)
            return False
        self._record_success()
        return True

    def get_status(self, eventtime):
        status = super(SHT3XTCA9548A, self).get_status(eventtime)
        status.update(self._get_environment_recovery_status())
        return status


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("SHT3X_TCA9548A", SHT3XTCA9548A)
