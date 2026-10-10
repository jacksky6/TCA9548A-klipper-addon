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
        self.calls = []

    def send(self, args, **kwargs):
        self.calls.append((args, kwargs))
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


class FakeLegacyI2C(FakeModernI2C):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.mcu = FakeMCU()

    def i2c_write(self, data, minclock=0, reqclock=0):
        self.calls.append((list(data), 0, None))
        self.responses.pop(0)

    def i2c_read(self, write, read_len, retry=True):
        self.calls.append((list(write), read_len, retry))
        return self.responses.pop(0)


class FakeReactor:
    NEVER = -1.
    NOW = 0.

    def __init__(self):
        self.now = 0.
        self.updated_timer = None
        self.timers = []

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        return waketime

    def register_timer(self, callback):
        timer = (len(self.timers) + 1, callback)
        self.timers.append(timer)
        return timer

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
        self.pause_environment_sampling = False

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

    def should_pause_environment_sampling(self, eventtime):
        return self.pause_environment_sampling

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


def initialization_responses():
    # BREAK, soft reset, status (0x0000, CRC=0x81), enable periodic mode.
    return [success(), success(), success([0, 0, 0x81]), success()]


SHT_MEASUREMENT = [0x63, 0x79, 0x89, 0x70, 0xA3, 0x15]


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

    def _make_sensor(self, responses, i2c_type=FakeModernI2C):
        raw_i2c = i2c_type(responses)
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.driver.SHT3XTCA9548A(self.config)
        sensor.setup_minmax(-50., 100.)
        published = []
        sensor.setup_callback(
            lambda print_time, temp: published.append((print_time, temp)))
        return sensor, published

    def test_failed_sample_retries_and_recovers(self):
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success(SHT_MEASUREMENT),
            {"i2c_bus_status": "START_NACK", "response": []},
            *initialization_responses(),
            success(SHT_MEASUREMENT),
        ])
        sensor.handle_connect()
        sensor.handle_ready()
        self.assertTrue(sensor.valid)

        self.reactor.now = 100.
        self.assertEqual(sensor._sample_sht3x(100.), 160.)
        self.assertFalse(sensor.valid)
        self.assertAlmostEqual(sensor.temp, 23., places=2)
        self.assertAlmostEqual(sensor.humidity, 44., places=2)
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
        self.assertEqual(len(published), 2)
        for actual, expected in zip(published, [.0185, 160.0185]):
            self.assertAlmostEqual(actual[0], expected)
        self.assertTrue(all(abs(value - 23.) < .01
                            for ignored, value in published))

    def test_failed_fetch_is_attempted_once_per_period(self):
        sensor, _ = self._make_sensor([
            *initialization_responses(),
            success(SHT_MEASUREMENT),
            {"i2c_bus_status": "START_NACK", "response": []},
        ])
        sensor.handle_connect()
        raw_i2c = sensor.i2c.i2c

        self.reactor.now = 100.
        self.assertEqual(sensor._sample_sht3x(100.), 160.)

        self.assertEqual(len(raw_i2c.i2c_transfer_cmd.calls), 6)
        self.assertEqual(self.reactor.now, 100.)
        self.assertFalse(raw_i2c.i2c_transfer_cmd.calls[-1][1]["retry"])

    def test_error_value_options_zero_only_selected_values(self):
        self.mux.zero_temperature_on_error = False
        self.mux.zero_humidity_on_error = True
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success(SHT_MEASUREMENT),
            {"i2c_bus_status": "NACK", "response": []},
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        sensor._sample_sht3x(20.)

        self.assertAlmostEqual(sensor.temp, 23., places=2)
        self.assertEqual(sensor.humidity, 0.)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0][1], 23., places=2)

    def test_initialization_failure_retries_without_shutdown(self):
        sensor, published = self._make_sensor([
            {"i2c_bus_status": "NACK", "response": []},
            *initialization_responses(),
            success(SHT_MEASUREMENT),
        ])

        sensor.handle_connect()

        self.assertFalse(sensor.valid)
        self.assertEqual(self.printer.shutdowns, [])
        self.assertEqual(self.reactor.updated_timer,
                         (sensor.sample_timer, 30.))
        self.reactor.now = 30.
        self.assertGreater(sensor._sample_sht3x(30.), 30.)
        self.assertTrue(sensor.valid)
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0][0], 30.0185)
        self.assertAlmostEqual(published[0][1], 23., places=2)

    def test_malformed_sample_retries_without_losing_valid_values(self):
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success(SHT_MEASUREMENT),
            success([255]),
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        self.assertEqual(sensor._sample_sht3x(20.), 80.)

        self.assertFalse(sensor.valid)
        self.assertAlmostEqual(sensor.temp, 23., places=2)
        self.assertAlmostEqual(sensor.humidity, 44., places=2)
        self.assertEqual(sensor.last_error["type"], "SHT3XMeasurementError")
        self.assertEqual(len(published), 1)
        self.assertAlmostEqual(published[0][1], 23., places=2)

    def test_initialization_and_sampling_disable_host_retries(self):
        sensor, _ = self._make_sensor([
            *initialization_responses(),
            success(SHT_MEASUREMENT),
        ])

        sensor.handle_connect()

        calls = sensor.i2c.i2c.i2c_transfer_cmd.calls
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call[1]["retry"] is False for call in calls))

    def test_standalone_initialization_and_measurement_on_both_protocols(self):
        for transport in (FakeModernI2C, FakeLegacyI2C):
            with self.subTest(transport=transport.__name__):
                self.reactor.now = 0.
                sensor, published = self._make_sensor([
                    *initialization_responses(), success(SHT_MEASUREMENT),
                ], i2c_type=transport)
                sensor.handle_connect()
                self.assertTrue(sensor.valid)
                self.assertEqual(len(published), 1)
                self.assertAlmostEqual(published[0][0], .0185)
                self.assertAlmostEqual(sensor.temp, 23., places=2)
                self.assertAlmostEqual(sensor.humidity, 44., places=2)
                self.assertIs(self.printer.objects["sht3x chamber"], sensor)
                self.assertEqual(sensor.get_report_time_delta(), 60)
                self.assertEqual(sensor.get_status(0.)["humidity"], 44.)
                raw = sensor.i2c.i2c
                if transport is FakeModernI2C:
                    calls = [(args[1], args[2], kwargs["retry"])
                             for args, kwargs in raw.i2c_transfer_cmd.calls]
                else:
                    calls = raw.calls
                self.assertEqual([(command, length)
                                  for command, length, _ in calls], [
                    ([0x30, 0x93], 0), ([0x30, 0xA2], 0),
                    ([0xF3, 0x2D], 3), ([0x22, 0x36], 0),
                    ([0xE0, 0x00], 6),
                ])
                self.assertTrue(all(retry is False for _, length, retry
                                    in calls if length))
                self.assertEqual(sensor.get_status(0.)[
                    "i2c_status_supported"], transport is FakeModernI2C)

    def test_reinitialization_reuses_single_sample_timer(self):
        sensor, _ = self._make_sensor(initialization_responses() * 2)
        timer = sensor.sample_timer
        with self.mux.session(close_on_exit=True):
            self.assertTrue(sensor._initialize_sensor())
            self.assertTrue(sensor._initialize_sensor())
        self.assertIs(sensor.sample_timer, timer)
        self.assertEqual(self.reactor.timers.count(timer), 1)
        self.assertEqual(len(self.reactor.timers), 2)  # sample + web notice

    def test_status_crc_or_length_failure_retries_next_period(self):
        for bad_status in ([0, 0, 0], [0]):
            with self.subTest(status=bad_status):
                self.reactor.now = 0.
                sensor, published = self._make_sensor([
                    success(), success(), success(bad_status),
                    *initialization_responses(), success(SHT_MEASUREMENT),
                ])
                sensor.handle_connect()
                self.assertFalse(sensor._initialized)
                self.assertEqual(published, [])
                self.assertEqual(sensor.consecutive_failure_count, 1)
                self.assertEqual(self.printer.shutdowns, [])
                self.assertEqual(len(sensor.i2c.i2c.i2c_transfer_cmd.calls), 3)
                self.reactor.now = 30.
                sensor._sample_sht3x(30.)
                self.assertTrue(sensor.valid)
                self.assertEqual(sensor.consecutive_failure_count, 0)

    def test_bad_measurement_crc_preserves_both_previous_values(self):
        for crc_index in (2, 5):
            with self.subTest(crc_index=crc_index):
                corrupt = SHT_MEASUREMENT.copy()
                corrupt[crc_index] ^= 1
                sensor, published = self._make_sensor([
                    *initialization_responses(), success(SHT_MEASUREMENT),
                    success(corrupt),
                ])
                sensor.handle_connect()
                previous = sensor.temp, sensor.humidity
                self.reactor.now = 100.
                self.assertEqual(sensor._sample_sht3x(100.), 160.)
                self.assertEqual((sensor.temp, sensor.humidity), previous)
                self.assertEqual(len(published), 1)
                self.assertFalse(sensor._initialized)

    def test_crc_includes_leading_zero_and_datasheet_example(self):
        self.assertEqual(self.driver._sht3x_crc8(0x0000), 0x81)
        self.assertEqual(self.driver._sht3x_crc8(0xBEEF), 0x92)

    def test_stopped_sensor_does_not_schedule_another_sample(self):
        sensor, _ = self._make_sensor([])
        sensor.consecutive_failure_count = 15

        self.assertEqual(sensor._sample_sht3x(0.), self.reactor.NEVER)

    def test_toolchange_pause_skips_sht3x_i2c(self):
        sensor, _ = self._make_sensor([])
        self.mux.pause_environment_sampling = True

        self.assertEqual(sensor._sample_sht3x(100.), 160.)
        self.assertEqual(self.mux.session_calls, [])

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
