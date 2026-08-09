Cell = tuple


class AllocationDecision:
    def __init__(self, goal, debug=None):
        self.goal = goal
        self.debug = {} if debug is None else debug
