"""The classifier interface.

Phase 2b adds Ollama / bart-large-mnli / Jev classifiers implementing this same
protocol, run only over postings the rule classifier marked UNCLEAR.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Decision, Posting


@runtime_checkable
class Classifier(Protocol):
    name: str

    def classify(self, posting: Posting) -> Decision: ...
