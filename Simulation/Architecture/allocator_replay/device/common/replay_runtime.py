try:
    import time
except ImportError:  # pragma: no cover
    time = None


def ticks_us():
    if hasattr(time, "ticks_us"):
        return time.ticks_us()
    return time.perf_counter_ns() // 1000


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


def timed_candidate_filter(method):
    def wrapper(self, robot, *args, **kwargs):
        started = ticks_us()
        try:
            return method(self, robot, *args, **kwargs)
        finally:
            elapsed = max(0, ticks_diff(ticks_us(), started))
            counters = getattr(robot, "counters", None)
            samples = getattr(
                counters,
                "candidate_filter_time_us_samples",
                None,
            )
            if samples is not None:
                samples.append(elapsed)

    return wrapper
