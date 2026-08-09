from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
GRID_SIZE = 19
ROBOT_COUNT = 4
ROBOT_IDS = ("00", "01", "02", "03")
START_POSITIONS = {
    "00": (0, 0),
    "01": (0, 6),
    "02": (0, 12),
    "03": (0, 18),
}
START_HEADINGS = {rid: (1, 0) for rid in ROBOT_IDS}
ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")
BAYESIAN_TOP_K_LEVELS = (
    ("K=1", 1.0 / 361.0, 1),
    ("1%", 0.01, 4),
    ("3%", 0.03, 11),
    ("5%", 0.05, 18),
    ("10%", 0.10, 36),
    ("25%", 0.25, 90),
    ("50%", 0.50, 181),
    ("75%", 0.75, 271),
    ("100%", 1.0, 361),
)
COLLABORATIVE_TOP_K_LEVELS = (
    ("K=1", 1.0 / 50.0, 1),
    ("K=2", 2.0 / 50.0, 2),
    ("5%", 0.05, 3),
    ("10%", 0.10, 5),
    ("25%", 0.25, 13),
    ("50%", 0.50, 25),
    ("75%", 0.75, 38),
    ("100%", 1.0, 50),
)
INITIAL_HARDWARE_TRIAL_COUNTS = {
    "bayesian": 25,
    "collaborative": 10,
}
CAPTURE_TRIAL_COUNT = 50
BAYESIAN_SEED = 20260727
COLLABORATIVE_SEED = 20311176
BAYESIAN_TRIAL_IDS = tuple(range(500, 550))
COLLABORATIVE_TRIAL_IDS = tuple(range(100, 150))
SIMULATION_SEED = 0
COMMITMENT_HORIZON = 3
CALL_TIMEOUT_SECONDS = 30.0
TIMEOUT_CONFIRMATION_ATTEMPTS = 3
CALIBRATION_MEDIAN_TOLERANCE = 0.05

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RESULTS_ROOT = REPOSITORY_ROOT / "Results" / "HIL" / "AllocatorReplay"
COHORT_ROOT = RESULTS_ROOT / "Cohorts"
TRACE_ROOT = RESULTS_ROOT / "Traces"
CAMPAIGN_ROOT = RESULTS_ROOT / "ActiveCampaigns"
DEVICE_BUILD_ROOT = (
    REPOSITORY_ROOT / "Simulation" / "Architecture" / "allocator_replay" / "builds"
)
BAYESIAN_SIM_ROOT = (
    REPOSITORY_ROOT / "Simulation" / "Architecture" / "simulator"
)
COLLABORATIVE_SIM_ROOT = REPOSITORY_ROOT / "Simulation" / "dcta_benchmark_sim"


@dataclass(frozen=True)
class Condition:
    mission: str
    algorithm: str
    top_k_level: str
    top_k_rate: float
    top_k_cells: int

    @property
    def condition_id(self) -> str:
        if self.top_k_level.startswith("K="):
            return (
                f"{self.mission}_{self.algorithm.lower()}_"
                f"topk_fixed_k{self.top_k_cells}"
            )
        rate = int(round(self.top_k_rate * 100))
        return (
            f"{self.mission}_{self.algorithm.lower()}_"
            f"topk_{rate:03d}_k{self.top_k_cells}"
        )


def conditions(mission: str | None = None) -> list[Condition]:
    result: list[Condition] = []
    mission_specs = (
        ("bayesian", BAYESIAN_TOP_K_LEVELS),
        ("collaborative", COLLABORATIVE_TOP_K_LEVELS),
    )
    for mission_name, levels in mission_specs:
        if mission is not None and mission != mission_name:
            continue
        for algorithm in ALGORITHMS:
            for level, rate, cells in levels:
                result.append(
                    Condition(mission_name, algorithm, level, rate, cells)
                )
    return result


def minimum_capture_trial_count(condition: Condition) -> int:
    """Minimum sealed trace size needed for the current hardware phase."""
    if condition.top_k_level in {"K=1", "K=2", "1%", "3%"}:
        return INITIAL_HARDWARE_TRIAL_COUNTS[condition.mission]
    return CAPTURE_TRIAL_COUNT


def cohort_path(mission: str) -> Path:
    names = {
        "bayesian": "bayesian_g19_r4_c4_trials_500_549_seed20260727.csv",
        "collaborative": (
            "collaborative_g19_r4_t50_trials_100_149_seed20311176.csv"
        ),
    }
    return COHORT_ROOT / names[mission]


def trace_condition_root(condition: Condition) -> Path:
    return TRACE_ROOT / condition.mission / condition.condition_id
