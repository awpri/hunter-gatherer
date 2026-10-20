"""Durable coordination for trusted local clients, separate from personal facts."""

from .store import CoordinationError, CoordinationStore

__all__ = ["CoordinationStore", "CoordinationError"]
