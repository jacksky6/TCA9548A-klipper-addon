"""Recoverable, standalone SHT3X driver for TCA9548A."""

# Command sequence and conversion formulas adapted from Klipper's sht3x.py.
# Copyright (C) 2024  Timofey Titovets <nefelim4ag@gmail.com>
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

from .. import bus, tca9548a
from .recovery import EnvironmentRecoveryMixin

SHT3X_I2C_ADDR = 0x44
SHT3X_BREAK_COMMAND = [0x30, 0x93]
SHT3X_RESET_COMMAND = [0x30, 0xA2]
SHT3X_STATUS_COMMAND = [0xF3, 0x2D]
SHT3X_PERIODIC_COMMAND = [0x22, 0x36]  # 2Hz, high repeatability
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


class SHT3XTCA9548A(EnvironmentRecoveryMixin):
    """Standalone SHT30/31/35 temperature/humidity driver behind a mux."""

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

        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % (mux_name,)
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % (
                mux_section,))
        self._mux = self.printer.load_object(config, mux_section)
        mux_config = config.getsection(mux_section)
        sensor_config = tca9548a.MuxedSensorConfig(config, mux_config)
        raw_i2c = bus.MCU_I2C_from_config(
            sensor_config, default_addr=SHT3X_I2C_ADDR,
            default_speed=100000)
        self.i2c = tca9548a.RecoverableMuxedI2C(
            self._mux, self._mux_channel, raw_i2c)
        self.report_time = self._mux.environment_report_time
        self.temp = self.humidity = self.min_temp = self.max_temp = 0.
        self._callback = None
        self._initialized = False
        self.sample_timer = self.reactor.register_timer(self._sample_sht3x)
        self._init_environment_recovery()
        self.printer.add_object("sht3x " + self.name, self)
        self.printer.register_event_handler("klippy:connect",
                                            self.handle_connect)
        self._mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
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
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

    def _initialize_sensor(self):
        self.i2c.clear_error()
        try:
            # Leave periodic mode before resetting (also on reinitialization).
            self.i2c.i2c_write(SHT3X_BREAK_COMMAND)
            self.reactor.pause(self.reactor.monotonic() + .0015)
            self.i2c.i2c_write(SHT3X_RESET_COMMAND)
            self.reactor.pause(self.reactor.monotonic() + .0015)
            status = self._read_response(SHT3X_STATUS_COMMAND, 3)
            if _sht3x_crc8((status[0] << 8) | status[1]) != status[2]:
                raise SHT3XMeasurementError("status checksum error")
            self.i2c.i2c_write(SHT3X_PERIODIC_COMMAND)
            self.reactor.pause(self.reactor.monotonic() + .0155)
        except Exception as exc:
            self._initialized = False
            self._record_failure("initialization", exc)
            return False
        self._initialized = True
        logging.info("%s %s: successfully initialized", self.model, self.name)
        return True

    def _read_response(self, command, read_len):
        params = self.i2c.i2c_read(command, read_len)
        if params is None:
            raise SHT3XMeasurementError("empty I2C read response")
        response = bytearray(params.get("response", []))
        if len(response) != read_len:
            raise SHT3XMeasurementError(
                "expected %d response bytes, received %d" % (
                    read_len, len(response)))
        return response

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
            success = self._sample_initialized()
        if not success:
            return eventtime + self.report_time
        return self.reactor.monotonic() + self.report_time

    def _sample_initialized(self):
        previous_values = (self.temp, self.humidity)
        self.i2c.clear_error()
        try:
            response = self._read_response(SHT3X_FETCH_COMMAND, 6)

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
        self._publish_temperature()
        self._record_success()
        return True

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, callback):
        self._callback = callback

    def get_report_time_delta(self):
        return self.report_time

    def get_status(self, eventtime):
        status = {"temperature": round(self.temp, 2),
                  "humidity": round(self.humidity, 1)}
        status.update(self._get_environment_recovery_status())
        return status


def register_sensor_factories(pheaters):
    pheaters.add_sensor_factory("SHT3X_TCA9548A", SHT3XTCA9548A)
