"""Classifiers, all behind `classify(posting) -> Decision`."""

from .base import Classifier
from .rules import RuleClassifier, RuleSet, resolve_rules_path

__all__ = ["Classifier", "RuleClassifier", "RuleSet", "resolve_rules_path"]
