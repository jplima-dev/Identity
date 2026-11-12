"""Inspect Yasmin's trained capability model without importing or executing Yasmin."""

from __future__ import annotations

import gzip
import json
import math
import re
import unicodedata
from collections import Counter
from itertools import pairwise
from pathlib import Path


def _normalize(value: str) -> str:
    base = unicodedata.normalize("NFKD", value.casefold())
    base = "".join(letter for letter in base if not unicodedata.combining(letter))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", base)).strip()


def _features(value: str) -> Counter[str]:
    words = _normalize(value).split()[:80]
    return Counter([*(f"u:{word}" for word in words),
                    *(f"b:{first}_{second}" for first, second in pairwise(words))])


class OfflineCapabilityModel:
    """Read the model's weights and reproduce its read-only prediction step."""

    def __init__(self, path: Path, expected_labels: list[str]) -> None:
        if not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError(f"Modelo de capacidades ausente ou inválido: {path}")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        labels = data.get("rotulos")
        if (data.get("versao") != 1 or labels != expected_labels
                or not isinstance(data.get("exemplos"), int) or data["exemplos"] < 1):
            raise ValueError("Modelo de capacidades incompatível com o scanner.")
        self.labels = labels
        self.examples = data["exemplos"]
        self.documents = Counter(data["documentos"])
        self.totals = Counter(data["totais"])
        self.counts = {label: Counter(data["contagens"][label]) for label in labels}
        if sum(self.documents.values()) != self.examples:
            raise ValueError("Contagem inválida no modelo de capacidades.")
        self.vocabulary_size = max(len(set().union(*(set(values) for values in self.counts.values()))), 1)

    def predict(self, value: str) -> tuple[str, float]:
        if _normalize(value) in {"yasmin", "yasm", "yas", "oi yasmin", "ei yasmin", "ola yasmin"}:
            return "CHAMADA_CURTA", 1.0
        features = _features(value)
        if not features:
            return "", 0.0
        scores: dict[str, float] = {}
        for label in self.labels:
            if label == "CHAMADA_CURTA":
                continue
            prior = (self.documents[label] + 1) / (self.examples + len(self.labels))
            denominator = self.totals[label] + self.vocabulary_size
            scores[label] = math.log(prior) + sum(
                count * math.log((self.counts[label][feature] + 1) / denominator)
                for feature, count in features.items()
            )
        maximum = max(scores.values())
        weights = {label: math.exp(score - maximum) for label, score in scores.items()}
        total = sum(weights.values())
        predicted = max(weights, key=weights.__getitem__)
        return predicted, weights[predicted] / total
