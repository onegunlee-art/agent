from __future__ import annotations

from copy import deepcopy

import pytest

from company_os.first_principles import FirstPrinciplesGate


def valid_standard_contract() -> dict:
    return {
        "decision_level": "FP_STANDARD",
        "observable_problem": {
            "statement": "A synthetic artifact is not yet present.",
            "observable": True,
        },
        "metric": {
            "name": "verified_artifact_count",
            "formula": "count(artifacts whose sha256 matches expected content)",
            "unit": "artifacts",
            "time_window": "one synthetic run",
            "data_source": "deterministic verifier output",
            "baseline": {"status": "KNOWN", "value": 0},
            "desired_outcome": {"operator": ">=", "value": 1},
        },
        "claims": [
            {
                "id": "fact-1",
                "type": "FACT",
                "statement": "The synthetic input fixture exists.",
                "evidence_refs": ["source-evidence-1"],
                "source_types": ["SYNTHETIC_FIXTURE"],
            },
            {
                "id": "assumption-1",
                "type": "ASSUMPTION",
                "statement": "Writing one artifact is sufficient for this experiment.",
                "evidence_refs": [],
                "source_types": ["MODEL_OPINION"],
            },
        ],
        "hard_constraints": [
            {
                "claim_id": "constraint-1",
                "statement": "No external network action is permitted.",
                "type": "HARD_CONSTRAINT",
            }
        ],
        "inherited_conventions": [
            {
                "claim_id": "convention-1",
                "statement": "A web dashboard is customary.",
                "type": "INHERITED_CONVENTION",
            }
        ],
        "decomposition": {
            "technology": "local file creation",
            "data": "synthetic fixture only",
            "time": "one run",
            "cash_and_unit_economics": "zero marginal cash cost",
            "rights_and_authority": "synthetic data; no third-party rights",
            "human_behavior": "CEO supplies later review manually",
            "risk_and_reversibility": "local files can be removed",
        },
        "zero_based_alternative": "Verify the content in memory without a file.",
        "do_nothing_option": "Leave the synthetic fixture unprocessed.",
        "highest_risk_assumption": "assumption-1",
        "cheapest_valid_experiment": {
            "description": "Create one deterministic text artifact.",
            "measurable_output": "verified_artifact_count",
        },
        "pass_condition": "verified_artifact_count >= 1",
        "fail_condition": "verified_artifact_count == 0",
        "stop_condition": "Stop after one verified artifact.",
        "revisit_condition": "Revisit if the verifier format changes.",
        "decision_basis": [{"claim_id": "fact-1", "type": "FACT"}],
        "financial_check": {
            "validation_cost_ceiling": {"amount": 0, "currency": "KRW"},
            "cash_ceiling": {"amount": 0, "currency": "KRW"},
            "time_ceiling": "one synthetic run",
            "variable_cost_assumptions": ["No marginal cash cost."],
            "unit_economic_unknowns": ["Real unit economics are unknown."],
            "financial_stop_threshold": "Stop before any paid service.",
        },
        "rights_and_legal_check": {
            "rights_owner": "synthetic fixture author",
            "authority_basis": "repository-local synthetic fixture",
            "consent_or_delegation_status": "NOT_APPLICABLE_SYNTHETIC_DATA",
            "personal_data_involved": False,
            "biometric_data_involved": False,
            "regulated_data_involved": False,
            "prohibited_external_actions": ["public publication"],
            "specialist_review_required": False,
            "explicit_ceo_approval_required": False,
        },
        "operations_check": {
            "first_72_hour_actions": ["Create one deterministic artifact."],
            "dependencies": ["Python standard library"],
            "current_bottleneck": "No verified artifact exists yet.",
            "operating_mode": "ON_DEMAND",
            "cadence": "one synthetic run",
            "recovery_plan": "Resume from canonical SQLite state.",
        },
        "strategy_check": {
            "why_now": "The operating kernel needs a deterministic proof.",
            "fundamental_advantage_hypothesis": "Auditable local state helps.",
            "strategic_chokepoint_hypothesis": "Verification is critical.",
            "zero_based_alternative": "Verify expected content in memory.",
            "do_nothing_consequence": "The vertical slice remains unverified.",
            "revisit_condition": "Revisit if the verifier format changes.",
        },
    }


def valid_lite_contract() -> dict:
    standard = valid_standard_contract()
    return {
        "decision_level": "FP_LITE",
        "observable_objective": {
            "statement": "Create one verified synthetic artifact.",
            "observable": True,
        },
        "known_facts": [
            {
                "type": "FACT",
                "statement": "The synthetic input fixture exists.",
                "evidence_refs": ["source-evidence-1"],
                "source_types": ["SYNTHETIC_FIXTURE"],
            }
        ],
        "completion_criteria": "One artifact passes deterministic verification.",
        "verification_method": "Compare the artifact hash with the expected hash.",
        "financial_check": deepcopy(standard["financial_check"]),
        "rights_and_legal_check": deepcopy(standard["rights_and_legal_check"]),
        "operations_check": deepcopy(standard["operations_check"]),
        "strategy_check": deepcopy(standard["strategy_check"]),
    }


def violation_codes(contract: dict) -> set[str]:
    return {item.code for item in FirstPrinciplesGate().validate(contract).violations}


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda c: c.pop("observable_problem"), "OBSERVABLE_PROBLEM_REQUIRED"),
        (lambda c: c.pop("metric"), "METRIC_DEFINITION_REQUIRED"),
        (
            lambda c: c["claims"][0].update(evidence_refs=[]),
            "FACT_WITHOUT_EVIDENCE",
        ),
        (
            lambda c: c["claims"].append(
                {
                    "id": "fact-1",
                    "type": "ASSUMPTION",
                    "statement": "The synthetic input fixture exists.",
                    "evidence_refs": [],
                    "source_types": ["MODEL_OPINION"],
                }
            ),
            "CLAIM_CLASSIFICATION_CONFLICT",
        ),
        (
            lambda c: c.update(
                decision_basis=[
                    {"claim_id": "convention-1", "type": "INHERITED_CONVENTION"}
                ]
            ),
            "INHERITED_CONVENTION_ONLY",
        ),
        (
            lambda c: c["inherited_conventions"].append(
                {
                    "claim_id": "constraint-1",
                    "statement": "No external network action is permitted.",
                    "type": "INHERITED_CONVENTION",
                }
            ),
            "CONSTRAINT_CLASSIFICATION_CONFLICT",
        ),
        (
            lambda c: c.pop("zero_based_alternative"),
            "ZERO_BASED_ALTERNATIVE_REQUIRED",
        ),
        (lambda c: c.pop("do_nothing_option"), "DO_NOTHING_OPTION_REQUIRED"),
        (
            lambda c: c.pop("highest_risk_assumption"),
            "HIGHEST_RISK_ASSUMPTION_REQUIRED",
        ),
        (
            lambda c: c["cheapest_valid_experiment"].pop("measurable_output"),
            "MEASURABLE_EXPERIMENT_REQUIRED",
        ),
        (lambda c: c.pop("pass_condition"), "PASS_CONDITION_REQUIRED"),
        (lambda c: c.pop("fail_condition"), "FAIL_CONDITION_REQUIRED"),
        (lambda c: c.pop("stop_condition"), "STOP_CONDITION_REQUIRED"),
        (lambda c: c.pop("revisit_condition"), "REVISIT_CONDITION_REQUIRED"),
    ],
)
def test_gate_rejects_invalid_standard_contract(mutate, expected_code: str) -> None:
    contract = deepcopy(valid_standard_contract())
    mutate(contract)

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert expected_code in {item.code for item in result.violations}


def test_gate_rejects_fp_full_without_evidence_pack_and_ceo_approval() -> None:
    contract = valid_standard_contract()
    contract.update(
        decision_level="FP_FULL",
        rights_and_authority_basis="Synthetic fixture owned by this test.",
        worst_realistic_loss="Loss of a disposable temporary file.",
        reversibility_assessment="Fully reversible.",
        specialist_review_status="NOT_REQUIRED_FOR_SYNTHETIC_FIXTURE",
    )

    codes = violation_codes(contract)

    assert "FP_FULL_EVIDENCE_PACK_REQUIRED" in codes
    assert "FP_FULL_CEO_APPROVAL_REQUIRED" in codes


@pytest.mark.parametrize(
    ("section", "required_field", "expected_code"),
    [
        ("financial_check", "validation_cost_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "cash_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "time_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "variable_cost_assumptions", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "unit_economic_unknowns", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "financial_stop_threshold", "FINANCIAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "rights_owner", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "authority_basis", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        (
            "rights_and_legal_check",
            "consent_or_delegation_status",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "personal_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "biometric_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "regulated_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "prohibited_external_actions",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "specialist_review_required",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "explicit_ceo_approval_required",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        ("operations_check", "first_72_hour_actions", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "dependencies", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "current_bottleneck", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "operating_mode", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "cadence", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "recovery_plan", "OPERATIONS_CHECK_REQUIRED"),
        ("strategy_check", "why_now", "STRATEGY_CHECK_REQUIRED"),
        (
            "strategy_check",
            "fundamental_advantage_hypothesis",
            "STRATEGY_CHECK_REQUIRED",
        ),
        (
            "strategy_check",
            "strategic_chokepoint_hypothesis",
            "STRATEGY_CHECK_REQUIRED",
        ),
        ("strategy_check", "zero_based_alternative", "STRATEGY_CHECK_REQUIRED"),
        ("strategy_check", "do_nothing_consequence", "STRATEGY_CHECK_REQUIRED"),
        ("strategy_check", "revisit_condition", "STRATEGY_CHECK_REQUIRED"),
    ],
)
def test_gate_rejects_governance_check_missing_required_field(
    section: str,
    required_field: str,
    expected_code: str,
) -> None:
    contract = valid_standard_contract()
    contract[section].pop(required_field)

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert expected_code in {item.code for item in result.violations}


@pytest.mark.parametrize(
    ("section", "expected_code"),
    [
        ("financial_check", "FINANCIAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        ("operations_check", "OPERATIONS_CHECK_REQUIRED"),
        ("strategy_check", "STRATEGY_CHECK_REQUIRED"),
    ],
)
def test_gate_rejects_missing_governance_check(
    section: str,
    expected_code: str,
) -> None:
    contract = valid_standard_contract()
    contract.pop(section)

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert expected_code in {item.code for item in result.violations}


@pytest.mark.parametrize("decision_level", ["FP_STANDARD", "FP_FULL"])
def test_gate_rejects_decision_basis_without_claim_id(decision_level: str) -> None:
    contract = valid_standard_contract()
    contract["decision_level"] = decision_level
    contract["decision_basis"] = [{"type": "FACT"}]

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert "DECISION_BASIS_CLAIM_ID_REQUIRED" in {
        item.code for item in result.violations
    }


def test_gate_rejects_decision_basis_referencing_removed_claim() -> None:
    contract = valid_standard_contract()
    contract["claims"] = [
        claim for claim in contract["claims"] if claim["id"] != "fact-1"
    ]

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert "DECISION_BASIS_CLAIM_NOT_FOUND" in {
        item.code for item in result.violations
    }


def test_gate_rejects_decision_basis_with_wrong_claim_classification() -> None:
    contract = valid_standard_contract()
    contract["decision_basis"][0]["type"] = "ASSUMPTION"

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert "DECISION_BASIS_CLASSIFICATION_CONFLICT" in {
        item.code for item in result.violations
    }


def test_gate_rejects_fact_basis_when_source_evidence_is_removed() -> None:
    contract = valid_standard_contract()
    contract["claims"][0]["evidence_refs"] = []

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    codes = {item.code for item in result.violations}
    assert "FACT_WITHOUT_EVIDENCE" in codes
    assert "DECISION_BASIS_FACT_WITHOUT_EVIDENCE" in codes


@pytest.mark.parametrize(
    "source_types",
    [
        ["MODEL_OPINION"],
        ["EXECUTIVE_OPINION"],
        ["CEO_INTUITION"],
        ["UNCITED_INTERNET"],
        ["INDUSTRY_CONVENTION"],
        ["COMMON_PRACTICE"],
        [
            "MODEL_OPINION",
            "EXECUTIVE_OPINION",
            "CEO_INTUITION",
            "UNCITED_INTERNET",
            "INDUSTRY_CONVENTION",
            "COMMON_PRACTICE",
        ],
    ],
)
def test_gate_rejects_fact_supported_only_by_non_evidence_source_types(
    source_types: list[str],
) -> None:
    contract = valid_standard_contract()
    contract["claims"][0]["source_types"] = source_types

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    codes = {item.code for item in result.violations}
    assert "FACT_SOURCE_NOT_EVIDENCE" in codes
    assert "DECISION_BASIS_FACT_WITHOUT_EVIDENCE" in codes


def test_gate_accepts_fact_with_traceable_source_alongside_model_opinion() -> None:
    contract = valid_standard_contract()
    contract["claims"][0]["source_types"] = [
        "MODEL_OPINION",
        "SYNTHETIC_FIXTURE",
    ]

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is True
    assert result.violations == ()


def test_gate_accepts_complete_lite_contract() -> None:
    result = FirstPrinciplesGate().validate(valid_lite_contract())

    assert result.passed is True
    assert result.violations == ()


@pytest.mark.parametrize(
    ("section", "expected_code"),
    [
        ("financial_check", "FINANCIAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        ("operations_check", "OPERATIONS_CHECK_REQUIRED"),
        ("strategy_check", "STRATEGY_CHECK_REQUIRED"),
    ],
)
def test_gate_rejects_lite_contract_missing_governance_section(
    section: str,
    expected_code: str,
) -> None:
    contract = valid_lite_contract()
    contract.pop(section)

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert expected_code in {item.code for item in result.violations}


@pytest.mark.parametrize(
    ("section", "required_field", "expected_code"),
    [
        ("financial_check", "validation_cost_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "cash_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "time_ceiling", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "variable_cost_assumptions", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "unit_economic_unknowns", "FINANCIAL_CHECK_REQUIRED"),
        ("financial_check", "financial_stop_threshold", "FINANCIAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "rights_owner", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        ("rights_and_legal_check", "authority_basis", "RIGHTS_AND_LEGAL_CHECK_REQUIRED"),
        (
            "rights_and_legal_check",
            "consent_or_delegation_status",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "personal_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "biometric_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "regulated_data_involved",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "prohibited_external_actions",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "specialist_review_required",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        (
            "rights_and_legal_check",
            "explicit_ceo_approval_required",
            "RIGHTS_AND_LEGAL_CHECK_REQUIRED",
        ),
        ("operations_check", "first_72_hour_actions", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "dependencies", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "current_bottleneck", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "operating_mode", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "cadence", "OPERATIONS_CHECK_REQUIRED"),
        ("operations_check", "recovery_plan", "OPERATIONS_CHECK_REQUIRED"),
        ("strategy_check", "why_now", "STRATEGY_CHECK_REQUIRED"),
        (
            "strategy_check",
            "fundamental_advantage_hypothesis",
            "STRATEGY_CHECK_REQUIRED",
        ),
        (
            "strategy_check",
            "strategic_chokepoint_hypothesis",
            "STRATEGY_CHECK_REQUIRED",
        ),
        ("strategy_check", "zero_based_alternative", "STRATEGY_CHECK_REQUIRED"),
        ("strategy_check", "do_nothing_consequence", "STRATEGY_CHECK_REQUIRED"),
        ("strategy_check", "revisit_condition", "STRATEGY_CHECK_REQUIRED"),
    ],
)
def test_gate_rejects_lite_governance_check_missing_required_field(
    section: str,
    required_field: str,
    expected_code: str,
) -> None:
    contract = valid_lite_contract()
    contract[section].pop(required_field)

    result = FirstPrinciplesGate().validate(contract)

    assert result.passed is False
    assert expected_code in {item.code for item in result.violations}


def test_gate_accepts_complete_standard_contract() -> None:
    result = FirstPrinciplesGate().validate(valid_standard_contract())

    assert result.passed is True
    assert result.violations == ()
