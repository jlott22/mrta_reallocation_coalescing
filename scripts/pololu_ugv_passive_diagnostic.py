"""Passive hardware diagnostic for a Pololu 3pi+ 2040 Robot.

Run from a host with:
    python -m mpremote connect COM11 run scripts/pololu_ugv_passive_diagnostic.py

The script never commands non-zero motor PWM.  It emits one JSON object so the
result can be archived and analyzed on the host.
"""

import gc
import math
import os
import sys
import time

try:
    import json
except ImportError:
    import ujson as json

import machine
import array
import rp2
from machine import ADC, I2C, Pin
from pololu_3pi_2040_robot import robot


def summarize_scalar(values):
    count = len(values)
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    return {
        "count": count,
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "stddev": math.sqrt(variance),
    }


def summarize_vectors(vectors):
    return [summarize_scalar([vector[axis] for vector in vectors]) for axis in range(len(vectors[0]))]


def sample_imu():
    imu = robot.IMU()
    detected = imu.detect()
    imu.reset()
    imu.enable_default()
    # Let startup transients settle before measuring noise/stationarity.
    time.sleep_ms(1000)
    if imu.gyro.data_ready():
        imu.gyro.read()
    if imu.acc.data_ready():
        imu.acc.read()
    if imu.mag.data_ready():
        imu.mag.read()
    gyro = []
    acceleration = []
    magnetic = []
    gyro_raw = []
    acceleration_raw = []
    magnetic_raw = []
    started = time.ticks_ms()
    while (len(gyro) < 100 or len(acceleration) < 50 or len(magnetic) < 20):
        if len(gyro) < 100 and imu.gyro.data_ready():
            imu.gyro.read()
            gyro.append(tuple(imu.gyro.last_reading_dps))
            gyro_raw.append(tuple(imu.gyro.last_reading_raw))
        if len(acceleration) < 50 and imu.acc.data_ready():
            imu.acc.read()
            acceleration.append(tuple(imu.acc.last_reading_g))
            acceleration_raw.append(tuple(imu.acc.last_reading_raw))
        if len(magnetic) < 20 and imu.mag.data_ready():
            imu.mag.read()
            magnetic.append(tuple(imu.mag.last_reading_gauss))
            magnetic_raw.append(tuple(imu.mag.last_reading_raw))
        if time.ticks_diff(time.ticks_ms(), started) > 5000:
            break
        time.sleep_ms(1)
    elapsed = time.ticks_diff(time.ticks_ms(), started)
    acceleration_magnitude = [math.sqrt(sum(axis * axis for axis in vector)) for vector in acceleration]
    magnetic_magnitude = [math.sqrt(sum(axis * axis for axis in vector)) for vector in magnetic]
    result = {
        "detected": detected,
        "sample_counts": {"gyro": len(gyro), "acceleration": len(acceleration), "magnetic": len(magnetic)},
        "elapsed_ms": elapsed,
        "gyro_dps": summarize_vectors(gyro),
        "acceleration_g": summarize_vectors(acceleration),
        "acceleration_magnitude_g": summarize_scalar(acceleration_magnitude),
        "magnetic_gauss": summarize_vectors(magnetic),
        "magnetic_magnitude_gauss": summarize_scalar(magnetic_magnitude),
        "first": {"gyro_dps": gyro[0], "acceleration_g": acceleration[0], "magnetic_gauss": magnetic[0]},
        "last": {"gyro_dps": gyro[-1], "acceleration_g": acceleration[-1], "magnetic_gauss": magnetic[-1]},
        "raw_first": {"gyro": gyro_raw[0], "acceleration": acceleration_raw[0], "magnetic": magnetic_raw[0]},
        "raw_last": {"gyro": gyro_raw[-1], "acceleration": acceleration_raw[-1], "magnetic": magnetic_raw[-1]},
        "unique_raw_vectors": {
            "gyro": len(set(gyro_raw)),
            "acceleration": len(set(acceleration_raw)),
            "magnetic": len(set(magnetic_raw)),
        },
    }
    # If the compass is pinned at its default +/-4 gauss limit, repeat at the
    # widest range.  This distinguishes a strong local field from stale data.
    if all(abs(axis) >= 32760 for vector in magnetic_raw for axis in vector):
        imu.mag.set_full_scale(16)
        wide_raw = []
        wide_gauss = []
        wide_started = time.ticks_ms()
        while len(wide_raw) < 10 and time.ticks_diff(time.ticks_ms(), wide_started) < 3000:
            if imu.mag.data_ready():
                imu.mag.read()
                wide_raw.append(tuple(imu.mag.last_reading_raw))
                wide_gauss.append(tuple(imu.mag.last_reading_gauss))
            time.sleep_ms(1)
        result["magnetic_16_gauss_retest"] = {
            "raw_first": wide_raw[0] if wide_raw else None,
            "raw_last": wide_raw[-1] if wide_raw else None,
            "unique_raw_vectors": len(set(wide_raw)),
            "gauss": summarize_vectors(wide_gauss) if wide_gauss else None,
        }
    return result


def sample_battery(sample_count=50, interval_ms=20):
    battery = robot.Battery()
    values = []
    for _ in range(sample_count):
        values.append(battery.get_level_millivolts())
        time.sleep_ms(interval_ms)
    return {"millivolts": summarize_scalar(values)}


class FallbackPIOQuadratureCounter:
    """Current upstream Pololu counter, used if the on-robot copy cannot import."""

    @rp2.asm_pio(autopush=False, autopull=False)
    def counter():
        jmp("update")
        jmp("decrement")
        jmp("increment")
        jmp("update")
        jmp("increment")
        jmp("update")
        jmp("update")
        jmp("decrement")
        jmp("decrement")
        jmp("update")
        jmp("update")
        jmp("increment")
        jmp("update")
        jmp("increment")
        label("decrement")
        jmp(y_dec, "update")
        label("update")
        wrap_target()
        set(x, 0)
        pull(noblock)
        mov(x, osr)
        mov(osr, isr)
        jmp(not_x, "sample_pins")
        mov(isr, y)
        push()
        label("sample_pins")
        mov(isr, null)
        in_(osr, 2)
        in_(pins, 2)
        mov(pc, isr)
        label("increment")
        mov(x, invert(y))
        jmp(x_dec, "increment2")
        label("increment2")
        mov(y, invert(x))
        wrap()
        nop()
        nop()
        nop()

    def __init__(self, state_machine, pin):
        Pin(pin, Pin.IN, Pin.PULL_UP)
        Pin(pin + 1, Pin.IN, Pin.PULL_UP)
        self.state_machine = rp2.StateMachine(
            state_machine, self.counter, freq=125_000_000, in_base=Pin(pin)
        )
        self.buffer = array.array("i", [0])
        self.state_machine.active(1)

    def read(self):
        self.state_machine.put(1)
        self.state_machine.get(self.buffer)
        return self.buffer[0]


class FallbackEncoders:
    def __init__(self):
        self.left = FallbackPIOQuadratureCounter(0, 12)
        self.right = FallbackPIOQuadratureCounter(1, 8)
        self.left_offset = 0
        self.right_offset = 0
        self.get_counts(reset=True)

    def get_counts(self, reset=False):
        left = -self.left.read() - self.left_offset
        right = -self.right.read() - self.right_offset
        if reset:
            self.left_offset += left
            self.right_offset += right
        return [left, right]


def sample_encoders(sample_count=100, interval_ms=20):
    library_error = None
    try:
        encoders = robot.Encoders()
        backend = "on_robot_pololu_library"
    except Exception as error:
        library_error = type(error).__name__ + ": " + str(error)
        encoders = FallbackEncoders()
        backend = "diagnostic_fallback_current_upstream_driver"
    encoders.get_counts(reset=True)
    samples = []
    pin_samples = []
    pins = [Pin(number, Pin.IN, Pin.PULL_UP) for number in (12, 13, 8, 9)]
    started = time.ticks_ms()
    for _ in range(sample_count):
        samples.append(tuple(encoders.get_counts()))
        pin_samples.append(tuple(pin.value() for pin in pins))
        time.sleep_ms(interval_ms)
    elapsed = time.ticks_diff(time.ticks_ms(), started)
    left = [sample[0] for sample in samples]
    right = [sample[1] for sample in samples]
    return {
        "backend": backend,
        "on_robot_library_error": library_error,
        "samples": sample_count,
        "elapsed_ms": elapsed,
        "counts": {"left": summarize_scalar(left), "right": summarize_scalar(right)},
        "start_counts": samples[0],
        "end_counts": samples[-1],
        "delta_counts": (left[-1] - left[0], right[-1] - right[0]),
        "quadrature_pin_order": ["left_A_GPIO12", "left_B_GPIO13", "right_A_GPIO8", "right_B_GPIO9"],
        "quadrature_pin_states_seen": sorted(set(pin_samples)),
    }


def sample_ir(sample_count=20, interval_ms=10):
    line = robot.LineSensors()
    bump = robot.BumpSensors()
    line_values = []
    bump_values = []
    for _ in range(sample_count):
        line_values.append(tuple(line.read()))
        bump_values.append(tuple(bump.read()))
        time.sleep_ms(interval_ms)
    return {
        "line_raw": summarize_vectors(line_values),
        "line_first": line_values[0],
        "line_last": line_values[-1],
        "bump_raw": summarize_vectors(bump_values),
        "bump_first": bump_values[0],
        "bump_last": bump_values[-1],
    }


def read_buttons():
    return {
        "A_pressed": robot.ButtonA().is_pressed(),
        "B_pressed": robot.ButtonB().is_pressed(),
        "C_pressed": robot.ButtonC().is_pressed(),
    }


def read_cpu_temperature():
    raw = ADC(4).read_u16()
    volts = raw * 3.3 / 65535
    celsius = 27 - (volts - 0.706) / 0.001721
    return {"adc_raw": raw, "estimated_celsius": celsius}


def run_test(name, function, results):
    try:
        results[name] = {"ok": True, "result": function()}
    except Exception as error:
        results[name] = {"ok": False, "error_type": type(error).__name__, "error": str(error)}


motors = robot.Motors()
motors.off()

gc.collect()
results = {
    "diagnostic": "pololu_ugv_passive_diagnostic_v2",
    "safety": "Motor PWM was held at zero for the entire diagnostic.",
    "board": {
        "implementation": str(sys.implementation),
        "uname": tuple(os.uname()),
        "machine_frequency_hz": machine.freq(),
        "unique_id_hex": machine.unique_id().hex(),
        "reset_cause": machine.reset_cause(),
        "ram_free_bytes_before_tests": gc.mem_free(),
    },
}

run_test("i2c_scan", lambda: {"addresses_decimal": I2C(0, scl=Pin(5), sda=Pin(4), freq=400_000).scan()}, results)
run_test("imu", sample_imu, results)
run_test("battery_voltage", sample_battery, results)
run_test("encoders_stationary_window", sample_encoders, results)
run_test("infrared_sensors", sample_ir, results)
run_test("buttons", read_buttons, results)
run_test("rp2040_internal_temperature", read_cpu_temperature, results)

gc.collect()
results["board"]["ram_free_bytes_after_tests"] = gc.mem_free()
motors.off()
print("POLOLU_UGV_DIAGNOSTIC_JSON=" + json.dumps(results))
