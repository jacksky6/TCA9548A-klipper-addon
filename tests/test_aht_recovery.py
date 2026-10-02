import contextlib
import importlib.util
import pathlib
import sys
import types
import unittest


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]


def _package(name):
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
    return package


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_driver_modules():
    for name in list(sys.modules):
        if name == "klippy" or name.startswith("klippy."):
            del sys.modules[name]
    _package("klippy")
    _package("klippy.extras")
    _package("klippy.extras.tca9548a_drivers")
    bus = types.ModuleType("klippy.extras.bus")
    bus.MCU_I2C_from_config = None
    sys.modules[bus.__name__] = bus
    bme280 = types.ModuleType("klippy.extras.bme280")
    bme280.BME280 = type("BME280", (), {})
    sys.modules[bme280.__name__] = bme280
    sht3x = types.ModuleType("klippy.extras.sht3x")
    sht3x.SHT3X = type("SHT3X", (), {})
    sys.modules[sht3x.__name__] = sht3x
    core = _load_module("klippy.extras.tca9548a", REPOSITORY / "tca9548a.py")
    driver = _load_module(
        "klippy.extras.tca9548a_drivers.aht",
        REPOSITORY / "tca9548a_drivers" / "aht.py")
    return bus, core, driver


class FakeMCU:
    def get_name(self):
        return "mcu"

    def is_fileoutput(self):
        return False

    def estimated_print_time(self, eventtime):
        return eventtime


class FakeTransferCommand:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeModernI2C:
    def __init__(self, responses):
        self.i2c_transfer_cmd = FakeTransferCommand(responses)
        self.mcu = FakeMCU()

    def get_oid(self):
        return 17

    def get_mcu(self):
        return self.mcu

    def get_i2c_address(self):
        return 0x38

    def get_command_queue(self):
        return None


class FakeLegacyI2C:
    def __init__(self):
        self.mcu = FakeMCU()
        self.writes = []

    def get_oid(self):
        return 23

    def get_mcu(self):
        return self.mcu

    def get_i2c_address(self):
        return 0x38

    def get_command_queue(self):
        return None

    # No retry keyword: this matches old Klipper's public write API.
    def i2c_write(self, data, minclock=0, reqclock=0):
        self.writes.append((list(data), minclock, reqclock))

    def i2c_read(self, write, read_len, retry=True):
        return {"response": [0] * read_len}


class FakeReactor:
    NEVER = -1.
    NOW = 0.

    def __init__(self):
        self.now = 0.
        self.updated_timer = None

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        return waketime

    def register_timer(self, callback):
        return callback

    def update_timer(self, timer, waketime):
        self.updated_timer = (timer, waketime)


class FakeMux:
    def __init__(self):
        self.name = "mux0"
        self.environment_report_time = 30
        self._in_session = False
        self.environment_sensors = []

    @contextlib.contextmanager
    def session(self):
        self._in_session = True
        try:
            yield
        finally:
            self._in_session = False

    def check_session_access(self, channel):
        return self._in_session

    def _select_channel_locked(self, channel):
        return True

    def is_busy(self):
        return False

    def register_environment_sensor(self, sensor, channel):
        self.environment_sensors.append((sensor, channel))

    def get_environment_waketime(self, sensor):
        return 30.


class FakeGCode:
    def __init__(self):
        self.responses = []

    def respond_info(self, message):
        self.responses.append(message)


class FakePrinter:
    def __init__(self, reactor, mux):
        self.reactor = reactor
        self.mux = mux
        self.gcode = FakeGCode()
        self.objects = {"gcode": self.gcode}
        self.events = []
        self.shutdowns = []

    def get_reactor(self):
        return self.reactor

    def load_object(self, config, section):
        self.objects[section] = self.mux
        return self.mux

    def add_object(self, name, object_):
        self.objects[name] = object_

    def register_event_handler(self, event, callback):
        self.events.append((event, callback))

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def invoke_shutdown(self, message):
        self.shutdowns.append(message)


class FakeFileConfig:
    def has_option(self, section, option):
        return False


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer
        self.fileconfig = FakeFileConfig()

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "temperature_sensor chamber"

    def get(self, option, default=None):
        values = {"tca9548a": "mux0", "sensor_type": "AHT2X_TCA9548A"}
        return values.get(option, default)

    def getint(self, option, default=None, minval=None, maxval=None):
        values = {"tca9548a_channel": 1}
        return values.get(option, default)

    def getboolean(self, option, default=False):
        return default

    def has_section(self, section):
        return section == "tca9548a mux0"

    def getsection(self, section):
        return self

    def error(self, message):
        return RuntimeError(message)


def success(response=None):
    return {"i2c_bus_status": "SUCCESS", "response": response or []}


MEASUREMENT = [0x08, 0x80, 0x00, 0x06, 0x00, 0x00]


class RecoverableTransportTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.aht = _load_driver_modules()

    def test_modern_status_error_is_returned_without_shutdown(self):
        raw_i2c = FakeModernI2C([
            {"i2c_bus_status": "START_NACK", "response": []},
        ])

        with self.assertRaises(self.core.I2CStatusError) as raised:
            self.core.i2c_transfer_recoverable(raw_i2c, [0xAC],
                                                operation="measurement")

        self.assertEqual(raised.exception.status, "START_NACK")
        self.assertEqual(len(raw_i2c.i2c_transfer_cmd.calls), 1)

    def test_legacy_write_uses_old_signature(self):
        raw_i2c = FakeLegacyI2C()

        self.core.i2c_transfer_recoverable(raw_i2c, [0xBA])

        self.assertEqual(raw_i2c.writes, [([0xBA], 0, 0)])


class AHTRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.aht = _load_driver_modules()
        self.reactor = FakeReactor()
        self.mux = FakeMux()
        self.printer = FakePrinter(self.reactor, self.mux)
        self.config = FakeConfig(self.printer)

    def test_failed_sample_preserves_values_and_recovers_next_period(self):
        raw_i2c = FakeModernI2C([
            success(),             # AHT2x initialization command
            success(),             # initial measurement command
            success(MEASUREMENT),  # initial measurement read
            {"i2c_bus_status": "NACK", "response": []},
            success(),             # retry initialization command
            success(),             # retry measurement command
            success(MEASUREMENT),  # retry measurement read
        ])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        sensor.setup_minmax(-50., 100.)

        with self.mux.session():
            self.assertTrue(sensor._initialize_sensor())
        valid_temperature = sensor.temp
        valid_humidity = sensor.humidity

        self.reactor.now = 100.
        retry_at = sensor._sample_aht(100.)
        self.assertEqual(retry_at, 130.)
        self.assertFalse(sensor.valid)
        self.assertFalse(sensor.communication_ok)
        self.assertFalse(sensor.init_sent)
        self.assertEqual(sensor.temp, valid_temperature)
        self.assertEqual(sensor.humidity, valid_humidity)
        self.assertEqual(sensor.i2c_error_count, 1)
        self.assertEqual(sensor.last_error["i2c_bus_status"], "NACK")
        self.assertEqual(self.printer.shutdowns, [])

        self.reactor.now = retry_at
        next_waketime = sensor._sample_aht(retry_at)
        self.assertTrue(sensor.valid)
        self.assertTrue(sensor.communication_ok)
        self.assertTrue(sensor.init_sent)
        self.assertGreater(next_waketime, retry_at)
        self.assertEqual(self.printer.shutdowns, [])

    def test_web_console_reports_failure_summary_and_recovery(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.core.I2CStatusError(raw_i2c, "START_NACK", "write",
                                         3, 0)

        sensor._record_failure("measurement", error)
        self.assertEqual(self.printer.gcode.responses, [
            "TCA9548A AHT chamber: I2C communication failed during "
            "measurement: MCU 'mcu' I2C request to addr 56 reports error "
            "START_NACK during write; retrying in 30s",
        ])

        for _ in range(119):
            sensor._record_failure("measurement", error)
        self.assertEqual(len(self.printer.gcode.responses), 1)

        sensor._record_failure("measurement", error)
        self.assertEqual(self.printer.gcode.responses[-1],
                         "TCA9548A AHT chamber: I2C communication still "
                         "failing; 120 repeated failed attempts since last "
                         "notice")
        self.assertEqual(len(self.printer.gcode.responses), 2)

        sensor.last_success_time = 1.
        sensor._record_success()
        self.assertEqual(self.printer.gcode.responses[-1],
                         "TCA9548A AHT chamber: I2C communication recovered")
        self.assertEqual(len(self.printer.gcode.responses), 3)
        self.assertIsNone(sensor._last_web_error_key)
        self.assertEqual(sensor._suppressed_web_errors, 0)


if __name__ == "__main__":
    unittest.main()
