from replay_codec import decode_value, encode_value
from replay_types import AllocationDecision


ALGORITHM_PREFIXES = (
    "cbaa_",
    "acbba_",
    "pi_",
    "hipc_",
    "dmchba_",
    "dga_",
    "candidate_count_",
    "max_candidate_cells",
    "_allocation_probability_",
)
EXCLUDED_ATTRS = (
    "_allocation_probability_source_id",
    "_allocation_probability_belief_id",
)
OUTBOUND_METHODS = (
    "make_messages",
    "get_outbound_messages",
    "build_dga_messages",
    "build_hipc_messages",
    "build_acbba_messages",
    "build_cbaa_messages",
    "make_message",
    "get_outbound_message",
    "build_dga_message",
    "build_hipc_message",
    "build_acbba_message",
    "build_cbaa_message",
)
RESTORE_STAGE = ""


def _is_replay_state_name(name):
    for prefix in ALGORITHM_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


class Bag:
    def __init__(self, values=None):
        if values:
            for name, value in values.items():
                setattr(self, name, value)


class ReplayCounters:
    def __init__(self):
        self.candidate_filter_time_us_samples = []


class ReplayRobot:
    def __init__(self, state):
        self.counters = ReplayCounters()
        self.published_messages = []
        self.restore(state)

    def restore(self, state):
        global RESTORE_STAGE
        RESTORE_STAGE = "pop_robot_attrs"
        robot_attrs = state.pop("robot_attrs")
        while robot_attrs:
            RESTORE_STAGE = "next_robot_attr"
            name = next(iter(robot_attrs))
            RESTORE_STAGE = "robot_attr:" + str(name)
            try:
                setattr(self, name, decode_value(robot_attrs.pop(name)))
            except Exception as exc:
                raise RuntimeError(
                    "robot_attr {}: {}".format(name, type(exc).__name__)
                )
        RESTORE_STAGE = "views"
        self._views = _consume_mapping(state.pop("views"), "views")
        RESTORE_STAGE = "cfg"
        self.cfg = Bag(_consume_mapping(state.pop("cfg"), "cfg"))
        RESTORE_STAGE = "belief"
        self.belief = Bag(_consume_mapping(state.pop("belief"), "belief"))
        RESTORE_STAGE = "complete"

    @property
    def known_clues(self):
        return self._views.get("known_clues", [])

    @property
    def searched(self):
        return self._views.get("searched", set())

    @property
    def local_searched(self):
        return self._views.get("local_searched", self.searched)

    @property
    def target_p(self):
        return self._views.get("target_p", {})

    @property
    def peer_positions(self):
        return self._views.get("peer_positions", {})

    @property
    def active_tasks(self):
        return self._views.get("active_tasks", set())

    @property
    def known_obstacles(self):
        return self._views.get("known_obstacles", set())

    @property
    def obstacles(self):
        return self._views.get("obstacles", set())

    @property
    def blocked(self):
        return self._views.get("blocked", set())

    @property
    def blocked_cells(self):
        return self._views.get("blocked_cells", self.blocked)

    def publish_algorithm_message(self, category, payload):
        message = dict(payload)
        if "type" not in message:
            message["type"] = category
        self.published_messages.append(message)


def restore_allocator(allocator, state):
    for name in list(allocator.__dict__):
        delattr(allocator, name)
    for name, value in _consume_mapping(
        state.pop("allocator_attrs"),
        "allocator_attrs",
    ).items():
        setattr(allocator, name, value)


def _consume_mapping(mapping, section):
    decoded = {}
    while mapping:
        name = next(iter(mapping))
        try:
            decoded[name] = decode_value(mapping.pop(name))
        except Exception as exc:
            raise RuntimeError(
                "{} {}: {}".format(section, name, type(exc).__name__)
            )
    return decoded


def outbound_messages(allocator, robot):
    for name in OUTBOUND_METHODS:
        method = getattr(allocator, name, None)
        if not callable(method):
            continue
        payloads = method(robot)
        if payloads is None:
            return []
        if isinstance(payloads, dict):
            return [payloads]
        return [payload for payload in payloads if isinstance(payload, dict)]
    return []


def snapshot_current(robot, allocator, fixture):
    expected = fixture["expected"]
    attrs = {}
    for name in expected["post_robot_attr_names"]:
        if name in EXCLUDED_ATTRS or not hasattr(robot, name):
            continue
        attrs[name] = encode_value(getattr(robot, name))
    allocator_attrs = {}
    for name in expected["post_allocator_attr_names"]:
        if hasattr(allocator, name):
            allocator_attrs[name] = encode_value(getattr(allocator, name))
    pre = fixture["pre_state"]
    return {
        "robot_attrs": attrs,
        "views": pre["views"],
        "cfg": pre["cfg"],
        "belief": pre["belief"],
        "allocator_attrs": allocator_attrs,
    }


def snapshot_mutable(robot, allocator, expected):
    robot_attrs = {}
    for name in expected["post_robot_attr_names"]:
        if name in EXCLUDED_ATTRS or not hasattr(robot, name):
            continue
        robot_attrs[name] = getattr(robot, name)
    allocator_attrs = {}
    for name in expected["post_allocator_attr_names"]:
        if hasattr(allocator, name):
            allocator_attrs[name] = getattr(allocator, name)
    return {
        "robot_attrs": robot_attrs,
        "allocator_attrs": allocator_attrs,
    }


def snapshot_authoritative(robot, allocator):
    """Encode all replay-supported mutable state after a live allocator call."""
    robot_attrs = {}
    for name, value in robot.__dict__.items():
        if (
            name in ("counters", "published_messages", "_views", "cfg", "belief")
            or name in EXCLUDED_ATTRS
        ):
            continue
        if not (
            name in ("rid", "pos", "heading", "grid_size", "current_goal",
                     "last_goal", "last_event", "collision_avoidance_active",
                     "collision_state", "_active_peer_positions")
            or _is_replay_state_name(name)
        ):
            continue
        try:
            robot_attrs[name] = encode_value(value)
        except TypeError:
            pass
    allocator_attrs = {}
    for name, value in allocator.__dict__.items():
        try:
            allocator_attrs[name] = encode_value(value)
        except TypeError:
            pass
    return {
        "robot_attrs": robot_attrs,
        "allocator_attrs": allocator_attrs,
    }
