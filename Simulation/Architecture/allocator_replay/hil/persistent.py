"""Host-side state helpers for persistent authoritative HIL."""

from __future__ import annotations

from typing import Any

from allocator_replay.capture.codec import canonical_json_bytes


STATE_SECTIONS = (
    "robot_attrs",
    "views",
    "cfg",
    "belief",
    "allocator_attrs",
)

# State transfer already streams large state fields.  Pending allocator
# messages are different: they used to remain in the small setup header and a
# context switch could therefore require one contiguous 20+ KiB JSON buffer on
# the controller.  Apply them after state restore in batches no larger than
# the ordinary part-payload budget.  A single event is never split because a
# physical allocator callback must receive that message atomically.
PERSISTENT_EVENT_BATCH_BYTES = 768


def empty_state() -> dict[str, dict[str, Any]]:
    return {section: {} for section in STATE_SECTIONS}


def event_batches(
    events: list[dict[str, Any]],
    max_bytes: int = PERSISTENT_EVENT_BATCH_BYTES,
) -> list[list[dict[str, Any]]]:
    """Return one bounded setup delta per ordered physical callback event.

    Applying exactly one event per delta preserves native collaborative
    ``event_counter`` and outbound timestamp behavior: a physical receiver
    invokes the allocator callback once per radio message.  Events are atomic
    and therefore cannot be split safely.  Reject an unexpectedly oversized
    event on the host instead of silently recreating an unbounded controller
    setup header.
    """

    if max_bytes < 2:
        raise ValueError("max_bytes must fit an empty JSON array")
    batches: list[list[dict[str, Any]]] = []
    for event in events:
        batch = [event]
        encoded_size = len(canonical_json_bytes(batch))
        if encoded_size > max_bytes:
            raise ValueError(
                "persistent callback event exceeds bounded setup payload: "
                f"{encoded_size} > {max_bytes} bytes"
            )
        batches.append(batch)
    return batches


def state_delta(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Return encoded set/delete changes between two simulator snapshots."""
    changed = empty_state()
    deleted = {section: [] for section in STATE_SECTIONS}
    for section in STATE_SECTIONS:
        old_values = previous.get(section, {})
        new_values = current.get(section, {})
        for name, value in new_values.items():
            if name not in old_values or old_values[name] != value:
                changed[section][name] = value
        deleted[section] = [
            name for name in old_values if name not in new_values
        ]
    return changed, deleted


def merge_minimal_state(
    base: dict[str, Any],
    minimal: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Overlay allocator-owned device state without copying environment maps."""
    merged = {
        section: dict(base.get(section, {}))
        for section in STATE_SECTIONS
    }
    for section in STATE_SECTIONS:
        merged[section].update(minimal.get(section, {}))
    return merged
