"""Recoverable BME280 driver for TCA9548A."""

import logging

from .. import bme280, tca9548a
from .recovery import EnvironmentRecoveryMixin


class BME280MeasurementError(Exception):
    """The installed BME280 driver stopped a sample without an I2C error."""


class BME280TCA9548A(EnvironmentRecoveryMixin, bme280.BME280):
    """Use Klipper's BME280 implementation with a recoverable mux transport."""

    model = "bme280_tca9548a"
    recovery_sensor_type = "BME280"

    def __init__(self, config):
        if tca9548a._has_option(config, "bme280_report_time"):
            raise config.error(
                "%s: bme280_report_time is not supported on TCA9548A "
                "BME280 sensors; set environment_report_time in the "
                "[tca9548a] mux section" % (config.get_name(),))
        for option in ("zero_temperature_on_error",
                       "zero_humidity_on_error"):
            if tca9548a._has_option(config, option):
                raise config.error(
                    "%s: %s is not supported in TCA9548A BME280 sensors; "
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
        super(BME280TCA9548A, self).__init__(
            tca9548a.MuxedSensorConfig(config, mux_config))

        self._mux = mux
        self._report_time = mux.environment_report_time
        self.report_time = self._report_time
        self.i2c = tca9548a.RecoverableMuxedI2C(
            mux, self._mux_channel, self.i2c)
        self.mcu = self.i2c.get_mcu()
        self._initialized = False
        # Native BME280 creates its timer during successful initialization.
        # Keep a timer available when initialization itself fails.
        self.sample_timer = self.reactor.register_timer(self._sample_bme280)
        self._init_environment_recovery()
        mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        with self._mux.session(close_on_exit=True):
            if self._initialize_sensor():
                self._sample_initialized(self.reactor.monotonic())
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

    def _initialize_sensor(self):
        self.i2c.clear_error()
        recovery_timer = self.sample_timer
        try:
            super(BME280TCA9548A, self)._init_bmxx80()
        except Exception as exc:
            self._record_failure("initialization", exc)
            return False
        native_timer = self.sample_timer
        if native_timer is not recovery_timer:
            unregister_timer = getattr(self.reactor, "unregister_timer", None)
            if unregister_timer is not None:
                unregister_timer(native_timer)
            self.sample_timer = recovery_timer
        if self.chip_type != "BME280":
            self.printer.invoke_shutdown(
                "BME280_TCA9548A %s detected unsupported chip type %s" % (
                    self.name, self.chip_type))
            return False
        self._initialized = True
        return True

    def _on_environment_failure(self):
        self._initialized = False

    def get_report_time_delta(self):
        return self._report_time

    def _sample_bme280(self, eventtime):
        if self._mux.is_busy():
            return eventtime + self._report_time
        with self._mux.session(close_on_exit=True):
            if not self._initialized and not self._initialize_sensor():
                return eventtime + self._report_time
            success = self._sample_initialized(eventtime)
        if not success:
            return eventtime + self._report_time
        return self.reactor.monotonic() + self._report_time

    def _sample_initialized(self, eventtime):
        previous_values = (self.temp, self.humidity, self.pressure)
        self.i2c.clear_error()
        try:
            result = super(BME280TCA9548A, self)._sample_bme280(eventtime)
        except Exception as exc:
            self.temp, self.humidity, self.pressure = previous_values
            self._record_failure("measurement", exc)
            return False
        if result == self.reactor.NEVER:
            # The native driver clears values before returning NEVER. Restore
            # them so the shared mux-level error policy can choose per value.
            self.temp, self.humidity, self.pressure = previous_values
            error = self.i2c.last_error or BME280MeasurementError(
                "measurement did not complete")
            self._record_failure("measurement", error)
            return False
        self._record_success()
        return True

    def get_status(self, eventtime):
        status = super(BME280TCA9548A, self).get_status(eventtime)
        # Keep the public BME280_TCA9548A data shape aligned with AHT.
        # Klipper's BME280 implementation still calculates pressure internally
        # as part of its normal measurement flow.
        status.pop("pressure", None)
        status.update(self._get_environment_recovery_status())
        return status


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("BME280_TCA9548A", BME280TCA9548A)
