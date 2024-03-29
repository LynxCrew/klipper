# Tracking of PWM controlled heaters and their temperature control
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os
import logging
import threading
import collections


######################################################################
# Heater
######################################################################

KELVIN_TO_CELSIUS = -273.15
MAX_HEAT_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.
PID_PROFILE_VERSION = 1
PID_PROFILE_OPTIONS = {
    'pid_target': (float, "%.2f"),
    'pid_tolerance': (float, "%.4f"),
    'control': (str, "%s"),
    'smooth_time': (float, "%.3f"),
    'pid_kp': (float, "%.3f"),
    'pid_ki': (float, "%.3f"),
    'pid_kd': (float, "%.3f")
}


class Heater:
    def __init__(self, config, sensor):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.config = config
        self.configfile = self.printer.lookup_object('configfile')
        self.name = config.get_name()
        self.short_name = self.name.split()[-1]
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
        self.enabled = True
        self.cold_extrude = False
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.config_smooth_time = config.getfloat('smooth_time', 1., above=0.)
        self.smooth_time = self.config_smooth_time
        self.inv_smooth_time = 1. / self.smooth_time
        self.is_shutdown = False
        self.lock = threading.Lock()
        self.last_temp = self.smoothed_temp = self.target_temp = 0.
        self.last_temp_time = 0.
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        # Setup control algorithm sub-class
        config.get('control', None)
        config.getfloat('pid_kp', None)
        config.getfloat('pid_ki', None)
        config.getfloat('pid_kd', None)
        config.getfloat('max_delta', None)
        # Setup output heater pin
        heater_pin = config.get('heater_pin')
        ppins = self.printer.lookup_object('pins')
        self.mcu_pwm = ppins.setup_pin('pwm', heater_pin)
        pwm_cycle_time = config.getfloat('pwm_cycle_time', 0.100, above=0.,
                                         maxval=self.pwm_delay)
        self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        # Load additional modules
        self.printer.load_object(config, "verify_heater %s"
                                 % (self.short_name,))
        self.printer.load_object(config, "pid_calibrate")
        self.gcode = self.printer.lookup_object('gcode')
        self.pmgr = self.ProfileManager(self)
        self.control = self.lookup_control(
            self.pmgr.init_default_profile(),
            True
        )
        self.gcode.register_mux_command(
            "SET_HEATER_TEMPERATURE",
            "HEATER",
            self.short_name,
            self.cmd_SET_HEATER_TEMPERATURE,
            desc=self.cmd_SET_HEATER_TEMPERATURE_help)
        self.gcode.register_mux_command("SET_SMOOTH_TIME",
                                        "HEATER",
                                        self.short_name,
                                        self.cmd_SET_SMOOTH_TIME,
                                        desc=self.cmd_SET_SMOOTH_TIME_help)
        self.gcode.register_mux_command("PID_PROFILE",
                                        "HEATER",
                                        self.short_name,
                                        self.pmgr.cmd_PID_PROFILE,
                                        desc=self.pmgr.cmd_PID_PROFILE_help)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)

    def notify_disabled(self, gcmd):
        if gcmd is not None:
            gcmd.respond_info("Heater [%s] is disabled due to an "
                              "accelerometer being connected."
                              % self.short_name)

    def lookup_control(self, profile, load_clean=False):
        algos = collections.OrderedDict({
            'watermark': ControlBangBang,
            'pid': ControlPID,
            'pid_v': ControlVelocityPID,
        })
        return algos[profile['control']](profile,
                                         self,
                                         load_clean)

    def set_pwm(self, read_time, value):
        if self.target_temp <= 0. or self.is_shutdown:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
                and abs(value - self.last_pwm_value) < 0.05):
            # No significant change in value - can suppress update
            return
        pwm_time = read_time + self.pwm_delay
        self.next_pwm_time = pwm_time + 0.75 * MAX_HEAT_TIME
        self.last_pwm_value = value
        self.mcu_pwm.set_pwm(pwm_time, value)
        # logging.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
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
            self.can_extrude = (self.smoothed_temp >= self.min_extrude_temp
                                or self.cold_extrude)
        # logging.debug("temp: %.3f %f = %f", read_time, temp)

    def _handle_shutdown(self):
        self.is_shutdown = True
    # External commands

    def get_name(self):
        return self.name

    def get_pwm_delay(self):
        return self.pwm_delay

    def get_max_power(self):
        return self.max_power

    def get_smooth_time(self):
        return self.smooth_time

    def set_inv_smooth_time(self, inv_smooth_time):
        self.inv_smooth_time = inv_smooth_time

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

    def set_control(self, control, keep_target=True):
        with self.lock:
            old_control = self.control
            self.control = control
            if not keep_target:
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
            self.short_name, target_temp, last_temp, last_pwm_value)

    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            smoothed_temp = self.smoothed_temp
            last_pwm_value = self.last_pwm_value
        return {'temperature': round(smoothed_temp, 2),
                'target': target_temp,
                'power': last_pwm_value,
                'pid_profile': self.get_control().get_profile()['name']}

    def set_enabled(self, enabled):
        self.enabled = enabled
    cmd_SET_HEATER_TEMPERATURE_help = "Sets a heater temperature"

    def cmd_SET_HEATER_TEMPERATURE(self, gcmd):
        temp = gcmd.get_float('TARGET', 0.)
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(self, temp, gcmd=gcmd)
    cmd_SET_SMOOTH_TIME_help = "Set the smooth time for the given heater"

    def cmd_SET_SMOOTH_TIME(self, gcmd):
        save_to_profile = gcmd.get_int('SAVE_TO_PROFILE',
                                       0,
                                       minval=0,
                                       maxval=1)
        self.smooth_time = gcmd.get_float('SMOOTH_TIME',
                                          self.config_smooth_time,
                                          above=0.)
        self.inv_smooth_time = 1. / self.smooth_time
        self.get_control().update_smooth_time()
        if save_to_profile:
            self.get_control().get_profile()['smooth_time'] = self.smooth_time
            self.pmgr.save_profile()

    class ProfileManager:
        def __init__(self, outer_instance):
            self.outer_instance = outer_instance
            self.profiles = {}
            self.incompatible_profiles = []
            # Fetch stored profiles from Config
            stored_profs = self.outer_instance.config.get_prefix_sections(
                "pid_profile %s"
                % self.outer_instance.short_name
            )
            for profile in stored_profs:
                self._init_profile(profile,
                                   profile.get_name().split(' ', 2)[2])

        def _init_profile(self, config_section, name):
            version = config_section.getint('pid_version', 1)
            if version != PID_PROFILE_VERSION:
                logging.info(
                    "Profile [%s] not compatible with this version "
                    "of pid_profile.\n"
                    "Profile Version: %d Current Version: %d"
                    % (name, version, PID_PROFILE_VERSION))
                self.incompatible_profiles.append(name)
                return None
            temp_profile = {}
            control = self._check_value_config('control',
                                               config_section,
                                               str,
                                               False)
            if control == 'watermark':
                temp_profile['max_delta'] = config_section.getfloat('max_delta',
                                                                    2.0,
                                                                    above=0.)
            elif control == 'pid' or control == 'pid_v':
                for key, (type,
                          placeholder) in PID_PROFILE_OPTIONS.items():
                    can_be_none = (key != 'pid_kp'
                                   and key != 'pid_ki'
                                   and key != 'pid_kd')
                    above = 0. if key == 'smooth_time' else None
                    temp_profile[key] = self._check_value_config(key,
                                                                 config_section,
                                                                 type,
                                                                 can_be_none,
                                                                 above)
                if name == 'default':
                    temp_profile['smooth_time'] = None
            else:
                raise self.outer_instance.printer.config_error(
                    "Unknown control type '%s' "
                    "in [pid_profile %s %s]."
                    % (control, self.outer_instance.short_name, name)
                )
            temp_profile['control'] = control
            temp_profile['name'] = name
            self.profiles[name] = temp_profile
            return temp_profile

        def _check_value_config(self,
                                key,
                                config_section,
                                type,
                                can_be_none,
                                above=None):
            if type is int:
                value = config_section.getint(key, None, above=above)
            elif type is float:
                value = config_section.getfloat(key, None, above=above)
            else:
                value = config_section.get(key, None)
            if not can_be_none and value is None:
                raise self.outer_instance.gcode.error(
                    "pid_profile: '%s' has to be "
                    "specified in [pid_profile %s %s]."
                    % (key,
                       self.outer_instance.short_name,
                       config_section.get_name())
                )
            return value

        def _compute_section_name(self, profile_name):
            return (self.outer_instance.short_name if profile_name == 'default'
                    else ("pid_profile "
                          + self.outer_instance.short_name
                          + " "
                          + profile_name)
                    )

        def _check_value_gcmd(self,
                              name,
                              default,
                              gcmd,
                              type,
                              can_be_none,
                              minval=None,
                              maxval=None,
                              above=None):
            if type is int:
                value = gcmd.get_int(name,
                                     default,
                                     minval=minval,
                                     maxval=maxval,
                                     above=above)
            elif type is float:
                value = gcmd.get_float(name,
                                       default,
                                       minval=minval,
                                       maxval=maxval,
                                       above=above)
            else:
                value = gcmd.get(name, default)
            if not can_be_none and value is None:
                raise self.outer_instance.gcode.error(
                    "pid_profile: '%s' has to be specified."
                    % name
                )
            return value.lower() if type == 'lower' else value

        def init_default_profile(self):
            return self._init_profile(self.outer_instance.config,
                                      'default')

        def set_values(self, profile_name, gcmd, verbose):
            current_profile = self.outer_instance.get_control().get_profile()
            target = self._check_value_gcmd('TARGET',
                                            None,
                                            gcmd,
                                            float,
                                            False)
            tolerance = self._check_value_gcmd('TOLERANCE',
                                               current_profile['pid_tolerance'],
                                               gcmd,
                                               float,
                                               False)
            control = self._check_value_gcmd('CONTROL',
                                             current_profile['control'],
                                             gcmd,
                                             'lower',
                                             False)
            kp = self._check_value_gcmd('KP',
                                        None,
                                        gcmd,
                                        float,
                                        False)
            ki = self._check_value_gcmd('KI',
                                        None,
                                        gcmd,
                                        float,
                                        False)
            kd = self._check_value_gcmd('KD',
                                        None,
                                        gcmd,
                                        float,
                                        False)
            smooth_time = self._check_value_gcmd('SMOOTH_TIME',
                                                 None,
                                                 gcmd,
                                                 float,
                                                 True,
                                                 above=0.)
            keep_target = self._check_value_gcmd('KEEP_TARGET',
                                                 0,
                                                 gcmd,
                                                 int,
                                                 True,
                                                 minval=0,
                                                 maxval=1)
            load_clean = self._check_value_gcmd('LOAD_CLEAN',
                                                0,
                                                gcmd,
                                                int,
                                                True,
                                                minval=0,
                                                maxval=1)
            temp_profile = {'pid_target': target,
                            'pid_tolerance': tolerance,
                            'control': control,
                            'smooth_time': smooth_time,
                            'pid_kp': kp,
                            'pid_ki': ki,
                            'pid_kd': kd}
            temp_control = self.outer_instance.lookup_control(temp_profile,
                                                              load_clean)
            self.outer_instance.set_control(temp_control, keep_target)
            msg = ("PID Parameters:\n"
                   "Target: %.2f,\n"
                   "Tolerance: %.4f\n"
                   "Control: %s\n"
                   % (target, tolerance, control))
            if smooth_time is not None:
                msg += "Smooth Time: %.3f\n" % smooth_time
            msg += ("pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
                    "have been set as current profile."
                    % (kp, ki, kd))
            self.outer_instance.gcode.respond_info(msg)
            self.save_profile(profile_name=profile_name, verbose=True)

        def get_values(self, profile_name, gcmd, verbose):
            temp_profile = self.outer_instance.get_control().get_profile()
            target = temp_profile['pid_target']
            tolerance = temp_profile['pid_tolerance']
            control = temp_profile['control']
            kp = temp_profile['pid_kp']
            ki = temp_profile['pid_ki']
            kd = temp_profile['pid_kd']
            smooth_time = (self.outer_instance.get_smooth_time()
                           if temp_profile['smooth_time'] is None
                           else temp_profile['smooth_time'])
            name = temp_profile['name']
            self.outer_instance.gcode.respond_info(
                "PID Parameters:\n"
                "Target: %.2f,\n"
                "Tolerance: %.4f\n"
                "Control: %s\n"
                "Smooth Time: %.3f\n"
                "pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
                "name: %s"
                % (target, tolerance, control, smooth_time, kp, ki, kd, name)
            )

        def save_profile(self, profile_name=None, gcmd=None, verbose=True):
            temp_profile = self.outer_instance.get_control().get_profile()
            if profile_name is None:
                profile_name = temp_profile['name']
            section_name = self._compute_section_name(profile_name)
            self.outer_instance.configfile.set(section_name,
                                               'pid_version',
                                               PID_PROFILE_VERSION)
            for key, (type,
                      placeholder) in PID_PROFILE_OPTIONS.items():
                value = temp_profile[key]
                if value is not None:
                    self.outer_instance.configfile.set(section_name,
                                                       key,
                                                       placeholder
                                                       % value)
            temp_profile['name'] = profile_name
            self.profiles[profile_name] = temp_profile
            if verbose:
                self.outer_instance.gcode.respond_info(
                    "Current PID profile for heater [%s] "
                    "has been saved to profile [%s] "
                    "for the current session.  The SAVE_CONFIG command will\n"
                    "update the printer config file and restart the printer."
                    % (self.outer_instance.short_name, profile_name))

        def load_profile(self, profile_name, gcmd, verbose):
            verbose = self._check_value_gcmd('VERBOSE',
                                             'low',
                                             gcmd,
                                             'lower',
                                             True)
            load_clean = self._check_value_gcmd('LOAD_CLEAN',
                                                0,
                                                gcmd,
                                                int,
                                                True,
                                                minval=0,
                                                maxval=1)
            if (profile_name
                    ==
                    self.outer_instance.get_control().get_profile()['name']
                    and not load_clean):
                if verbose == 'high' or verbose == 'low':
                    self.outer_instance.gcode.respond_info(
                        "PID Profile [%s] already loaded for heater [%s]."
                        % (profile_name, self.outer_instance.short_name)
                    )
                return
            keep_target = self._check_value_gcmd('KEEP_TARGET',
                                                 0,
                                                 gcmd,
                                                 int,
                                                 True,
                                                 minval=0,
                                                 maxval=1)
            profile = self.profiles.get(profile_name, None)
            defaulted = False
            default = gcmd.get('DEFAULT', None)
            if profile is None:
                if default is None:
                    raise self.outer_instance.gcode.error(
                        "pid_profile: Unknown profile [%s] for heater [%s]."
                        % (profile_name, self.outer_instance.short_name)
                    )
                profile = self.profiles.get(default, None)
                defaulted = True
                if profile is None:
                    raise self.outer_instance.gcode.error(
                        "pid_profile: Unknown default "
                        "profile [%s] for heater [%s]."
                        % (default,
                           self.outer_instance.short_name)
                    )
            control = self.outer_instance.lookup_control(profile, load_clean)
            self.outer_instance.set_control(control, keep_target)

            if verbose != 'high' and verbose != 'low':
                return
            if defaulted:
                self.outer_instance.gcode.respond_info("Couldn't find profile "
                                                       "[%s] for heater [%s]"
                                                       ", defaulted to [%s]."
                                                       % (profile_name,
                                                          self
                                                          .outer_instance
                                                          .short_name,
                                                          default))
            self.outer_instance.gcode.respond_info(
                "PID Profile [%s] loaded for heater [%s].\n"
                % (profile['name'],
                   self.outer_instance.short_name)
            )
            if verbose == 'high':
                smooth_time = (self.outer_instance.get_smooth_time()
                               if profile['smooth_time'] is None
                               else profile['smooth_time'])
                msg = ("Target: %.2f\n"
                       "Tolerance: %.4f\n"
                       "Control: %s\n"
                       % (profile['pid_target'],
                          profile['pid_tolerance'],
                          profile['control']))
                if smooth_time is not None:
                    msg += "Smooth Time: %.3f\n" % smooth_time
                msg += ("PID Parameters: pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
                        % (profile['pid_kp'],
                           profile['pid_ki'],
                           profile['pid_kd']))
                self.outer_instance.gcode.respond_info(msg)

        def remove_profile(self, profile_name, gcmd, verbose):
            if profile_name in self.profiles:
                section_name = self._compute_section_name(profile_name)
                self.outer_instance.configfile.remove_section(section_name)
                profiles = dict(self.profiles)
                del profiles[profile_name]
                self.profiles = profiles
                self.outer_instance.gcode.respond_info(
                    "Profile [%s] for heater [%s] "
                    "removed from storage for this session.\n"
                    "The SAVE_CONFIG command will update the printer\n"
                    "configuration and restart the printer"
                    % (profile_name, self.outer_instance.short_name)
                )
            else:
                self.outer_instance.gcode.respond_info(
                    "No profile named [%s] to remove" % profile_name)
        cmd_PID_PROFILE_help = "PID Profile Persistent Storage management"

        def cmd_PID_PROFILE(self, gcmd):
            options = collections.OrderedDict({
                'LOAD': self.load_profile,
                'SAVE': self.save_profile,
                'GET_VALUES': self.get_values,
                'SET_VALUES': self.set_values,
                'REMOVE': self.remove_profile
            })
            for key in options:
                profile_name = gcmd.get(key, None)
                if profile_name is not None:
                    if not profile_name.strip():
                        raise self.outer_instance.gcode.error(
                            "pid_profile: Profile must be specified"
                        )
                    options[key](profile_name, gcmd, True)
                    return
            raise self.outer_instance.gcode.error(
                "pid_profile: Invalid syntax '%s'" % (gcmd.get_commandline(),)
            )


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, profile, heater, load_clean=False):
        self.profile = profile
        self.heater = heater
        self.heater_max_power = self.heater.get_max_power()
        self.max_delta = profile['max_delta']
        self.heating = False

    def temperature_update(self, read_time, temp, target_temp):
        if self.heating and temp >= target_temp + self.max_delta:
            self.heating = False
        elif not self.heating and temp <= target_temp - self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater_max_power)
        else:
            self.heater.set_pwm(read_time, 0.)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return smoothed_temp < target_temp - self.max_delta

    def update_smooth_time(self):
        return

    def get_profile(self):
        return self.profile

    def get_type(self):
        return 'watermark'


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1


class ControlPID:
    def __init__(self, profile, heater, load_clean=False):
        self.profile = profile
        self.heater = heater
        self.heater_max_power = self.heater.get_max_power()
        self.dt = self.heater.pwm_delay
        self.Kp = profile['pid_kp'] / PID_PARAM_BASE
        self.Ki = profile['pid_ki'] / PID_PARAM_BASE
        self.Kd = profile['pid_kd'] / PID_PARAM_BASE
        smooth_time = (self.heater.get_smooth_time()
                       if profile['smooth_time'] is None
                       else profile['smooth_time'])
        self.heater.set_inv_smooth_time(1. / smooth_time)
        self.smooth = 1. + smooth_time / self.dt
        self.prev_temp = (AMBIENT_TEMP
                          if load_clean
                          else self.heater.get_temp(self
                                                    .heater
                                                    .reactor
                                                    .monotonic())[0])
        self.prev_err = 0.
        self.prev_der = 0.
        self.int_sum = 0.

    def temperature_update(self, read_time, temp, target_temp):
        # calculate the error
        err = target_temp - temp
        # calculate the current integral amount using the Trapezoidal rule
        ic = ((self.prev_err + err) / 2.) * self.dt
        i = self.int_sum + ic
        # calculate the current derivative using a modified moving average,
        # and derivative on measurement, to account for derivative kick
        # when the set point changes
        dc = -(temp - self.prev_temp) / self.dt
        dc = ((self.smooth - 1.) * self.prev_der + dc) / self.smooth
        # calculate the output
        o = self.Kp * err + self.Ki * i + self.Kd * dc
        # calculate the saturated output
        so = max(0., min(self.heater_max_power, o))

        # update the heater
        self.heater.set_pwm(read_time, so)
        # update the previous values
        self.prev_temp = temp
        self.prev_der = dc
        if target_temp > 0.:
            self.prev_err = err
            if o == so:
                # not saturated so an update is allowed
                self.int_sum = i
            else:
                # saturated, so conditionally integrate
                if (o > 0.) - (o < 0.) != (ic > 0.) - (ic < 0.):
                    # the signs are opposite so an update is allowed
                    self.int_sum = i
        else:
            self.prev_err = 0.
            self.int_sum = 0.

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_der) > PID_SETTLE_SLOPE)

    def update_smooth_time(self):
        self.smooth = 1. + self.heater.get_smooth_time() / self.dt

    def get_profile(self):
        return self.profile

    def get_type(self):
        return 'pid'


######################################################################
# Velocity (PID) control algo
######################################################################

class ControlVelocityPID:
    def __init__(self, profile, heater, load_clean=False):
        self.profile = profile
        self.heater = heater
        self.heater_max_power = self.heater.get_max_power()
        self.Kp = profile['pid_kp'] / PID_PARAM_BASE
        self.Ki = profile['pid_ki'] / PID_PARAM_BASE
        self.Kd = profile['pid_kd'] / PID_PARAM_BASE
        smooth_time = (self.heater.get_smooth_time()
                       if profile['smooth_time'] is None
                       else profile['smooth_time'])
        self.heater.set_inv_smooth_time(1. / smooth_time)
        self.smooth_time = smooth_time  # smoothing window
        self.temps = (([AMBIENT_TEMP] * 3)
                      if load_clean
                      else ([self.heater.get_temp(self
                                                  .heater
                                                  .reactor
                                                  .monotonic())[0]] * 3))
        self.times = [0.] * 3  # temperature reading times
        self.d1 = 0.  # previous smoothed 1st derivative
        self.d2 = 0.  # previous smoothed 2nd derivative
        self.pwm = 0. if load_clean else self.heater.last_pwm_value

    def temperature_update(self, read_time, temp, target_temp):
        # update the temp and time lists
        self.temps.pop(0)
        self.temps.append(temp)
        self.times.pop(0)
        self.times.append(read_time)

        # calculate the 1st derivative: p part in velocity form
        # note the derivative is of the temp and not the error
        # this is to prevent derivative kick
        d1 = self.temps[-1] - self.temps[-2]

        # calculate the error : i part in velocity form
        error = self.times[-1] - self.times[-2]
        error = error * (target_temp - self.temps[-1])

        # calculate the 2nd derivative: d part in velocity form
        # note the derivative is of the temp and not the error
        # this is to prevent derivative kick
        d2 = self.temps[-1] - 2. * self.temps[-2] + self.temps[-3]
        d2 = d2 / (self.times[-1] - self.times[-2])

        # smooth both the derivatives using a modified moving average
        # that handles unevenly spaced data points
        n = max(1., self.smooth_time / (self.times[-1] - self.times[-2]))
        self.d1 = ((n - 1.) * self.d1 + d1) / n
        self.d2 = ((n - 1.) * self.d2 + d2) / n

        # calculate the output
        p = self.Kp * -self.d1  # invert sign to prevent derivative kick
        i = self.Ki * error
        d = self.Kd * -self.d2  # invert sign to prevent derivative kick

        if target_temp > 0.:
            self.pwm = max(0., min(self.heater_max_power, self.pwm + p + i + d))
        else:
            self.pwm = 0.

        # update the heater
        self.heater.set_pwm(read_time, self.pwm)

    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.d1) > PID_SETTLE_SLOPE)

    def update_smooth_time(self):
        self.smooth_time = self.heater.get_smooth_time()  # smoothing window

    def get_profile(self):
        return self.profile

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
        self.available_monitors = []
        self.has_started = self.have_load_sensors = False
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("gcode:request_restart",
                                            self.turn_off_all_heaters)

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

    def register_monitor(self, config):
        self.available_monitors.append(config.get_name())

    def get_status(self, eventtime):
        return {'available_heaters': self.available_heaters,
                'available_sensors': self.available_sensors,
                'available_monitors': self.available_monitors}

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

    def set_temperature(self, heater, temp, wait=False, gcmd=None):
        if not heater.enabled:
            heater.notify_disabled(gcmd)
            return
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


def load_config(config):
    return PrinterHeaters(config)
