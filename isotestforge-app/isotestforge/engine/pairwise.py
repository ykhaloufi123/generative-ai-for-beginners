"""Génération pairwise (toutes les paires de valeurs couvertes) — algorithme glouton déterministe.

Exemple : 6 cartes x 5 terminaux x 4 montants x 3 modes = 360 combinaisons -> ~30 cas.
"""
from __future__ import annotations

from itertools import combinations, product


def pairwise(dimensions: dict[str, list]) -> list[dict]:
    names = list(dimensions)
    values = [list(dimensions[n]) for n in names]
    if not names:
        return [{}]
    if len(names) == 1:
        return [{names[0]: v} for v in values[0]]

    uncovered: set[tuple] = set()
    for (i, j) in combinations(range(len(names)), 2):
        for a, b in product(range(len(values[i])), range(len(values[j]))):
            uncovered.add((i, a, j, b))

    rows: list[list[int]] = []
    while uncovered:
        # On part de la première paire non couverte, puis on complète chaque dimension au mieux
        i0, a0, j0, b0 = min(uncovered)
        row: list[int | None] = [None] * len(names)
        row[i0], row[j0] = a0, b0
        for k in range(len(names)):
            if row[k] is not None:
                continue
            best, best_gain = 0, -1
            for v in range(len(values[k])):
                gain = 0
                for m in range(len(names)):
                    if m == k or row[m] is None:
                        continue
                    key = (m, row[m], k, v) if m < k else (k, v, m, row[m])
                    gain += key in uncovered
                if gain > best_gain:
                    best, best_gain = v, gain
            row[k] = best
        for (i, j) in combinations(range(len(names)), 2):
            uncovered.discard((i, row[i], j, row[j]))
        rows.append(row)  # type: ignore[arg-type]

    return [{names[k]: values[k][r[k]] for k in range(len(names))} for r in rows]


def full_product_size(dimensions: dict[str, list]) -> int:
    n = 1
    for v in dimensions.values():
        n *= max(1, len(v))
    return n
