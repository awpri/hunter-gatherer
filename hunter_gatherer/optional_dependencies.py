"""Actionable errors for explicitly requested optional capabilities.

Keep source-checkout installation hints local: the distribution has not been
published to a package index under the new product name yet.
"""
from __future__ import annotations


class OptionalDependencyError(RuntimeError):
    """A requested capability needs an optional installation extra."""

    def __init__(self, capability: str, extra: str, package: str):
        self.extra = extra
        super().__init__(
            f"{capability} requires {package}. Install h/g with the [{extra}] extra "
            f"(from the source checkout: python -m pip install '.[{extra}]'). "
            "No replacement model or backend was selected."
        )
