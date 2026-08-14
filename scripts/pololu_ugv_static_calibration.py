"""Generate safe, stationary calibration data on a Pololu 3pi+ 2040.

This script commands zero motor PWM and writes /ugv_calibration.json on the
robot only when the stationary checks pass.  A full six-face accelerometer,
rotating magnetometer, line-target, bumper-press, and wheel-motion calibration
still require guided physical interaction.
"""

import math
import os
import sys
import time

try:
    import json
except ImportError:
    import ujson as json

import machine
from machine import ADC
from pololu_3pi_2040_robot import robot


def scalar_stats(values):
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "count": len(values),
        "mean": mean,
        "stddev": math.sqrt(variance),
        "min": min(values),
        "max": max(values),
    }


def vector_stats(vectors):
    return [scalar_stats([vector[axis] for vector in vectors]) for axis in range(3)]


def new_running_stats(axis_count):
    return {
        "count": 0,
        "mean": [0.0] * axis_count,
        "m2": [0.0] * axis_count,
        "min": [float("inf")] * axis_count,
        "max": [float("-inf")] * axis_count,
    }


def update_running_stats(stats, values):
    stats["count"] += 1
    count = stats["count"]
    for axis in range(len(values)):
        value = values[axis]
        stats["min"][axis] = min(stats["min"][axis], value)
        stats["max"][axis] = max(stats["max"][axis], value)
        delta = value - stats["mean"][axis]
        stats["mean"][axis] += delta / count
        stats["m2"][axis] += delta * (value - stats["mean"][axis])


def finish_running_stats(stats):
    return [
        {
            "count": stats["count"],
            "mean": stats["mean"][axis],
            "stddev": math.sqrt(stats["m2"][axis] / stats["count"]),
            "min": stats["min"][axis],
            "max": stats["max"][axis],
        }
        for axis in range(len(stats["mean"]))
    ]


def wait_and_sample_imu():
    imu = robot.IMU()
    if not imu.detect():
        raise RuntimeError("IMU chips did not pass WHO_AM_I detection")
    imu.reset()
    imu.enable_default()
    time.sleep_ms(2000)

    gyro = new_running_stats(3)
    acceleration = new_running_stats(3)
    acceleration_magnitude = new_running_stats(1)
    started = time.ticks_ms()
    while gyro["count"] < 500 or acceleration["count"] < 250:
        if gyro["count"] < 500 and imu.gyro.data_ready():
            imu.gyro.read()
            update_running_stats(gyro, imu.gyro.last_reading_dps)
        if acceleration["count"] < 250 and imu.acc.data_ready():
            imu.acc.read()
            values = imu.acc.last_reading_g
            update_running_stats(acceleration, values)
            update_running_stats(acceleration_magnitude, [math.sqrt(sum(value * value for value in values))])
        if time.ticks_diff(time.ticks_ms(), started) > 10000:
            raise RuntimeError("Timed out collecting stationary IMU data")
        time.sleep_ms(1)

    gyro_stats = finish_running_stats(gyro)
    acceleration_stats = finish_running_stats(acceleration)
    gyro_bias = [axis["mean"] for axis in gyro_stats]
    acceleration_mean = [axis["mean"] for axis in acceleration_stats]
    magnitude_stats = finish_running_stats(acceleration_magnitude)[0]

    # These limits reject obvious motion but do not turn the level measurement
    # into a substitute for a proper six-face accelerometer calibration.
    stationary = (
        max(axis["stddev"] for axis in gyro_stats) < 0.25
        and max(axis["stddev"] for axis in acceleration_stats) < 0.01
        and abs(magnitude_stats["mean"] - 1.0) < 0.08
    )
    level = abs(acceleration_mean[0]) < 0.05 and abs(acceleration_mean[1]) < 0.05 and acceleration_mean[2] > 0.9

    return imu, {
        "stationary_check_passed": stationary,
        "level_plus_z_check_passed": level,
        "gyro": {
            "samples": gyro["count"],
            "bias_dps": gyro_bias,
            "axis_stats_dps": gyro_stats,
            "application": "corrected_dps = measured_dps - bias_dps",
            "valid": stationary,
        },
        "accelerometer": {
            "samples": acceleration["count"],
            "mean_g": acceleration_mean,
            "magnitude_g": magnitude_stats,
            "provisional_level_offset_g": [
                acceleration_mean[0],
                acceleration_mean[1],
                acceleration_mean[2] - 1.0,
            ],
            "application": "corrected_g = measured_g - provisional_level_offset_g",
            "valid": stationary and level,
            "scope": "Provisional +Z-up level bias only; six-face calibration still required.",
        },
    }


def sample_magnetometer(imu):
    # Retest at the default scale first.  If it saturates, collect diagnostic
    # values at the widest scale but never mark heading calibration valid.
    imu.mag.set_full_scale(4)
    time.sleep_ms(200)
    raw_default = []
    for _ in range(10):
        while not imu.mag.data_ready():
            time.sleep_ms(1)
        imu.mag.read()
        raw_default.append(tuple(imu.mag.last_reading_raw))
    saturated = any(abs(axis) >= 32760 for vector in raw_default for axis in vector)

    result = {
        "default_range_gauss": 4,
        "default_raw_min": [min(vector[axis] for vector in raw_default) for axis in range(3)],
        "default_raw_max": [max(vector[axis] for vector in raw_default) for axis in range(3)],
        "saturated": saturated,
        "heading_calibration_valid": False,
        "reason": "A rotating multi-orientation hard/soft-iron calibration is required.",
    }
    if saturated:
        imu.mag.set_full_scale(16)
        time.sleep_ms(200)
        values = []
        for _ in range(10):
            while not imu.mag.data_ready():
                time.sleep_ms(1)
            imu.mag.read()
            values.append(tuple(imu.mag.last_reading_gauss))
        result["diagnostic_16_gauss_mean"] = [
            sum(vector[axis] for vector in values) / len(values) for axis in range(3)
        ]
        result["reason"] = "Default range saturated; remove magnetic interference before rotating calibration."
    return result


def sample_encoders():
    encoders = robot.Encoders()
    encoders.get_counts(reset=True)
    first = None
    last = None
    for _ in range(100):
        last = tuple(encoders.get_counts())
        if first is None:
            first = last
        time.sleep_ms(10)
    return {
        "library_import_valid": True,
        "stationary_start": first,
        "stationary_end": last,
        "stationary_delta": [last[0] - first[0], last[1] - first[1]],
        "distance_calibration_valid": False,
        "reason": "Wheel rotation and a measured travel distance are required.",
    }


def sample_ir():
    line = robot.LineSensors()
    bump = robot.BumpSensors()
    line_values = new_running_stats(5)
    bump_values = new_running_stats(2)
    for _ in range(100):
        update_running_stats(line_values, line.read())
        update_running_stats(bump_values, bump.read())
        time.sleep_ms(5)
    line_stats = finish_running_stats(line_values)
    bump_stats = finish_running_stats(bump_values)
    proposed_min = [round(axis["mean"] * 1.4) for axis in bump_stats]
    proposed_max = [round(axis["mean"] * 1.6) for axis in bump_stats]
    bump_baseline_usable = all(value <= 1024 for value in proposed_max)

    return {
        "line": {
            "raw_stats": line_stats,
            "calibration_valid": False,
            "reason": "White and black reference surfaces at operating ride height are required.",
        },
        "bump": {
            "unpressed_raw_stats": bump_stats,
            "library_proposed_threshold_min": proposed_min,
            "library_proposed_threshold_max": proposed_max,
            "unpressed_baseline_usable": bump_baseline_usable,
            "calibration_valid": False,
            "reason": "Both unpressed and individually pressed bumper states must be sampled.",
        },
    }


def sample_battery():
    values = []
    battery = robot.Battery()
    for _ in range(50):
        values.append(battery.get_level_millivolts())
        time.sleep_ms(10)
    stats = scalar_stats(values)
    return {
        "millivolts": stats,
        "powered_pack_detected": stats["mean"] > 3000,
        "calibration_valid": False,
        "reason": "Compare an energized battery reading against a calibrated multimeter.",
    }


def cpu_temperature():
    raw = ADC(4).read_u16()
    voltage = raw * 3.3 / 65535
    return 27 - (voltage - 0.706) / 0.001721


motors = robot.Motors()
motors.off()

imu, imu_calibration = wait_and_sample_imu()
calibration = {
    "schema": "pololu_3pi_2040_ugv_calibration_v1",
    "created_date": "2026-08-13",
    "board": {
        "unique_id_hex": machine.unique_id().hex(),
        "implementation": str(sys.implementation),
        "uname": tuple(os.uname()),
        "rp2040_internal_temperature_c": cpu_temperature(),
    },
    "safety": "Motor PWM was held at zero.",
    "imu": imu_calibration,
    "magnetometer": sample_magnetometer(imu),
    "encoders": sample_encoders(),
    "infrared": sample_ir(),
    "battery": sample_battery(),
}

motors.off()
if not imu_calibration["stationary_check_passed"]:
    raise RuntimeError("Stationary check failed; calibration was not written")

with open("/ugv_calibration.json", "w") as output:
    output.write(json.dumps(calibration))

print("POLOLU_UGV_CALIBRATION_JSON=" + json.dumps(calibration))
