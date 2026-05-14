# TCA9548A I2C mux support for Klipper AHT2X sensors
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import aht10, bus

TCA9548A_I2C_ADDR = 0x70
INHERITED_I2C_OPTIONS = set([
    "i2c_mcu", "i2c_bus", "i2c_speed",
    "i2c_software_scl_pin", "i2c_software_sda_pin",
])
INHERITED_OPTION_ALIASES = {
    "aht10_report_time": "environment_report_time",
}


def _has_option(config, option):
    return config.fileconfig.has_option(config.get_name(), option)


class MuxedSensorConfig:
    def __init__(self, sensor_config, mux_config):
        self.sensor_config = sensor_config
        self.mux_config = mux_config

    def __getattr__(self, name):
        return getattr(self.sensor_config, name)

    def _get_config_for_option(self, option):
        if option in INHERITED_I2C_OPTIONS and not _has_option(
                self.sensor_config, option):
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
            "environment_report_time", 30, minval=5)
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=TCA9548A_I2C_ADDR, default_speed=100000)
        self.write_i2c = bus.MCU_I2C_from_config(
            config, default_addr=TCA9548A_I2C_ADDR, default_speed=100000,
            async_write_only=True)
        self.mutex = self.reactor.mutex()
        logging.info("TCA9548A '%s': configured on mcu '%s', bus '%s', "
                     "address %d",
                     self.name, self.config_mcu, self.config_bus,
                     self.config_address)
        self.last_channel = None
        self.last_control = None
        self.environment_sensors = []
        self.environment_schedule = {}
        self.environment_schedule_ready = False
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command("TCA_SELECT", "MUX", self.name,
                                   self.cmd_TCA_SELECT,
                                   desc=self.cmd_TCA_SELECT_help)
        gcode.register_mux_command("TCA_STATUS", "MUX", self.name,
                                   self.cmd_TCA_STATUS,
                                   desc=self.cmd_TCA_STATUS_help)

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

    def _write_control_locked(self, value):
        if self.last_control == value:
            return True
        if self.verify_select:
            self.i2c.i2c_write([value])
        else:
            self.write_i2c.i2c_write([value])
        if self.select_delay:
            self.reactor.pause(self.reactor.monotonic() + self.select_delay)
        if self.verify_select:
            if self._read_control_locked() != value:
                return False
        self.last_control = value
        if value == 0:
            self.last_channel = None
        return True

    def _write_control(self, value):
        with self.mutex:
            return self._write_control_locked(value)

    def _read_control_locked(self):
        params = self.i2c.i2c_read([], 1)
        if params is None:
            return None
        response = params.get("response")
        if not response:
            return None
        return response[0]

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
            return False
        self.last_channel = channel
        return True

    def select_channel(self, channel):
        with self.mutex:
            return self._select_channel_locked(channel)

    def disable_all(self):
        with self.mutex:
            if self.last_control == 0:
                return True
            if not self._write_control_locked(0x00):
                return False
            self.last_channel = None
            return True

    def get_status(self, eventtime):
        return {
            "channel": self.last_channel,
            "i2c_mcu": self.config_mcu,
            "i2c_bus": self.config_bus,
            "i2c_address": self.config_address,
            "environment_report_time": self.environment_report_time,
            "environment_sensor_count": len(self.environment_sensors),
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
            "TCA9548A '%s': control=0x%02x active_channels=%s" % (
                self.name, value, channel_text))


class MuxedI2C:
    def __init__(self, mux, channel, i2c):
        self.mux = mux
        self.channel = channel
        self.i2c = i2c

    def _select(self):
        self.mux.select_channel(self.channel)

    def _select_locked(self):
        self.mux._select_channel_locked(self.channel)

    def get_oid(self):
        return self.i2c.get_oid()

    def get_mcu(self):
        return self.i2c.get_mcu()

    def get_i2c_address(self):
        return self.i2c.get_i2c_address()

    def get_command_queue(self):
        return self.i2c.get_command_queue()

    def i2c_write_noack(self, data, minclock=0, reqclock=0):
        with self.mux.mutex:
            self._select_locked()
            return self.i2c.i2c_write_noack(data, minclock=minclock,
                                            reqclock=reqclock)

    def i2c_write(self, data, minclock=0, reqclock=0, retry=True):
        with self.mux.mutex:
            self._select_locked()
            return self.i2c.i2c_write(data, minclock=minclock,
                                      reqclock=reqclock)

    def i2c_read(self, write, read_len, retry=True):
        with self.mux.mutex:
            self._select_locked()
            return self.i2c.i2c_read(write, read_len, retry=retry)

    def i2c_transfer(self, write, read_len=0, minclock=0, reqclock=0,
                     retry=True):
        with self.mux.mutex:
            self._select_locked()
            return self.i2c.i2c_transfer(write, read_len=read_len,
                                         minclock=minclock, reqclock=reqclock,
                                         retry=retry)


class AHTTCA9548AMixin:
    def __init__(self, config):
        if _has_option(config, "aht10_report_time"):
            raise config.error(
                "%s: aht10_report_time is not supported on TCA9548A AHT "
                "sensors; set environment_report_time in the [tca9548a] "
                "mux section" % (config.get_name(),))
        self._mux = None
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        self._debug_skip_init = config.getboolean("debug_skip_init", False)
        self._status_patched = False
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % (mux_name,)
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % (
                mux_section,))
        mux = config.get_printer().load_object(config, mux_section)
        mux_config = config.getsection(mux_section)
        super(AHTTCA9548AMixin, self).__init__(
            MuxedSensorConfig(config, mux_config))
        self._mux = mux
        self.i2c = MuxedI2C(mux, self._mux_channel, self.i2c)
        mux.register_environment_sensor(self, self._mux_channel)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def handle_connect(self):
        self._patch_temperature_sensor_status()
        if self._debug_skip_init:
            logging.info("%s %s: debug_skip_init enabled, skipping sensor init",
                         self.model, self.name)
            return
        self._init_sensor()
        waketime = self._mux.get_environment_waketime(self)
        self.reactor.update_timer(self.sample_timer, waketime)

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
            status["humidity"] = sensor.humidity
            status["tca9548a_channel"] = sensor._mux_channel
            return status

        tsensor.get_status = get_status_with_environment
        self._status_patched = True
        logging.info("%s %s: exposed humidity on '%s'",
                     self.model, self.name, tsensor_name)

    def get_status(self, eventtime):
        status = super(AHTTCA9548AMixin, self).get_status(eventtime)
        status["tca9548a_channel"] = self._mux_channel
        return status


class AHT1xTCA9548A(AHTTCA9548AMixin, aht10.AHT1x):
    model = "aht1x_tca9548a"


class AHT2xTCA9548A(AHTTCA9548AMixin, aht10.AHT2x):
    model = "aht2x_tca9548a"


class AHT3xTCA9548A(AHTTCA9548AMixin, aht10.AHT3x):
    model = "aht3x_tca9548a"


def _register_sensor_factory(config):
    pheaters = config.get_printer().load_object(config, "heaters")
    pheaters.add_sensor_factory("AHT1X_TCA9548A", AHT1xTCA9548A)
    pheaters.add_sensor_factory("AHT2X_TCA9548A", AHT2xTCA9548A)
    pheaters.add_sensor_factory("AHT3X_TCA9548A", AHT3xTCA9548A)


def load_config(config):
    _register_sensor_factory(config)


def load_config_prefix(config):
    _register_sensor_factory(config)
    return TCA9548A(config)
