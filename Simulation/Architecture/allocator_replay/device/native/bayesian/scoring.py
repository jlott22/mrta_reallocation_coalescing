"""Shared native probability objective for the Bayesian allocators.

This module intentionally avoids CPython-only language features.  It is used by
the stationary HIL runtime and can be copied unchanged into a MicroPython
physical wrapper.
"""

try:
    from math import isfinite
except ImportError:  # pragma: no cover - MicroPython fallback
    def isfinite(value):
        return value == value and value not in (float("inf"), -float("inf"))


PROBABILITY_ALPHA = 8.0


def manhattan(first, second):
    return abs(int(first[0]) - int(second[0])) + abs(
        int(first[1]) - int(second[1])
    )


class NormalizedProbabilityScorer:
    """Cache ``max(target_p)`` and expose the study's shared cost function.

    Call ``replace_probabilities`` when a full belief map arrives and
    ``apply_updates`` when a delta changes individual cells.  The maximum is
    rescanned lazily only when a changed cell may have invalidated it.
    """

    def __init__(self, probabilities=None, grid_size=19, alpha=PROBABILITY_ALPHA):
        self.grid_size = int(grid_size)
        self.alpha = float(alpha)
        self._probabilities = probabilities if probabilities is not None else {}
        self._maximum = 1.0
        self._maximum_valid = False

    @property
    def probabilities(self):
        return self._probabilities

    @property
    def maximum(self):
        if not self._maximum_valid:
            self._refresh_maximum()
        return self._maximum

    def replace_probabilities(self, probabilities, grid_size=None):
        if grid_size is not None:
            self.grid_size = int(grid_size)
        self._probabilities = probabilities if probabilities is not None else {}
        self._maximum_valid = False

    def mark_dirty(self):
        """Invalidate the cache after an owner mutates the map directly."""

        self._maximum_valid = False

    def apply_updates(self, updates):
        """Apply ``cell -> probability`` changes and maintain the cache safely."""

        if updates is None:
            return
        old_maximum = self.maximum
        invalidated = False
        for raw_cell, raw_value in updates.items():
            cell = _cell(raw_cell)
            if cell is None:
                continue
            old_value = self.raw_probability(cell)
            value = _safe_probability(raw_value)
            self._set_probability(cell, value)
            if value > self._maximum:
                self._maximum = value
            if old_value >= old_maximum and value < old_value:
                invalidated = True
        if invalidated:
            self._maximum_valid = False
        elif self._maximum <= 0.0 or not isfinite(self._maximum):
            self._maximum = 1.0
            self._maximum_valid = True

    def raw_probability(self, cell):
        cell = _cell(cell)
        if cell is None:
            return 0.0
        values = self._probabilities
        if isinstance(values, dict):
            if cell in values:
                return _safe_probability(values.get(cell, 0.0))
            text_key = str(cell[0]) + "," + str(cell[1])
            return _safe_probability(values.get(text_key, 0.0))
        try:
            # Native Pololu maps are flat arrays indexed by y * width + x.
            return _safe_probability(
                values[cell[1] * self.grid_size + cell[0]]
            )
        except Exception:
            try:
                return _safe_probability(values[cell[1]][cell[0]])
            except Exception:
                return 0.0

    def normalized_probability(self, cell):
        maximum = self.maximum
        value = self.raw_probability(cell)
        if maximum <= 0.0:
            return 0.0
        normalized = value / maximum
        if normalized <= 0.0:
            return 0.0
        if normalized >= 1.0:
            return 1.0
        return float(normalized)

    def penalty(self, cell):
        return self.alpha * (1.0 - self.normalized_probability(cell))

    def cost(self, distance, cell):
        try:
            distance = float(distance)
        except Exception:
            distance = 0.0
        if distance < 0.0 or not isfinite(distance):
            distance = 0.0
        return distance + self.penalty(cell)

    def score(self, distance, cell):
        """Return the higher-is-better negative adjusted cost."""

        return -self.cost(distance, cell)

    def score_from(self, reference, cell):
        return self.score(manhattan(reference, cell), cell)

    def _refresh_maximum(self):
        maximum = 0.0
        values = self._probabilities
        if isinstance(values, dict):
            iterator = values.values()
        else:
            iterator = values
        try:
            for value in iterator:
                if isinstance(value, (list, tuple)):
                    for nested in value:
                        probability = _safe_probability(nested)
                        if probability > maximum:
                            maximum = probability
                else:
                    probability = _safe_probability(value)
                    if probability > maximum:
                        maximum = probability
        except Exception:
            maximum = 0.0
        self._maximum = maximum if maximum > 0.0 else 1.0
        self._maximum_valid = True
        return self._maximum

    def _set_probability(self, cell, value):
        values = self._probabilities
        if isinstance(values, dict):
            values[cell] = value
            return
        try:
            values[cell[1] * self.grid_size + cell[0]] = value
            return
        except Exception:
            values[cell[1]][cell[0]] = value


def _safe_probability(value):
    try:
        probability = float(value)
    except Exception:
        return 0.0
    if not isfinite(probability) or probability <= 0.0:
        return 0.0
    return probability


def _cell(value):
    try:
        return int(value[0]), int(value[1])
    except Exception:
        if isinstance(value, str):
            try:
                x, y = value.split(",", 1)
                return int(x), int(y)
            except Exception:
                return None
        return None
