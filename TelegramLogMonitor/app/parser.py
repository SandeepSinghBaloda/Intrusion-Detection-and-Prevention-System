"""Classifies message severity and extracts indicators using rules from config.yaml."""
from __future__ import annotations

from .config import Rules


def classify(text: str, rules: Rules) -> str:
    for level in rules.levels:  # highest rank first
        if any(p.search(text) for p in level.patterns):
            return level.name
    return rules.default_severity


def extract(text: str, rules: Rules) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, pattern in rules.extractors.items():
        values: list[str] = []
        for m in pattern.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            if value and value not in values:
                values.append(value)
        if values:
            found[name] = values
    return found
