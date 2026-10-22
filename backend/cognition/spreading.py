from __future__ import annotations

import math
from typing import Dict


DAMPING = 0.5
MAX_DEPTH = 3
MIN_ACT = 0.01


class SpreadingActivationEngine:
    def __init__(self, cognitive_store):
        self.store = cognitive_store

    def spread(self, seeds: Dict[str, float]) -> Dict[str, float]:
        activation: Dict[str, float] = dict(seeds)
        visited = set(seeds.keys())
        frontier = list(seeds.items())

        for depth in range(MAX_DEPTH):
            if not frontier:
                break
            next_frontier: list[tuple[str, float]] = []

            for node_id, node_act in frontier:
                if node_act < MIN_ACT:
                    continue

                neighbors = self.store.get_neighbors(node_id)
                if not neighbors:
                    continue
                fan_penalty = 1.0 / max(1.0, math.sqrt(len(neighbors)))

                for neighbor_id, edge_weight in neighbors:
                    propagated = node_act * edge_weight * (DAMPING ** depth) * fan_penalty
                    if propagated < MIN_ACT:
                        continue

                    activation[neighbor_id] = activation.get(neighbor_id, 0.0) + propagated
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        next_frontier.append((neighbor_id, activation[neighbor_id]))

            frontier = next_frontier

        item_scores = {node_id: score for node_id, score in activation.items() if node_id.startswith("item:")}
        if not item_scores:
            return {}

        max_score = max(item_scores.values())
        if max_score <= 0:
            return {}
        return {node_id: score / max_score for node_id, score in item_scores.items()}
