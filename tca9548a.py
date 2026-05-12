# TCA9548A I2C mux support for Klipper AHT2X sensors
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import aht10, bus

TCA9548A_I2C_ADDR = 0x70


class TCA9548A:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=TCA9548A_I2C_ADDR, default_speed=100000)
        self.last_channel = None
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)

    def _handle_connect(self):
        self.disable_all()

    def select_channel(self, channel):
        value = 1 << channel
        self.i2c.i2c_write([value])
        self.last_channel = channel

    def disable_all(self):
        self.i2c.i2c_write([0x00])
        self.last_channel = None

    def get_status(self, eventtime):
        return {"channel": self.last_channel}


class MuxedI2C:
    def __init__(self, mux, channel, i2c):
        self.mux = mux
        self.channel = channel
        self.i2c = i2c

    def _select(self):
        self.mux.select_channel(self.channel)

    def get_oid(self):
        return self.i2c.get_oid()

    def get_mcu(self):
        return self.i2c.get_mcu()

    def get_i2c_address(self):
        return self.i2c.get_i2c_address()

    def get_command_queue(self):
        return self.i2c.get_command_queue()

    def i2c_write_noack(self, data, minclock=0, reqclock=0):
        self._select()
        return self.i2c.i2c_write_noack(data, minclock=minclock,
                                        reqclock=reqclock)

    def i2c_write(self, data, minclock=0, reqclock=0, retry=True):
        self._select()
        return self.i2c.i2c_write(data, minclock=minclock, reqclock=reqclock,
                                  retry=retry)

    def i2c_read(self, write, read_len, retry=True):
        self._select()
        return self.i2c.i2c_read(write, read_len, retry=retry)

    def i2c_transfer(self, write, read_len=0, minclock=0, reqclock=0,
                     retry=True):
        self._select()
        return self.i2c.i2c_transfer(write, read_len=read_len,
                                     minclock=minclock, reqclock=reqclock,
                                     retry=retry)


class AHT2xTCA9548A(aht10.AHT2x):
    model = "aht2x_tca9548a"

    def __init__(self, config):
        self._mux = None
        self._mux_channel = config.getint("tca9548a_channel", minval=0,
                                          maxval=7)
        mux_name = config.get("tca9548a")
        mux_section = "tca9548a %s" % (mux_name,)
        if not config.has_section(mux_section):
            raise config.error("Section '%s' must be defined" % (
                mux_section,))
        mux = config.get_printer().load_object(config, mux_section)
        super(AHT2xTCA9548A, self).__init__(config)
        self._mux = mux
        self.i2c = MuxedI2C(mux, self._mux_channel, self.i2c)
        logging.info("%s %s: using TCA9548A '%s' channel %d",
                     self.model, self.name, mux_name, self._mux_channel)

    def get_status(self, eventtime):
        status = super(AHT2xTCA9548A, self).get_status(eventtime)
        status["tca9548a_channel"] = self._mux_channel
        return status


def _register_sensor_factory(config):
    pheaters = config.get_printer().load_object(config, "heaters")
    pheaters.add_sensor_factory("AHT2X_TCA9548A", AHT2xTCA9548A)


def load_config(config):
    _register_sensor_factory(config)


def load_config_prefix(config):
    _register_sensor_factory(config)
    return TCA9548A(config)
