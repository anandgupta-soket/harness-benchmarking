"""Experiment matrix: slots (one per task x harness x profile x repetition) and the
seeded, barriered dispatch order. Pure functions; no I/O."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

from .config import HARNESSES


@dataclass(frozen=True)
class Slot:
    slot_id: str
    rep: int
    task: str
    profile_mib: int
    harness: str
    cell: int        # barrier group index: (rep, task, profile)
    order: int       # position within the cell

    @property
    def cell_key(self) -> str:
        return f"r{self.rep}/{self.task}/{self.profile_mib}"


def slot_id(rep: int, task: str, profile: int, harness: str) -> str:
    return f"r{rep}.{task}.{profile}.{harness}"


def build_matrix(tasks: list[str], harnesses: list[str], profiles: list[int],
                 repetitions: int, seed: int) -> list[Slot]:
    """Barrier design: every harness for one (rep, task, profile) cell is dispatched
    together; the next cell starts only when the whole cell has finished. Repetitions
    are blocks (all of rep 1 before rep 2) so a partial run stays balanced. Cell order
    within a repetition and harness order within a cell are seeded shuffles."""
    rng = random.Random(f"{seed}")
    slots: list[Slot] = []
    cell = 0
    for rep in range(1, repetitions + 1):
        cells = [(t, p) for t in tasks for p in profiles]
        rng.shuffle(cells)
        for task, prof in cells:
            hs = list(harnesses)
            rng.shuffle(hs)
            for i, h in enumerate(hs):
                slots.append(Slot(slot_id(rep, task, prof, h), rep, task, prof, h, cell, i))
            cell += 1
    return slots


def expected_count(n_tasks: int, n_harness: int, n_profiles: int, reps: int) -> int:
    return n_tasks * n_harness * n_profiles * reps


def waves(slots_in_cell: Iterable[Slot], max_concurrent: int) -> list[list[Slot]]:
    s = sorted(slots_in_cell, key=lambda x: x.order)
    return [s[i:i + max_concurrent] for i in range(0, len(s), max_concurrent)]


STAGES = ("protocol", "matrix", "scored")


def stage_matrix(data: dict, stage: str, all_tasks: list[str]) -> list[Slot]:
    """Slots for a stage. protocol/matrix are UNSCORED qualification stages."""
    ex = data["experiment"]
    harnesses = list((data.get("matrix") or {}).get("harnesses") or HARNESSES)
    if stage == "single":
        return build_matrix([data["single_trial"]["task"]], harnesses, [int(m) for m in ex["memory_mib"]], 1, int(ex["seed"]))
    if stage == "scored":
        return build_matrix(all_tasks, harnesses, [int(m) for m in ex["memory_mib"]], int(ex["repetitions"]), int(ex["seed"]))
    q = data["qualification"]
    if stage == "protocol":
        return build_matrix([q["protocol"]["task"]], harnesses, [int(q["protocol"]["profile_mib"])], 1, int(ex["seed"]))
    if stage == "matrix":
        m = q["matrix"]
        return build_matrix(list(m["tasks"]), list(m["harnesses"]), [int(x) for x in m["profiles"]],
                            int(m["repetitions"]), int(ex["seed"]))
    raise ValueError(f"unknown stage {stage}")
