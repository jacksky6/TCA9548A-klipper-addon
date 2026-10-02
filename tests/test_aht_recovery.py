import contextlib
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


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
        self.session_close_on_exit = []

    @contextlib.contextmanager
    def session(self, close_on_exit=False):
        self.session_close_on_exit.append(close_on_exit)
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


class FakeMutex:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeResetPin:
    def __init__(self, mcu):
        self.mcu = mcu
        self.max_duration = None
        self.start_values = None
        self.digital_values = []

    def get_mcu(self):
        return self.mcu

    def setup_max_duration(self, max_duration):
        self.max_duration = max_duration

    def setup_start_value(self, start_value, shutdown_value):
        self.start_values = (start_value, shutdown_value)

    def set_digital(self, print_time, value):
        self.digital_values.append((print_time, value))


class FakeTcaGCode(FakeGCode):
    def __init__(self):
        super(FakeTcaGCode, self).__init__()
        self.commands = []

    def register_mux_command(self, command, key, value, callback, desc=None):
        self.commands.append((command, key, value, callback, desc))


class FakeTcaReactor(FakeReactor):
    def mutex(self):
        return FakeMutex()


class FakePins:
    def __init__(self, reset_pin):
        self.reset_pin = reset_pin
        self.requests = []

    def setup_pin(self, pin_type, pin_desc):
        self.requests.append((pin_type, pin_desc))
        return self.reset_pin


class FakeTcaPrinter:
    def __init__(self, reactor, pins, gcode):
        self.reactor = reactor
        self.objects = {"pins": pins, "gcode": gcode}
        self.events = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, callback):
        self.events.append((event, callback))


class FakeTcaFileConfig:
    def __init__(self, options):
        self.options = options

    def has_option(self, section, option):
        return option in self.options


class FakeTcaConfig:
    def __init__(self, printer, options=None):
        self.printer = printer
        self.options = options or {}
        self.fileconfig = FakeTcaFileConfig(self.options)

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "tca9548a mux0"

    def get(self, option, default=None):
        defaults = {"i2c_mcu": "mcu", "i2c_bus": "i2c1_PB6_PB7"}
        return self.options.get(option, defaults.get(option, default))

    def getint(self, option, default=None, minval=None, maxval=None):
        return self.options.get(option, default)

    def getfloat(self, option, default=None, minval=None, maxval=None,
                 above=None):
        return self.options.get(option, default)

    def getboolean(self, option, default=False):
        return self.options.get(option, default)

    def error(self, message):
        return RuntimeError(message)


class FakeGCmd:
    def __init__(self):
        self.responses = []

    def respond_info(self, message):
        self.responses.append(message)


class MuxSessionTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.aht = _load_driver_modules()
        self.mux = object.__new__(self.core.TCA9548A)
        self.mux.mutex = FakeMutex()
        self.mux.last_control = 0x04
        self.mux.last_channel = 2
        self.mux._session_owner = None
        self.mux._session_depth = 0
        self.write_control = mock.Mock(return_value=True)
        self.mux._write_control_locked = self.write_control

    def test_close_on_exit_disables_after_successful_operation(self):
        with self.mux.session(close_on_exit=True):
            self.assertTrue(self.mux.is_session_owner())

        self.assertEqual(self.write_control.call_args_list, [mock.call(0x00)])
        self.assertEqual(self.mux.last_control, 0)
        self.assertIsNone(self.mux.last_channel)
        self.assertIsNone(self.mux._session_owner)

    def test_close_on_exit_runs_when_operation_raises(self):
        with self.assertRaisesRegex(RuntimeError, "sensor failure"):
            with self.mux.session(close_on_exit=True):
                raise RuntimeError("sensor failure")

        self.assertEqual(self.write_control.call_args_list, [mock.call(0x00)])
        self.assertEqual(self.mux.last_control, 0)
        self.assertIsNone(self.mux.last_channel)

    def test_nested_session_waits_for_outer_exit_before_disabling(self):
        with self.mux.session(close_on_exit=True):
            with self.mux.session(close_on_exit=True):
                pass
            self.assertEqual(self.write_control.call_count, 0)

        self.assertEqual(self.write_control.call_args_list, [mock.call(0x00)])

    def test_default_session_preserves_existing_channel_selection(self):
        with self.mux.session():
            pass

        self.assertEqual(self.write_control.call_count, 0)
        self.assertEqual(self.mux.last_control, 0x04)
        self.assertEqual(self.mux.last_channel, 2)

    def test_failed_close_marks_channel_state_unknown(self):
        self.write_control.return_value = False

        with self.mux.session(close_on_exit=True):
            pass

        self.assertEqual(self.write_control.call_args_list, [mock.call(0x00)])
        self.assertIsNone(self.mux.last_control)
        self.assertIsNone(self.mux.last_channel)


class TcaResetTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.aht = _load_driver_modules()
        self.reactor = FakeTcaReactor()
        self.reset_pin = FakeResetPin(FakeMCU())

    def _make_mux(self, active_high=False, responses=None):
        mux = object.__new__(self.core.TCA9548A)
        mux.name = "mux0"
        mux.reactor = self.reactor
        mux.mutex = FakeMutex()
        mux.select_delay = 0.
        mux.verify_select = False
        mux.i2c = FakeModernI2C(responses or [success(), success([0])])
        mux.reset_pin = self.reset_pin
        mux.reset_active_high = active_high
        mux.reset_pulse_time = .010
        mux.reset_settle_time = .010
        mux.reset_recovery_cooldown = 30.
        mux.reset_count = 0
        mux.auto_reset_count = 0
        mux.last_reset_time = None
        mux.last_reset_result = None
        mux.last_auto_reset_time = None
        mux.last_control = 0x10
        mux.last_channel = 4
        mux._reported_i2c_failures = set()
        mux.gcode = FakeTcaGCode()
        return mux

    def test_direct_reset_pin_pulses_low_then_returns_high(self):
        mux = self._make_mux(active_high=False)

        self.assertTrue(mux.reset())

        self.assertEqual(self.reset_pin.digital_values, [
            (0., False), (.010, True),
        ])
        self.assertEqual(self.reactor.now, .020)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.last_reset_result, "verified")
        self.assertEqual(mux.last_control, 0)
        self.assertIsNone(mux.last_channel)

    def test_mos_reset_pin_pulses_high_then_returns_low(self):
        mux = self._make_mux(active_high=True)

        self.assertTrue(mux.reset())

        self.assertEqual(self.reset_pin.digital_values, [
            (0., True), (.010, False),
        ])
        self.assertEqual(mux.last_reset_result, "verified")

    def test_legacy_i2c_reset_skips_bus_verification(self):
        mux = self._make_mux()
        mux.i2c = FakeLegacyI2C()

        self.assertTrue(mux.reset())

        self.assertEqual(mux.last_reset_result,
                         "pulsed (not verified on legacy I2C)")
        self.assertEqual(mux.i2c.writes, [])
        self.assertIsNone(mux.last_control)
        self.assertIsNone(mux.last_channel)

    def test_manual_command_reports_unconfigured_reset_pin(self):
        mux = self._make_mux()
        mux.reset_pin = None
        gcmd = FakeGCmd()

        mux.cmd_TCA_RESET(gcmd)

        self.assertEqual(gcmd.responses, [
            "TCA9548A 'mux0': reset_pin is not configured",
        ])

    def test_reset_pin_setup_uses_release_value_for_start_and_shutdown(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        gcode = FakeTcaGCode()
        pins = FakePins(self.reset_pin)
        printer = FakeTcaPrinter(self.reactor, pins, gcode)
        config = FakeTcaConfig(printer, {
            "reset_pin": "EMU_1:PC12",
            "reset_active_high": True,
        })

        mux = self.core.TCA9548A(config)

        self.assertIs(mux.reset_pin, self.reset_pin)
        self.assertEqual(pins.requests, [("digital_out", "EMU_1:PC12")])
        self.assertEqual(self.reset_pin.max_duration, 0.)
        self.assertEqual(self.reset_pin.start_values, (False, False))
        self.assertIn("TCA_RESET", [command[0] for command in gcode.commands])

    def test_reset_pin_rejects_implicit_pin_inversion(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        gcode = FakeTcaGCode()
        pins = FakePins(self.reset_pin)
        printer = FakeTcaPrinter(self.reactor, pins, gcode)
        config = FakeTcaConfig(printer, {"reset_pin": "!EMU_1:PC12"})

        with self.assertRaisesRegex(RuntimeError, "reset_active_high"):
            self.core.TCA9548A(config)

    def test_tca_start_nack_automatically_resets_and_retries_once(self):
        mux = self._make_mux(responses=[
            {"i2c_bus_status": "START_NACK", "response": []},
            success(),
            success([0]),
            success(),
        ])

        self.assertTrue(mux._write_control_locked(0x04))

        self.assertEqual(self.reset_pin.digital_values, [
            (0., False), (.010, True),
        ])
        self.assertEqual(mux.auto_reset_count, 1)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.last_control, 0x04)
        self.assertEqual(mux.gcode.raw_responses, [
            "!! TCA9548A mux0: START_NACK; hardware reset",
        ])
        self.assertEqual(mux.gcode.responses, [
            "TCA9548A mux0: reset verified; retrying",
        ])

    def test_tca_control_read_error_resets_and_retries_once(self):
        mux = self._make_mux(responses=[
            {"i2c_bus_status": "NACK", "response": []},
            success(),
            success([0]),
            success([0]),
        ])

        self.assertEqual(mux._read_control_locked(), 0)

        self.assertEqual(mux.auto_reset_count, 1)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.gcode.raw_responses, [
            "!! TCA9548A mux0: NACK; hardware reset",
        ])

    def test_select_verification_error_reselects_after_hardware_reset(self):
        mux = self._make_mux(responses=[
            success(),
            {"i2c_bus_status": "BUS_TIMEOUT", "response": []},
            success(),
            success([0]),
            success([0]),
            success(),
            success([4]),
        ])
        mux.verify_select = True

        self.assertTrue(mux._write_control_locked(0x04))

        self.assertEqual(mux.auto_reset_count, 1)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.last_control, 0x04)
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 7)

    def test_tca_reset_cooldown_prevents_repeated_hardware_pulses(self):
        mux = self._make_mux()
        mux.last_auto_reset_time = 0.
        self.reactor.now = 1.
        error = self.core.I2CStatusError(mux.i2c, "BUS_TIMEOUT",
                                         "TCA9548A control write", 1, 0)

        self.assertFalse(mux._attempt_auto_reset_locked(error))

        self.assertEqual(self.reset_pin.digital_values, [])
        self.assertEqual(mux.auto_reset_count, 0)
        self.assertEqual(mux.gcode.raw_responses, [])


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

    def test_aht_sample_requests_channel_isolation_on_session_exit(self):
        raw_i2c = FakeModernI2C([
            success(),             # AHT2x initialization command
            success(),             # initial measurement command
            success(MEASUREMENT),  # initial measurement read
        ])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        sensor.setup_minmax(-50., 100.)

        sensor._sample_aht(0.)

        self.assertEqual(self.mux.session_close_on_exit, [True])

    def test_web_console_reports_failure_summary_and_recovery(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        sensor.handle_ready()
        error = self.core.I2CStatusError(raw_i2c, "START_NACK", "write",
                                         3, 0)

        sensor._record_failure("measurement", error)
        self.assertEqual(self.printer.gcode.raw_responses, [
            "!! TCA9548A AHT chamber: START_NACK; retry 30s",
        ])
        self.assertEqual(self.printer.gcode.responses, [])

        for _ in range(119):
            sensor._record_failure("measurement", error)
        self.assertEqual(len(self.printer.gcode.raw_responses), 1)

        sensor._record_failure("measurement", error)
        self.assertEqual(self.printer.gcode.raw_responses[-1],
                         "!! TCA9548A AHT chamber: still failing "
                         "(120 attempts)")
        self.assertEqual(len(self.printer.gcode.raw_responses), 2)

        sensor.last_success_time = 1.
        sensor._record_success()
        self.assertEqual(self.printer.gcode.responses[-1],
                         "TCA9548A AHT chamber: recovered")
        self.assertEqual(len(self.printer.gcode.responses), 1)
        self.assertIsNone(sensor._last_web_error_key)
        self.assertEqual(sensor._suppressed_web_errors, 0)

    def test_startup_failure_is_reported_after_klippy_ready(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.core.I2CStatusError(raw_i2c, "START_NACK", "write",
                                         3, 0)

        sensor._record_failure("initialization", error)
        self.assertEqual(self.printer.gcode.responses, [])
        self.assertEqual(self.printer.gcode.raw_responses, [])
        self.assertIsNotNone(sensor._pending_web_notification)

        sensor.handle_ready()
        self.assertEqual(self.reactor.updated_timer, (
            sensor._pending_web_notification_timer, 1.))
        self.assertEqual(sensor._emit_pending_web_notification(1.),
                         self.reactor.NEVER)
        self.assertEqual(self.printer.gcode.raw_responses, [
            "!! TCA9548A AHT chamber: START_NACK; retry 30s",
        ])
        self.assertEqual(sensor._last_web_error_key, (
            "initialization", "START_NACK", "I2CStatusError",
            "MCU 'mcu' I2C request to addr 56 reports error START_NACK "
            "during write"))

    def test_startup_recovery_cancels_pending_console_failure(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.aht.AHTMeasurementError("test failure")

        sensor._record_failure("initialization", error)
        sensor._record_success()
        sensor.handle_ready()
        sensor._emit_pending_web_notification(1.)

        self.assertEqual(self.printer.gcode.responses, [])
        self.assertIsNone(sensor._pending_web_notification)

    def test_log_summary_uses_failure_count_and_retry_interval(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.aht.AHTMeasurementError("test failure")

        with mock.patch.object(self.aht.logging, "warning") as warning:
            sensor._record_failure("measurement", error)
            for _ in range(9):
                sensor._record_failure("measurement", error)
            self.assertEqual(warning.call_count, 1)

            sensor._record_failure("measurement", error)

        self.assertEqual(warning.call_count, 2)
        self.assertEqual(warning.call_args_list[-1][0], (
            "%s %s: communication failure persists; "
            "%d repeated failure(s) over %ds",
            sensor.model, sensor.name, 10, 300))
        self.assertEqual(sensor._suppressed_log_errors, 0)


if __name__ == "__main__":
    unittest.main()
