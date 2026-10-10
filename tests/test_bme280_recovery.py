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
        return 0x76

    def get_command_queue(self):
        return None


class FakeLegacyI2C(FakeModernI2C):
    # Old Klipper and Kalico writes do not accept the retry keyword.
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
        self.unregistered_timers = []
        self._timer_id = 0

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        return waketime

    def register_timer(self, callback):
        self._timer_id += 1
        timer = (self._timer_id, callback)
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        self.unregistered_timers.append(timer)
        self.timers.remove(timer)

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
        values = {"tca9548a": "mux0", "sensor_type": "BME280_TCA9548A"}
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
    # T1=27504, T2=26435, T3=-1000; H1=117, H2=362, H3=0,
    # H4=819, H5=1, H6=0. Pressure calibration is deliberately unused.
    calibration = [0x70, 0x6B, 0x43, 0x67, 0x18, 0xFC] + [0] * 19 + [117]
    cal2 = [0x6A, 0x01, 0, 0x33, 0x13, 0, 0] + [0] * 9
    return [success([0x60]), success(), success([0]),
            success(calibration), success(cal2), success(), success(),
            success()]


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
    driver = _load_module("klippy.extras.tca9548a_drivers.bme280",
                          REPOSITORY / "tca9548a_drivers" / "bme280.py")
    return bus, core, driver


class BME280RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.driver = _load_driver_modules()
        self.reactor = FakeReactor()
        self.mux = FakeMux()
        self.printer = FakePrinter(self.reactor, self.mux)
        self.config = FakeConfig(self.printer)

    def _make_sensor(self, responses, stub_compensation=True,
                     i2c_type=FakeModernI2C):
        raw_i2c = i2c_type(responses)
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.driver.BME280TCA9548A(self.config)
        sensor.setup_minmax(-50., 100.)
        if stub_compensation:
            sensor._compensate_temp = lambda raw: 24.
            sensor._compensate_humidity = lambda raw: 45.
        published = []
        sensor.setup_callback(
            lambda print_time, temp: published.append((print_time, temp)))
        return sensor, published

    def test_failed_sample_retries_without_pressure_compensation(self):
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success([0] * 5),  # initial sample
            {"i2c_bus_status": "START_NACK", "response": []},
            *initialization_responses(),
            success([0] * 5),  # retry sample
        ])
        sensor.handle_connect()
        sensor.handle_ready()
        self.assertTrue(sensor.valid)
        self.assertEqual((sensor.temp, sensor.humidity), (24., 45.))

        self.reactor.now = 100.
        retry_at = sensor._sample_bme280(100.)

        self.assertEqual(retry_at, 160.)
        self.assertFalse(sensor.valid)
        self.assertFalse(sensor.communication_ok)
        self.assertEqual((sensor.temp, sensor.humidity), (24., 45.))
        self.assertEqual(sensor.last_error["i2c_bus_status"], "START_NACK")
        self.assertEqual(self.printer.gcode.raw_responses, [
            "!! TCA9548A BME280 chamber: measurement failed: START_NACK; "
            "retry in 60s",
        ])

        self.reactor.now = retry_at
        self.assertGreater(sensor._sample_bme280(retry_at), retry_at)
        self.assertTrue(sensor.valid)
        self.assertEqual(self.printer.gcode.responses, [
            "TCA9548A BME280 chamber: recovered",
        ])
        self.assertEqual(published, [(0.58, 24.), (160.58, 24.)])

    def test_error_value_options_zero_only_selected_values(self):
        self.mux.zero_temperature_on_error = True
        self.mux.zero_humidity_on_error = False
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success([0] * 5),
            {"i2c_bus_status": "NACK", "response": []},
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        sensor._sample_bme280(20.)

        self.assertEqual((sensor.temp, sensor.humidity), (0., 45.))
        self.assertEqual(published, [(0.58, 24.), (20., 0.)])

    def test_status_matches_aht_without_pressure(self):
        sensor, _ = self._make_sensor([
            *initialization_responses(),
            success([0] * 5),
        ])
        sensor.handle_connect()

        status = sensor.get_status(self.reactor.monotonic())

        self.assertEqual(status["temperature"], 24.)
        self.assertEqual(status["humidity"], 45.)
        self.assertEqual(status["consecutive_failure_count"], 0)
        self.assertFalse(status["sampling_stopped"])
        self.assertNotIn("pressure", status)

    def test_initialization_and_sampling_disable_host_retries(self):
        sensor, _ = self._make_sensor([
            *initialization_responses(),
            success([0] * 5),
        ])

        sensor.handle_connect()

        calls = sensor.i2c.i2c.i2c_transfer_cmd.calls
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(call[1]["retry"] is False for call in calls))

    def test_stopped_sensor_does_not_schedule_another_sample(self):
        sensor, _ = self._make_sensor([])
        sensor.consecutive_failure_count = 15

        self.assertEqual(sensor._sample_bme280(0.), self.reactor.NEVER)

    def test_toolchange_pause_skips_bme280_i2c(self):
        sensor, _ = self._make_sensor([])
        self.mux.pause_environment_sampling = True

        self.assertEqual(sensor._sample_bme280(100.), 160.)
        self.assertEqual(self.mux.session_calls, [])

    def test_printer_shutdown_stops_sampling_without_console_output(self):
        sensor, _ = self._make_sensor([])
        self.printer.shutdown_state = True

        self.assertEqual(sensor._sample_bme280(0.), self.reactor.NEVER)
        self.assertEqual(self.mux.session_calls, [])
        self.assertEqual(self.printer.gcode.responses, [])
        self.assertEqual(self.printer.gcode.raw_responses, [])

    def test_initialization_failure_retries_without_shutdown(self):
        sensor, published = self._make_sensor([
            {"i2c_bus_status": "NACK", "response": []},
            *initialization_responses(),
            success([0] * 5),
        ])

        sensor.handle_connect()

        self.assertFalse(sensor.valid)
        self.assertEqual(self.printer.shutdowns, [])
        self.assertEqual(self.reactor.updated_timer,
                         (sensor.sample_timer, 30.))
        self.reactor.now = 30.
        self.assertGreater(sensor._sample_bme280(30.), 30.)
        self.assertTrue(sensor.valid)
        self.assertEqual(published, [(30.58, 24.)])

    def test_reinitialization_reuses_single_sample_timer(self):
        sensor, _ = self._make_sensor(initialization_responses() * 2)
        timer = sensor.sample_timer
        with self.mux.session(close_on_exit=True):
            self.assertTrue(sensor._initialize_sensor())
            self.assertTrue(sensor._initialize_sensor())
        self.assertIs(sensor.sample_timer, timer)
        self.assertEqual(len(self.reactor.unregistered_timers), 0)
        self.assertEqual(self.reactor.timers.count(timer), 1)

    def test_malformed_sample_retries_without_losing_valid_values(self):
        sensor, published = self._make_sensor([
            *initialization_responses(),
            success([0] * 5),
            success([255]),
        ])
        sensor.handle_connect()

        self.reactor.now = 20.
        self.assertEqual(sensor._sample_bme280(20.), 80.)

        self.assertFalse(sensor.valid)
        self.assertEqual((sensor.temp, sensor.humidity), (24., 45.))
        self.assertEqual(sensor.last_error["type"], "BME280MeasurementError")
        self.assertEqual(published, [(0.58, 24.)])

    def test_real_calibration_and_compensation_on_both_protocols(self):
        for transport in (FakeModernI2C, FakeLegacyI2C):
            with self.subTest(transport=transport.__name__):
                self.reactor.now = 0.
                sensor, published = self._make_sensor([
                    *initialization_responses(),
                    # Bosch temperature example: raw T=519888; raw H=60000.
                    success([0x7E, 0xED, 0, 0xEA, 0x60]),
                ], stub_compensation=False, i2c_type=transport)
                sensor.handle_connect()
                self.assertTrue(sensor.valid)
                self.assertEqual(sensor.dig["T3"], -1000)
                self.assertEqual(sensor.dig["H4"], 819)
                self.assertEqual(sensor.dig["H5"], 1)
                self.assertFalse(any(k.startswith("P") for k in sensor.dig))
                self.assertAlmostEqual(sensor.temp, 25.08247793, places=6)
                self.assertGreater(sensor.humidity, 0.)
                self.assertLess(sensor.humidity, 100.)
                self.assertEqual(published, [(0.58, sensor.temp)])
                if transport is FakeLegacyI2C:
                    calls = sensor.i2c.i2c.calls
                    self.assertEqual(len(calls), 9)
                    self.assertTrue(all(retry is False for _, length, retry
                                        in calls if length))
                    self.assertFalse(sensor.get_status(0.)[
                        "i2c_status_supported"])
                else:
                    calls = sensor.i2c.i2c.i2c_transfer_cmd.calls
                    writes = [args[1] for args, _ in calls if not args[2]]
                    self.assertIn([0xF4, 0x43], writes)
                    self.assertEqual(calls[-1][0][1:], [[0xFA], 5])

    def test_nvm_busy_is_checked_once_then_retried_next_period(self):
        sensor, _ = self._make_sensor([
            success([0x60]), success(), success([1]),
            *initialization_responses(), success([0] * 5),
        ])
        sensor.handle_connect()
        self.assertEqual(len(sensor.i2c.i2c.i2c_transfer_cmd.calls), 3)
        self.assertFalse(sensor._initialized)
        self.assertEqual(sensor.consecutive_failure_count, 1)
        self.assertEqual(self.printer.shutdowns, [])
        self.reactor.now = 30.
        sensor._sample_bme280(30.)
        self.assertTrue(sensor.valid)
        self.assertEqual(sensor.consecutive_failure_count, 0)

    def test_unsupported_chip_and_malformed_calibration_are_recoverable(self):
        bad_calibration = initialization_responses()
        bad_calibration[3] = success([0])
        for responses in ([success([0x58])], bad_calibration):
            with self.subTest(responses=responses):
                sensor, published = self._make_sensor(responses)
                sensor.handle_connect()
                self.assertFalse(sensor._initialized)
                self.assertEqual(published, [])
                self.assertEqual(self.printer.shutdowns, [])

    def test_invalid_measurement_sentinel_preserves_last_values(self):
        sensor, published = self._make_sensor([
            *initialization_responses(), success([0] * 5),
            success([0x80, 0, 0, 0, 0]),
        ])
        sensor.handle_connect()
        self.reactor.now = 20.
        self.assertEqual(sensor._sample_bme280(20.), 80.)
        self.assertFalse(sensor.valid)
        self.assertEqual((sensor.temp, sensor.humidity), (24., 45.))
        self.assertEqual(len(published), 1)

    def test_driver_rejects_sensor_level_error_value_option(self):
        config = FakeConfig(self.printer, {"zero_humidity_on_error": True})

        with self.assertRaisesRegex(RuntimeError, "\\[tca9548a\\] mux section"):
            self.driver.BME280TCA9548A(config)


if __name__ == "__main__":
    unittest.main()
