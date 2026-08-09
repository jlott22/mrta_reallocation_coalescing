"""Small native scoring cores shared by persistent Bayesian allocators."""

from .scoring import manhattan


def route_distance(origin, path):
    total = 0
    previous = origin
    for cell in path:
        total += manhattan(previous, cell)
        previous = cell
    return float(total)


class CBAACore:
    """CBAA's normalized probability-and-distance bid."""

    def __init__(self, scorer):
        self.scorer = scorer

    def bid(self, origin, cell):
        return self.scorer.score(manhattan(origin, cell), cell)

    def best_candidate(self, origin, candidates, can_claim=None):
        best = None
        best_bid = -float("inf")
        for cell in candidates:
            bid = self.bid(origin, cell)
            if can_claim is not None and not can_claim(cell, bid):
                continue
            if best is None or bid > best_bid or (
                bid == best_bid and cell < best
            ):
                best = cell
                best_bid = bid
        return best, best_bid


class ACBBAInsertionCore:
    """ACBBA route insertion using the same objective as CBAA."""

    def __init__(self, scorer):
        self.scorer = scorer

    def best_insertion(self, origin, path, cell):
        current_distance = route_distance(origin, path)
        best_index = 0
        best_bid = -float("inf")
        for index in range(len(path) + 1):
            candidate = list(path)
            candidate.insert(index, cell)
            marginal = route_distance(origin, candidate) - current_distance
            if marginal < 0.0:
                marginal = 0.0
            bid = self.scorer.score(marginal, cell)
            if bid > best_bid or (bid == best_bid and index < best_index):
                best_index = index
                best_bid = bid
        return best_index, best_bid

    def bid_from_reference(self, reference, cell):
        return self.scorer.score(manhattan(reference, cell), cell)


class PICore:
    """Shared PI route cost; consensus stays in the native PI state machine."""

    def __init__(self, scorer):
        self.scorer = scorer

    def route_cost(self, origin, path):
        total = 0.0
        previous = origin
        for cell in path:
            total += self.scorer.cost(manhattan(previous, cell), cell)
            previous = cell
        return total

    def insertion_cost(self, origin, path, cell, index):
        before = self.route_cost(origin, path)
        candidate = list(path)
        candidate.insert(index, cell)
        return max(0.0, self.route_cost(origin, candidate) - before)


class HIPCCore(ACBBAInsertionCore):
    """HIPC uses the same normalized insertion/reference bid objective."""

    pass
