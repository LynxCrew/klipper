# Support fans that are enabled when temperature exceeds a set threshold
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import statistics

from . import fan

KELVIN_TO_CELSIUS = -273.15
MAX_FAN_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.


class TemperatureFan:
    def __init__(self, config, defined_fan=None, super_fan=None):
        self.name = config.get_name().split()[1]
        self.printer = config.get_printer()
        if defined_fan is None:
            self.fan = fan.Fan(config, default_shutdown_speed=1.)
        else:
            self.fan = defined_fan
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.reverse = config.getboolean('reverse', False)
        pheaters = self.printer.load_object(config, 'heaters')
        self.sensor = pheaters.setup_sensor(config)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        pheaters.register_sensor(config, self)
        self.speed_delay = self.sensor.get_report_time_delta()
        self.max_speed_conf = config.getfloat(
            'max_speed', 1., above=0., maxval=1.)
        self.max_speed = self.max_speed_conf
        self.min_speed_conf = config.getfloat(
            'min_speed', 0.3, minval=0., maxval=1.)
        self.min_speed = self.min_speed_conf
        self.last_temp = 0.
        self.last_temp_time = 0.
        self.target_temp_conf = config.getfloat(
            'target_temp', 40. if self.max_temp > 40. else self.max_temp,
            minval=self.min_temp, maxval=self.max_temp)
        self.target_temp = self.target_temp_conf
        algos = {'watermark': ControlBangBang,
                 'pid': ControlPID,
                 'curve': ControlCurve}
        algo = config.getchoice('control', algos)
        self.control = algo(self,
                            config,
                            super_fan)
        self.next_speed_time = 0.
        self.last_speed_value = 0.
        self.enabled = 1
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "SET_TEMPERATURE_FAN_TARGET", "TEMPERATURE_FAN", self.name,
            self.cmd_SET_TEMPERATURE_FAN_TARGET,
            desc=self.cmd_SET_TEMPERATURE_FAN_TARGET_help)
        gcode.register_mux_command(
            "SET_TEMPERATURE_FAN", "TEMPERATURE_FAN", self.name,
            self.cmd_SET_TEMPERATURE_FAN,
            desc=self.cmd_SET_TEMPERATURE_FAN_help)

    def set_speed(self, read_time, value):
        if value <= 0.:
            value = 0.
        elif value < self.min_speed:
            value = self.min_speed
        if self.target_temp <= 0.:
            value = 0.
        if ((read_time < self.next_speed_time or not self.last_speed_value)
                and abs(value - self.last_speed_value) < 0.05):
            # No significant change in value - can suppress update
            return
        speed_time = read_time + self.speed_delay
        self.next_speed_time = speed_time + 0.75 * MAX_FAN_TIME
        self.last_speed_value = value
        self.fan.set_speed(speed_time, value)

    def temperature_callback(self, read_time, temp):
        self.last_temp = temp
        self.control.temperature_callback(read_time, temp)

    def get_temp(self, eventtime):
        return self.last_temp, self.target_temp

    def get_min_speed(self):
        return self.min_speed

    def get_max_speed(self):
        return self.max_speed

    def get_status(self, eventtime):
        status = self.fan.get_status(eventtime)
        status["temperature"] = round(self.last_temp, 2)
        status["target"] = self.target_temp
        return status
    cmd_SET_TEMPERATURE_FAN_TARGET_help = \
        "Sets a temperature fan target and fan speed limits"

    def cmd_SET_TEMPERATURE_FAN_TARGET(self, gcmd):
        target = gcmd.get_float('TARGET', None)
        if target is not None and self.control.get_type() == 'curve':
            raise gcmd.error("Setting Target not supported for control curve")
        min_speed = gcmd.get_float('MIN_SPEED', self.min_speed)
        max_speed = gcmd.get_float('MAX_SPEED', self.max_speed)
        if min_speed > max_speed:
            raise self.printer.command_error(
                "Requested min speed (%.1f) is greater than max speed (%.1f)"
                % (min_speed, max_speed))
        self.set_min_speed(min_speed)
        self.set_max_speed(max_speed)
        self.set_temp(self.target_temp_conf if target is None else target)
    cmd_SET_TEMPERATURE_FAN_help = "Enable or Disable a heater_fan"

    def cmd_SET_TEMPERATURE_FAN(self, gcmd):
        target = gcmd.get_float('TARGET', None)
        min_speed = gcmd.get_float('MIN_SPEED', self.min_speed)
        max_speed = gcmd.get_float('MAX_SPEED', self.max_speed)
        if min_speed > max_speed:
            raise self.printer.command_error(
                "Requested min speed (%.1f) is greater than max speed (%.1f)"
                % (min_speed, max_speed))
        if target is not None and self.control.get_type() == 'curve':
            raise gcmd.error("Setting Target not supported for control curve")
        self.enabled = gcmd.get_int('ENABLE', self.enabled, minval=0, maxval=1)
        self.set_min_speed(min_speed)
        self.set_max_speed(max_speed)
        self.set_temp(self.target_temp_conf if target is None else target)
        if not self.enabled:
            curtime = self.printer.get_reactor().monotonic()
            print_time = self.fan.get_mcu().estimated_print_time(curtime)
            self.fan.set_speed(print_time, 0.0)

    def set_temp(self, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise self.printer.command_error(
                "Requested temperature (%.1f) out of range (%.1f:%.1f)"
                % (degrees, self.min_temp, self.max_temp))
        self.target_temp = degrees

    def set_min_speed(self, speed):
        if speed and (speed < 0. or speed > 1.):
            raise self.printer.command_error(
                "Requested min speed (%.1f) out of range (0.0 : 1.0)"
                % (speed))
        self.min_speed = speed

    def set_max_speed(self, speed):
        if speed and (speed < 0. or speed > 1.):
            raise self.printer.command_error(
                "Requested max speed (%.1f) out of range (0.0 : 1.0)"
                % (speed))
        self.max_speed = speed

######################################################################
# Bang-bang control algo
######################################################################


class ControlBangBang:
    def __init__(self, temperature_fan, config, controlled_fan=None):
        self.temperature_fan = temperature_fan
        self.controlled_fan = (temperature_fan
                               if controlled_fan is None
                               else controlled_fan)
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False

    def temperature_callback(self, read_time, temp):
        current_temp, target_temp = self.temperature_fan.get_temp(read_time)
        temp_diff = target_temp - temp
        if self.temperature_fan.reverse:
            temp_diff = -temp_diff
        if (self.heating
                and temp_diff >= self.max_delta):
            self.heating = False
        elif (not self.heating
              and temp_diff <= -self.max_delta):
            self.heating = True
        if self.heating:
            self.controlled_fan.set_speed(read_time, 0.)
        else:
            self.controlled_fan.set_speed(read_time,
                                          self.temperature_fan.get_max_speed())

    def get_type(self):
        return 'watermark'


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1


class ControlPID:
    def __init__(self, temperature_fan, config, controlled_fan=None):
        self.temperature_fan = temperature_fan
        self.controlled_fan = (temperature_fan
                               if controlled_fan is None
                               else controlled_fan)
        self.Kp = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.min_deriv_time = config.getfloat('pid_deriv_time', 2., above=0.)
        self.temp_integ_max = 0.
        if self.Ki:
            self.temp_integ_max = self.temperature_fan.get_max_speed() / self.Ki
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.

    def temperature_callback(self, read_time, temp):
        current_temp, target_temp = self.temperature_fan.get_temp(read_time)
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature, flip sign if set to reverse
        if self.temperature_fan.reverse:
            temp_diff = target_temp - temp
        else:
            temp_diff = temp - target_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv * (self.min_deriv_time - time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.temp_integ_max, temp_integ))
        # Calculate output
        co = self.Kp * temp_err + self.Ki * temp_integ - self.Kd * temp_deriv
        bounded_co = max(0., min(self.temperature_fan.get_max_speed(), co))
        self.controlled_fan.set_speed(
            read_time, max(self.temperature_fan.get_min_speed(),
                           self.temperature_fan.get_max_speed() - bounded_co))
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ

    def get_type(self):
        return 'pid'


class ControlCurve:
    def __init__(self, temperature_fan, config, controlled_fan=None):
        self.temperature_fan = temperature_fan
        self.controlled_fan = (temperature_fan
                               if controlled_fan is None
                               else controlled_fan)
        self.points = []
        for i in range(1, 99):
            current_point = config.getfloatlist('point%d' % i, None)
            if current_point is None:
                continue
            if len(current_point) != 2:
                raise temperature_fan.printer.config_error(
                    "Point needs to have exactly one temperature and one speed "
                    "value."
                )
            if current_point[0] > temperature_fan.target_temp:
                raise temperature_fan.printer.config_error(
                    "Temperature in point can not exceed target temperature."
                )
            if current_point[0] < temperature_fan.min_temp:
                raise temperature_fan.printer.config_error(
                    "Temperature in point can not fall below min_temp."
                )
            if current_point[1] > temperature_fan.get_max_speed():
                raise temperature_fan.printer.config_error(
                    "Speed in point can not exceed max_speed."
                )
            if current_point[1] < temperature_fan.get_min_speed():
                raise temperature_fan.printer.config_error(
                    "Speed in point can not fall below min_speed."
                )
            self.points.append(current_point)
        self.points.append([temperature_fan.target_temp,
                            temperature_fan.get_max_speed()])
        if len(self.points) < 2:
            raise temperature_fan.printer.config_error(
                "At least two points need to be defined for curve in "
                "temperature_fan."
            )
        self.points.sort(key=lambda p: p[0])
        last_point = [temperature_fan.min_temp, temperature_fan.get_min_speed()]
        for point in self.points:
            if point[1] < last_point[1]:
                raise temperature_fan.printer.config_error(
                    "Points with higher temperatures have to have higher or "
                    "equal speed than points with lower temperatures."
                )
            last_point = point
        self.cooling_hysteresis = config.getfloat('cooling_hysteresis', 0.0)
        self.heating_hysteresis = config.getfloat('heating_hysteresis', 0.0)
        self.smooth_readings = config.getint('smooth_readings', 10, minval=1)
        self.stored_temps = []
        for i in range(self.smooth_readings):
            self.stored_temps.append(0.)
        self.last_temp = 0.

    def temperature_callback(self, read_time, temp):
        current_temp, target_temp = self.temperature_fan.get_temp(read_time)
        temp = self.smooth_temps(temp)
        if temp >= target_temp:
            self.temperature_fan.set_speed(read_time,
                                           self.temperature_fan.get_max_speed())
            return
        below = [self.temperature_fan.min_temp,
                 self.temperature_fan.get_min_speed()]
        above = [self.temperature_fan.max_temp,
                 self.temperature_fan.get_max_speed()]
        for config_temp in self.points:
            if config_temp[0] < temp:
                below = config_temp
            else:
                above = config_temp
                break
        self.controlled_fan.set_speed(read_time, self.interpolate(below,
                                                                  above,
                                                                  temp))

    def interpolate(self, below, above, temp):
        return (((below[1] * (above[0] - temp))
                 + (above[1] * (temp - below[0])))
                / (above[0] - below[0]))

    def smooth_temps(self, current_temp):
        if (self.last_temp - self.cooling_hysteresis
                <=
                current_temp
                <=
                self.last_temp + self.heating_hysteresis):
            temp = self.last_temp
        else:
            temp = current_temp
        self.last_temp = temp
        for i in range(1, len(self.stored_temps)):
            self.stored_temps[i] = self.stored_temps[i - 1]
        self.stored_temps[0] = temp
        return statistics.median(self.stored_temps)

    def get_type(self):
        return 'curve'


def load_config_prefix(config):
    return TemperatureFan(config)
