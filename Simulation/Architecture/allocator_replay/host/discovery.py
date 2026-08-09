from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Iterable

from allocator_replay.host.transport import DEFAULT_BAUDRATE, ReplayTransportError


DISCOVERY_MARKER = "AR_DISCOVER"


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
    selected = (
        list(descriptions)
        if ports == "auto"
        else [str(port) for port in ports]
    )
    devices: list[DiscoveredDevice] = []
    failures: list[dict[str, str]] = []
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
