from __future__ import annotations

from math import ceil
from statistics import median
from typing import Dict, List

from known_visit_sim.core.scheduler import TrialState


def _counts(values: Dict[str, int]) -> str:
    return ";".join(f"{key}:{value}" for key, value in sorted(values.items()))


def _cells(cells) -> str:
    return ";".join(f"({cell[0]},{cell[1]})" for cell in cells)


def gini(values: List[float]) -> float:
    ordered = sorted(float(value) for value in values if value >= 0)
    if not ordered or sum(ordered) == 0:
        return 0.0
    n, total = len(ordered), sum(ordered)
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return (2.0 * weighted) / (n * total) - (n + 1.0) / n


def _runtime_stats_ms(samples_ns: List[int]) -> dict:
    samples_ms = sorted(max(0, value) / 1_000_000.0 for value in samples_ns)
    if not samples_ms:
        return {
            "calls": 0,
            "total": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    total = sum(samples_ms)
    p95_index = max(0, ceil(0.95 * len(samples_ms)) - 1)
    return {
        "calls": len(samples_ms),
        "total": total,
        "mean": total / len(samples_ms),
        "median": float(median(samples_ms)),
        "p95": samples_ms[p95_index],
        "max": samples_ms[-1],
    }


def build_rows(state: TrialState, algorithm: str, comm_model: str,
               comm_level: str, scenario_file: str) -> tuple[dict, dict, list[dict], list[dict]]:
    world, robots, bus = state.world, state.robots, state.bus.counters
    common = {
        "trial_id": state.scenario.trial_id,
        "trial_mode": "known_visit",
        "algorithm": algorithm,
        "comm_model": comm_model,
        "comm_level": comm_level,
        "grid_size": state.cfg.grid_size,
        "grid_cells": state.cfg.grid_size ** 2,
        "robot_count": len(state.cfg.robot_ids),
        "target_count": len(world.target_records),
        "condition_id": state.cfg.condition_id,
        "scenario_file": scenario_file,
    }
    completed = len(world.completed_targets)
    all_completed = world.all_targets_completed()
    completion_times = [
        record.first_completion_time_s
        for record in world.target_records.values()
        if record.first_completion_time_s is not None
    ]
    target_counts = {rid: robot.counters.targets_found for rid, robot in robots.items()}
    total_steps = sum(robot.counters.steps_total for robot in robots.values())
    unique_cells = world.unique_cells_searched()

    trial_summary = {
        **common,
        "target_locations": _cells(world.targets),
        "completed_target_count": completed,
        "all_targets_visited": all_completed,
        "final_target_completion_time_s": max(completion_times) if completion_times else "",
        "final_target_completion_sim_time_s": max(completion_times) if completion_times else "",
        "targets_found_by_robot": _counts(target_counts),
        "robot_start_locations": _cells([state.cfg.start_positions[rid] for rid in state.cfg.robot_ids]),
        "robot_end_locations": _cells([robots[rid].pos for rid in state.cfg.robot_ids]),
        "events_processed": state.events_processed,
        "stall_recoveries_total": sum(robot._stall_recovery_count for robot in robots.values()),
    }

    unprotected_attempts = bus.unprotected_delivered_total + bus.dropped_total
    duplicate_target_visits = sum(
        record.duplicate_visits for record in world.target_records.values()
    )
    system = {
        **common,
        "completed_target_count": completed,
        "all_targets_visited": all_completed,
        "final_target_completion_time_s": max(completion_times) if completion_times else "",
        "final_target_completion_sim_time_s": max(completion_times) if completion_times else "",
        "duplicate_target_visits": duplicate_target_visits,
        "target_conflicts": duplicate_target_visits,
        "task_cell_revisits_total": sum(
            robot.counters.task_cell_revisits for robot in robots.values()
        ),
        "total_team_steps": total_steps,
        "max_robot_steps": max((robot.counters.steps_total for robot in robots.values()), default=0),
        "unique_cells_visited": unique_cells,
        "system_revisits": world.system_revisits(),
        "task_cell_replans_total": sum(robot.counters.task_cell_replans for robot in robots.values()),
        "path_replans_total": sum(robot.counters.path_replans for robot in robots.values()),
        "collision_prevention_events": sum(robot.counters.collision_prevention_events for robot in robots.values()),
        "blocked_task_quarantines_total": sum(robot.counters.blocked_task_quarantines for robot in robots.values()),
        "blocked_task_quarantine_time_s_total": sum(robot.counters.blocked_task_quarantine_time_s for robot in robots.values()),
        "maximum_quarantine_level": max((robot.counters.maximum_quarantine_level for robot in robots.values()), default=0),
        "events_processed": state.events_processed,
        "stall_recoveries_total": sum(robot._stall_recovery_count for robot in robots.values()),
        "messages_sent_total": bus.sent_total,
        "messages_delivered_total": bus.delivered_total,
        "messages_dropped_total": bus.dropped_total,
        "protected_messages_sent_total": bus.protected_sent_total,
        "unprotected_messages_sent_total": bus.unprotected_sent_total,
        "core_messages_sent_total": bus.core_sent_total,
        "allocation_messages_sent_total": bus.allocation_sent_total,
        "message_drop_fraction": bus.dropped_total / unprotected_attempts if unprotected_attempts else 0.0,
        "messages_per_completed_target": bus.sent_total / completed if completed else 0.0,
        "allocation_messages_per_completed_target": bus.allocation_sent_total / completed if completed else 0.0,
        "messages_sent_by_topic": _counts(bus.sent_by_topic),
        "workload_gini_targets_found": gini(list(target_counts.values())),
        "workload_gini_unique_cells_contributed": gini([
            robot.counters.unique_cells_contributed for robot in robots.values()
        ]),
        "allocator_calls_total": sum(
            len(robot.counters.allocator_time_ns_samples) for robot in robots.values()
        ),
        "allocator_time_ms_team_total": sum(
            sum(robot.counters.allocator_time_ns_samples) for robot in robots.values()
        ) / 1_000_000.0,
        "allocator_time_ms_team_max": max(
            (
                max(robot.counters.allocator_time_ns_samples, default=0)
                for robot in robots.values()
            ),
            default=0,
        ) / 1_000_000.0,
        "allocator_solve_time_ms_team_total": sum(
            sum(robot.counters.allocator_solve_time_ns_samples) for robot in robots.values()
        ) / 1_000_000.0,
        "allocator_solve_time_ms_team_max": max(
            (
                max(robot.counters.allocator_solve_time_ns_samples, default=0)
                for robot in robots.values()
            ),
            default=0,
        ) / 1_000_000.0,
        "candidate_filter_calls_total": sum(
            len(robot.counters.candidate_filter_time_ns_samples) for robot in robots.values()
        ),
        "candidate_filter_time_ms_team_total": sum(
            sum(robot.counters.candidate_filter_time_ns_samples) for robot in robots.values()
        ) / 1_000_000.0,
        "candidate_filter_time_ms_team_max": max(
            (
                max(robot.counters.candidate_filter_time_ns_samples, default=0)
                for robot in robots.values()
            ),
            default=0,
        ) / 1_000_000.0,
    }

    robot_rows = []
    for rid, robot in sorted(robots.items()):
        counters = robot.counters
        allocator = _runtime_stats_ms(counters.allocator_time_ns_samples)
        solve = _runtime_stats_ms(counters.allocator_solve_time_ns_samples)
        candidate_filter = _runtime_stats_ms(counters.candidate_filter_time_ns_samples)
        robot_rows.append({
            **common,
            "robot_id": rid,
            "steps_total": counters.steps_total,
            "targets_found": counters.targets_found,
            "task_cell_revisits": counters.task_cell_revisits,
            "unique_cells_contributed": counters.unique_cells_contributed,
            "system_revisits_by_robot": counters.system_revisits_by_robot,
            "task_cell_replans": counters.task_cell_replans,
            "path_replans": counters.path_replans,
            "collision_prevention_events": counters.collision_prevention_events,
            "blocked_task_quarantines": counters.blocked_task_quarantines,
            "blocked_task_quarantine_time_s": counters.blocked_task_quarantine_time_s,
            "maximum_quarantine_level": counters.maximum_quarantine_level,
            "stall_recoveries": robot._stall_recovery_count,
            "messages_sent": bus.sent_by_robot.get(rid, 0),
            "core_messages_sent": bus.core_sent_by_robot.get(rid, 0),
            "allocation_messages_sent": bus.allocation_sent_by_robot.get(rid, 0),
            "messages_delivered_to_robot": bus.delivered_to_robot.get(rid, 0),
            "messages_dropped_to_robot": bus.dropped_to_robot.get(rid, 0),
            "allocator_calls": allocator["calls"],
            "allocator_time_ms_total": allocator["total"],
            "allocator_time_ms_mean": allocator["mean"],
            "allocator_time_ms_median": allocator["median"],
            "allocator_time_ms_p95": allocator["p95"],
            "allocator_time_ms_max": allocator["max"],
            "allocator_solve_time_ms_total": solve["total"],
            "allocator_solve_time_ms_mean": solve["mean"],
            "allocator_solve_time_ms_median": solve["median"],
            "allocator_solve_time_ms_p95": solve["p95"],
            "allocator_solve_time_ms_max": solve["max"],
            "candidate_filter_calls": candidate_filter["calls"],
            "candidate_filter_time_ms_total": candidate_filter["total"],
            "candidate_filter_time_ms_mean": candidate_filter["mean"],
            "candidate_filter_time_ms_median": candidate_filter["median"],
            "candidate_filter_time_ms_p95": candidate_filter["p95"],
            "candidate_filter_time_ms_max": candidate_filter["max"],
        })

    target_rows = []
    for record in sorted(world.target_records.values(), key=lambda item: item.index):
        target_rows.append({
            **common,
            "target_index": record.index,
            "target_x": record.cell[0],
            "target_y": record.cell[1],
            "first_completion_time_s": record.first_completion_time_s if record.completed else "",
            "first_completion_sim_time_s": record.first_completion_time_s if record.completed else "",
            "first_found_by_robot": record.first_found_by or "",
            "first_finder_robot_id": record.first_found_by or "",
            "total_visits": record.total_visits,
            "duplicate_visits": record.duplicate_visits,
            "completed": record.completed,
        })
    return trial_summary, system, robot_rows, target_rows
