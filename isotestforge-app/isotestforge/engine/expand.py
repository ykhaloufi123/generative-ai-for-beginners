"""Expansion d'une suite DSL : héritage (derived_from), mutations et combinatoire pairwise."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..dsl.models import Scenario, Step, Suite
from ..referential.repository import Referential
from . import mutators
from .pairwise import full_product_size, pairwise


@dataclass
class ExpansionReport:
    input_scenarios: int = 0
    output_scenarios: int = 0
    pairwise_saved: int = 0
    skipped: list[str] = field(default_factory=list)


def _find_base(suite: Suite, ref: str) -> tuple[Scenario, str | None]:
    sid, _, step = ref.partition(".")
    for s in suite.scenarios:
        if s.id == sid:
            return s, step or None
    raise ValueError(f"derived_from : scénario {sid} introuvable")


def _with_defaults(s: Scenario, suite: Suite) -> Scenario:
    s = s.model_copy(deep=True)
    s.profiles.card = s.profiles.card or suite.defaults.card
    s.profiles.terminal = s.profiles.terminal or suite.defaults.terminal
    s.profiles.keys = s.profiles.keys or suite.defaults.keys
    return s


def expand_suite(suite: Suite, ref: Referential) -> tuple[list[Scenario], ExpansionReport]:
    rep = ExpansionReport(input_scenarios=len(suite.scenarios))
    resolved: list[Scenario] = []

    # 1) héritage + mutation
    stage1: list[Scenario] = []
    for s in suite.scenarios:
        s = _with_defaults(s, suite)
        if s.derived_from:
            base, step_id = _find_base(suite, s.derived_from)
            base = _with_defaults(base, suite)
            merged = base.model_copy(deep=True)
            merged.id, merged.title = s.id, s.title
            merged.requirement = s.requirement or base.requirement
            merged.tags = sorted(set(base.tags) | set(s.tags))
            merged.profiles = s.profiles.model_copy() if (s.profiles.card or s.profiles.terminal) else merged.profiles
            merged.variables = {**base.variables, **s.variables}
            merged.expand = s.expand or {}
            merged.derived_from = None
            if step_id and not s.mutation:
                merged.steps = [st for st in merged.steps if st.id == step_id] or merged.steps
            if s.steps:
                merged.steps = s.steps
            s2 = merged
            if s.mutation:
                args = dict(s.mutation_args)
                if step_id:
                    args.setdefault("step", step_id)
                s2 = mutators.apply(merged, s.mutation, args, new_id=s.id)
                s2.title = s.title
                s2.requirement = merged.requirement
            if s.expect and s2.steps:
                s2.steps[-1].expect.update(s.expect)
            s2.origin = "mutation" if s.mutation else s.origin
            stage1.append(s2)
        elif s.mutation:
            m = mutators.apply(s, s.mutation, dict(s.mutation_args), new_id=s.id)
            m.title = s.title
            stage1.append(m)
        else:
            if s.expect and s.steps:
                s.steps[-1].expect.update(s.expect)
            stage1.append(s)

    # 2) combinatoire pairwise
    for s in stage1:
        if not s.expand:
            resolved.append(s)
            continue
        dims = {k: list(v) for k, v in s.expand.items()}
        combos = pairwise(dims)
        rep.pairwise_saved += full_product_size(dims) - len(combos)
        for i, combo in enumerate(combos, 1):
            c = s.model_copy(deep=True)
            c.id = f"{s.id}-{i:02d}"
            c.expand = {}
            c.origin = "combinatorial" if s.origin == "manual" else s.origin
            label = []
            for dim, val in combo.items():
                if dim == "card":
                    c.profiles.card = str(val)
                elif dim == "terminal":
                    c.profiles.terminal = str(val)
                elif dim == "amount":
                    for st in c.steps:
                        if st.amount is None and not st.ref:
                            st.amount = float(val)
                else:
                    c.variables[dim] = val
                label.append(f"{dim}={val}")
            c.title = f"{s.title} [{', '.join(label)}]"
            # Filtre de cohérence : on écarte les mutations non applicables à la combinaison
            if c.mutation and c.profiles.card and c.profiles.terminal:
                if not mutators.applicable(c.mutation, ref.card(c.profiles.card), ref.terminal(c.profiles.terminal)):
                    rep.skipped.append(f"{c.id} ({c.mutation} non applicable)")
                    continue
            resolved.append(c)

    # 3) contrôle d'applicabilité des mutations hors combinatoire
    final = []
    for s in resolved:
        if s.mutation and s.profiles.card and s.profiles.terminal and s.mutation in mutators.CATALOG:
            if not mutators.applicable(s.mutation, ref.card(s.profiles.card), ref.terminal(s.profiles.terminal)):
                rep.skipped.append(f"{s.id} ({s.mutation} non applicable à {s.profiles.card}/{s.profiles.terminal})")
                continue
        final.append(s)
    rep.output_scenarios = len(final)
    return final, rep


def auto_negatives(base: Scenario, ref: Referential, names: list[str] | None = None) -> list[Scenario]:
    """Génère automatiquement les cas négatifs applicables à un scénario nominal."""
    card = ref.card(base.profiles.card) if base.profiles.card else None
    term = ref.terminal(base.profiles.terminal) if base.profiles.terminal else None
    out = []
    for name in names or list(mutators.CATALOG):
        if card and term and not mutators.applicable(name, card, term):
            continue
        out.append(mutators.apply(base, name))
    return out


def make_step(**kw) -> Step:
    return Step(**kw)
