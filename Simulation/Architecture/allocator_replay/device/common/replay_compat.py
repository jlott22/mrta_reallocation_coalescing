def deepcopy(value):
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, tuple):
        return tuple(deepcopy(item) for item in value)
    if isinstance(value, list):
        return [deepcopy(item) for item in value]
    if isinstance(value, set):
        return set(deepcopy(item) for item in value)
    if isinstance(value, dict):
        return {deepcopy(key): deepcopy(item) for key, item in value.items()}
    copier = getattr(value, "copy", None)
    if callable(copier):
        return copier()
    raise TypeError("unsupported deepcopy value: " + type(value).__name__)


class _CmpKey:
    __slots__ = ("value", "comparison")

    def __init__(self, value, comparison):
        self.value = value
        self.comparison = comparison

    def __lt__(self, other):
        return self.comparison(self.value, other.value) < 0

    def __gt__(self, other):
        return self.comparison(self.value, other.value) > 0

    def __eq__(self, other):
        return self.comparison(self.value, other.value) == 0

    def __le__(self, other):
        return self.comparison(self.value, other.value) <= 0

    def __ge__(self, other):
        return self.comparison(self.value, other.value) >= 0


def cmp_to_key(comparison):
    return lambda value: _CmpKey(value, comparison)
