# Physical allocator adapter

`PhysicalAllocatorAdapter` lets a new physical Bayesian control program use
the **same complete persistent allocator runtime** as HIL. It does not import
or initialize motors, sensors, radio, USB, or other board hardware. Existing
`Hardware/Algorithms/Pololu_*.py` programs are not modified.

The physical control wrapper remains responsible for motion and sensing:

The deployable build exposes the exact factory used by the HIL worker:

```python
from replay_physical_adapter import PhysicalAllocatorAdapter
from replay_physical_factory import create_complete_runtime

allocator = PhysicalAllocatorAdapter(create_complete_runtime)
allocator.reset_trial(
    {
        "algorithm": "CBAA",
        "grid_size": 19,
        "mission": "bayesian",
    },
    initial_allocator_state,
)

# Called by the existing physical loop after movement/sensing/radio updates.
allocator.apply_physical_update(
    delta=compact_environment_delta,
    events=allocator_events,
    messages=decoded_peer_messages,
)
result = allocator.allocate()
physical_goal = result["goal"]
radio_send(result["messages"])
metrics_log(result["metrics"])
```

One adapter belongs to one robot for one trial. `reset_trial` constructs the
runtime once; subsequent updates never recreate it. `choose_goal` times only
the resident runtime call. Applying deltas, decoding radio messages, draining
outbound messages, logging, and motion remain outside the measured interval.

The complete HIL runtime's input schema is also the physical adapter's input
schema. A physical wrapper should therefore convert sensor/control events into
the same compact deltas used by HIL rather than taking or restoring full robot
snapshots. `receive_message` is a convenience for the persistent replay
runtime's `allocator_message` event.

For Bayesian CBAA, ACBBA, PI, HIPC, DMCHBA, and DGA, the factory selects the
complete generated MicroPython study port. For collaborative visit it selects
the compact native 50-target runtime. The HIL worker calls this same factory;
the physical wrapper does not maintain a separate allocator implementation.

The returned metrics include allocator, nested filter, allocator-exclusive
time, filter call/candidate counts, call classification, and free heap. If the
runtime already times its native allocator internally, that authoritative
value is used; otherwise the adapter times `runtime.choose_goal()` with
`ticks_us()`. `wrapper_elapsed_us` remains available as a boundary check.
