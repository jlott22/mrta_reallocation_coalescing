# Pololu 3pi+ 2040 UGV Hardware Assessment

Assessment date: 2026-08-13  
Connected device: Pololu 3pi+ 2040 Robot, USB VID:PID `1FFB:2043`, serial `e4621cb30b4b372f`  
Host interface: `COM11` plus the `POLOLU 00` MicroPython mass-storage volume

## Bottom line

This robot has a real 9-axis inertial sensor suite and two motor-shaft quadrature encoders. It has the hardware needed to implement differential-drive dead reckoning, but it does **not** have a complete onboard dead-reckoning/localization system. It measures switched battery voltage, but it does **not** measure current, electrical power, energy, or state of charge.

The platform is well suited to compact indoor UGV, swarm, motion-control, line-following, and embedded autonomy experiments. It needs additional sensing, communications, and usually a companion computer or radio for general-purpose field UGV work.

## Live verification results

All automated tests below ran on the attached RP2040. Motor PWM was forced to zero throughout the passive diagnostic.

| Capability | Present | Live result | Verdict |
|---|---:|---|---|
| Board and USB | Yes | The board identified itself as `Pololu 3pi+ 2040 Robot with RP2040`; USB serial and mass storage both worked. | Pass |
| CPU and memory interface | Yes | RP2040 ran at 125 MHz; 188,528 bytes of heap were free before tests. | Pass |
| Accelerometer | Yes, 3-axis LSM6DSO | 50 data-ready samples after warm-up; magnitude mean 1.016073 g, standard deviation 0.000499 g. | Pass, calibrate scale/bias for metrology |
| Gyroscope | Yes, 3-axis LSM6DSO | 100 stationary samples; mean bias `(0.293, -0.205, 0.503)` degrees/s, per-axis standard deviation about 0.028–0.040 degrees/s. | Pass, estimate bias at every start |
| Magnetometer/compass | Yes, 3-axis LIS3MDL | Detected and produces changing data. The default +/-4 gauss setting saturated on all axes. At +/-16 gauss it measured about `(6.40, 7.26, 11.72)` gauss, approximately 15.1 gauss total. | Electronics pass; **not usable for heading in the present magnetic environment** |
| IMU I2C bus | Yes | Devices acknowledged at decimal addresses 30 (`0x1E`, LIS3MDL) and 107 (`0x6B`, LSM6DSO). | Pass |
| Quadrature encoders | Yes, two | The repaired official PIO counter initialized and returned stable zero counts during a no-motion window. GPIO states were readable. No wheel transition occurred. | Software pass; motion response pending |
| Installed encoder library | Repaired | Replaced only `pio_quadrature_counter.py` from Pololu upstream commit `9617bf2`; robot and upstream SHA-256 are both `65674076F46ABF326C4D3B471E1318875A2F583C9E555E9EA7EEB8B1836C008C`. | Pass |
| Battery voltage sensing | Voltage only | 50 samples: mean 0.306 V, range 0.288–0.316 V. | ADC path responds, but the switched battery rail is off or unpowered |
| Current/power/energy meter | No | No current shunt monitor, wattmeter, coulomb counter, or fuel gauge is present. | Not available without add-on hardware |
| Five downward IR line sensors | Yes | All five returned the timeout value 1024 in the current placement. | Inconclusive; likely no reflective surface under the sensors, but an emitter/sensor fault is not yet excluded |
| Two forward IR bump sensors | Yes | Raw values were stable near 354 and 933 in the final run. | Read path pass; calibrated press/release response pending |
| Buttons A/B/C | Yes | All read unpressed. | Read path pass; pressed-state test pending |
| OLED, RGB LEDs, buzzer | Yes | SH1106 display, all six RGB command buffers, and GP7 buzzer were commanded successfully; the host received no exceptions. | Command path pass; physical light/display/sound requires observer confirmation |
| RP2040 internal temperature | Yes | Uncalibrated estimate 24.24 degrees C. | Pass as coarse board telemetry, not precision ambient temperature |
| Motors/motor drivers | Yes, two DRV8838 channels | Deliberately not energized: battery rail is unavailable and chassis safety is unknown. | Motion/load test pending |

## What the robot contains

### Navigation and motion sensing

- LSM6DSO 3-axis accelerometer and 3-axis gyroscope.
- LIS3MDL 3-axis magnetometer.
- One 2-channel magnetic quadrature encoder per motor, 12 counts per motor-shaft revolution when all quadrature edges are counted.
- Five downward RC reflectance sensors for a line or nearby floor edge.
- Two forward IR reflectance-based bump sensors built into the flexible bumper skirt.

The IMU hardware is a nine-axis sensor suite, not an AHRS. The library returns raw/scaled acceleration, angular rate, and magnetic-field vectors. It does not calculate a quaternion, fused yaw/pitch/roll, pose, velocity, or position.

### Dead reckoning

There is no turnkey odometry or dead-reckoning service in the loaded Pololu software. The necessary building blocks are present:

1. Convert encoder-count changes to left/right wheel distances using measured wheel circumference and the motor edition's counts per wheel revolution.
2. Compute differential-drive translation and yaw from the two wheel distances and a calibrated effective track width.
3. Fuse gyro yaw rate with encoder yaw; use the accelerometer mainly for attitude/gravity, not double-integrated planar position.
4. Treat the magnetometer only as a corrected, gated heading observation after hard-iron/soft-iron calibration and magnetic-interference checks.
5. Periodically correct drift using an external reference such as fiducials, UWB, motion capture, GNSS (outdoors), or lidar/vision localization.

For a 30:1 Standard Edition motor, Pololu gives approximately 358.3 counts per wheel revolution. Other editions differ, so the installed motor edition must be identified before converting counts to distance. Effective wheel radius and track width should be fitted from straight-line and in-place-turn trials rather than taken only from nominal dimensions.

### Power instrumentation

The on-board reading is `VSW`, the reverse-protected, switched battery voltage, divided by 11 into RP2040 ADC GP26. Four AAA cells are expected (nominally 4.8 V NiMH or 6 V alkaline). An 8 V motor regulator powers the motor drivers, while USB can power logic but cannot power the motors.

This is a **voltmeter only**. For UGV energy experiments, add an external bidirectional current/voltage monitor or coulomb counter on the appropriate battery path, with adequate transient range and sampling bandwidth. Log voltage, current, power, accumulated watt-hours, reset/brownout events, and motor commands against the same monotonic clock.

### Compute, interfaces, and experiment utilities

- Dual-core Arm Cortex-M0+ RP2040 at 125 MHz, 264 kB SRAM, 16 MB flash.
- USB-C device connection with serial REPL/data and MicroPython storage.
- SWD pins for hardware debugging.
- 128x64 OLED, six addressable RGB LEDs, yellow status LED, buzzer, and three user buttons.
- Expansion headers expose shared and unused GPIO, ADC, PWM, UART, SPI, and I2C-capable pins. GP4/GP5 are the occupied IMU I2C bus; GP28/GP29 are a useful free pair for another I2C bus.
- The 3.3 V rail can supply external electronics within the board's power and thermal limits; Pololu specifies up to about 1.5 A available under typical battery-powered conditions.

## Important limitations for a UGV testbed

- No built-in Wi-Fi, Bluetooth, Ethernet, mesh radio, or long-range telemetry.
- No GNSS, UWB, lidar, sonar, camera, optical flow, or absolute pose source.
- No hardware current/power/energy meter.
- No real-time clock with battery backup and no high-quality absolute time source.
- No Linux/ROS runtime; the RP2040 is appropriate for deterministic low-level control and compact experiments, not heavy mapping or vision.
- Small wheels and ball caster make it primarily an indoor, smooth-floor platform rather than an outdoor/rough-terrain UGV.
- Encoder odometry will drift with tire compression, asymmetric wheel diameter, caster effects, collisions, and slip.
- Raw compass heading is especially vulnerable to the motor, encoder magnets, batteries, payload metal, current, and the environment. The live test currently shows severe interference.

## Software and firmware findings

The robot is running Pololu MicroPython 1.24.0 dated 2024-10-25. Pololu's current guide lists a newer 1.27.0 build. A firmware update erases all programs and data, and this board currently contains substantial custom algorithm files, so back up and verify the backup before any firmware replacement.

The encoder failure was local to `pololu_3pi_2040_robot/_lib/pio_quadrature_counter.py`: it imported `ctypes`, which does not exist and was unused. After a complete filesystem backup, that one module was replaced from Pololu upstream commit `9617bf2`. The replacement imports `array`, allocates a signed integer buffer, and passes it to `StateMachine.get`. `robot.Encoders()` now imports and reads correctly on the robot.

## Repair and stationary calibration update

The complete pre-repair robot filesystem is preserved at `artifacts/pololu_backup_20260813_3pi2040_e4621cb30b4b372f` (113 files, 1,433,112 bytes in both source and backup).

The robot now contains `/ugv_calibration.json` and `/ugv_calibrated_imu.py`. A 500-sample stationary gyro calibration and 250-sample +Z-up accelerometer calibration produced:

- Gyro bias: `(0.2906402, -0.21364, 0.4905602)` degrees/s.
- Gyro per-axis stationary standard deviation: approximately `(0.0392, 0.0344, 0.0365)` degrees/s.
- Provisional level accelerometer offset: `(-0.0034289, -0.0115407, 0.0153897)` g.
- Accelerometer magnitude before correction: mean `1.015461` g, standard deviation `0.000466` g.
- A live calibrated read after installation gave gyro `(-0.0106, 0.0036, -0.0006)` degrees/s and acceleration `(0.0008, 0.0009, 1.0006)` g.

The wrapper returns `magnetic_heading_valid = False` and no corrected magnetic vector while the default magnetometer range is saturated. This fail-closed behavior is intentional. A rotating magnetometer calibration away from the present interference, a six-face accelerometer calibration, encoder motion/distance calibration, line reference calibration, and bumper press calibration remain physical procedures.

## Remaining physical validation

The following tests require a person to establish a safe and meaningful physical setup:

1. Install four charged AAA cells, turn on switched power, and confirm the blue motor-regulator LED. Re-run voltage telemetry; expect a plausible pack voltage rather than 0.306 V.
2. Identify the Standard, Turtle, Hyper, or custom motor edition.
3. Secure the chassis with both wheels clear of the floor. Run each motor separately at low PWM in both directions for less than a second while checking encoder direction, count rate, cross-coupling, and stop behavior.
4. Run a measured straight-distance test and several measured in-place rotations to fit distance-per-count, effective track width, wheel asymmetry, and repeatability.
5. Place the five line sensors over white paper and black tape at normal ride height and test raw separation before calibration.
6. Press and release both bumper sides and A/B/C during a timestamped sampling window.
7. Move the robot away from magnets, steel surfaces, speakers, motors, and high-current wiring; rotate it through multiple orientations and repeat the +/-4 gauss compass test. If it still saturates, investigate payload placement or sensor damage.

## Sources

- Pololu 3pi+ 2040 User's Guide: https://www.pololu.com/docs/0J86/all
- Microcontroller: https://www.pololu.com/docs/0J86/6.1
- User interface: https://www.pololu.com/docs/0J86/6.2
- Encoders: https://www.pololu.com/docs/0J86/6.4
- Line and bump sensors: https://www.pololu.com/docs/0J86/6.5
- Inertial sensors: https://www.pololu.com/docs/0J86/6.6
- Power: https://www.pololu.com/docs/0J86/6.7
- Expansion headers and pin assignments: https://www.pololu.com/docs/0J86/6.8 and https://www.pololu.com/docs/0J86/6.9
- Current upstream encoder PIO driver: https://github.com/pololu/pololu-3pi-2040-robot/blob/master/micropython_demo/pololu_3pi_2040_robot/_lib/pio_quadrature_counter.py

## Re-running the passive diagnostic

From this repository on the same Windows host:

```powershell
python -m mpremote connect COM11 run scripts/pololu_ugv_passive_diagnostic.py
```

The diagnostic is read-only with respect to the robot filesystem and never sends non-zero motor PWM.
