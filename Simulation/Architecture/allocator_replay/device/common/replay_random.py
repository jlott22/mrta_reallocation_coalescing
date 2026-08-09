"""CPython-compatible MT19937 operations used by the DGA replay port."""

import math


_N = 624
_M = 397
_MATRIX_A = 0x9908B0DF
_UPPER_MASK = 0x80000000
_LOWER_MASK = 0x7FFFFFFF
_MASK = 0xFFFFFFFF


class Random:
    VERSION = 3

    def __init__(
        self,
        seed=None,
        _restored_state=None,
        _restored_index=None,
    ):
        """Create a seeded generator or adopt one complete MT19937 state.

        MicroPython 1.24 does not expose ``type.__new__`` on user classes, so
        replay context restoration cannot allocate an uninitialized Random
        object the way CPython can.  The private restore arguments let the
        normal, supported class constructor adopt a compact mutable sequence
        of 624 uint32 words without seeding a second state array.
        """

        if _restored_state is not None:
            if len(_restored_state) != _N:
                raise ValueError("invalid random state length")
            restored_index = int(_restored_index)
            if restored_index < 0 or restored_index > _N:
                raise ValueError("invalid random state index")
            # Normalize in place.  The streamed restore path supplies an
            # array('I'), avoiding a 624/625-element pointer container.
            for state_index in range(_N):
                _restored_state[state_index] = (
                    int(_restored_state[state_index]) & _MASK
                )
            self._state = _restored_state
            self._index = restored_index
            return
        self._state = [0] * _N
        self._index = _N
        self.seed(0 if seed is None else seed)

    def seed(self, value=0):
        number = int(value)
        if number < 0:
            number = -number
        key = []
        while number:
            key.append(number & _MASK)
            number >>= 32
        if not key:
            key = [0]
        self._init_by_array(key)

    def _init_genrand(self, seed):
        self._state[0] = seed & _MASK
        for index in range(1, _N):
            previous = self._state[index - 1]
            self._state[index] = (
                1812433253 * (previous ^ (previous >> 30)) + index
            ) & _MASK
        self._index = _N

    def _init_by_array(self, key):
        self._init_genrand(19650218)
        i = 1
        j = 0
        count = _N if _N > len(key) else len(key)
        for _ in range(count):
            previous = self._state[i - 1]
            self._state[i] = (
                (
                    self._state[i]
                    ^ ((previous ^ (previous >> 30)) * 1664525)
                )
                + key[j]
                + j
            ) & _MASK
            i += 1
            j += 1
            if i >= _N:
                self._state[0] = self._state[_N - 1]
                i = 1
            if j >= len(key):
                j = 0
        for _ in range(_N - 1):
            previous = self._state[i - 1]
            self._state[i] = (
                (
                    self._state[i]
                    ^ ((previous ^ (previous >> 30)) * 1566083941)
                )
                - i
            ) & _MASK
            i += 1
            if i >= _N:
                self._state[0] = self._state[_N - 1]
                i = 1
        self._state[0] = 0x80000000
        self._index = _N

    def getstate(self):
        # A seeded generator owns a list, while a context-restored generator
        # owns a compact array('I').  Build the public CPython-compatible
        # tuple directly from either representation; array + list is invalid.
        # This method is not used by the timed persistent output path, which
        # streams ``_state`` in bounded chunks.
        return (
            self.VERSION,
            tuple(
                self._state[state_index]
                if state_index < _N
                else self._index
                for state_index in range(_N + 1)
            ),
            None,
        )

    def setstate(self, state):
        version, internal, gaussian = state
        if int(version) not in (2, 3) or gaussian is not None:
            if int(version) not in (2, 3):
                raise ValueError("unsupported random state")
        values = list(internal)
        if len(values) != _N + 1:
            raise ValueError("invalid random state length")
        self._state = [int(value) & _MASK for value in values[:_N]]
        self._index = int(values[_N])

    def _twist(self):
        state = self._state
        for index in range(_N):
            value = (state[index] & _UPPER_MASK) | (
                state[(index + 1) % _N] & _LOWER_MASK
            )
            state[index] = (
                state[(index + _M) % _N]
                ^ (value >> 1)
                ^ (_MATRIX_A if value & 1 else 0)
            ) & _MASK
        self._index = 0

    def _uint32(self):
        if self._index >= _N:
            self._twist()
        value = self._state[self._index]
        self._index += 1
        value ^= value >> 11
        value ^= (value << 7) & 0x9D2C5680
        value ^= (value << 15) & 0xEFC60000
        value ^= value >> 18
        return value & _MASK

    def random(self):
        upper = self._uint32() >> 5
        lower = self._uint32() >> 6
        return (upper * 67108864.0 + lower) / 9007199254740992.0

    def getrandbits(self, bits):
        bits = int(bits)
        if bits <= 0:
            if bits == 0:
                return 0
            raise ValueError("number of bits must be non-negative")
        if bits <= 32:
            return self._uint32() >> (32 - bits)
        words = (bits - 1) // 32 + 1
        result = 0
        remaining = bits
        for index in range(words):
            value = self._uint32()
            if remaining < 32:
                value >>= 32 - remaining
            result |= value << (32 * index)
            remaining -= 32
        return result

    @staticmethod
    def _bit_length(value):
        count = 0
        while value:
            count += 1
            value >>= 1
        return count

    def _randbelow(self, limit):
        if limit <= 0:
            raise ValueError("empty range")
        bits = self._bit_length(limit)
        value = self.getrandbits(bits)
        while value >= limit:
            value = self.getrandbits(bits)
        return value

    def randrange(self, start, stop=None, step=1):
        if stop is None:
            return self._randbelow(int(start))
        start = int(start)
        stop = int(stop)
        step = int(step)
        width = stop - start
        if step == 1 and width > 0:
            return start + self._randbelow(width)
        if step == 0:
            raise ValueError("zero step")
        count = (width + step - 1) // step if step > 0 else (
            width + step + 1
        ) // step
        if count <= 0:
            raise ValueError("empty range")
        return start + step * self._randbelow(count)

    def choice(self, sequence):
        return sequence[self._randbelow(len(sequence))]

    def shuffle(self, values):
        for index in range(len(values) - 1, 0, -1):
            chosen = self._randbelow(index + 1)
            values[index], values[chosen] = values[chosen], values[index]

    def sample(self, population, count):
        population = list(population)
        count = int(count)
        size = len(population)
        if count < 0 or count > size:
            raise ValueError("sample larger than population")
        result = [None] * count
        set_size = 21
        if count > 5:
            set_size += 4 ** int(math.ceil(math.log(count * 3, 4)))
        if size <= set_size:
            pool = list(population)
            for index in range(count):
                chosen = self._randbelow(size - index)
                result[index] = pool[chosen]
                pool[chosen] = pool[size - index - 1]
        else:
            selected = set()
            for index in range(count):
                chosen = self._randbelow(size)
                while chosen in selected:
                    chosen = self._randbelow(size)
                selected.add(chosen)
                result[index] = population[chosen]
        return result
