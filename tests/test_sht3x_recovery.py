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

    def send(self, args, **kwargs):
        return self.responses.pop(0)


class FakeModernI2C:
    def __init__(self, responses):
        self.i2c_transfer_cmd = FakeTransferCommand(responses)
        self.mcu = FakeMCU()

    def get_oid(self):
        return 17

    def get_mcu(self):
        return self.mcu

    def get_i2c_address(self):
        return 0x44

    def get_command_queue(self):
        return None


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
        self.environment_report_time = 60
        self.zero_temperature_on_error = False
        self.zero_humidity_on_error = False
        self._in_session = False
        self.environment_sensors = []
        self.session_calls = []

    @contextlib.contextmanager
    def session(self, close_on_exit=False):
        self.session_calls.append(close_on_exit)
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
        self.raw_responses = []

    def respond_info(self, message):
        self.responses.append(message)

    def respond_raw(self, message):
        self.raw_responses.append(message)


class FakePrinter:
    def __init__(self, reactor, mux):
        self.reactor = reactor
        self.mux = mux
        self.gcode = FakeGCode()
        self.objects = {"gcode": self.gcode}
        self.events = []
        self.shutdowns = []
        self.shutdown_state = False

    def get_reactor(self):
        return self.reactor

    def is_shutdown(self):
        return self.shutdown_state

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
    def __init__(self, options):
        self.options = options

    def has_option(self, section, option):
        return option in self.options


class FakeConfig:
    def __init__(self, printer, options=None):
        self.printer = printer
        self.options = options or {}
        self.fileconfig = FakeFileConfig(self.options)

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "temperature_sensor chamber"

    def get(self, option, default=None):
        values = {"tca9548a": "mux0", "sensor_type": "SHT3X_TCA9548A"}
        return self.options.get(option, values.get(option, default))

    def getint(self, option, default=None, minval=None, maxval=None):
        values = {"tca9548a_channel": 1}
        return self.options.get(option, values.get(option, default))

    def getboolean(self, option, default=False):
        return self.options.get(option, default)

    def has_section(self, section):
        return section == "tca9548a mux0"

    def getsection(self, section):
        return self

    def error(self, message):
        return RuntimeError(message)


def success(response=None):
    return {"i2c_bus_status": "SUCCESS", "response": response or []}


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

    class NativeSHT3X:
        def __init__(self, config):
            self.printer = config.get_printer()
            self.name = config.get_name().split()[-1]
            self.reactor = self.printer.get_reactor()
            self.i2c = bus.MCU_I2C_from_config(config, default_addr=0x44,
                                                default_speed=100000)
            self.report_time = config.getint("sht3x_report_time", 1,
                                             minval=1)
            self.temp = self.humidity = 0.
            self.min_temp = self.max_temp = 0.
            self._callback = None
            self.sample_timer = self.reactor.register_timer(
                self._sample_sht3x)
            self.printer.register_event_handler("klippy:connect",
                                                self.handle_connect)

        def _init_sht3x(self):
            self.i2c.i2c_write([0x30, 0xA2])

        def _sample_sht3x(self, eventtime):
            try:
                params = self.i2c.i2c_read([0xE0, 0x00], 6, retry=False)
            except Exception:
                self.temp = self.humidity = 0.
                return self.reactor.NEVER
            if params["response"] == [255]:
                raise ValueError("malformed measurement")
            self.temp = 23.
            self.humidity = 44.
            self._callback(self.i2c.get_mcu().estimated_print_time(
                self.reactor.monotonic()), self.temp)
            return eventtime + self.report_time

        def setup_minmax(self, min_temp, max_temp):
            self.min_temp = min_temp
            self.max_temp = max_temp

        def setup_callback(self, callback):
            self._callback = callback

        def get_status(self, eventtime):
            return {"temperature": round(self.temp, 2),
                    "humidity": round(self.humidity, 1)}

    sht3x = types.ModuleType("klippy.extras.sht3x")
    sht3x.SHT3X = NativeSHT3X
    sys.modules[sht3x.__name__] = sht3x
    core = _load_module("klippy.extras.tca9548a", REPOSITORY / "tca9548a.py")
    _load_module("klippy.extras.tca9548a_drivers.recovery",
                 REPOSITORY / "tca9548a_drivers" / "recovery.py")
    driver = _load_module("klippy.extras.tca9548a_drivers.sht3x",
                          REPOSITORY / "tca9548a_drivers" / "sht3x.py")
    return bus, core, driver


class SHT3XRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.driver = _load_driver_modules()
        self.reactor = FakeReactor()
        self.mux = FakeMux()
        self.printer = FakePrinter(self.reactor, self.mux)
        self.config = FakeConfig(self.printer)

    def _make_sensor(self, responses):
        raw_i2c = FakeModernI2C(responses)
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.driver.SHT3XTCA9548A(self.config)
        sensor.setup_minmax(-50., 100.)
        published = []
        sensor.setup_callback(
            lambda print_time, temp: published.append((print_time, temp)))
        return sensor, published

    def test_failed_sample_retries_and_recovers(self):
        sensor, published = self._make_sensor([
            success(),
            success([0] * 6),
            {"i2c_bus_status": "START_NACK", "response": []},
            success(),
            success([0] * 6),
        ])
        sensor.handle_connect()
        sensor.handle_ready()
        self.assertTrue(sensor.valid)

        self.reactor.now = 100.
        self.assertEqual(sensor._sample_sht3x(100.), 160.)
        self.assertFalse(sensor.valid)
        self.assertEqual((sensor.temp, sensor.humidity), (23., 44.))
        self.assertEqual(self.printer.gcode.raw_responses, [
            "!! TCA9548A SHT3X chamber: measurement failed: START_NACK; "
            "retry in 60s",
        ])

        self.reactor.now = 160.
        self.assertGreater(sensor._sample_sht3x(160.), 160.)
        self.assertTrue(sensor.valid)
        self.assertEqual(self.printer.gcode.responses, [
            "TCA9548A SHT3X chamber: recovered",
        ])
        self.assertEqual(published, [(0., 23.), (160., 23.)])

    def test_error_value_options_zero_only_selected_values(self):
        self.mux.zero_temperature_on_error = False
        self.mux.zero_humidity_on_error = True
        sensor, published = self._make_sensor([
            success(),
            success([0] * 6),
            {"i2c_bus_status": "NACK", "response": []},
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        sensor._sample_sht3x(20.)

        self.assertEqual((sensor.temp, sensor.humidity), (23., 0.))
        self.assertEqual(published, [(0., 23.)])

    def test_initialization_failure_retries_without_shutdown(self):
        sensor, published = self._make_sensor([
            {"i2c_bus_status": "NACK", "response": []},
            success(),
            success([0] * 6),
        ])

        sensor.handle_connect()

        self.assertFalse(sensor.valid)
        self.assertEqual(self.printer.shutdowns, [])
        self.assertEqual(self.reactor.updated_timer,
                         (sensor.sample_timer, 30.))
        self.reactor.now = 30.
        self.assertGreater(sensor._sample_sht3x(30.), 30.)
        self.assertTrue(sensor.valid)
        self.assertEqual(published, [(30., 23.)])

    def test_malformed_sample_retries_without_losing_valid_values(self):
        sensor, published = self._make_sensor([
            success(),
            success([0] * 6),
            success([255]),
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        self.assertEqual(sensor._sample_sht3x(20.), 80.)

        self.assertFalse(sensor.valid)
        self.assertEqual((sensor.temp, sensor.humidity), (23., 44.))
        self.assertEqual(sensor.last_error["type"], "ValueError")
        self.assertEqual(published, [(0., 23.)])

    def test_stopped_sensor_does_not_schedule_another_sample(self):
        sensor, _ = self._make_sensor([])
        sensor.consecutive_failure_count = 15

        self.assertEqual(sensor._sample_sht3x(0.), self.reactor.NEVER)

    def test_printer_shutdown_stops_sampling_without_console_output(self):
        sensor, _ = self._make_sensor([])
        self.printer.shutdown_state = True

        self.assertEqual(sensor._sample_sht3x(0.), self.reactor.NEVER)
        self.assertEqual(self.mux.session_calls, [])
        self.assertEqual(self.printer.gcode.responses, [])
        self.assertEqual(self.printer.gcode.raw_responses, [])

    def test_driver_rejects_sensor_level_error_value_option(self):
        config = FakeConfig(self.printer, {"zero_temperature_on_error": True})

        with self.assertRaisesRegex(RuntimeError, "\\[tca9548a\\] mux section"):
            self.driver.SHT3XTCA9548A(config)


if __name__ == "__main__":
    unittest.main()
