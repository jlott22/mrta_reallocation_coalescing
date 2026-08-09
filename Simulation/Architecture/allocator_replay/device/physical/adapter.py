"""Physical-loop adapter for the same persistent runtime used by HIL.

This module intentionally imports no board, motor, sensor, radio, or serial
driver.  A physical control program owns those devices and supplies compact
state deltas and allocator messages here.
"""

try:
    from time import ticks_diff as _platform_ticks_diff
    from time import ticks_us as _platform_ticks_us
except ImportError:  # CPython
    from time import perf_counter_ns

    def _platform_ticks_us():
        return perf_counter_ns() // 1000

    def _platform_ticks_diff(new, old):
        return new - old


_CONSENSUS_ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")


def _platform_heap_free():
    try:
        import gc

        return int(gc.mem_free())
    except (AttributeError, TypeError):
        return None


def _mapping_value(value, names, default=None):
    if not isinstance(value, dict):
        return default
    for name in names:
        if name in value:
            return value[name]
    metrics = value.get("metrics")
    if isinstance(metrics, dict):
        for name in names:
            if name in metrics:
                return metrics[name]
    return default


class PhysicalAllocatorAdapter:
    """Own exactly one persistent allocator runtime for a physical trial.

    ``runtime_factory`` must be the same complete factory deployed for HIL.
    The adapter does not translate allocator state or recreate an allocator
    between calls.  Setup and message draining are deliberately separate from
    the timed ``choose_goal`` call.
    """

    def __init__(
        self,
        runtime_factory,
        ticks_us=None,
        ticks_diff=None,
        heap_free=None,
    ):
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable")
        self._runtime_factory = runtime_factory
        self._ticks_us = ticks_us or _platform_ticks_us
        self._ticks_diff = ticks_diff or _platform_ticks_diff
        self._heap_free = heap_free or _platform_heap_free
        self.runtime = None
        self.config = None
        self.metadata = None
        self.call_index = 0
        self.last_decision = None
        self.last_timing = None

    def reset_trial(self, config, initial_state):
        """Construct one resident runtime at the start of a physical trial."""

        self.end_trial()
        config = dict(config or {})
        if config.get("allow_diagnostic_local_core"):
            raise ValueError(
                "physical trials cannot use diagnostic local allocator cores"
            )
        runtime = self._runtime_factory(config)
        self._validate_runtime(runtime)
        metadata = runtime.reset_trial(config, initial_state)
        try:
            self._validate_complete_allocator(config, runtime, metadata)
        except Exception:
            runtime = None
            raise
        self.runtime = runtime
        self.config = config
        self.metadata = metadata or {}
        self.call_index = 0
        self.last_decision = None
        self.last_timing = None
        return metadata

    def end_trial(self):
        """Release allocator state without touching any physical hardware."""

        self.runtime = None
        self.config = None
        self.metadata = None
        self.call_index = 0
        self.last_decision = None
        self.last_timing = None

    def apply_delta(self, delta):
        """Apply a compact physical-world state delta outside timed allocation."""

        self._require_trial()
        self.runtime.apply_delta(delta or {})

    def apply_physical_update(self, delta=None, events=None, messages=None):
        """Apply movement/sensing events and received allocator messages.

        Messages are represented as ``allocator_message`` events, matching the
        complete persistent replay runtime.  Payloads are not serialized or
        copied by this adapter.
        """

        update = dict(delta or {})
        combined_events = list(update.get("events", ()) or ())
        if events:
            combined_events.extend(events)
        if messages:
            if isinstance(messages, dict):
                messages = (messages,)
            for message in messages:
                if (
                    isinstance(message, dict)
                    and "payload" in message
                    and (
                        "receiver" in message
                        or "kind" in message
                    )
                ):
                    event = dict(message)
                    event.setdefault("kind", "allocator_message")
                else:
                    event = {
                        "kind": "allocator_message",
                        "payload": message,
                    }
                combined_events.append(event)
        if combined_events:
            update["events"] = combined_events
        self.apply_delta(update)

    def receive_message(self, payload, receiver=None):
        """Deliver one radio-decoded allocator message outside timing."""

        event = {
            "kind": "allocator_message",
            "payload": payload,
        }
        if receiver is not None:
            event["receiver"] = receiver
        self.apply_physical_update(events=(event,))

    def choose_goal(self):
        """Run only the resident runtime's allocator and retain timing metrics.

        The allocator's decision is returned unchanged.  Read
        ``timing_metrics`` afterward, or use ``allocate`` to also drain
        outbound messages.
        """

        self._require_trial()
        filter_probe = self._start_filter_probe()
        heap_before = self._heap_free()
        started = self._ticks_us()
        decision = None
        error = None
        try:
            decision = self.runtime.choose_goal()
            return decision
        except BaseException as exc:
            error = exc
            raise
        finally:
            wrapper_elapsed = max(
                0,
                int(self._ticks_diff(self._ticks_us(), started)),
            )
            heap_after = self._heap_free()
            filter_us, filter_calls = self._finish_filter_probe(
                filter_probe, decision
            )
            embedded_total = _mapping_value(
                decision,
                ("allocator_time_us", "total_allocator_time_us"),
                None,
            )
            if embedded_total is None:
                allocator_us = wrapper_elapsed
                timing_source = "physical_adapter"
            else:
                allocator_us = max(0, int(embedded_total))
                timing_source = "native_runtime"
            before_count, after_count = self._candidate_counts(decision)
            call_path = self._call_path(decision)
            self.last_timing = {
                "call_index": int(self.call_index),
                "status": "ok" if error is None else "allocator_error",
                "allocator_time_us": int(allocator_us),
                "total_allocator_time_us": int(allocator_us),
                "candidate_filter_time_us": int(filter_us),
                "filter_time_us": int(filter_us),
                "allocator_exclusive_time_us": max(
                    0, int(allocator_us) - int(filter_us)
                ),
                "candidate_filter_calls": int(filter_calls),
                "candidate_count_before": int(before_count),
                "candidate_count_after": int(after_count),
                "call_path": call_path,
                "call_path_classification": call_path,
                "heap_free_before": heap_before,
                "heap_free_after": heap_after,
                "timing_source": timing_source,
                "wrapper_elapsed_us": int(wrapper_elapsed),
            }
            if error is not None:
                self.last_timing["error_type"] = type(error).__name__
            self.last_decision = decision
            self.call_index += 1

    def drain_messages(self):
        """Drain outbound allocator messages after the timed region."""

        self._require_trial()
        messages = self.runtime.drain_messages()
        if messages is None:
            return []
        return messages

    def allocate(self):
        """Choose a goal, then drain messages without timing the drain."""

        decision = self.choose_goal()
        messages = self.drain_messages()
        return {
            "goal": _mapping_value(decision, ("goal",), decision),
            "decision": decision,
            "messages": messages,
            "metrics": self.timing_metrics(),
        }

    def timing_metrics(self):
        """Return a small copy safe for the physical program's metrics log."""

        if self.last_timing is None:
            return None
        return dict(self.last_timing)

    def snapshot_minimal(self):
        """Delegate the runtime's compact resume/debug snapshot."""

        self._require_trial()
        return self.runtime.snapshot_minimal()

    def _require_trial(self):
        if self.runtime is None:
            raise RuntimeError("reset_trial must be called first")

    @staticmethod
    def _validate_runtime(runtime):
        required = (
            "reset_trial",
            "apply_delta",
            "choose_goal",
            "drain_messages",
            "snapshot_minimal",
        )
        missing = [
            name
            for name in required
            if not callable(getattr(runtime, name, None))
        ]
        if missing:
            raise TypeError(
                "persistent runtime missing methods: " + ", ".join(missing)
            )

    @staticmethod
    def _validate_complete_allocator(config, runtime, metadata):
        algorithm = str(
            config.get("algorithm", config.get("allocator", ""))
        ).upper()
        if getattr(runtime, "allow_local_core", False):
            raise ValueError(
                "physical trials cannot use diagnostic local allocator cores"
            )
        if (
            algorithm in _CONSENSUS_ALGORITHMS
            and isinstance(metadata, dict)
            and metadata.get("complete_adapter") is False
        ):
            raise ValueError(
                algorithm
                + " physical trial requires the complete consensus/message "
                + "allocator adapter"
            )

    def _start_filter_probe(self):
        counters_method = getattr(self.runtime, "timing_counters", None)
        if not callable(counters_method):
            return None
        counters = counters_method()
        samples = getattr(
            counters, "candidate_filter_time_us_samples", None
        )
        if samples is None:
            return None
        return samples, len(samples)

    @staticmethod
    def _finish_filter_probe(probe, decision):
        if probe is not None:
            samples, start = probe
            end = len(samples)
            if end < start:
                start = 0
            total = 0
            for index in range(start, end):
                total += max(0, int(samples[index]))
            return total, max(0, end - start)
        filter_us = _mapping_value(
            decision,
            ("candidate_filter_time_us", "filter_time_us"),
            0,
        )
        filter_calls = _mapping_value(
            decision, ("candidate_filter_calls", "filter_invocations"), 0
        )
        return max(0, int(filter_us or 0)), max(0, int(filter_calls or 0))

    def _candidate_counts(self, decision):
        method = getattr(self.runtime, "candidate_counts", None)
        if callable(method):
            counts = method()
            if counts is not None:
                return int(counts[0] or 0), int(counts[1] or 0)
        before = _mapping_value(
            decision,
            ("candidate_count_before", "candidates_before"),
            0,
        )
        after = _mapping_value(
            decision,
            ("candidate_count_after", "candidates_after"),
            0,
        )
        return int(before or 0), int(after or 0)

    def _call_path(self, decision):
        method = getattr(self.runtime, "call_class", None)
        if callable(method):
            value = method()
            if value:
                return str(value)
        value = _mapping_value(
            decision,
            ("call_path", "call_path_classification", "call_class"),
            None,
        )
        return "unknown" if value is None else str(value)
