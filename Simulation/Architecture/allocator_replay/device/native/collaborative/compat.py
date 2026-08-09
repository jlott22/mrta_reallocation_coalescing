"""Small CPython/MicroPython compatibility layer used by native allocators."""

try:
    from time import ticks_diff, ticks_us
except ImportError:  # CPython
    from time import perf_counter_ns

    def ticks_us():
        return perf_counter_ns() // 1000

    def ticks_diff(new, old):
        return new - old


def heap_free():
    """Return free MicroPython heap bytes, or ``None`` on desktop Python."""

    try:
        import gc

        return int(gc.mem_free())
    except (AttributeError, TypeError):
        return None


class NativeRandom:
    """Tiny deterministic RNG with no dependency on ``random.Random``.

    MicroPython builds do not consistently expose ``random.Random``.  Keeping
    the state here also makes a trial resumable and prevents other firmware
    code from perturbing the allocator's random stream.
    """

    def __init__(self, seed=1):
        value = int(seed) & 0xFFFFFFFF
        self.state = value if value else 0x6D2B79F5

    def _next(self):
        value = self.state
        value ^= (value << 13) & 0xFFFFFFFF
        value ^= value >> 17
        value ^= (value << 5) & 0xFFFFFFFF
        self.state = value & 0xFFFFFFFF
        return self.state

    def random(self):
        return self._next() / 4294967296.0

    def randrange(self, start, stop=None):
        if stop is None:
            stop = start
            start = 0
        width = int(stop) - int(start)
        if width <= 0:
            raise ValueError("empty range")
        return int(start) + (self._next() % width)

    def choice(self, values):
        if not values:
            raise IndexError("cannot choose from an empty sequence")
        return values[self.randrange(len(values))]

    def shuffle(self, values):
        index = len(values) - 1
        while index > 0:
            other = self.randrange(index + 1)
            values[index], values[other] = values[other], values[index]
            index -= 1

    def sample(self, values, count):
        if count < 0 or count > len(values):
            raise ValueError("sample larger than population")
        copy = list(values)
        self.shuffle(copy)
        return copy[:count]
