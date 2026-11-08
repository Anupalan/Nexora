"""Workload classes: lanes with reserved capacity on each deployment (roadmap §6.2).

A class is a named lane (`pms`, `coding`, `extraction`, ...) assigned by an administrator to a project or an
API key (never self-declared by a client). Each class reserves a percentage of every deployment's slots.

Scheduling rules (enforced atomically in Redis, see scheduler.CLAIM_LUA):
  * a free slot goes first to a waiting class that is still under its own reservation (the owner reclaims);
  * a `critical` class (extraction) is served before everything else at each free slot, even before a class
    under its reservation, but never more than `critical_burst` times in a row while another class waits,
    so critical work cannot starve the rest forever. Reserved slots held for recently-active classes are
    still respected: criticality changes the order, not the capacity guarantees;
  * among classes competing for spare slots, lower `priority` number goes first (extraction = 1);
  * within one class, order stays strictly FIFO;
  * idle reserved slots can be borrowed, EXCEPT slots reserved for a class that has been active on that
    deployment within `borrow_idle_after_s` seconds: those are held back, so a chatbot that was just in use
    is not stuck behind a long extraction run. With `borrow_idle_after_s` = 0 nothing is ever held back;
  * running requests are never preempted: a borrowed slot is simply handed back when its request finishes.
"""

import json
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session as DBSession

from app.models import PlatformSetting

SETTING_KEY = "workload_classes"
DEFAULT_CLASS = "standard"
NAME = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")

# Starting point agreed with the operators: PMS 50%, coding 25%, extraction 25%, extraction first when
# classes compete for spare capacity. `standard` is the catch-all for anything not assigned a class.
DEFAULTS = {
    "pms": {"reserved_pct": 50, "priority": 2},
    "coding": {"reserved_pct": 25, "priority": 3},
    "extraction": {"reserved_pct": 25, "priority": 1, "critical": True},
    DEFAULT_CLASS: {"reserved_pct": 0, "priority": 9},
}


@dataclass
class WorkloadConfig:
    classes: dict[str, dict] = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))
    borrow_idle_after_s: int = 60
    critical_burst: int = 4

    def known(self, name: str | None) -> str:
        return name if name in self.classes else DEFAULT_CLASS

    def as_dict(self) -> dict:
        return {"classes": self.classes, "borrow_idle_after_s": self.borrow_idle_after_s,
                "critical_burst": self.critical_burst}


def validate(classes: dict, borrow_idle_after_s: int, critical_burst: int = 4) -> WorkloadConfig:
    if not 2 <= len(classes) + (DEFAULT_CLASS not in classes) <= 16:
        raise ValueError("Define between 1 and 15 classes.")
    out: dict[str, dict] = {}
    for name, c in classes.items():
        if not NAME.match(name):
            raise ValueError(f"Invalid class name '{name}': use lowercase letters, digits, '-' or '_'.")
        pct, pri = int(c.get("reserved_pct", 0)), int(c.get("priority", 5))
        if not 0 <= pct <= 100 or not 1 <= pri <= 99:
            raise ValueError(f"Class '{name}': reserved_pct must be 0-100 and priority 1-99.")
        out[name] = {"reserved_pct": pct, "priority": pri}
        if c.get("critical"):
            out[name]["critical"] = True
    out.setdefault(DEFAULT_CLASS, {"reserved_pct": 0, "priority": 9})
    if sum(c["reserved_pct"] for c in out.values()) > 100:
        raise ValueError("Reserved percentages add up to more than 100.")
    if not 0 <= borrow_idle_after_s <= 86_400:
        raise ValueError("borrow_idle_after_s must be between 0 and 86400.")
    if not 1 <= critical_burst <= 1000:
        raise ValueError("critical_burst must be between 1 and 1000.")
    return WorkloadConfig(out, borrow_idle_after_s, critical_burst)


def load_config(db: DBSession) -> WorkloadConfig:
    row = db.get(PlatformSetting, SETTING_KEY)
    if row is None or not row.value:
        return WorkloadConfig()
    try:
        return validate(row.value.get("classes", {}), int(row.value.get("borrow_idle_after_s", 60)),
                        int(row.value.get("critical_burst", 4)))
    except (ValueError, TypeError):
        return WorkloadConfig()  # a corrupt setting must never take scheduling down


def reserved_slots(max_active: int, cfg: WorkloadConfig) -> dict[str, int]:
    """Whole slots per class on a deployment (largest-remainder rounding, priority breaks ties).

    With very few slots small classes round to zero; raise the deployment's max_active (the node's
    parallelism) to give them a guarantee.
    """
    exact = {n: max_active * c["reserved_pct"] / 100 for n, c in cfg.classes.items()}
    base = {n: int(v + 1e-9) for n, v in exact.items()}
    target = int(sum(exact.values()) + 1e-9)
    leftover = max(0, target - sum(base.values()))
    order = sorted(exact, key=lambda n: (-(exact[n] - base[n]), cfg.classes[n]["priority"], n))
    for n in order[:leftover]:
        if exact[n] > base[n] + 1e-9:
            base[n] += 1
    return base


def claim_json(max_active: int, cfg: WorkloadConfig) -> str:
    """Per-deployment class table handed to the claim script."""
    slots = reserved_slots(max_active, cfg)
    table = {n: {"r": slots[n], "p": c["priority"], "k": 1 if c.get("critical") else 0}
             for n, c in cfg.classes.items()}
    table["_idle"] = cfg.borrow_idle_after_s
    table["_burst"] = cfg.critical_burst
    return json.dumps(table, sort_keys=True)
