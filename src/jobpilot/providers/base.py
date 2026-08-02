"""Provider interface and the degraded-mode implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .schemas import FactBase, TailorConstraints, TailorPlan


class ProviderUnavailable(Exception):
    """No provider can serve this call; the caller parks the work behind a
    blocker and the system keeps functioning manually."""


class ProviderCallFailed(Exception):
    """The provider was called and did not produce usable output."""


@runtime_checkable
class AIProvider(Protocol):
    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan: ...


class NullProvider:
    def tailor(
        self, facts: FactBase, job_md: str, constraints: TailorConstraints
    ) -> TailorPlan:
        raise ProviderUnavailable("no AI provider is configured")
