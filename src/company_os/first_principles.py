"""Deterministic structural validation for first-principles decisions.

The gate deliberately has a narrow responsibility: it checks whether a
decision record is complete, consistently classified, and traceable.  It does
not inspect the world, rank evidence quality, or decide whether a claim is
true.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class Violation:
    """A stable, machine-readable reason a contract did not pass the gate."""

    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class GateResult:
    """The immutable result of one gate evaluation."""

    passed: bool
    violations: tuple[Violation, ...]


_DECISION_LEVELS: Final = frozenset({"FP_LITE", "FP_STANDARD", "FP_FULL"})
_DECISION_LEVEL_RANK: Final = {
    "FP_LITE": 1,
    "FP_STANDARD": 2,
    "FP_FULL": 3,
}
_CLAIM_TYPES: Final = frozenset(
    {
        "OBSERVATION",
        "FACT",
        "HARD_CONSTRAINT",
        "SOFT_CONSTRAINT",
        "INHERITED_CONVENTION",
        "ASSUMPTION",
        "HYPOTHESIS",
        "UNKNOWN",
        "EVIDENCE",
        "DECISION",
    }
)
_TRUSTED_FACT_SOURCE_TYPES: Final = frozenset(
    {
        "SYNTHETIC_FIXTURE",
        "PRIMARY_SOURCE",
        "OFFICIAL_RECORD",
        "MEASURED_OBSERVATION",
        "VERIFIED_ARTIFACT",
        "USER_SUPPLIED_DOCUMENT",
    }
)
_METRIC_FIELDS: Final = ("name", "formula", "unit", "time_window", "data_source")
_DECOMPOSITION_FIELDS: Final = (
    "technology",
    "data",
    "time",
    "cash_and_unit_economics",
    "rights_and_authority",
    "human_behavior",
    "risk_and_reversibility",
)
_GOVERNANCE_CHECKS: Final = (
    (
        "financial_check",
        "FINANCIAL_CHECK_REQUIRED",
        (
            "validation_cost_ceiling",
            "cash_ceiling",
            "time_ceiling",
            "variable_cost_assumptions",
            "unit_economic_unknowns",
            "financial_stop_threshold",
        ),
    ),
    (
        "rights_and_legal_check",
        "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        (
            "rights_owner",
            "authority_basis",
            "consent_or_delegation_status",
            "personal_data_involved",
            "biometric_data_involved",
            "regulated_data_involved",
            "prohibited_external_actions",
            "specialist_review_required",
            "explicit_ceo_approval_required",
        ),
    ),
    (
        "operations_check",
        "OPERATIONS_CHECK_REQUIRED",
        (
            "first_72_hour_actions",
            "dependencies",
            "current_bottleneck",
            "operating_mode",
            "cadence",
            "recovery_plan",
        ),
    ),
    (
        "strategy_check",
        "STRATEGY_CHECK_REQUIRED",
        (
            "why_now",
            "fundamental_advantage_hypothesis",
            "strategic_chokepoint_hypothesis",
            "zero_based_alternative",
            "do_nothing_consequence",
            "revisit_condition",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class _ClaimProvenance:
    claim_type: str
    evidence_traceable: bool


class FirstPrinciplesGate:
    """Validate first-principles records without making truth judgements."""

    def validate(
        self,
        contract: Mapping[str, Any],
        *,
        min_decision_level: str = "FP_LITE",
        trusted_evidence_refs: frozenset[str] = frozenset(),
        ceo_approved: bool = False,
    ) -> GateResult:
        """Return all structurally detectable violations in a stable order.

        Validation is intentionally pure: ``contract`` is never mutated and no
        filesystem, network, clock, or model is consulted.
        """

        violations: list[Violation] = []
        if not isinstance(contract, Mapping):
            self._add(
                violations,
                "CONTRACT_REQUIRED",
                "contract",
                "The contract must be a mapping.",
            )
            return GateResult(passed=False, violations=tuple(violations))

        normalized_minimum = (
            min_decision_level.strip()
            if isinstance(min_decision_level, str)
            else min_decision_level
        )
        if normalized_minimum not in _DECISION_LEVELS:
            self._add(
                violations,
                "MIN_DECISION_LEVEL_INVALID",
                "min_decision_level",
                "The minimum decision level must be FP_LITE, FP_STANDARD, or FP_FULL.",
            )
            return GateResult(passed=False, violations=tuple(violations))

        trusted_refs = frozenset(
            reference.strip()
            for reference in trusted_evidence_refs
            if _non_empty_text(reference)
        )

        raw_level = contract.get("decision_level")
        if not _non_empty_text(raw_level):
            self._add(
                violations,
                "DECISION_LEVEL_REQUIRED",
                "decision_level",
                "A decision level is required.",
            )
            return GateResult(passed=False, violations=tuple(violations))

        level = raw_level.strip() if isinstance(raw_level, str) else raw_level
        if level not in _DECISION_LEVELS:
            self._add(
                violations,
                "DECISION_LEVEL_INVALID",
                "decision_level",
                "Decision level must be FP_LITE, FP_STANDARD, or FP_FULL.",
            )
            return GateResult(passed=False, violations=tuple(violations))

        if _DECISION_LEVEL_RANK[level] < _DECISION_LEVEL_RANK[normalized_minimum]:
            self._add(
                violations,
                "DECISION_LEVEL_BELOW_MINIMUM",
                "decision_level",
                f"{level} is below the required minimum {normalized_minimum}.",
            )

        self._validate_governance_checks(contract, violations)
        if level == "FP_LITE":
            self._validate_lite(contract, trusted_refs, violations)
        else:
            self._validate_standard(contract, trusted_refs, violations)
            if level == "FP_FULL":
                self._validate_full(contract, ceo_approved, violations)

        return GateResult(passed=not violations, violations=tuple(violations))

    def _validate_lite(
        self,
        contract: Mapping[str, Any],
        trusted_evidence_refs: frozenset[str],
        violations: list[Violation],
    ) -> None:
        objective = contract.get("observable_objective")
        if not _present(objective):
            self._add(
                violations,
                "OBSERVABLE_OBJECTIVE_REQUIRED",
                "observable_objective",
                "FP_LITE requires an observable objective.",
            )
        elif isinstance(objective, Mapping):
            if not _non_empty_text(objective.get("statement")):
                self._add(
                    violations,
                    "OBSERVABLE_OBJECTIVE_REQUIRED",
                    "observable_objective.statement",
                    "The observable objective requires a statement.",
                )
            elif objective.get("observable") is not True:
                self._add(
                    violations,
                    "OBJECTIVE_NOT_OBSERVABLE",
                    "observable_objective.observable",
                    "The objective must be explicitly marked observable.",
                )

        known_facts = contract.get("known_facts")
        if not _non_string_sequence(known_facts) or not known_facts:
            self._add(
                violations,
                "KNOWN_FACTS_REQUIRED",
                "known_facts",
                "FP_LITE requires at least one known fact or fact reference.",
            )
        else:
            for index, fact in enumerate(known_facts):
                field = f"known_facts[{index}]"
                if isinstance(fact, Mapping):
                    if fact.get("type", "FACT") != "FACT":
                        self._add(
                            violations,
                            "CLAIM_CLASSIFICATION_CONFLICT",
                            f"{field}.type",
                            "An item in known_facts must be classified as FACT.",
                        )
                    if not _evidence_references(fact.get("evidence_refs")):
                        self._add(
                            violations,
                            "FACT_WITHOUT_EVIDENCE",
                            field,
                            "A FACT requires at least one Evidence reference.",
                        )
                    elif not _evidence_references_are_trusted(
                        fact.get("evidence_refs"), trusted_evidence_refs
                    ):
                        self._add(
                            violations,
                            "FACT_EVIDENCE_NOT_TRUSTED",
                            f"{field}.evidence_refs",
                            "Every FACT Evidence reference must resolve to trusted Evidence.",
                        )
                    if not _fact_source_types_allowed(fact.get("source_types")):
                        self._add(
                            violations,
                            "FACT_SOURCE_NOT_EVIDENCE",
                            f"{field}.source_types",
                            "Every FACT source type must be in the trusted allowlist.",
                        )
                elif not _non_empty_text(fact):
                    self._add(
                        violations,
                        "KNOWN_FACTS_REQUIRED",
                        field,
                        "A known-fact reference must be non-empty.",
                    )

        self._require_present(
            contract,
            violations,
            key="completion_criteria",
            code="COMPLETION_CRITERIA_REQUIRED",
            message="FP_LITE requires completion criteria.",
        )
        self._require_present(
            contract,
            violations,
            key="verification_method",
            code="VERIFICATION_METHOD_REQUIRED",
            message="FP_LITE requires a verification method.",
        )

    def _validate_standard(
        self,
        contract: Mapping[str, Any],
        trusted_evidence_refs: frozenset[str],
        violations: list[Violation],
    ) -> dict[str, set[str]]:
        self._validate_observable_problem(contract, violations)
        self._validate_metric(contract, violations)
        claim_index, claim_provenance = self._validate_claims(
            contract,
            trusted_evidence_refs,
            violations,
        )
        constraint_provenance = self._validate_constraints(contract, violations)
        for claim_id, provenance in constraint_provenance.items():
            claim_provenance.setdefault(claim_id, provenance)
        self._validate_decomposition(contract, violations)

        self._require_present(
            contract,
            violations,
            key="zero_based_alternative",
            code="ZERO_BASED_ALTERNATIVE_REQUIRED",
            message="A zero-based alternative is required.",
        )
        self._require_present(
            contract,
            violations,
            key="do_nothing_option",
            code="DO_NOTHING_OPTION_REQUIRED",
            message="A do-nothing option is required.",
        )
        self._validate_highest_risk_assumption(contract, claim_index, violations)
        self._validate_experiment(contract, violations)

        condition_specs = (
            ("pass_condition", "PASS_CONDITION_REQUIRED", "A pass condition is required."),
            ("fail_condition", "FAIL_CONDITION_REQUIRED", "A fail condition is required."),
            ("stop_condition", "STOP_CONDITION_REQUIRED", "A stop condition is required."),
            (
                "revisit_condition",
                "REVISIT_CONDITION_REQUIRED",
                "A revisit condition is required.",
            ),
        )
        for key, code, message in condition_specs:
            self._require_present(
                contract,
                violations,
                key=key,
                code=code,
                message=message,
            )

        self._validate_decision_basis(contract, claim_provenance, violations)
        return claim_index

    def _validate_observable_problem(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> None:
        problem = contract.get("observable_problem")
        if not isinstance(problem, Mapping) or not _non_empty_text(
            problem.get("statement")
        ):
            self._add(
                violations,
                "OBSERVABLE_PROBLEM_REQUIRED",
                "observable_problem",
                "An observable problem with a statement is required.",
            )
            return

        if problem.get("observable") is not True:
            self._add(
                violations,
                "PROBLEM_NOT_OBSERVABLE",
                "observable_problem.observable",
                "The problem must be explicitly marked observable.",
            )

    def _validate_metric(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> None:
        metric = contract.get("metric")
        if not isinstance(metric, Mapping):
            self._add(
                violations,
                "METRIC_DEFINITION_REQUIRED",
                "metric",
                "A structured metric definition is required.",
            )
            return

        missing = [key for key in _METRIC_FIELDS if not _present(metric.get(key))]
        if missing:
            self._add(
                violations,
                "METRIC_DEFINITION_REQUIRED",
                "metric",
                "Metric definition is missing: " + ", ".join(missing) + ".",
            )

        baseline = metric.get("baseline")
        if not isinstance(baseline, Mapping) or baseline.get("status") not in {
            "KNOWN",
            "UNKNOWN",
        }:
            self._add(
                violations,
                "METRIC_BASELINE_REQUIRED",
                "metric.baseline",
                "Metric baseline must explicitly be KNOWN or UNKNOWN.",
            )
        elif baseline.get("status") == "KNOWN" and "value" not in baseline:
            self._add(
                violations,
                "METRIC_BASELINE_REQUIRED",
                "metric.baseline.value",
                "A KNOWN baseline requires a value.",
            )

        desired = metric.get("desired_outcome")
        if (
            not isinstance(desired, Mapping)
            or not _non_empty_text(desired.get("operator"))
            or "value" not in desired
        ):
            self._add(
                violations,
                "DESIRED_OUTCOME_REQUIRED",
                "metric.desired_outcome",
                "Metric requires a desired outcome with an operator and value.",
            )

    def _validate_claims(
        self,
        contract: Mapping[str, Any],
        trusted_evidence_refs: frozenset[str],
        violations: list[Violation],
    ) -> tuple[dict[str, set[str]], dict[str, _ClaimProvenance]]:
        entries: list[tuple[str, Any, str | None]] = []
        claims = contract.get("claims")
        if _non_string_sequence(claims):
            entries.extend(
                (f"claims[{index}]", claim, None)
                for index, claim in enumerate(claims)
            )
        elif claims is not None:
            self._add(
                violations,
                "CLAIMS_STRUCTURE_INVALID",
                "claims",
                "Claims must be a list of structured claim records.",
            )

        # Also accept the explicit facts/assumptions representation from the
        # constitution.  A contract may use either it or the unified list.
        for key, forced_type in (("facts", "FACT"), ("assumptions", "ASSUMPTION")):
            separate = contract.get(key)
            if _non_string_sequence(separate):
                entries.extend(
                    (f"{key}[{index}]", claim, forced_type)
                    for index, claim in enumerate(separate)
                )
            elif separate is not None:
                self._add(
                    violations,
                    "CLAIMS_STRUCTURE_INVALID",
                    key,
                    f"{key} must be a list of structured claim records.",
                )

        found_types: set[str] = set()
        types_by_identity: dict[tuple[str, str], str] = {}
        claim_index: dict[str, set[str]] = {"FACT": set(), "ASSUMPTION": set()}
        claim_provenance: dict[str, _ClaimProvenance] = {}

        for field, claim, forced_type in entries:
            if not isinstance(claim, Mapping):
                self._add(
                    violations,
                    "CLAIM_STRUCTURE_INVALID",
                    field,
                    "Each claim must be a mapping.",
                )
                continue

            claim_type = claim.get("type", forced_type)
            if forced_type is not None and claim_type != forced_type:
                self._add(
                    violations,
                    "CLAIM_CLASSIFICATION_CONFLICT",
                    f"{field}.type",
                    f"An item in this collection must be {forced_type}.",
                )
            if claim_type not in _CLAIM_TYPES:
                self._add(
                    violations,
                    "CLAIM_TYPE_INVALID",
                    f"{field}.type",
                    "Claim type is missing or is not a supported classification.",
                )
                continue

            found_types.add(claim_type)
            claim_id = claim.get("id", claim.get("claim_id"))
            statement = claim.get("statement")
            if not _non_empty_text(claim_id) or not _non_empty_text(statement):
                self._add(
                    violations,
                    "CLAIM_STRUCTURE_INVALID",
                    field,
                    "Each claim requires a non-empty id and statement.",
                )

            identities: list[tuple[str, str]] = []
            if _non_empty_text(claim_id):
                normalized_id = claim_id.strip()
                identities.append(("id", normalized_id))
                if claim_type in claim_index:
                    claim_index[claim_type].add(normalized_id)
                claim_provenance.setdefault(
                    normalized_id,
                    _ClaimProvenance(
                        claim_type=claim_type,
                        evidence_traceable=(
                            claim_type == "FACT"
                            and _fact_evidence_traceable(
                                claim,
                                trusted_evidence_refs,
                            )
                        ),
                    ),
                )
            if _non_empty_text(statement):
                normalized_statement = _normalize(statement)
                identities.append(("statement", normalized_statement))
                if claim_type in claim_index:
                    claim_index[claim_type].add(normalized_statement)

            has_conflict = False
            for identity in identities:
                existing_type = types_by_identity.get(identity)
                if existing_type is not None and existing_type != claim_type:
                    has_conflict = True
                else:
                    types_by_identity[identity] = claim_type
            if has_conflict:
                self._add(
                    violations,
                    "CLAIM_CLASSIFICATION_CONFLICT",
                    field,
                    "The same claim is assigned conflicting classifications.",
                )

            if claim_type == "FACT" and not _evidence_references(
                claim.get("evidence_refs")
            ):
                self._add(
                    violations,
                    "FACT_WITHOUT_EVIDENCE",
                    field,
                    "A FACT requires at least one Evidence reference.",
                )
            elif claim_type == "FACT" and not _evidence_references_are_trusted(
                claim.get("evidence_refs"), trusted_evidence_refs
            ):
                self._add(
                    violations,
                    "FACT_EVIDENCE_NOT_TRUSTED",
                    f"{field}.evidence_refs",
                    "Every FACT Evidence reference must resolve to trusted Evidence.",
                )
            if claim_type == "FACT" and not _fact_source_types_allowed(
                claim.get("source_types")
            ):
                self._add(
                    violations,
                    "FACT_SOURCE_NOT_EVIDENCE",
                    f"{field}.source_types",
                    "Every FACT source type must be in the trusted allowlist.",
                )

        if "FACT" not in found_types:
            self._add(
                violations,
                "FACTS_REQUIRED",
                "claims",
                "FP_STANDARD requires at least one classified FACT.",
            )
        if "ASSUMPTION" not in found_types:
            self._add(
                violations,
                "ASSUMPTIONS_REQUIRED",
                "claims",
                "FP_STANDARD requires at least one classified ASSUMPTION.",
            )

        return claim_index, claim_provenance

    def _validate_constraints(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> dict[str, _ClaimProvenance]:
        hard_constraints = contract.get("hard_constraints")
        conventions = contract.get("inherited_conventions")

        hard_identities = self._classified_item_identities(
            hard_constraints,
            key="hard_constraints",
            expected_type="HARD_CONSTRAINT",
            required_code="HARD_CONSTRAINTS_REQUIRED",
            violations=violations,
        )
        convention_identities = self._classified_item_identities(
            conventions,
            key="inherited_conventions",
            expected_type="INHERITED_CONVENTION",
            required_code="INHERITED_CONVENTIONS_REQUIRED",
            violations=violations,
        )
        overlap = hard_identities.intersection(convention_identities)
        if overlap:
            self._add(
                violations,
                "CONSTRAINT_CLASSIFICATION_CONFLICT",
                "hard_constraints,inherited_conventions",
                "A hard constraint and inherited convention share an identity.",
            )

        provenance: dict[str, _ClaimProvenance] = {}
        for items, expected_type in (
            (hard_constraints, "HARD_CONSTRAINT"),
            (conventions, "INHERITED_CONVENTION"),
        ):
            if not _non_string_sequence(items):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                claim_id = item.get("claim_id", item.get("id"))
                if _non_empty_text(claim_id):
                    provenance.setdefault(
                        claim_id.strip(),
                        _ClaimProvenance(
                            claim_type=item.get("type", expected_type),
                            evidence_traceable=False,
                        ),
                    )
        return provenance

    def _classified_item_identities(
        self,
        value: Any,
        *,
        key: str,
        expected_type: str,
        required_code: str,
        violations: list[Violation],
    ) -> set[tuple[str, str]]:
        if not _non_string_sequence(value) or not value:
            self._add(
                violations,
                required_code,
                key,
                f"FP_STANDARD requires at least one {key.replace('_', ' ')} entry.",
            )
            return set()

        identities: set[tuple[str, str]] = set()
        for index, item in enumerate(value):
            field = f"{key}[{index}]"
            if not isinstance(item, Mapping):
                self._add(
                    violations,
                    "CONSTRAINT_STRUCTURE_INVALID",
                    field,
                    "Constraint and convention entries must be mappings.",
                )
                continue

            item_type = item.get("type")
            if item_type != expected_type:
                self._add(
                    violations,
                    "CONSTRAINT_CLASSIFICATION_CONFLICT",
                    f"{field}.type",
                    f"This entry must be classified as {expected_type}.",
                )

            claim_id = item.get("claim_id", item.get("id"))
            statement = item.get("statement")
            if not _non_empty_text(claim_id) or not _non_empty_text(statement):
                self._add(
                    violations,
                    "CONSTRAINT_STRUCTURE_INVALID",
                    field,
                    "Each entry requires a non-empty claim_id and statement.",
                )
            if _non_empty_text(claim_id):
                identities.add(("id", claim_id.strip()))
            if _non_empty_text(statement):
                identities.add(("statement", _normalize(statement)))
        return identities

    def _validate_decomposition(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> None:
        decomposition = contract.get("decomposition")
        if not isinstance(decomposition, Mapping):
            self._add(
                violations,
                "DECOMPOSITION_REQUIRED",
                "decomposition",
                "A seven-dimension problem decomposition is required.",
            )
            return

        missing = [
            key for key in _DECOMPOSITION_FIELDS if not _present(decomposition.get(key))
        ]
        if missing:
            self._add(
                violations,
                "DECOMPOSITION_REQUIRED",
                "decomposition",
                "Problem decomposition is missing: " + ", ".join(missing) + ".",
            )

    def _validate_highest_risk_assumption(
        self,
        contract: Mapping[str, Any],
        claim_index: Mapping[str, set[str]],
        violations: list[Violation],
    ) -> None:
        value = contract.get("highest_risk_assumption")
        if not _present(value):
            self._add(
                violations,
                "HIGHEST_RISK_ASSUMPTION_REQUIRED",
                "highest_risk_assumption",
                "The highest-risk assumption is required.",
            )
            return

        reference: Any = value
        if isinstance(value, Mapping):
            reference = value.get("claim_id", value.get("id", value.get("statement")))
        if not _non_empty_text(reference):
            self._add(
                violations,
                "HIGHEST_RISK_ASSUMPTION_REQUIRED",
                "highest_risk_assumption",
                "The highest-risk assumption must identify an assumption.",
            )
            return

        assumption_keys = claim_index.get("ASSUMPTION", set())
        if reference.strip() not in assumption_keys and _normalize(reference) not in assumption_keys:
            self._add(
                violations,
                "HIGHEST_RISK_ASSUMPTION_INVALID",
                "highest_risk_assumption",
                "The highest-risk assumption must reference a classified ASSUMPTION.",
            )

    def _validate_experiment(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> None:
        experiment = contract.get("cheapest_valid_experiment")
        if (
            not isinstance(experiment, Mapping)
            or not _present(experiment.get("description"))
            or not _present(experiment.get("measurable_output"))
        ):
            self._add(
                violations,
                "MEASURABLE_EXPERIMENT_REQUIRED",
                "cheapest_valid_experiment",
                "The cheapest valid experiment requires a description and measurable output.",
            )

    def _validate_governance_checks(
        self,
        contract: Mapping[str, Any],
        violations: list[Violation],
    ) -> None:
        for section_name, code, required_fields in _GOVERNANCE_CHECKS:
            section = contract.get(section_name)
            if not isinstance(section, Mapping):
                self._add(
                    violations,
                    code,
                    section_name,
                    f"Every VentureContract requires a structured {section_name}.",
                )
                continue

            missing = [
                field for field in required_fields if not _present(section.get(field))
            ]
            if missing:
                self._add(
                    violations,
                    code,
                    section_name,
                    f"{section_name} is missing: " + ", ".join(missing) + ".",
                )

    def _validate_decision_basis(
        self,
        contract: Mapping[str, Any],
        claim_provenance: Mapping[str, _ClaimProvenance],
        violations: list[Violation],
    ) -> None:
        basis = contract.get("decision_basis")
        if not _non_string_sequence(basis) or not basis:
            self._add(
                violations,
                "DECISION_BASIS_REQUIRED",
                "decision_basis",
                "A decision requires a non-empty evidence provenance basis.",
            )
            return

        basis_types: list[str] = []
        for index, item in enumerate(basis):
            field = f"decision_basis[{index}]"
            if not isinstance(item, Mapping) or item.get("type") not in _CLAIM_TYPES:
                self._add(
                    violations,
                    "DECISION_BASIS_INVALID",
                    field,
                    "Each decision-basis item requires a supported classification.",
                )
                continue

            basis_type = item["type"]
            basis_types.append(basis_type)
            claim_id = item.get("claim_id")
            if not _non_empty_text(claim_id):
                self._add(
                    violations,
                    "DECISION_BASIS_CLAIM_ID_REQUIRED",
                    f"{field}.claim_id",
                    "Each decision-basis item must identify its source claim_id.",
                )
                continue

            provenance = claim_provenance.get(claim_id.strip())
            if provenance is None:
                self._add(
                    violations,
                    "DECISION_BASIS_CLAIM_NOT_FOUND",
                    f"{field}.claim_id",
                    "The decision-basis claim_id must reference a contract claim.",
                )
                continue

            if basis_type != provenance.claim_type:
                self._add(
                    violations,
                    "DECISION_BASIS_CLASSIFICATION_CONFLICT",
                    f"{field}.type",
                    "The decision-basis classification must match its source claim.",
                )

            if provenance.claim_type == "FACT" and not provenance.evidence_traceable:
                self._add(
                    violations,
                    "DECISION_BASIS_FACT_WITHOUT_EVIDENCE",
                    field,
                    "A FACT decision basis must trace to Evidence on its source claim.",
                )

        if basis_types and all(
            item_type == "INHERITED_CONVENTION" for item_type in basis_types
        ):
            self._add(
                violations,
                "INHERITED_CONVENTION_ONLY",
                "decision_basis",
                "Inherited convention cannot be the decision's only justification.",
            )

    def _validate_full(
        self,
        contract: Mapping[str, Any],
        ceo_approved: bool,
        violations: list[Violation],
    ) -> None:
        full_fields = (
            (
                "rights_and_authority_basis",
                "FP_FULL_RIGHTS_AND_AUTHORITY_BASIS_REQUIRED",
                "FP_FULL requires a rights and authority basis.",
            ),
            (
                "worst_realistic_loss",
                "FP_FULL_WORST_REALISTIC_LOSS_REQUIRED",
                "FP_FULL requires the worst realistic loss.",
            ),
            (
                "reversibility_assessment",
                "FP_FULL_REVERSIBILITY_ASSESSMENT_REQUIRED",
                "FP_FULL requires a reversibility assessment.",
            ),
            (
                "specialist_review_status",
                "FP_FULL_SPECIALIST_REVIEW_STATUS_REQUIRED",
                "FP_FULL requires a specialist-review status.",
            ),
        )
        for key, code, message in full_fields:
            self._require_present(
                contract,
                violations,
                key=key,
                code=code,
                message=message,
            )

        if not _present(contract.get("evidence_pack")):
            self._add(
                violations,
                "FP_FULL_EVIDENCE_PACK_REQUIRED",
                "evidence_pack",
                "FP_FULL requires a non-empty Evidence Pack.",
            )

        if ceo_approved is not True:
            self._add(
                violations,
                "FP_FULL_CEO_APPROVAL_REQUIRED",
                "ceo_approved",
                "FP_FULL requires CEO approval from the external authority state.",
            )

    @staticmethod
    def _require_present(
        contract: Mapping[str, Any],
        violations: list[Violation],
        *,
        key: str,
        code: str,
        message: str,
    ) -> None:
        if not _present(contract.get(key)):
            FirstPrinciplesGate._add(violations, code, key, message)

    @staticmethod
    def _add(
        violations: list[Violation],
        code: str,
        field: str,
        message: str,
    ) -> None:
        violations.append(Violation(code=code, field=field, message=message))


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_string_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if _non_string_sequence(value):
        return bool(value)
    return True


def _evidence_references(value: Any) -> bool:
    return bool(
        _non_string_sequence(value)
        and value
        and all(_non_empty_text(reference) for reference in value)
    )


def _evidence_references_are_trusted(
    value: Any,
    trusted_evidence_refs: frozenset[str],
) -> bool:
    return bool(
        _evidence_references(value)
        and all(reference.strip() in trusted_evidence_refs for reference in value)
    )


def _fact_source_types_allowed(value: Any) -> bool:
    return bool(
        _non_string_sequence(value)
        and value
        and all(
            _non_empty_text(source_type)
            and source_type.strip().upper() in _TRUSTED_FACT_SOURCE_TYPES
            for source_type in value
        )
    )


def _fact_evidence_traceable(
    claim: Mapping[str, Any],
    trusted_evidence_refs: frozenset[str],
) -> bool:
    return _evidence_references_are_trusted(
        claim.get("evidence_refs"),
        trusted_evidence_refs,
    ) and _fact_source_types_allowed(claim.get("source_types"))


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
