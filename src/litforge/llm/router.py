from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from litforge.llm.evidence import EvidencePacket

RoutePolicy = Literal["local_only", "ask_before_cloud", "hybrid"]


class RoutingError(ValueError):
    pass


class CloudApprovalRequired(RoutingError):
    pass


class ModelProfile(BaseModel):
    name: str
    provider: str
    model: str
    remote: bool


class ModelRequest(BaseModel):
    task: str
    evidence: EvidencePacket
    preferred_profile: str | None = None
    allow_cloud: bool = False


class RouteDecision(BaseModel):
    profile: ModelProfile


class ModelRouter:
    def __init__(self, profiles: dict[str, ModelProfile], local_profile: str) -> None:
        if local_profile not in profiles or profiles[local_profile].remote:
            raise RoutingError("local profile must exist and be local")
        self.profiles = profiles
        self.local_profile = local_profile

    def route(self, request: ModelRequest, policy: RoutePolicy, approved: bool = False) -> RouteDecision:
        local = self.profiles[self.local_profile]
        if request.preferred_profile is None:
            return RouteDecision(profile=local)
        if request.preferred_profile not in self.profiles:
            raise RoutingError(f"unknown profile: {request.preferred_profile}")
        chosen = self.profiles[request.preferred_profile]
        if not chosen.remote:
            return RouteDecision(profile=chosen)
        if policy == "local_only":
            raise RoutingError("local_only policy rejects remote profiles")
        if policy == "hybrid" and not request.allow_cloud:
            return RouteDecision(profile=local)
        if not approved and (policy == "ask_before_cloud" or request.evidence.sensitive):
            raise CloudApprovalRequired("remote routing requires explicit approval")
        return RouteDecision(profile=chosen)
