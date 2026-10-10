"""Recoverable, standalone BME280 driver for TCA9548A."""

# Compensation formulas adapted from Klipper's bme280.py.
# Copyright (C) 2020  Eric Callahan <arksine.code@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

from .. import bus, tca9548a
from .recovery import EnvironmentRecoveryMixin

BME280_CHIP_ADDR = 0x76
BME280_CHIP_ID_REG = 0xD0
BME280_REGS = {
    "RESET": 0xE0, "CTRL_HUM": 0xF2, "STATUS": 0xF3,
    "CTRL_MEAS": 0xF4, "CONFIG": 0xF5, "TEMP_MSB": 0xFA,
    "CAL_1": 0x88, "CAL_2": 0xE1,
}
RESET_CHIP_VALUE = 0xB6
STATUS_IM_UPDATE = 1
MODE_PERIODIC = 3


def _twos_complement(value, bit_size):
    if value & (1 << (bit_size - 1)):
        value -= 1 << bit_size
    return value


def _unsigned_short(data):
    return data[1] << 8 | data[0]


def _signed_short(data):
    return _twos_complement(_unsigned_short(data), 16)


class BME280MeasurementError(Exception):
    """The BME280 returned an incomplete or unusable measurement."""


class BME280TCA9548A(EnvironmentRecoveryMixin):
    """Standalone BME280 temperature/humidity driver behind a mux."""

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
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % mux_name
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % mux_section)
        self._mux = self.printer.load_object(config, mux_section)
        mux_config = config.getsection(mux_section)
        sensor_config = tca9548a.MuxedSensorConfig(config, mux_config)
        raw_i2c = bus.MCU_I2C_from_config(
            sensor_config, default_addr=BME280_CHIP_ADDR,
            default_speed=100000)
        self.i2c = tca9548a.RecoverableMuxedI2C(
            self._mux, self._mux_channel, raw_i2c)
        self.mcu = self.i2c.get_mcu()
        self.report_time = self._mux.environment_report_time
        self.iir_filter = config.getint("bme280_iir_filter", 1,
                                        minval=0, maxval=4)
        self.os_temp = config.getint("bme280_oversample_temp", 2,
                                     minval=1, maxval=5)
        self.os_hum = config.getint("bme280_oversample_hum", 2,
                                    minval=1, maxval=5)
        self.temp = self.min_temp = self.max_temp = 0.
        self.humidity = self.t_fine = 0.
        self.dig = None
        self._initialized = False
        self._callback = None
        uses_scheduler = getattr(
            self._mux, "uses_environment_scheduler", lambda: False)()
        self.sample_timer = (None if uses_scheduler else
                             self.reactor.register_timer(self._sample_bme280))
        self._init_environment_recovery()
        self.printer.add_object("bme280 " + self.name, self)
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self._mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        if getattr(self._mux, "uses_environment_scheduler", lambda: False)():
            return
        if (self._sampling_stopped()
                or self._stop_sampling_if_printer_shutdown()):
            return
        if self._mux.should_pause_environment_sampling(
                self.reactor.monotonic()):
            self.reactor.update_timer(
                self.sample_timer, self._mux.get_environment_waketime(self))
            return
        with self._mux.session(close_on_exit=True):
            if self._initialize_sensor():
                self._sample_initialized()
        self.reactor.update_timer(
            self.sample_timer, self._mux.get_environment_waketime(self))

    def _initialize_sensor(self):
        self.i2c.clear_error()
        try:
            chip_id = self._read_register(BME280_CHIP_ID_REG, 1)[0]
            if chip_id != 0x60:
                raise BME280MeasurementError(
                    "unsupported chip ID 0x%02x (expected 0x60)" % chip_id)
            self._write_register(BME280_REGS["RESET"], [RESET_CHIP_VALUE])
            self.reactor.pause(self.reactor.monotonic() + .5)
            status = self._read_register(BME280_REGS["STATUS"], 1)[0]
            if status & STATUS_IM_UPDATE:
                raise BME280MeasurementError(
                    "calibration data still updating after reset")
            cal_1 = self._read_register(BME280_REGS["CAL_1"], 26)
            cal_2 = self._read_register(BME280_REGS["CAL_2"], 16)
            self.dig = self._read_calibration(cal_1, cal_2)
            # 1s chip standby; host sampling uses the much longer mux period.
            self._write_register(BME280_REGS["CONFIG"],
                                 [(5 << 5) | (self.iir_filter << 2)])
            self._write_register(BME280_REGS["CTRL_HUM"], [self.os_hum])
            self._write_register(BME280_REGS["CTRL_MEAS"],
                                 [(self.os_temp << 5) | MODE_PERIODIC])
            # First conversion takes <=76ms even at 16x temperature/humidity
            # oversampling. Do not publish the reset register contents.
            self.reactor.pause(self.reactor.monotonic() + .080)
        except Exception as exc:
            self._initialized = False
            self._record_failure("initialization", exc)
            return False
        self._initialized = True
        logging.info("%s %s: successfully initialized", self.model, self.name)
        return True

    @staticmethod
    def _read_calibration(cal_1, cal_2):
        return {
            "T1": _unsigned_short(cal_1[0:2]),
            "T2": _signed_short(cal_1[2:4]),
            "T3": _signed_short(cal_1[4:6]),
            "H1": cal_1[25] & 0xff,
            "H2": _signed_short(cal_2[0:2]),
            "H3": cal_2[2] & 0xff,
            "H4": _twos_complement((cal_2[3] << 4)
                                   | (cal_2[4] & 0x0f), 12),
            "H5": _twos_complement((cal_2[5] << 4)
                                   | ((cal_2[4] & 0xf0) >> 4), 12),
            "H6": _twos_complement(cal_2[6], 8),
        }

    def _read_register(self, register, read_len):
        params = self.i2c.i2c_read([register], read_len)
        if params is None:
            raise BME280MeasurementError("empty I2C read response")
        response = bytearray(params.get("response", []))
        if len(response) != read_len:
            raise BME280MeasurementError(
                "expected %d bytes, received %d" % (read_len, len(response)))
        return response

    def _write_register(self, register, data):
        self.i2c.i2c_write([register] + list(data))

    def _sample_bme280(self, eventtime):
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
            success = self._sample_initialized()
        if not success:
            return eventtime + self.report_time
        return self.reactor.monotonic() + self.report_time

    def sample_environment(self, eventtime):
        return self._sample_bme280(eventtime)

    def _sample_initialized(self):
        previous_values = (self.temp, self.humidity)
        self.i2c.clear_error()
        try:
            data = self._read_register(BME280_REGS["TEMP_MSB"], 5)
            temp_raw = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
            humid_raw = (data[3] << 8) | data[4]
            if temp_raw == 0x80000 or humid_raw == 0x8000:
                raise BME280MeasurementError("measurement is not ready")
            self.temp = self._compensate_temp(temp_raw)
            self.humidity = self._compensate_humidity(humid_raw)
        except Exception as exc:
            self.temp, self.humidity = previous_values
            self._record_failure("measurement", exc)
            return False
        if self.temp < self.min_temp or self.temp > self.max_temp:
            self.printer.invoke_shutdown(
                "BME280 temperature %0.1f outside range of %0.1f:%.01f" % (
                    self.temp, self.min_temp, self.max_temp))
        self._publish_temperature()
        self._record_success()
        return True

    def _on_environment_failure(self):
        self._initialized = False

    def _compensate_temp(self, raw_temp):
        dig = self.dig
        var1 = (raw_temp / 16384. - dig["T1"] / 1024.) * dig["T2"]
        var2 = ((raw_temp / 131072. - dig["T1"] / 8192.) ** 2) * dig["T3"]
        self.t_fine = var1 + var2
        return self.t_fine / 5120.

    def _compensate_humidity(self, raw_humidity):
        dig = self.dig
        humidity = self.t_fine - 76800.
        h1 = raw_humidity - (dig["H4"] * 64.
                             + dig["H5"] / 16384. * humidity)
        h2 = (dig["H2"] / 65536. *
              (1. + dig["H6"] / 67108864. * humidity *
               (1. + dig["H3"] / 67108864. * humidity)))
        humidity = h1 * h2
        return min(100., max(0., humidity *
                              (1. - dig["H1"] * humidity / 524288.)))

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, callback):
        self._callback = callback

    def get_report_time_delta(self):
        return self.report_time

    def get_status(self, eventtime):
        status = {"temperature": round(self.temp, 2),
                  "humidity": self.humidity}
        status.update(self._get_environment_recovery_status())
        return status


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("BME280_TCA9548A", BME280TCA9548A)
