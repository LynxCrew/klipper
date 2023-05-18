# Tracking of PWM controlled heaters and their temperature control
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging, threading, collections


######################################################################
# Heater
######################################################################

PID_PROFILE_VERSION = 1
KELVIN_TO_CELSIUS = -273.15
MAX_HEAT_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.

class Heater:
    def __init__(self, config, sensor):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        # Setup sensor
        self.sensor = sensor
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.max_set_temp = config.getfloat(
            'max_set_temp', self.max_temp,
            minval=self.min_temp, maxval=self.max_temp)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        self.pwm_delay = self.sensor.get_report_time_delta()
        # Setup temperature checks
        self.min_extrude_temp = config.getfloat(
            'min_extrude_temp', 170.,
            minval=self.min_temp, maxval=self.max_temp)
        is_fileoutput = (self.printer.get_start_args().get('debugoutput')
                         is not None)
        self.can_extrude = self.min_extrude_temp <= 0. or is_fileoutput
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.smooth_time = config.getfloat('smooth_time', 1., above=0.)
        self.inv_smooth_time = 1. / self.smooth_time
        self.lock = threading.Lock()
        self.last_temp = self.smoothed_temp = self.target_temp = 0.
        self.last_temp_time = 0.
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        # Setup control algorithm sub-class
        self.control = self.lookup_control(config, 'default')
        # Setup output heater pin
        heater_pin = config.get('heater_pin')
        ppins = self.printer.lookup_object('pins')
        self.mcu_pwm = ppins.setup_pin('pwm', heater_pin)
        pwm_cycle_time = config.getfloat('pwm_cycle_time', 0.100, above=0.,
                                         maxval=self.pwm_delay)
        self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        # Load additional modules
        self.printer.load_object(config, "verify_heater %s" % (self.name,))
        self.printer.load_object(config, "pid_calibrate")
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command("SET_HEATER_TEMPERATURE", "HEATER",
                                   self.name, self.cmd_SET_HEATER_TEMPERATURE,
                                   desc=self.cmd_SET_HEATER_TEMPERATURE_help)
    def lookup_control(self, config, profile_name):
        algos = {
            'watermark': ControlBangBang,
            'pid': ControlPID,
            'pid_v': ControlVelocityPID,
        }
        algo = config.getchoice('control', algos)
        return algo(self, config, profile_name)
    def set_pwm(self, read_time, value):
        if self.target_temp <= 0.:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
            and abs(value - self.last_pwm_value) < 0.05):
            # No significant change in value - can suppress update
            return
        pwm_time = read_time + self.pwm_delay
        self.next_pwm_time = pwm_time + 0.75 * MAX_HEAT_TIME
        self.last_pwm_value = value
        self.mcu_pwm.set_pwm(pwm_time, value)
        #logging.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
        #              self.name, value, pwm_time,
        #              self.last_temp, self.last_temp_time, self.target_temp)
    def temperature_callback(self, read_time, temp):
        with self.lock:
            time_diff = read_time - self.last_temp_time
            self.last_temp = temp
            self.last_temp_time = read_time
            self.control.temperature_update(read_time, temp, self.target_temp)
            temp_diff = temp - self.smoothed_temp
            adj_time = min(time_diff * self.inv_smooth_time, 1.)
            self.smoothed_temp += temp_diff * adj_time
            self.can_extrude = (self.smoothed_temp >= self.min_extrude_temp)
        #logging.debug("temp: %.3f %f = %f", read_time, temp)
    # External commands
    def get_pwm_delay(self):
        return self.pwm_delay
    def get_max_power(self):
        return self.max_power
    def get_smooth_time(self):
        return self.smooth_time
    def set_temp(self, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_set_temp):
            raise self.printer.command_error(
                "Requested temperature (%.1f) out of range (%.1f:%.1f)"
                % (degrees, self.min_temp, self.max_set_temp))
        with self.lock:
            self.target_temp = degrees
    def get_temp(self, eventtime):
        print_time = self.mcu_pwm.get_mcu().estimated_print_time(eventtime) - 5.
        with self.lock:
            if self.last_temp_time < print_time:
                return 0., self.target_temp
            return self.smoothed_temp, self.target_temp
    def check_busy(self, eventtime):
        with self.lock:
            return self.control.check_busy(
                eventtime, self.smoothed_temp, self.target_temp)
    def set_control(self, control):
        with self.lock:
            old_control = self.control
            self.control = control
            self.target_temp = 0.
        return old_control
    def get_control(self):
        return self.control
    def alter_target(self, target_temp):
        if target_temp:
            target_temp = max(self.min_temp, min(self.max_temp, target_temp))
        self.target_temp = target_temp
    def stats(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
            last_pwm_value = self.last_pwm_value
        is_active = target_temp or last_temp > 50.
        return is_active, '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.name, target_temp, last_temp, last_pwm_value)
    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            smoothed_temp = self.smoothed_temp
            last_pwm_value = self.last_pwm_value
        return {'temperature': round(smoothed_temp, 2), 'target': target_temp,
                'power': last_pwm_value}
    cmd_SET_HEATER_TEMPERATURE_help = "Sets a heater temperature"
    def cmd_SET_HEATER_TEMPERATURE(self, gcmd):
        temp = gcmd.get_float('TARGET', 0.)
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(self, temp)


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, heater, config, profile_name):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False
    def temperature_update(self, read_time, temp, target_temp):
        if self.heating and temp >= target_temp+self.max_delta:
            self.heating = False
        elif not self.heating and temp <= target_temp-self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater_max_power)
        else:
            self.heater.set_pwm(read_time, 0.)
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return smoothed_temp < target_temp-self.max_delta
    def get_profile_name(self):
        return 'default'
    def get_type(self):
        return 'watermark'


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1

class ControlPID:
    def __init__(self, heater, config, profile_name):
        self.profile_name = profile_name
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.Kp = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.dt = heater.pwm_delay
        self.smooth = 1. + heater.get_smooth_time() / self.dt
        self.prev_temp = AMBIENT_TEMP
        self.prev_err = 0.
        self.prev_der = 0.
        self.int_sum = 0.

    def temperature_update(self, read_time, temp, target_temp):
        # calculate the error
        err = target_temp - temp
        # calculate the current integral amount using the Trapezoidal rule
        ic =  ((self.prev_err + err) / 2.) * self.dt
        i = self.int_sum + ic
        # calculate the current derivative using a modified moving average,
        # and derivative on measurement, to account for derivative kick
        # when the set point changes
        dc = -(temp - self.prev_temp) / self.dt
        dc = ((self.smooth - 1.) * self.prev_der + dc)/self.smooth
        # calculate the output
        o = self.Kp * err + self.Ki * i + self.Kd * dc
        # calculate the saturated output
        so = max(0., min(self.heater_max_power, o))

        # update the heater
        self.heater.set_pwm(read_time, so)
        #update the previous values
        self.prev_temp = temp
        self.prev_der = dc
        if target_temp > 0.:
            self.prev_err = err
            if o == so:
                # not saturated so an update is allowed
                self.int_sum = i
            else:
                # saturated, so conditionally integrate
                if (o>0.)-(o<0.) != (ic>0.)-(ic<0.):
                    # the signs are opposite so an update is allowed
                    self.int_sum = i
        else:
            self.prev_err = 0.
            self.int_sum = 0.

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_der) > PID_SETTLE_SLOPE)
    def get_profile_name(self):
        return self.profile_name
    def get_type(self):
        return 'pid'


######################################################################
# Velocity (PID) control algo
######################################################################

class ControlVelocityPID:
    def __init__(self, heater, config, profile_name):
        self.profile_name = profile_name
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.dt = heater.pwm_delay
        self.Kp = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.smooth = 1. + heater.get_smooth_time() / self.dt
        self.t = [0.] * 3 # temperature readings
        self.d1 = 0. # previous 1st derivative
        self.d2 = 0. # previous 2nd derivative
        self.pwm = 0.

    def temperature_update(self, read_time, temp, target_temp):
        self.t.pop(0)
        self.t.append(temp)

        # calculate the derivatives using a modified moving average,
        # also account for derivative and proportional kick
        d1 = self.t[-1] - self.t[-2]
        self.d1 = ((self.smooth - 1.) * self.d1 + d1)/self.smooth
        d2 = (self.t[-1] - 2.*self.t[-2] + self.t[-3])/self.dt
        self.d2 = ((self.smooth - 1.) * self.d2 + d2)/self.smooth

        # calcualte the output
        p = self.Kp * -self.d1
        i = self.Ki * self.dt * (target_temp - self.t[-1])
        d = self.Kd * -self.d2
        self.pwm = max(0., min(self.heater_max_power, self.pwm + p + i + d))

        # ensure no weird artifacts
        if target_temp == 0.:
            self.d1 = 0.
            self.d2 = 0.
            self.pwm = 0.

        # update the heater
        self.heater.set_pwm(read_time, self.pwm)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.d1) > PID_SETTLE_SLOPE)
    def get_profile_name(self):
        return self.profile_name
    def get_type(self):
        return 'pid_v'


######################################################################
# Sensor and heater lookup
######################################################################

class PrinterHeaters:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sensor_factories = {}
        self.heaters = {}
        self.gcode_id_to_sensor = {}
        self.available_heaters = []
        self.available_sensors = []
        self.has_started = self.have_load_sensors = False
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("gcode:request_restart",
                                            self.turn_off_all_heaters)
        self.pmgr = ProfileManager(config.get_printer(), self)

        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("TURN_OFF_HEATERS", self.cmd_TURN_OFF_HEATERS,
                               desc=self.cmd_TURN_OFF_HEATERS_help)
        gcode.register_command("M105", self.cmd_M105, when_not_ready=True)
        gcode.register_command("TEMPERATURE_WAIT", self.cmd_TEMPERATURE_WAIT,
                               desc=self.cmd_TEMPERATURE_WAIT_help)
    def load_config(self, config):
        self.have_load_sensors = True
        # Load default temperature sensors
        pconfig = self.printer.lookup_object('configfile')
        dir_name = os.path.dirname(__file__)
        filename = os.path.join(dir_name, 'temperature_sensors.cfg')
        try:
            dconfig = pconfig.read_config(filename)
        except Exception:
            raise config.config_error("Cannot load config '%s'" % (filename,))
        for c in dconfig.get_prefix_sections(''):
            self.printer.load_object(dconfig, c.get_name())
    def add_sensor_factory(self, sensor_type, sensor_factory):
        self.sensor_factories[sensor_type] = sensor_factory
    def setup_heater(self, config, gcode_id=None):
        heater_name = config.get_name().split()[-1]
        if heater_name in self.heaters:
            raise config.error("Heater %s already registered" % (heater_name,))
        # Setup sensor
        sensor = self.setup_sensor(config)
        # Create heater
        self.heaters[heater_name] = heater = Heater(config, sensor)
        self.register_sensor(config, heater, gcode_id)
        self.available_heaters.append(config.get_name())
        return heater
    def get_all_heaters(self):
        return self.available_heaters
    def lookup_heater(self, heater_name):
        if heater_name not in self.heaters:
            raise self.printer.config_error(
                "Unknown heater '%s'" % (heater_name,))
        return self.heaters[heater_name]
    def setup_sensor(self, config):
        if not self.have_load_sensors:
            self.load_config(config)
        sensor_type = config.get('sensor_type')
        if sensor_type not in self.sensor_factories:
            raise self.printer.config_error(
                "Unknown temperature sensor '%s'" % (sensor_type,))
        if sensor_type == 'NTC 100K beta 3950':
            config.deprecate('sensor_type', 'NTC 100K beta 3950')
        return self.sensor_factories[sensor_type](config)
    def register_sensor(self, config, psensor, gcode_id=None):
        self.available_sensors.append(config.get_name())
        if gcode_id is None:
            gcode_id = config.get('gcode_id', None)
            if gcode_id is None:
                return
        if gcode_id in self.gcode_id_to_sensor:
            raise self.printer.config_error(
                "G-Code sensor id %s already registered" % (gcode_id,))
        self.gcode_id_to_sensor[gcode_id] = psensor
    def get_status(self, eventtime):
        return {'available_heaters': self.available_heaters,
                'available_sensors': self.available_sensors}
    def turn_off_all_heaters(self, print_time=0.):
        for heater in self.heaters.values():
            heater.set_temp(0.)
    cmd_TURN_OFF_HEATERS_help = "Turn off all heaters"
    def cmd_TURN_OFF_HEATERS(self, gcmd):
        self.turn_off_all_heaters()
    # G-Code M105 temperature reporting
    def _handle_ready(self):
        self.has_started = True
    def _get_temp(self, eventtime):
        # Tn:XXX /YYY B:XXX /YYY
        out = []
        if self.has_started:
            for gcode_id, sensor in sorted(self.gcode_id_to_sensor.items()):
                cur, target = sensor.get_temp(eventtime)
                out.append("%s:%.1f /%.1f" % (gcode_id, cur, target))
        if not out:
            return "T:0"
        return " ".join(out)
    def cmd_M105(self, gcmd):
        # Get Extruder Temperature
        reactor = self.printer.get_reactor()
        msg = self._get_temp(reactor.monotonic())
        did_ack = gcmd.ack(msg)
        if not did_ack:
            gcmd.respond_raw(msg)
    def _wait_for_temperature(self, heater):
        # Helper to wait on heater.check_busy() and report M105 temperatures
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        gcode = self.printer.lookup_object("gcode")
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        while not self.printer.is_shutdown() and heater.check_busy(eventtime):
            print_time = toolhead.get_last_move_time()
            gcode.respond_raw(self._get_temp(eventtime))
            eventtime = reactor.pause(eventtime + 1.)
    def set_temperature(self, heater, temp, wait=False):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback((lambda pt: None))
        heater.set_temp(temp)
        if wait and temp:
            self._wait_for_temperature(heater)
    cmd_TEMPERATURE_WAIT_help = "Wait for a temperature on a sensor"
    def cmd_TEMPERATURE_WAIT(self, gcmd):
        sensor_name = gcmd.get('SENSOR')
        if sensor_name not in self.available_sensors:
            raise gcmd.error("Unknown sensor '%s'" % (sensor_name,))
        min_temp = gcmd.get_float('MINIMUM', float('-inf'))
        max_temp = gcmd.get_float('MAXIMUM', float('inf'), above=min_temp)
        if min_temp == float('-inf') and max_temp == float('inf'):
            raise gcmd.error(
                "Error on 'TEMPERATURE_WAIT': missing MINIMUM or MAXIMUM.")
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        if sensor_name in self.heaters:
            sensor = self.heaters[sensor_name]
        else:
            sensor = self.printer.lookup_object(sensor_name)
        toolhead = self.printer.lookup_object("toolhead")
        reactor = self.printer.get_reactor()
        eventtime = reactor.monotonic()
        while not self.printer.is_shutdown():
            temp, target = sensor.get_temp(eventtime)
            if temp >= min_temp and temp <= max_temp:
                return
            print_time = toolhead.get_last_move_time()
            gcmd.respond_raw(self._get_temp(eventtime))
            eventtime = reactor.pause(eventtime + 1.)

class ProfileManager:
    def __init__(self, printer, printerheaters):
        self.printer = printer
        self.gcode = self.printer.lookup_object('gcode')
        self.printerheaters = printerheaters
        self.gcode.register_command(
            "PID_PROFILE_LOAD", self.cmd_PID_PROFILE_LOAD,
            desc=self.cmd_PID_PROFILE_LOAD_help)
    def get_section_name(self, heater_name, profile_name):
        return (heater_name if profile_name == 'default'
                            else ("pid_profile "
                                  + heater_name
                                  + " "
                                  + profile_name))
    cmd_PID_PROFILE_LOAD_help = "PID Profile Persistent Storage management"
    def cmd_PID_PROFILE_LOAD(self, gcmd):
        heater_name = gcmd.get('HEATER', None)
        if heater_name is None:
            raise self.gcode.error(
                "pid_profile: Heater must be specified")
        current_heater = self.printerheaters.lookup_heater(heater_name)
        if current_heater is None:
            raise self.gcode.error(
                "pid_profile: Unknown heater [%s]" % current_heater)
        profile_name = gcmd.get('PROFILE', 'default')
        if profile_name == current_heater.get_control().get_profile_name():
            self.gcode.respond_info(
                "PID Profile [%s] already loaded."
                % profile_name)
        else:
            config = (self.printer
                      .lookup_object('configfile')
                      .read_main_config())
            section_name = self.get_section_name(heater_name, profile_name)
            default_name = gcmd.get('DEFAULT', None)
            if not config.has_section(section_name):
                if default_name is None:
                    raise self.gcode.error(
                        "pid_profile: Unknown profile [%s]" % profile_name)
                else:
                    profile_name = default_name
                    section_name = self.get_section_name(
                        heater_name,
                        profile_name)
                    if not config.has_section(section_name):
                        raise self.gcode.error(
                            "pid_profile: Unknown default profile [%s]"
                            % profile_name)
            profile_config = (config.getsection(section_name))
            if profile_config is None:
                raise self.gcode.error(
                    "pid_profile: Unknown profile [%s]" % profile_name)
            pid_version = profile_config.getint('pid_version', 1)
            if pid_version != PID_PROFILE_VERSION:
                raise self.gcode.error(
                    "pid_profile: Profile [%s] "
                    "not compatible with this version\n"
                    "of pid_profile.\n"
                    "Profile Version: %d Current Version: %d "
                    % (profile_name, pid_version, PID_PROFILE_VERSION))
            control = current_heater.lookup_control(
                profile_config,
                profile_name)
            current_heater.set_control(control)
            self.gcode.respond_info(
                "PID Profile [%s] loaded for heater [%s].\n"
                "PID parameters: pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
                "Control: %s\n"
                "Tolerance: %.4f\n"
                "Target: %.2f"
                % (profile_name,
                   heater_name,
                   profile_config.get('control'),
                   profile_config.getfloat('pid_Kp'),
                   profile_config.getfloat('pid_Ki'),
                   profile_config.getfloat('pid_Kd'),
                   profile_config.getfloat('pid_tolerance'),
                   profile_config.getfloat('pid_target')))

def load_config(config):
    return PrinterHeaters(config)
