"""Calibration-aware IMU wrapper for the Pololu 3pi+ 2040.

Deploy this file as /ugv_calibrated_imu.py beside /ugv_calibration.json.
The wrapper refuses to present the magnetometer as heading-valid until a
rotating hard/soft-iron calibration has been recorded.
"""

try:
    import json
except ImportError:
    import ujson as json

from pololu_3pi_2040_robot import robot


def load_calibration(path="/ugv_calibration.json"):
    with open(path, "r") as source:
        return json.loads(source.read())


def subtract(vector, offset):
    return [vector[axis] - offset[axis] for axis in range(3)]


def matrix_vector_multiply(matrix, vector):
    return [sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3)]


class CalibratedIMU:
    def __init__(self, calibration_path="/ugv_calibration.json"):
        self.calibration = load_calibration(calibration_path)
        self.imu = robot.IMU()
        if not self.imu.detect():
            raise RuntimeError("IMU chips did not pass WHO_AM_I detection")
        self.imu.reset()
        self.imu.enable_default()
        self.gyro_dps = [0.0, 0.0, 0.0]
        self.acceleration_g = [0.0, 0.0, 0.0]
        self.magnetic_gauss_uncalibrated = [0.0, 0.0, 0.0]
        self.magnetic_gauss = None
        self.magnetic_heading_valid = False

    def read(self):
        self.imu.read()
        imu_calibration = self.calibration["imu"]
        gyro_calibration = imu_calibration["gyro"]
        acceleration_calibration = imu_calibration["accelerometer"]

        if not gyro_calibration["valid"]:
            raise RuntimeError("Gyroscope calibration is not valid")
        self.gyro_dps = subtract(self.imu.gyro.last_reading_dps, gyro_calibration["bias_dps"])

        if not acceleration_calibration["valid"]:
            raise RuntimeError("Accelerometer calibration is not valid")
        self.acceleration_g = subtract(
            self.imu.acc.last_reading_g,
            acceleration_calibration["provisional_level_offset_g"],
        )

        self.magnetic_gauss_uncalibrated = list(self.imu.mag.last_reading_gauss)
        magnetic_calibration = self.calibration["magnetometer"]
        self.magnetic_heading_valid = magnetic_calibration.get("heading_calibration_valid", False)
        if self.magnetic_heading_valid:
            centered = subtract(
                self.magnetic_gauss_uncalibrated,
                magnetic_calibration["hard_iron_offset_gauss"],
            )
            self.magnetic_gauss = matrix_vector_multiply(
                magnetic_calibration["soft_iron_matrix"], centered
            )
        else:
            self.magnetic_gauss = None

        return {
            "gyro_dps": self.gyro_dps,
            "acceleration_g": self.acceleration_g,
            "magnetic_gauss": self.magnetic_gauss,
            "magnetic_heading_valid": self.magnetic_heading_valid,
        }
