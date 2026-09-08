"""Deterministic paired-block scheduling across stable board cohorts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from study.manifests import sha256_file, validate_manifest_set

from .model import CausalConfig, CausalJob, PairedBlock, fingerprint


def _stable_order_key(seed: int, *parts: str) -> str:
    material = ":".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def plan_paired_blocks(config: CausalConfig, *, zero_compute: bool = False) -> list[PairedBlock]:
    """Plan policy blocks, keeping every policy in a block on one board.

    Blocks are assigned round-robin after a deterministic hash shuffle.  Policy
    order is a cyclic Latin rotation whose offset changes for every scheduled
    block; this balances first/last position without separating paired policies.
    """

    index = validate_manifest_set(config.manifest_root)
    scenarios: dict[str, dict[str, Any]] = {}
    releases: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in index["entries"]:
        if entry["manifest_kind"] == "scenario":
            scenarios[entry["trace_id"]] = entry
        elif entry.get("load_id") in config.loads:
            releases[(entry["load_id"], entry["trace_id"])] = entry
    trace_ids = sorted(scenarios)
    if config.trace_limit is not None:
        trace_ids = trace_ids[: config.trace_limit]
    raw_blocks = [
        (algorithm, load_id, trace_id)
        for algorithm in config.algorithms
        for load_id in config.loads
        for trace_id in trace_ids
    ]
    available_block_ids = {
        f"{algorithm}__{load_id}__{trace_id}"
        for algorithm, load_id, trace_id in raw_blocks
    }
    excluded_block_ids = set(config.excluded_completed_blocks)
    unknown_exclusions = excluded_block_ids - available_block_ids
    if unknown_exclusions:
        raise ValueError(
            "continuation excludes unknown blocks: "
            + ", ".join(sorted(unknown_exclusions))
        )
    priority_trace_ids = frozenset(trace_ids[: config.priority_trace_count])
    raw_blocks.sort(
        key=lambda item: (
            item[2] not in priority_trace_ids,
            _stable_order_key(config.schedule_seed, *item),
        )
    )
    blocks: list[PairedBlock] = []
    policy_count = len(config.policies)
    algorithm_index = {value: index for index, value in enumerate(config.algorithms)}
    load_index = {value: index for index, value in enumerate(config.loads)}
    trace_index = {value: index for index, value in enumerate(trace_ids)}
    board_rotation_count: Counter[int] = Counter()
    board_offset = config.schedule_seed % len(config.boards)
    for algorithm, load_id, trace_id in raw_blocks:
        # A crossed Latin assignment balances every algorithm/load/trace
        # dimension across board identity in the final 4 x 3 x 25 design.
        # The hash-sorted order above still decorrelates execution order from
        # condition, while this assignment prevents random board confounding.
        worker_index = (
            algorithm_index[algorithm]
            + load_index[load_id]
            + trace_index[trace_id]
            + board_offset
        ) % len(config.boards)
        board = config.boards[worker_index]
        core_id = config.core_affinities[worker_index]
        # Cycle policy order independently within each board.  For the final
        # 100 blocks/board and five policies, every policy occupies every order
        # position exactly 20 times on each physical board.
        rotation = board_rotation_count[worker_index] % policy_count
        board_rotation_count[worker_index] += 1
        ordered_policies = config.policies[rotation:] + config.policies[:rotation]
        scenario_entry = scenarios[trace_id]
        release_entry = releases[(load_id, trace_id)]
        scenario_path = config.manifest_root / scenario_entry["path"]
        release_path = config.manifest_root / release_entry["path"]
        scenario = _load(scenario_path)
        block_id = f"{algorithm}__{load_id}__{trace_id}"
        jobs: list[CausalJob] = []
        for policy_order_index, policy in enumerate(ordered_policies):
            suffix = "__zero" if zero_compute else ""
            job_id = f"{block_id}__{policy.policy_id}{suffix}"
            jobs.append(CausalJob(
                job_id=job_id,
                block_id=block_id,
                algorithm=algorithm,
                load_id=load_id,
                trace_id=trace_id,
                policy=policy,
                policy_order_index=policy_order_index,
                board_id=board.board_id,
                worker_index=worker_index,
                core_id=core_id,
                scenario_path=scenario_path,
                release_path=release_path,
                scenario_sha256=scenario_entry["sha256"],
                release_sha256=release_entry["sha256"],
                runtime_seed=int(scenario["runtime_seed"]),
                zero_compute=zero_compute,
            ))
        blocks.append(PairedBlock(
            block_id=block_id,
            algorithm=algorithm,
            load_id=load_id,
            trace_id=trace_id,
            board=board,
            worker_index=worker_index,
            core_id=core_id,
            jobs=tuple(jobs),
        ))
    # Build the complete original schedule before removing predecessor blocks.
    # This preserves each unfinished block's board assignment and Latin policy
    # order exactly; filtering earlier would silently rotate later pairs.
    selected = [block for block in blocks if block.block_id not in excluded_block_ids]
    if config.job_allowlist:
        all_job_ids = {job.job_id for block in blocks for job in block.jobs}
        retry_ids = set(config.job_allowlist)
        completed_ids = set(config.sealed_completed_job_ids)
        unknown = (retry_ids | completed_ids) - all_job_ids
        if unknown:
            raise ValueError(
                "checkpoint contains jobs outside the fixed design: "
                + ", ".join(sorted(unknown))
            )
        if retry_ids | completed_ids != all_job_ids:
            raise ValueError(
                "sealed successes and retry jobs do not partition the design"
            )
        selected = []
        for block in blocks:
            jobs = tuple(job for job in block.jobs if job.job_id in retry_ids)
            if jobs:
                selected.append(
                    PairedBlock(
                        block_id=block.block_id,
                        algorithm=block.algorithm,
                        load_id=block.load_id,
                        trace_id=block.trace_id,
                        board=block.board,
                        worker_index=block.worker_index,
                        core_id=block.core_id,
                        jobs=jobs,
                    )
                )
    validate_schedule(config, selected, zero_compute=zero_compute)
    return selected


def validate_schedule(
    config: CausalConfig,
    blocks: list[PairedBlock],
    *,
    zero_compute: bool = False,
) -> dict[str, Any]:
    if not blocks:
        raise ValueError("campaign schedule is empty")
    expected_policy_ids = {policy.policy_id for policy in config.policies}
    seen_jobs: set[str] = set()
    board_counts: Counter[str] = Counter()
    first_positions: dict[str, Counter[str]] = defaultdict(Counter)
    last_positions: dict[str, Counter[str]] = defaultdict(Counter)
    dimension_counts: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for block in blocks:
        if block.board.board_id != config.boards[block.worker_index].board_id:
            raise AssertionError("worker/board binding changed inside schedule")
        observed_policy_ids = {job.policy.policy_id for job in block.jobs}
        if config.job_allowlist:
            if not observed_policy_ids or not observed_policy_ids.issubset(
                expected_policy_ids
            ):
                raise AssertionError(f"retry block has invalid policies: {block.block_id}")
        elif observed_policy_ids != expected_policy_ids:
            raise AssertionError(f"paired block is missing policies: {block.block_id}")
        if any(job.board_id != block.board.board_id for job in block.jobs):
            raise AssertionError("paired policies were split across boards")
        if any(job.zero_compute != zero_compute for job in block.jobs):
            raise AssertionError("zero-compute schedule flag mismatch")
        for job in block.jobs:
            if job.job_id in seen_jobs:
                raise AssertionError(f"duplicate job ID: {job.job_id}")
            seen_jobs.add(job.job_id)
            if sha256_file(job.scenario_path) != job.scenario_sha256:
                raise ValueError(f"scenario hash changed: {job.scenario_path}")
            if sha256_file(job.release_path) != job.release_sha256:
                raise ValueError(f"release hash changed: {job.release_path}")
        board_counts[block.board.board_id] += 1
        first_positions[block.board.board_id][block.jobs[0].policy.policy_id] += 1
        last_positions[block.board.board_id][block.jobs[-1].policy.policy_id] += 1
        dimensions = dimension_counts[block.board.board_id]
        dimensions["algorithm"][block.algorithm] += 1
        dimensions["load"][block.load_id] += 1
        dimensions["trace"][block.trace_id] += 1
    if (
        not config.excluded_completed_blocks
        and not config.job_allowlist
        and max(board_counts.values()) - min(board_counts.values()) > 1
    ):
        raise AssertionError("paired blocks are not approximately balanced across boards")
    if config.stage == "full" and not config.job_allowlist:
        board_ids = [board.board_id for board in config.boards]
        for dimension_name, values in (
            ("algorithm", config.algorithms),
            ("load", config.loads),
            ("trace", sorted({block.trace_id for block in blocks})),
        ):
            for value in values:
                counts = [dimension_counts[board][dimension_name][value] for board in board_ids]
                if max(counts) - min(counts) > 1:
                    raise AssertionError(
                        f"full schedule does not balance {dimension_name}={value} across boards: {counts}"
                    )
    return {
        "schema_version": 1,
        "block_count": len(blocks),
        "job_count": sum(len(block.jobs) for block in blocks),
        "excluded_completed_block_count": len(config.excluded_completed_blocks),
        "excluded_completed_job_count": (
            len(config.excluded_completed_blocks) * len(config.policies)
        ),
        "sealed_completed_job_count": len(config.sealed_completed_job_ids),
        "allowlisted_job_count": len(config.job_allowlist),
        "board_block_counts": dict(sorted(board_counts.items())),
        "first_policy_counts_by_board": {
            board: dict(sorted(counts.items())) for board, counts in sorted(first_positions.items())
        },
        "last_policy_counts_by_board": {
            board: dict(sorted(counts.items())) for board, counts in sorted(last_positions.items())
        },
        "schedule_sha256": fingerprint({
            "config_sha256": config.config_sha256,
            "zero_compute": zero_compute,
            "blocks": [
                {
                    "block_id": block.block_id,
                    "board_id": block.board.board_id,
                    "worker_index": block.worker_index,
                    "core_id": block.core_id,
                    "jobs": [job.identity() for job in block.jobs],
                }
                for block in blocks
            ],
        }),
    }
