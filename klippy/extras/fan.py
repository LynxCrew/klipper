# Printer cooling fan
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import pulse_counter

FAN_MIN_TIME = 0.100
SAFETY_CHECK_INIT_TIME = 3.

class Fan:
    def __init__(self, config, default_shutdown_speed=0.):
        self.printer = config.get_printer()
        self.last_fan_value = 0.
        self.last_fan_time = 0.
        # Read config
        self.kick_start_time = config.getfloat('kick_start_time', 0.1,
                                               minval=0.)
        self.min_power = config.getfloat('min_power', default=0.,
                                         minval=0., maxval=1.)
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        if self.min_power > self.max_power:
            raise config.error(
                "min_power=%f can't be larger than max_power=%f"
                % (self.min_power, self.max_power)
            )

        cycle_time = config.getfloat('cycle_time', 0.010, above=0.)
        hardware_pwm = config.getboolean('hardware_pwm', False)
        shutdown_speed = config.getfloat(
            'shutdown_speed', default_shutdown_speed, minval=0., maxval=1.)
        # Setup pwm object
        ppins = self.printer.lookup_object('pins')
        self.mcu_fan = ppins.setup_pin('pwm', config.get('pin'))
        self.mcu_fan.setup_max_duration(0.)
        self.mcu_fan.setup_cycle_time(cycle_time, hardware_pwm)
        shutdown_power = max(0., min(self.max_power, shutdown_speed))
        self.mcu_fan.setup_start_value(0., shutdown_power)

        self.enable_pin = None
        enable_pin = config.get('enable_pin', None)
        if enable_pin is not None:
            self.enable_pin = ppins.setup_pin('digital_out', enable_pin)
            self.enable_pin.setup_max_duration(0.)

        # Setup tachometer
        self.tachometer = FanTachometer(config)

        self.name = config.get_name().split()[-1]
        self.num_err = 0
        self.min_rpm = config.getint("min_rpm", None, minval=0)
        self.max_err = config.getint("max_error", None, minval=0)
        if (self.min_rpm is not None
                and self.min_rpm > 0
                and self.tachometer._freq_counter is None):
            raise config.error(
                "'tachometer_pin' must be specified before enabling `min_rpm`")
        if self.max_err is not None and self.min_rpm is None:
            raise config.error(
                "'min_rpm' must be specified before enabling `max_error`")
        if self.min_rpm is None:
            self.min_rpm = 0
        if self.max_err is None:
            self.max_err = 3

        self.speed = None

        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        # Register callbacks
        self.printer.register_event_handler("gcode:request_restart",
                                            self._handle_request_restart)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "SET_FAN", "FAN", self.name,
            self.cmd_SET_FAN,
            desc=self.cmd_SET_FAN_help)

    def handle_ready(self):
        reactor = self.printer.get_reactor()
        if self.min_rpm > 0:
            reactor.register_timer(
                self.fan_check, reactor.monotonic()+SAFETY_CHECK_INIT_TIME)
    def get_mcu(self):
        return self.mcu_fan.get_mcu()
    def set_speed(self, print_time, value, force=False):
        self.speed = value
        if value == self.last_fan_value and not force:
            return
        if value > 0:
            # Scale value between min_power and max_power
            pwm_value =\
                value * (self.max_power - self.min_power) + self.min_power
            pwm_value = max(self.min_power, min(self.max_power, pwm_value))
        else:
            pwm_value = 0
        print_time = max(self.last_fan_time + FAN_MIN_TIME, print_time)
        if self.enable_pin:
            if value > 0 and self.last_fan_value == 0:
                self.enable_pin.set_digital(print_time, 1)
            elif value == 0 and self.last_fan_value > 0:
                self.enable_pin.set_digital(print_time, 0)
        if (value and value < self.max_power and self.kick_start_time
                and (not self.last_fan_value
                     or value - self.last_fan_value > .5)):
            # Run fan at full speed for specified kick_start_time
            self.mcu_fan.set_pwm(print_time, self.max_power)
            print_time += self.kick_start_time
        self.mcu_fan.set_pwm(print_time, pwm_value)
        self.last_fan_time = print_time
        self.last_fan_value = value
    def set_speed_from_command(self, value):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback((lambda pt:
                                              self.set_speed(pt, value)))
    def _handle_request_restart(self, print_time):
        self.set_speed(print_time, 0.)

    def get_status(self, eventtime):
        tachometer_status = self.tachometer.get_status(eventtime)
        return {
            'speed': self.last_fan_value,
            'rpm': tachometer_status['rpm'],
        }

    def fan_check(self, eventtime):
        rpm = self.tachometer.get_status(eventtime)['rpm']
        if self.last_fan_value and rpm is not None and rpm < self.min_rpm:
            self.num_err += 1
            if self.num_err > self.max_err:
                msg = "'%s' spinning below minimum safe speed of %d rev/min" % (
                    self.name, self.min_rpm)
                logging.error(msg)
                self.printer.invoke_shutdown(msg)
                return self.printer.get_reactor().NEVER
        else:
            self.num_err = 0
        return eventtime + 1.5
    cmd_SET_FAN_help = "Change settings for a fan"
    def cmd_SET_FAN(self, gcmd):
        self.min_power = gcmd.get_float("MIN_POWER",
                                        self.min_power,
                                        minval=0.,
                                        maxval=1.)
        self.max_power = gcmd.get_float("MAX_POWER",
                                        self.max_power,
                                        above=self.min_power,
                                        maxval=1.)
        self.min_rpm = gcmd.get_float("MIN_RPM",
                                      self.min_rpm,
                                      minval=0.)
        curtime = self.printer.get_reactor().monotonic()
        print_time = self.get_mcu().estimated_print_time(curtime)
        self.set_speed(print_time, self.speed, force=True)

class FanTachometer:
    def __init__(self, config):
        printer = config.get_printer()
        self._freq_counter = None

        pin = config.get('tachometer_pin', None)
        if pin is not None:
            self.ppr = config.getint('tachometer_ppr', 2, minval=1)
            poll_time = config.getfloat('tachometer_poll_interval',
                                        0.0015, above=0.)
            sample_time = 1.
            self._freq_counter = pulse_counter.FrequencyCounter(
                printer, pin, sample_time, poll_time)

    def get_status(self, eventtime):
        if self._freq_counter is not None:
            rpm = self._freq_counter.get_frequency() * 30. / self.ppr
        else:
            rpm = None
        return {'rpm': rpm}

class PrinterFan:
    def __init__(self, config):
        self.fan = Fan(config)
        # Register commands
        gcode = config.get_printer().lookup_object('gcode')
        gcode.register_command("M106", self.cmd_M106)
        gcode.register_command("M107", self.cmd_M107)
    def get_status(self, eventtime):
        return self.fan.get_status(eventtime)
    def cmd_M106(self, gcmd):
        # Set fan speed
        value = gcmd.get_float('S', 255., minval=0.) / 255.
        self.fan.set_speed_from_command(value)
    def cmd_M107(self, gcmd):
        # Turn fan off
        self.fan.set_speed_from_command(0.)

def load_config(config):
    return PrinterFan(config)
