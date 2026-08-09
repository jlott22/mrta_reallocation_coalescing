"""Deterministic, explicitly non-hardware RP2040 substitute for local tests."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from allocator_replay.host.transport import DeviceIdentity, ReplayTransportError


ResultFactory = Callable[
    [Mapping[str, Any], Mapping[str, Any], str], Mapping[str, Any]
]


def _default_result(
    setup: Mapping[str, Any], _: Mapping[str, Any], attempt_id: str
) -> Mapping[str, Any]:
    result = setup.get("mock_device_result")
    if not isinstance(result, Mapping):
        raise RuntimeError("virtual call setup has no mock_device_result")
    value = copy.deepcopy(dict(result))
    value.setdefault("attempt_id", attempt_id)
    return value


class DeterministicVirtualDevice:
    """Split-protocol virtual device with isolated, resettable contexts.

    This class is for software-valid tests and dry runs only.  Its identity
    advertises ``virtual-device`` so reports cannot mistake it for native
    hardware validation.
    """

    def __init__(
        self,
        device_id: str,
        *,
        result_factory: ResultFactory | None = None,
        port: str | None = None,
        build_id: str = "virtual-build",
        firmware_sha256: str = "virtual-firmware",
        module_set_sha256: str = "virtual-modules",
        frequency_hz: int = 125_000_000,
    ) -> None:
        self.identity = DeviceIdentity(
            port=port or f"VIRTUAL:{device_id}",
            device_id=str(device_id),
            build_id=str(build_id),
            implementation="virtual-device",
            frequency_hz=int(frequency_hz),
            heap_free=128_000,
            firmware_sha256=str(firmware_sha256),
        )
        self.port = self.identity.port
        self.module_set_sha256 = str(module_set_sha256)
        self.result_factory = result_factory or _default_result
        self.connected = True
        self.closed = False
        self.active_trial: dict[str, Any] | None = None
        self.contexts: dict[str, dict[str, Any]] = {}
        self.prepared: tuple[dict[str, Any], str] | None = None
        self.seen_attempt_ids: set[str] = set()
        self.reboot_count = 0
        self.prepare_order: list[str] = []

    def _online(self) -> None:
        if self.closed or not self.connected:
            raise ReplayTransportError(f"virtual device {self.port} is disconnected")

    def hello(self) -> DeviceIdentity:
        self._online()
        return self.identity

    def check(self) -> dict[str, Any]:
        self._online()
        return {
            "double_array": True,
            "motors_initialized": False,
            "sensors_initialized": False,
            "heap_free": self.identity.heap_free,
            "actual_module_set_sha256": self.module_set_sha256,
            "expected_module_set_sha256": self.module_set_sha256,
            "timer_unit": "us",
            "timer_resolution_us": 1,
            "timer_monotonic": True,
            "timer_wraparound_safe": True,
            "virtual_device": True,
        }

    def restart_clean_worker(self) -> DeviceIdentity:
        self._online()
        self.active_trial = None
        self.contexts.clear()
        self.prepared = None
        self.seen_attempt_ids.clear()
        self.prepare_order.clear()
        self.reboot_count += 1
        return self.identity

    def begin_persistent_trial(self, config: Mapping[str, Any]) -> None:
        self._online()
        if self.active_trial is not None:
            raise RuntimeError("virtual device already has an active trial")
        robot_ids = tuple(str(item) for item in config.get("robot_ids", ()))
        if len(robot_ids) != 4 or len(set(robot_ids)) != 4:
            raise RuntimeError("virtual device requires four unique logical contexts")
        self.active_trial = copy.deepcopy(dict(config))
        self.contexts = {item: {} for item in robot_ids}
        self.prepared = None
        self.seen_attempt_ids.clear()
        self.prepare_order.clear()

    def prepare_persistent_call(
        self, setup: Mapping[str, Any], attempt_id: str
    ) -> None:
        self._online()
        if self.active_trial is None:
            raise RuntimeError("virtual device has no active trial")
        if self.prepared is not None:
            raise RuntimeError("virtual device already has a prepared call")
        if attempt_id in self.seen_attempt_ids:
            raise RuntimeError("duplicate attempt ID rejected by virtual device")
        context_id = str(setup.get("context_id", ""))
        if context_id not in self.contexts:
            raise RuntimeError("unknown virtual logical context")
        detached = copy.deepcopy(dict(setup))
        self.prepared = detached, str(attempt_id)
        self.seen_attempt_ids.add(str(attempt_id))
        self.prepare_order.append(context_id)

    def run_persistent_ready(
        self, attempt_id: str, timeout_seconds: float
    ) -> dict[str, Any]:
        del timeout_seconds
        self._online()
        if self.prepared is None:
            raise RuntimeError("virtual device has no prepared call")
        setup, prepared_attempt = self.prepared
        self.prepared = None
        if str(attempt_id) != prepared_attempt:
            raise RuntimeError("stale/mismatched run attempt ID")
        context_id = str(setup["context_id"])
        prior = copy.deepcopy(self.contexts[context_id])
        result = copy.deepcopy(
            dict(self.result_factory(copy.deepcopy(setup), prior, attempt_id))
        )
        result.setdefault("attempt_id", attempt_id)
        result.setdefault("status", "completed")
        result.setdefault("failure_type", "")
        if result.get("status") == "completed":
            post = result.get("post_state", setup.get("pre_state", {}))
            self.contexts[context_id] = copy.deepcopy(dict(post))
        return result

    def end_persistent_trial(self) -> None:
        self._online()
        if self.active_trial is None:
            raise RuntimeError("virtual device has no active trial")
        self.active_trial = None
        self.contexts.clear()
        self.prepared = None

    def interrupt(self) -> None:
        self._online()
        self.prepared = None

    def disconnect(self) -> None:
        self.connected = False

    def reconnect(self, *, port: str | None = None) -> None:
        if self.closed:
            raise ReplayTransportError("closed virtual device cannot reconnect")
        self.connected = True
        if port is not None:
            self.identity = DeviceIdentity(
                port=str(port),
                device_id=self.identity.device_id,
                build_id=self.identity.build_id,
                implementation=self.identity.implementation,
                frequency_hz=self.identity.frequency_hz,
                heap_free=self.identity.heap_free,
                firmware_sha256=self.identity.firmware_sha256,
            )
            self.port = str(port)

    def close(self) -> None:
        self.closed = True
        self.connected = False
        self.active_trial = None
        self.contexts.clear()
        self.prepared = None
