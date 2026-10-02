"""Shared recovery state for TCA9548A environment sensor drivers."""

import logging

from .. import tca9548a


WEB_CONSOLE_READY_DELAY = 1.
# Log ongoing failures at 5 and 10; stop the sensor after the third group.
MAX_CONSECUTIVE_ENVIRONMENT_FAILURES = (
    3 * tca9548a.RECOVERY_FAILURE_GROUP_SIZE)

RECOVERY_STATUS_FIELDS = (
    "valid",
    "communication_ok",
    "last_error",
    "last_error_time",
    "last_success_time",
    "i2c_error_count",
    "error_count",
    "consecutive_failure_count",
    "sampling_stopped",
    "i2c_status_supported",
)


class EnvironmentRecoveryMixin:
    """Add retry state, Console notices, and error-value policy to a sensor.

    Implementations provide the measurement and initialization flow. This mixin
    only owns the common observable recovery behavior after a driver catches a
    transport or sensor error.
    """

    recovery_sensor_type = None

    def _init_environment_recovery(self):
        self.valid = False
        self.communication_ok = False
        self.last_error = None
        self.last_error_time = None
        self.last_success_time = None
        self.i2c_error_count = 0
        self.error_count = 0
        self.consecutive_failure_count = 0
        self._last_log_error_key = None
        self._last_web_error_key = None
        self._klippy_ready = False
        self._pending_web_notification = None
        self._status_patched = False
        self._pending_web_notification_timer = self.reactor.register_timer(
            self._emit_pending_web_notification)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

    def handle_ready(self):
        self._klippy_ready = True
        if self._pending_web_notification is not None:
            self.reactor.update_timer(
                self._pending_web_notification_timer,
                self.reactor.monotonic() + WEB_CONSOLE_READY_DELAY)

    def _record_failure(self, stage, error):
        if self._sampling_stopped():
            return
        now = self.reactor.monotonic()
        self.valid = False
        self.communication_ok = False
        self.error_count += 1
        self.consecutive_failure_count += 1
        self._on_environment_failure()
        details = {
            "stage": stage,
            "type": error.__class__.__name__,
            "message": str(error) or error.__class__.__name__,
        }
        status = getattr(error, "status", None)
        if status is not None:
            self.i2c_error_count += 1
            details["i2c_bus_status"] = status
            details["i2c_address"] = getattr(error, "i2c_address", None)
        self.last_error = details
        self.last_error_time = now
        self._apply_error_values()

        key = (stage, details.get("i2c_bus_status"), details["type"],
               details["message"])
        if key != self._last_log_error_key:
            logging.warning("%s %s: communication failed during %s: %s; "
                            "retrying in %ds", self.model, self.name,
                            stage, details["message"], self.report_time)
            self._last_log_error_key = key
        elif (not self._sampling_stopped()
              and self.consecutive_failure_count %
              tca9548a.RECOVERY_FAILURE_GROUP_SIZE == 0):
            logging.warning("%s %s: communication failure persists; %d "
                            "consecutive failure(s); retrying in %ds",
                            self.model, self.name,
                            self.consecutive_failure_count,
                            self.report_time)

        self._record_web_failure(key, stage, details)
        if self._sampling_stopped():
            self._report_sampling_stopped(details)

    def _sampling_stopped(self):
        return (self.consecutive_failure_count >=
                MAX_CONSECUTIVE_ENVIRONMENT_FAILURES)

    def _report_sampling_stopped(self, details):
        reason = details.get("i2c_bus_status", details["message"])
        message = (
            "%s: sampling stopped after %d failures (%s). Check wiring "
            "and sensor; restart Klipper after repair." % (
                self._recovery_display_name(),
                MAX_CONSECUTIVE_ENVIRONMENT_FAILURES, reason))
        logging.error(message)
        if self._klippy_ready:
            self._respond_error(message)
            return
        self._pending_web_notification = (("sampling_stopped", reason),
                                          message)

    def _on_environment_failure(self):
        """Let a concrete driver invalidate its initialization state."""

    def _apply_error_values(self):
        publish_temperature = (
            self._mux.zero_temperature_on_error and self.temp != 0.)
        if self._mux.zero_temperature_on_error:
            self.temp = 0.
        if self._mux.zero_humidity_on_error:
            self.humidity = 0.
        if publish_temperature:
            self._publish_temperature()

    def _record_web_failure(self, key, stage, details):
        error_name = details.get("i2c_bus_status", details["message"])
        message = "%s: %s failed: %s; retry in %ds" % (
            self._recovery_display_name(), stage, error_name,
            self.report_time)
        if key != self._last_web_error_key:
            if not self._klippy_ready:
                # Moonraker subscribes to G-code output after klippy:connect.
                # Retain startup failures until the Console can receive them.
                self._pending_web_notification = (key, message)
            elif self._pending_web_notification is not None:
                self._pending_web_notification = (key, message)
            else:
                self._respond_error(message)
                self._last_web_error_key = key

    def _emit_pending_web_notification(self, eventtime):
        pending = self._pending_web_notification
        self._pending_web_notification = None
        if pending is not None and self.communication_ok is False:
            key, message = pending
            self._respond_error(message)
            self._last_web_error_key = key
        return self.reactor.NEVER

    def _record_success(self):
        if self._sampling_stopped():
            return
        was_unavailable = not self.communication_ok
        web_failure_reported = self._last_web_error_key is not None
        if was_unavailable and (self.last_success_time is not None
                                or web_failure_reported):
            logging.info("%s %s: I2C communication recovered",
                         self.model, self.name)
            if web_failure_reported:
                self._respond_info("%s: recovered" % (
                    self._recovery_display_name(),))
        self.valid = True
        self.communication_ok = True
        self.last_success_time = self.reactor.monotonic()
        self.consecutive_failure_count = 0
        self._last_log_error_key = None
        self._last_web_error_key = None
        self._pending_web_notification = None

    def _publish_temperature(self, measured_time=None):
        if measured_time is None:
            measured_time = self.reactor.monotonic()
        if self._callback is not None:
            print_time = self.i2c.get_mcu().estimated_print_time(measured_time)
            self._callback(print_time, self.temp)

    def _recovery_display_name(self):
        return "TCA9548A %s %s" % (self.recovery_sensor_type, self.name)

    def _respond_info(self, message):
        try:
            gcode = self.printer.lookup_object("gcode", None)
            if gcode is not None:
                gcode.respond_info(message)
        except Exception:
            logging.exception("%s %s: unable to send Console notification",
                              self.model, self.name)

    def _respond_error(self, message):
        try:
            gcode = self.printer.lookup_object("gcode", None)
            if gcode is None:
                return
            respond_raw = getattr(gcode, "respond_raw", None)
            if respond_raw is not None:
                # Klipper's !! prefix is rendered as a frontend error without
                # changing printer state.
                respond_raw("!! " + message)
            else:
                gcode.respond_info(message)
        except Exception:
            logging.exception("%s %s: unable to send Console error",
                              self.model, self.name)

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
            sensor_status = sensor.get_status(eventtime)
            for key in ("humidity",) + RECOVERY_STATUS_FIELDS:
                if key in sensor_status:
                    status[key] = sensor_status[key]
            status["tca9548a_channel"] = sensor._mux_channel
            return status

        tsensor.get_status = get_status_with_environment
        self._status_patched = True
        logging.info("%s %s: exposed environment data on '%s'",
                     self.model, self.name, tsensor_name)

    def _get_environment_recovery_status(self):
        return {
            "valid": self.valid,
            "communication_ok": self.communication_ok,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "last_success_time": self.last_success_time,
            "i2c_error_count": self.i2c_error_count,
            "error_count": self.error_count,
            "consecutive_failure_count": self.consecutive_failure_count,
            "sampling_stopped": self._sampling_stopped(),
            "i2c_status_supported": getattr(
                self.i2c, "status_supported", False),
            "tca9548a_channel": self._mux_channel,
        }
