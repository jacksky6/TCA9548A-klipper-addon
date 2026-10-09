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
    _load_module("klippy.extras.tca9548a_drivers.recovery",
                 REPOSITORY / "tca9548a_drivers" / "recovery.py")
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

    def min_schedule_time(self):
        return .100


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
        self.environment_report_time = 60
        self.zero_temperature_on_error = False
        self.zero_humidity_on_error = False
        self._in_session = False
        self.environment_sensors = []
        self.session_close_on_exit = []
        self.pause_environment_sampling = False

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
    def __init__(self, options=None):
        self.options = options or {}

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
        values = {"tca9548a": "mux0", "sensor_type": "AHT2X_TCA9548A"}
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
        self.assertFalse(raw_i2c.i2c_transfer_cmd.calls[0][1]["retry"])

    def test_missing_i2c_response_is_returned_as_recoverable_error(self):
        raw_i2c = FakeModernI2C([
            RuntimeError("Unable to obtain 'i2c_response' response"),
        ])

        with self.assertRaises(self.core.I2CResponseError) as raised:
            self.core.i2c_transfer_recoverable(raw_i2c, [0xAC],
                                                operation="measurement")

        self.assertEqual(raised.exception.status, "NO_RESPONSE")
        self.assertIn("did not return i2c_response", str(raised.exception))
        self.assertEqual(len(raw_i2c.i2c_transfer_cmd.calls), 1)
        self.assertFalse(raw_i2c.i2c_transfer_cmd.calls[0][1]["retry"])

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
        self.shutdown_state = False

    def get_reactor(self):
        return self.reactor

    def is_shutdown(self):
        return self.shutdown_state

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
        defaults = {"i2c_mcu": "EMU_1", "i2c_bus": "i2c1_PB6_PB7"}
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


class FakeStatusObject:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def get_status(self, eventtime):
        self.calls.append(eventtime)
        return dict(self.status)


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
        mux.i2c = FakeModernI2C(responses or [success([0])])
        mux.reset_pin = self.reset_pin
        mux.reset_active_high = active_high
        mux.reset_pulse_time = .010
        mux.reset_settle_time = .010
        mux.reset_recovery_cooldown = 30.
        mux.environment_report_time = 60
        mux.reset_count = 0
        mux.auto_reset_count = 0
        mux.auto_reset_failure_count = 0
        mux.last_reset_time = None
        mux.last_reset_result = None
        mux.last_auto_reset_time = None
        mux._i2c_pause_until = None
        mux.last_control = 0x10
        mux.last_channel = 4
        mux._reported_i2c_failures = set()
        mux.gcode = FakeTcaGCode()
        return mux

    def test_direct_reset_pin_pulses_low_then_returns_high(self):
        mux = self._make_mux(active_high=False)

        self.assertTrue(mux.reset())

        self.assertEqual(self.reset_pin.digital_values, [
            (.100, False), (.110, True),
        ])
        self.assertEqual(self.reactor.now, .120)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.last_reset_result, "verified")
        self.assertEqual(mux.last_control, 0)
        self.assertIsNone(mux.last_channel)

    def test_mos_reset_pin_pulses_high_then_returns_low(self):
        mux = self._make_mux(active_high=True)

        self.assertTrue(mux.reset())

        self.assertEqual(self.reset_pin.digital_values, [
            (.100, True), (.110, False),
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

    def test_reset_pin_defaults_to_active_high_for_start_and_shutdown(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        gcode = FakeTcaGCode()
        pins = FakePins(self.reset_pin)
        printer = FakeTcaPrinter(self.reactor, pins, gcode)
        config = FakeTcaConfig(printer, {
            "reset_pin": "EMU_1:PC12",
        })

        mux = self.core.TCA9548A(config)

        self.assertIs(mux.reset_pin, self.reset_pin)
        self.assertEqual(mux.environment_report_time, 60)
        self.assertFalse(mux.zero_temperature_on_error)
        self.assertFalse(mux.zero_humidity_on_error)
        self.assertTrue(mux.reset_active_high)
        self.assertEqual(pins.requests, [("digital_out", "EMU_1:PC12")])
        self.assertEqual(self.reset_pin.max_duration, 0.)
        self.assertEqual(self.reset_pin.start_values, (False, False))
        self.assertIn("TCA_RESET", [command[0] for command in gcode.commands])

    def test_error_value_options_are_read_from_mux_config(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        gcode = FakeTcaGCode()
        pins = FakePins(self.reset_pin)
        printer = FakeTcaPrinter(self.reactor, pins, gcode)
        config = FakeTcaConfig(printer, {
            "zero_temperature_on_error": True,
            "zero_humidity_on_error": False,
        })

        mux = self.core.TCA9548A(config)

        self.assertTrue(mux.zero_temperature_on_error)
        self.assertFalse(mux.zero_humidity_on_error)

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
            success([0]),
            success(),
        ])

        self.assertTrue(mux._write_control_locked(0x04))

        self.assertEqual(self.reset_pin.digital_values, [
            (.100, False), (.110, True),
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

    def test_tca_no_response_automatically_resets_and_retries_once(self):
        mux = self._make_mux(responses=[
            RuntimeError("Unable to obtain 'i2c_response' response"),
            success([0]),
            success(),
        ])

        self.assertTrue(mux._write_control_locked(0x04))

        self.assertEqual(mux.auto_reset_count, 1)
        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.last_control, 0x04)
        self.assertEqual(mux.gcode.raw_responses, [
            "!! TCA9548A mux0: NO_RESPONSE; hardware reset",
        ])

    def test_no_response_during_reset_verification_pauses_without_escaping(self):
        mux = self._make_mux(responses=[
            RuntimeError("Unable to obtain 'i2c_response' response"),
            RuntimeError("Unable to obtain 'i2c_response' response"),
        ])

        self.assertFalse(mux._write_control_locked(0x04))

        self.assertEqual(mux.reset_count, 1)
        self.assertEqual(mux.auto_reset_failure_count, 1)
        self.assertEqual(mux.last_reset_result,
                         "verification failed: NO_RESPONSE")
        self.assertAlmostEqual(mux._get_i2c_pause_remaining(), 60.)
        self.assertIsNone(mux.last_control)
        self.assertIsNone(mux.last_channel)

    def test_no_response_during_session_cleanup_does_not_escape(self):
        mux = self._make_mux(responses=[
            RuntimeError("Unable to obtain 'i2c_response' response"),
            RuntimeError("Unable to obtain 'i2c_response' response"),
        ])
        mux._session_owner = None
        mux._session_depth = 0

        with mux.session(close_on_exit=True):
            pass

        self.assertEqual(mux.reset_count, 1)
        self.assertIsNone(mux.last_control)
        self.assertIsNone(mux.last_channel)

    def test_tca_control_read_error_resets_and_retries_once(self):
        mux = self._make_mux(responses=[
            {"i2c_bus_status": "NACK", "response": []},
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
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 6)

    def test_reset_cooldown_prevents_repeated_hardware_pulses(self):
        mux = self._make_mux()
        mux.last_reset_time = 0.
        self.reactor.now = 1.
        error = self.core.I2CStatusError(mux.i2c, "BUS_TIMEOUT",
                                         "TCA9548A control write", 1, 0)

        self.assertFalse(mux._attempt_auto_reset_locked(error))

        self.assertEqual(self.reset_pin.digital_values, [])
        self.assertEqual(mux.auto_reset_count, 0)
        self.assertEqual(mux.gcode.raw_responses, [])

    def test_failed_reset_pauses_control_transfers_for_report_interval(self):
        mux = self._make_mux(responses=[
            {"i2c_bus_status": "BUS_TIMEOUT", "response": []},
            {"i2c_bus_status": "BUS_TIMEOUT", "response": []},
            success(),
        ])

        self.assertFalse(mux._write_control_locked(0x04))
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 2)
        self.assertEqual(mux.last_reset_result,
                         "verification failed: BUS_TIMEOUT")
        self.assertAlmostEqual(mux._get_i2c_pause_remaining(), 60.)

        self.assertFalse(mux._write_control_locked(0x04))
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 2)

        self.reactor.now = 60.2
        self.assertTrue(mux._write_control_locked(0x04))
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 3)

    def test_five_failed_automatic_resets_stop_i2c_attempts(self):
        responses = []
        for ignored in range(5):
            responses.extend([
                {"i2c_bus_status": "BUS_TIMEOUT", "response": []},
                {"i2c_bus_status": "START_READ_NACK", "response": []},
            ])
        mux = self._make_mux(responses=responses)

        for attempt in range(5):
            self.assertFalse(mux._write_control_locked(0x04))
            if attempt != 4:
                self.reactor.now = mux._i2c_pause_until

        self.assertEqual(mux.auto_reset_failure_count, 5)
        self.assertTrue(mux._automatic_recovery_stopped())
        self.assertEqual(mux.reset_count, 5)
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 10)
        self.assertEqual(mux.gcode.raw_responses[-1],
                         "!! TCA9548A mux0: automatic recovery stopped "
                         "after 5 failed resets (START_READ_NACK). "
                         "Check RESET# wiring and TCA "
                         "power; repair, then run TCA_RESET or power-cycle.")

        self.assertFalse(mux._write_control_locked(0x04))
        self.assertEqual(len(mux.i2c.i2c_transfer_cmd.calls), 10)

    def test_verified_manual_reset_restores_stopped_automatic_recovery(self):
        mux = self._make_mux(responses=[success([0])])
        mux.auto_reset_failure_count = 5
        mux._i2c_pause_until = 60.

        self.assertTrue(mux.reset())

        self.assertEqual(mux.auto_reset_failure_count, 0)
        self.assertFalse(mux._automatic_recovery_stopped())
        self.assertEqual(mux._get_i2c_pause_remaining(), 0.)

    def test_manual_reset_prevents_immediate_automatic_reset(self):
        mux = self._make_mux()
        error = self.core.I2CStatusError(mux.i2c, "BUS_TIMEOUT",
                                         "TCA9548A control write", 1, 0)

        self.assertTrue(mux.reset())
        self.assertFalse(mux._attempt_auto_reset_locked(error))

        self.assertEqual(mux.auto_reset_count, 0)
        self.assertEqual(self.reset_pin.digital_values, [
            (.100, False), (.110, True),
        ])

    def test_manual_reset_reports_pulse_and_verification_separately(self):
        mux = self._make_mux(responses=[
            {"i2c_bus_status": "BUS_TIMEOUT", "response": []},
        ])
        gcmd = FakeGCmd()

        mux.cmd_TCA_RESET(gcmd)

        self.assertEqual(gcmd.responses, [
            "TCA9548A 'mux0': reset pulse sent; verification failed: "
            "BUS_TIMEOUT; I2C paused for 60s",
        ])


class ToolchangePauseTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.core, self.aht = _load_driver_modules()
        self.reactor = FakeTcaReactor()
        self.reset_pin = FakeResetPin(FakeMCU())
        self.gcode = FakeTcaGCode()
        self.pins = FakePins(self.reset_pin)
        self.printer = FakeTcaPrinter(self.reactor, self.pins, self.gcode)
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: FakeModernI2C([])

    def _make_mux(self, enabled=True):
        return self.core.TCA9548A(FakeTcaConfig(self.printer, {
            "pause_env_on_toolchange": enabled,
        }))

    def test_disabled_by_default_does_not_register_detection_timer(self):
        mux = self._make_mux(enabled=False)

        self.assertIsNone(mux._toolchange_detection_timer)
        self.assertNotIn("klippy:ready", [event for event, ignored
                                           in self.printer.events])

    def test_ready_detection_logs_sources_and_pauses_only_active_prints(self):
        print_stats = FakeStatusObject({"state": "printing"})
        afc = FakeStatusObject({"current_state": "Idle"})
        mmu = FakeStatusObject({"action": "Idle"})
        self.printer.objects.update({
            "print_stats": print_stats,
            "AFC": afc,
            "mmu": mmu,
        })
        mux = self._make_mux()
        ready_callback = next(callback for event, callback
                              in self.printer.events if event == "klippy:ready")

        ready_callback()

        self.assertEqual(self.reactor.updated_timer, (
            mux._toolchange_detection_timer, 30.))
        with self.assertLogs(level="INFO") as logged:
            self.assertEqual(mux._detect_toolchange_sources(30.),
                             self.reactor.NEVER)
        self.assertIn(
            "TCA9548A 'mux0': pause_env_on_toolchange enabled; "
            "detected AFC, Happy Hare", "\n".join(logged.output))
        self.assertFalse(mux.should_pause_environment_sampling(31.))

        afc.status["current_state"] = "Unloading"
        self.assertTrue(mux.should_pause_environment_sampling(32.))
        afc.status["current_state"] = "Idle"
        mmu.status["action"] = "Purging"
        self.assertTrue(mux.should_pause_environment_sampling(33.))

        print_stats.status["state"] = "paused"
        self.assertFalse(mux.should_pause_environment_sampling(34.))

    def test_missing_status_field_keeps_sampling_enabled(self):
        self.printer.objects.update({
            "print_stats": FakeStatusObject({"state": "printing"}),
            "AFC": FakeStatusObject({}),
        })
        mux = self._make_mux()

        mux._detect_toolchange_sources(30.)

        self.assertFalse(mux.should_pause_environment_sampling(31.))

    def test_detection_does_nothing_after_shutdown(self):
        self.printer.shutdown_state = True
        mux = self._make_mux()

        self.assertEqual(mux._detect_toolchange_sources(30.),
                         self.reactor.NEVER)
        self.assertIsNone(mux._print_stats)
        self.assertIsNone(mux._afc)
        self.assertIsNone(mux._mmu)


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
        self.assertEqual(retry_at, 160.)
        self.assertFalse(sensor.valid)
        self.assertFalse(sensor.communication_ok)
        self.assertFalse(sensor.init_sent)
        self.assertEqual(sensor.temp, valid_temperature)
        self.assertEqual(sensor.humidity, valid_humidity)
        self.assertEqual(sensor.i2c_error_count, 1)
        self.assertEqual(sensor.consecutive_failure_count, 1)
        self.assertEqual(sensor.last_error["i2c_bus_status"], "NACK")
        self.assertEqual(self.printer.shutdowns, [])

        self.reactor.now = retry_at
        next_waketime = sensor._sample_aht(retry_at)
        self.assertTrue(sensor.valid)
        self.assertTrue(sensor.communication_ok)
        self.assertTrue(sensor.init_sent)
        self.assertEqual(sensor.consecutive_failure_count, 0)
        self.assertGreater(next_waketime, retry_at)
        self.assertEqual(self.printer.shutdowns, [])

    def test_missing_i2c_response_is_recorded_as_sensor_failure(self):
        raw_i2c = FakeModernI2C([
            RuntimeError("Unable to obtain 'i2c_response' response"),
        ])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)

        with self.mux.session():
            self.assertFalse(sensor._initialize_sensor())

        self.assertFalse(sensor.valid)
        self.assertFalse(sensor.communication_ok)
        self.assertEqual(sensor.i2c_error_count, 1)
        self.assertEqual(sensor.consecutive_failure_count, 1)
        self.assertEqual(sensor.last_error["i2c_bus_status"], "NO_RESPONSE")
        self.assertEqual(sensor.last_error["type"], "I2CResponseError")
        self.assertEqual(self.printer.shutdowns, [])

    def test_busy_measurement_is_skipped_without_repeating_or_soft_reset(self):
        raw_i2c = FakeModernI2C([
            success(),                         # measurement command
            success([0x80, 0, 0, 0, 0, 0]),   # busy response
        ])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        sensor.init_sent = True

        with self.mux.session():
            self.assertFalse(sensor._make_measurement("measurement"))

        self.assertEqual(len(raw_i2c.i2c_transfer_cmd.calls), 2)
        self.assertEqual(self.reactor.now, .110)
        self.assertEqual(sensor.last_error["message"],
                         "device remained busy after measurement")
        self.assertTrue(all(
            call[1]["retry"] is False
            for call in raw_i2c.i2c_transfer_cmd.calls))

    def test_failed_sample_can_zero_values_without_range_shutdown(self):
        raw_i2c = FakeModernI2C([
            success(),             # AHT2x initialization command
            success(),             # initial measurement command
            success(MEASUREMENT),  # initial measurement read
            {"i2c_bus_status": "NACK", "response": []},
        ])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        self.mux.zero_temperature_on_error = True
        self.mux.zero_humidity_on_error = True
        sensor = self.aht.AHT2x(self.config)
        sensor.setup_minmax(10., 90.)
        published_temperatures = []
        sensor.setup_callback(
            lambda print_time, temp: published_temperatures.append(
                (print_time, temp)))

        with self.mux.session():
            self.assertTrue(sensor._initialize_sensor())

        self.reactor.now = 100.
        retry_at = sensor._sample_aht(100.)

        self.assertEqual(retry_at, 160.)
        self.assertFalse(sensor.valid)
        self.assertEqual(sensor.temp, 0.)
        self.assertEqual(sensor.humidity, 0.)
        self.assertEqual(published_temperatures, [(100., 0.)])
        self.assertEqual(self.printer.shutdowns, [])

    def test_failure_value_options_are_independent(self):
        cases = (
            (False, False, 24., 45., []),
            (True, False, 0., 45., [(0., 0.)]),
            (False, True, 24., 0., []),
            (True, True, 0., 0., [(0., 0.)]),
        )
        error = self.aht.AHTMeasurementError("test failure")

        for zero_temperature, zero_humidity, temp, humidity, published in cases:
            raw_i2c = FakeModernI2C([])
            self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
            self.mux.zero_temperature_on_error = zero_temperature
            self.mux.zero_humidity_on_error = zero_humidity
            sensor = self.aht.AHT2x(self.config)
            sensor.temp = 24.
            sensor.humidity = 45.
            published_temperatures = []
            sensor.setup_callback(
                lambda print_time, value: published_temperatures.append(
                    (print_time, value)))

            sensor._record_failure("measurement", error)

            self.assertEqual(sensor.temp, temp)
            self.assertEqual(sensor.humidity, humidity)
            self.assertEqual(published_temperatures, published)

    def test_error_value_options_must_be_set_on_mux(self):
        for option in ("zero_temperature_on_error",
                       "zero_humidity_on_error"):
            config = FakeConfig(self.printer, {option: True})

            with self.assertRaisesRegex(RuntimeError,
                                        "\\[tca9548a\\] mux section"):
                self.aht.AHT2x(config)

    def test_aht10_report_time_uses_shared_interval_error(self):
        config = FakeConfig(self.printer, {"aht10_report_time": 30})

        with self.assertRaisesRegex(RuntimeError, "environment_report_time"):
            self.aht.AHT2x(config)

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

    def test_toolchange_pause_skips_aht_i2c(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        self.mux.pause_environment_sampling = True
        sensor = self.aht.AHT2x(self.config)

        self.assertEqual(sensor._sample_aht(100.), 160.)
        self.assertEqual(raw_i2c.i2c_transfer_cmd.calls, [])
        self.assertEqual(self.mux.session_close_on_exit, [])

    def test_printer_shutdown_stops_sampling_without_i2c_or_console(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        self.printer.shutdown_state = True

        with mock.patch.object(self.aht.logging, "info") as logged_info:
            self.assertEqual(sensor._sample_aht(100.), self.reactor.NEVER)

        logged_info.assert_called_once_with(
            "%s %s: sampling stopped because Klipper is shutdown",
            "aht2x_tca9548a", "chamber")
        self.assertEqual(raw_i2c.i2c_transfer_cmd.calls, [])
        self.assertEqual(self.mux.session_close_on_exit, [])
        self.assertEqual(self.printer.gcode.responses, [])
        self.assertEqual(self.printer.gcode.raw_responses, [])
        status = sensor.get_status(100.)
        self.assertFalse(status["valid"])
        self.assertFalse(status["communication_ok"])
        self.assertTrue(status["sampling_stopped"])

    def test_fifteen_failures_stop_sensor_sampling(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        sensor.handle_ready()
        error = self.core.I2CStatusError(raw_i2c, "START_NACK", "write",
                                         3, 0)

        with mock.patch.object(self.aht.logging, "error") as logged_error:
            for ignored in range(15):
                sensor._record_failure("measurement", error)

        logged_error.assert_called_once_with(
            "TCA9548A AHT chamber: sampling stopped after 15 failures "
            "(START_NACK). Check wiring and sensor; restart Klipper after "
            "repair.")
        self.assertEqual(self.printer.gcode.raw_responses, [
            "!! TCA9548A AHT chamber: measurement failed: START_NACK; "
            "retry in 60s",
            "!! TCA9548A AHT chamber: sampling stopped after 15 failures "
            "(START_NACK). Check wiring and sensor; restart Klipper after "
            "repair.",
        ])
        self.assertEqual(sensor.consecutive_failure_count, 15)
        self.assertTrue(sensor._sampling_stopped())
        self.assertEqual(sensor._sample_aht(900.), self.reactor.NEVER)
        self.assertEqual(self.mux.session_close_on_exit, [])

        sensor._record_success()
        self.assertEqual(sensor.consecutive_failure_count, 15)
        self.assertEqual(self.printer.gcode.responses, [])

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
            "!! TCA9548A AHT chamber: initialization failed: START_NACK; "
            "retry in 60s",
        ])
        self.assertEqual(sensor._last_web_error_key, (
            "initialization", "START_NACK", "I2CStatusError",
            "MCU 'mcu' I2C request to addr 56 reports error START_NACK "
            "during write"))

    def test_shutdown_discards_pending_console_failure(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.core.I2CStatusError(raw_i2c, "START_NACK", "write",
                                         3, 0)

        sensor._record_failure("initialization", error)
        self.printer.shutdown_state = True
        sensor.handle_ready()

        self.assertEqual(sensor._emit_pending_web_notification(1.),
                         self.reactor.NEVER)
        self.assertEqual(self.printer.gcode.responses, [])
        self.assertEqual(self.printer.gcode.raw_responses, [])
        self.assertIsNone(sensor._pending_web_notification)
        self.assertTrue(sensor._sampling_stopped())

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

    def test_log_summary_uses_five_failure_groups(self):
        raw_i2c = FakeModernI2C([])
        self.bus.MCU_I2C_from_config = lambda *args, **kwargs: raw_i2c
        sensor = self.aht.AHT2x(self.config)
        error = self.aht.AHTMeasurementError("test failure")

        with mock.patch.object(self.aht.logging, "warning") as warning:
            sensor._record_failure("measurement", error)
            for _ in range(3):
                sensor._record_failure("measurement", error)
            self.assertEqual(warning.call_count, 1)

            sensor._record_failure("measurement", error)
            self.assertEqual(warning.call_count, 2)

            for _ in range(5):
                sensor._record_failure("measurement", error)

        self.assertEqual(warning.call_count, 3)
        self.assertEqual(warning.call_args_list[-1][0], (
            "%s %s: communication failure persists; "
            "%d consecutive failure(s); retrying in %ds",
            sensor.model, sensor.name, 10, 60))


if __name__ == "__main__":
    unittest.main()
