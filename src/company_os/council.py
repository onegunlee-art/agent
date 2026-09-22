"""Deterministic, role-owned council synthesis.

Each executive owns explicit VentureContract fields.  Independent responses
therefore do not need byte-identical payloads, while genuinely shared fields
still require a CEO resolution when they disagree.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .storage import payload_fingerprint


ROLE_FIELD_OWNERS: dict[str, tuple[str, ...]] = {
    "cto": (
        "claims",
        "hard_constraints",
        "inherited_conventions",
        "decomposition",
        "highest_risk_assumption",
        "pass_condition",
        "fail_condition",
        "stop_condition",
        "revisit_condition",
        "decision_basis",
        "operations_check",
        "rights_and_authority_basis",
        "worst_realistic_loss",
        "reversibility_assessment",
        "specialist_review_status",
        "evidence_pack",
    ),
    "cpo": (
        "observable_problem",
        "metric",
        "zero_based_alternative",
        "do_nothing_option",
        "rights_and_legal_check",
    ),
    "cmo": (
        "cheapest_valid_experiment",
        "financial_check",
        "strategy_check",
    ),
}

SHARED_FIELDS = ("decision_level",)


def council_ignored_fields(
    contributions: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    """Return hash-only dropped fields and deterministic non-owner conflicts."""

    field_owner = {
        field: role
        for role, fields in ROLE_FIELD_OWNERS.items()
        for field in fields
    }
    ignored: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, str]] = []
    for role, owned_fields in ROLE_FIELD_OWNERS.items():
        owned = set(owned_fields).union(SHARED_FIELDS)
        contribution = contributions[role]
        role_ignored: dict[str, str] = {}
        for field in sorted(contribution):
            if field in owned:
                continue
            opinion_hash = payload_fingerprint(contribution[field])
            role_ignored[field] = opinion_hash
            owner_role = field_owner.get(field)
            if owner_role is None or field not in contributions[owner_role]:
                continue
            owner_hash = payload_fingerprint(contributions[owner_role][field])
            if owner_hash != opinion_hash:
                conflicts.append(
                    {
                        "field": field,
                        "owner_role": owner_role,
                        "owner_value_hash": owner_hash,
                        "opinion_role": role,
                        "opinion_value_hash": opinion_hash,
                    }
                )
        ignored[role] = role_ignored
    conflicts.sort(key=lambda item: (item["field"], item["opinion_role"]))
    return ignored, conflicts


@dataclass(frozen=True, slots=True)
class CouncilMergeConflict(ValueError):
    conflicts: dict[str, dict[str, Any]]

    def __str__(self) -> str:
        return "Council shared fields conflict: " + ", ".join(self.conflicts)


def merge_council_responses(
    responses: Mapping[str, Mapping[str, Any]],
    *,
    response_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one contract using stable field ownership and full provenance."""

    missing_roles = [role for role in ROLE_FIELD_OWNERS if role not in responses]
    if missing_roles:
        raise ValueError("Missing council roles: " + ", ".join(missing_roles))

    contributions = {
        role: responses[role]["contract_contribution"]
        for role in ROLE_FIELD_OWNERS
    }
    conflicts: dict[str, dict[str, Any]] = {}
    for field in SHARED_FIELDS:
        values = {role: contributions[role].get(field) for role in ROLE_FIELD_OWNERS}
        if len({payload_fingerprint(value) for value in values.values()}) != 1:
            conflicts[field] = values
    if conflicts:
        raise CouncilMergeConflict(conflicts)

    contract: dict[str, Any] = {
        field: deepcopy(contributions["cto"].get(field)) for field in SHARED_FIELDS
    }
    for role, fields in ROLE_FIELD_OWNERS.items():
        contribution = contributions[role]
        for field in fields:
            if field in contribution:
                contract[field] = deepcopy(contribution[field])

    contract["executive_outputs"] = {
        role: deepcopy(responses[role]["outputs"]) for role in ROLE_FIELD_OWNERS
    }
    metadata = response_metadata or {}
    ignored_fields, non_owner_conflicts = council_ignored_fields(contributions)
    contract["council_provenance"] = {
        "compiler": "deterministic_role_field_ownership_v2",
        "field_ownership": {
            role: list(fields) for role, fields in ROLE_FIELD_OWNERS.items()
        },
        "shared_fields": list(SHARED_FIELDS),
        "responses": {
            role: {
                **dict(metadata.get(role, {})),
                "response_hash": payload_fingerprint(responses[role]),
                "contribution_hash": payload_fingerprint(contributions[role]),
                "outputs_hash": payload_fingerprint(responses[role]["outputs"]),
            }
            for role in ROLE_FIELD_OWNERS
        },
        "ignored_fields": ignored_fields,
        "non_owner_conflicts": non_owner_conflicts,
        "agreement_is_not_evidence": True,
    }
    return contract


__all__ = [
    "CouncilMergeConflict",
    "ROLE_FIELD_OWNERS",
    "SHARED_FIELDS",
    "council_ignored_fields",
    "merge_council_responses",
]
