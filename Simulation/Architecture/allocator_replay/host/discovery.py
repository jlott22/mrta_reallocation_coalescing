from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Iterable

from allocator_replay.host.transport import DEFAULT_BAUDRATE, ReplayTransportError


DISCOVERY_MARKER = "AR_DISCOVER"
# Auto-discovery must never send REPL control bytes to arbitrary serial
# devices. Raspberry Pi's RP2040 USB VID covers the MicroPython firmware used
# on the Pololu 3pi+ 2040. PID 0x0005 is Raspberry Pi's registered Pico
# MicroPython CDC firmware. An explicitly named port is treated as the
# operator's narrow allow-list for a board with any custom USB identity.
SAFE_AUTO_USB_IDS = frozenset(((0x2E8A, 0x0005),))


@dataclass(frozen=True)
class DiscoveredDevice:
    port: str
    description: str
    device_id: str
    implementation: str
    version: str
    mpy_abi: int
    frequency_hz: int

    @property
    def compatibility(self) -> str:
        match = re.match(r"(\d+)\.(\d+)", self.version)
        if not match:
            return ""
        return f"{match.group(1)}.{match.group(2)}"

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["compatibility"] = self.compatibility
        return value


def _serial_dependencies():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover - host dependency
        raise RuntimeError(
            "pyserial is required; install allocator_replay/requirements-host.txt"
        ) from exc
    return serial, list_ports


def available_ports() -> list[tuple[str, str]]:
    _, list_ports = _serial_dependencies()
    return sorted(
        ((item.device, item.description or "") for item in list_ports.comports()),
        key=lambda item: item[0],
    )


def safe_auto_ports() -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Return only recognized RP2040/Pololu ports plus skipped audit rows."""

    _, list_ports = _serial_dependencies()
    selected: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    for item in sorted(list_ports.comports(), key=lambda value: value.device):
        port = str(item.device)
        description = str(item.description or "")
        vid = getattr(item, "vid", None)
        pid = getattr(item, "pid", None)
        if (vid, pid) in SAFE_AUTO_USB_IDS:
            selected.append((port, description))
        else:
            usb_id = (
                "unknown"
                if vid is None
                else f"{int(vid):04x}:{int(pid or 0):04x}"
            )
            skipped.append(
                {
                    "port": port,
                    "error": (
                        "not probed by safe auto-discovery "
                        f"(USB VID:PID {usb_id}); pass the exact port explicitly "
                        "only after confirming it is a replay RP2040"
                    ),
                }
            )
    return selected, skipped


def _query_port(port: str, description: str) -> DiscoveredDevice:
    serial, _ = _serial_dependencies()
    connection = serial.Serial(
        port,
        baudrate=DEFAULT_BAUDRATE,
        timeout=0.20,
        write_timeout=2.0,
        dsrdtr=False,
        rtscts=False,
    )
    command = (
        "import machine,sys,ubinascii;"
        "print('AR_DISCOVER|'+ubinascii.hexlify(machine.unique_id()).decode()"
        "+'|'+str(sys.implementation.name)"
        "+'|'+'.'.join(str(x) for x in sys.implementation.version[:3])"
        "+'|'+str(getattr(sys.implementation,'_mpy',0))"
        "+'|'+str(machine.freq()))\r\n"
    )
    try:
        connection.reset_input_buffer()
        # A replay worker launched through raw REPL returns to the raw prompt
        # when interrupted or after EXIT.  CTRL-B is harmless at a friendly
        # prompt and guarantees that discovery always evaluates its query in
        # the friendly REPL, as advertised.
        connection.write(b"\x03\x03\x02\r\n")
        time.sleep(0.10)
        connection.write(command.encode("ascii"))
        connection.flush()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            raw = connection.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            marker = line.find(DISCOVERY_MARKER + "|")
            if marker < 0:
                continue
            fields = line[marker:].split("|")
            if len(fields) < 6:
                continue
            try:
                mpy_abi = int(fields[4])
                frequency_hz = int(fields[5])
            except ValueError:
                # Friendly REPLs may echo the discovery source before
                # printing its result. Ignore that echoed marker.
                continue
            return DiscoveredDevice(
                port=port,
                description=description,
                device_id=fields[1],
                implementation=fields[2],
                version=fields[3],
                mpy_abi=mpy_abi,
                frequency_hz=frequency_hz,
            )
        raise ReplayTransportError(f"{port} did not identify as MicroPython")
    finally:
        connection.close()


def discover(
    ports: Iterable[str] | str = "auto",
) -> tuple[list[DiscoveredDevice], list[dict[str, str]]]:
    descriptions = dict(available_ports())
    if ports == "auto":
        safe, failures = safe_auto_ports()
        selected = [item[0] for item in safe]
        descriptions.update(dict(safe))
    else:
        selected = [str(port) for port in ports]
        failures = []
    devices: list[DiscoveredDevice] = []
    for port in selected:
        try:
            device = _query_port(port, descriptions.get(port, ""))
            if device.implementation != "micropython":
                raise ReplayTransportError(
                    f"{port} runs {device.implementation}, not MicroPython"
                )
            devices.append(device)
        except Exception as exc:
            failures.append({"port": port, "error": str(exc)})
    duplicate_ids = {
        device.device_id
        for device in devices
        if sum(other.device_id == device.device_id for other in devices) > 1
    }
    if duplicate_ids:
        raise RuntimeError(
            "duplicate machine.unique_id values: " + ", ".join(duplicate_ids)
        )
    return devices, failures


def common_compatibility(devices: list[DiscoveredDevice]) -> str:
    versions = {device.compatibility for device in devices}
    if not devices or "" in versions:
        raise RuntimeError("could not detect a MicroPython compatibility version")
    if len(versions) != 1:
        raise RuntimeError(
            "connected devices use different MicroPython versions: "
            + ", ".join(sorted(versions))
        )
    return next(iter(versions))
