"""Executive role specifications and deterministic council fixtures.

The executive council is deliberately small: V0.1 has exactly the CTO, CPO,
and CMO roles defined here.  A role specification is data, not an always-on
agent, and the synthetic response builders do not invoke or impersonate a
language model.  They exist so acceptance tests can exercise the file-handoff
and council compiler paths repeatably.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """Immutable operating boundary for one executive role."""

    mission: str
    permitted_inputs: tuple[str, ...]
    available_tools: tuple[str, ...]
    mandatory_questions: tuple[str, ...]
    required_structured_outputs: tuple[str, ...]
    authority: tuple[str, ...]
    escalation_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        # ``frozen=True`` prevents reassignment, while coercing every collection
        # to a tuple also prevents a caller from retaining and mutating a list
        # supplied to the constructor.
        tuple_fields = (
            "permitted_inputs",
            "available_tools",
            "mandatory_questions",
            "required_structured_outputs",
            "authority",
            "escalation_conditions",
        )
        for field_name in tuple_fields:
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of the role specification."""

        return {
            "mission": self.mission,
            "permitted_inputs": list(self.permitted_inputs),
            "available_tools": list(self.available_tools),
            "mandatory_questions": list(self.mandatory_questions),
            "required_structured_outputs": list(
                self.required_structured_outputs
            ),
            "authority": list(self.authority),
            "escalation_conditions": list(self.escalation_conditions),
        }

    # ``as_dict`` is a convenient, unsurprising spelling for request builders.
    as_dict = to_dict


_CTO = RoleSpec(
    mission=(
        "Convert CEO, product, and business objectives into the simplest "
        "technically valid architecture, technical experiment, implementation "
        "plan, Work Orders, and verification method."
    ),
    permitted_inputs=(
        "CEO idea, objectives, constraints, and recorded decisions",
        "provided product and market requirements",
        "provided repository, environment, and architecture context",
        "identified Evidence and its provenance",
        "First Principles decision requirements",
    ),
    available_tools=(
        "read-only inspection of provided local repository and artifacts",
        "local Python, PowerShell, Git, and deterministic test output",
        "structured JSON and Markdown file handoff",
        "deterministic architecture and verification analysis",
    ),
    mandatory_questions=(
        "What is the observable technical problem?",
        "What technical facts are known?",
        "What assumptions are being presented as facts?",
        "What is the highest-risk technical assumption?",
        "Which constraints are genuinely technical, physical, or security-related?",
        "Which constraints are inherited conventions?",
        "What is the smallest technical unit that must work?",
        "What is the cheapest valid technical experiment?",
        "What evidence would falsify the approach?",
        "What Work Orders are required?",
        "What mechanical verifier determines completion?",
        "What should not be built yet?",
    ),
    required_structured_outputs=(
        "technical_facts",
        "technical_assumptions",
        "hard_technical_constraints",
        "inherited_technical_conventions",
        "fundamental_technical_units",
        "architecture_proposal",
        "zero_based_alternative",
        "highest_risk_technical_assumption",
        "cheapest_validating_experiment",
        "work_orders",
        "mechanical_acceptance_criteria",
        "technical_risks",
        "stop_condition",
        "revisit_condition",
        "ceo_decision_required",
    ),
    authority=(
        "Propose reversible technical architecture, experiments, Work Orders, and verifiers.",
        "Classify technical claims only from supplied context and Evidence.",
        "Reject technically unverifiable completion criteria.",
        "May not approve spend, external actions, production deployment, or CEO decisions.",
    ),
    escalation_conditions=(
        "A required choice is irreversible, externally visible, or materially costly.",
        "Rights, personal data, biometric data, regulated data, or confidential data are involved.",
        "Evidence is insufficient to distinguish a FACT from an ASSUMPTION.",
        "A hard constraint conflicts with the requested outcome.",
        "The verifier cannot determine completion mechanically.",
    ),
)


_CPO = RoleSpec(
    mission=(
        "Convert the CEO's idea into an observable user problem and the smallest "
        "genuinely usable product."
    ),
    permitted_inputs=(
        "CEO idea, objectives, constraints, and recorded decisions",
        "provided user observations, interviews, and behavioral Evidence",
        "provided technical feasibility and market context",
        "identified Evidence and its provenance",
        "First Principles decision requirements",
    ),
    available_tools=(
        "inspection of provided product, user, and Evidence artifacts",
        "structured JSON and Markdown file handoff",
        "deterministic scope, journey, and acceptance-criteria analysis",
        "manual-before-automation experiment design",
    ),
    mandatory_questions=(
        "Who is the user?",
        "What observable problem does the user experience?",
        "What current behavior proves that the problem exists?",
        "What outcome does the user actually need?",
        "Which proposed features are assumptions?",
        "What is the minimum genuinely usable form?",
        "What should explicitly not be built?",
        "Can value be tested manually before automation?",
        "What real user behavior validates the product?",
        "What are the product acceptance criteria?",
        "What would falsify the product hypothesis?",
    ),
    required_structured_outputs=(
        "user_definition",
        "observable_user_problem",
        "current_user_behavior",
        "desired_user_outcome",
        "product_facts",
        "product_assumptions",
        "product_hypothesis",
        "minimum_usable_scope",
        "explicitly_excluded_scope",
        "user_journey",
        "product_requirements",
        "product_acceptance_criteria",
        "product_validation_experiment",
        "product_stop_condition",
        "ceo_decision_required",
    ),
    authority=(
        "Propose reversible product scope, requirements, and user experiments.",
        "Exclude unvalidated features from the minimum usable scope.",
        "Classify product claims only from supplied context and Evidence.",
        "May not approve external contact, public claims, spend, or CEO decisions.",
    ),
    escalation_conditions=(
        "The target user or observable problem cannot be identified.",
        "Validation requires contacting a real person or using non-synthetic data.",
        "Product scope creates legal, rights, safety, privacy, or irreversible risk.",
        "Evidence is insufficient to distinguish a FACT from an ASSUMPTION.",
        "A material product choice requires CEO judgment.",
    ),
)


_CMO = RoleSpec(
    mission=(
        "Determine whether a real customer and market exist and design the "
        "cheapest valid market test."
    ),
    permitted_inputs=(
        "CEO idea, objectives, constraints, and recorded decisions",
        "provided customer, buyer, channel, and alternative Evidence",
        "provided product definition and technical constraints",
        "identified Evidence and its provenance",
        "First Principles decision requirements",
    ),
    available_tools=(
        "inspection of provided market and Evidence artifacts",
        "structured JSON and Markdown file handoff",
        "deterministic demand-signal and falsification analysis",
        "synthetic, non-contact market experiment design",
    ),
    mandatory_questions=(
        "Who experiences the problem?",
        "Who uses the product?",
        "Who pays?",
        "Are the user and buyer the same?",
        "What evidence shows that the problem is important?",
        "What alternative is currently being used?",
        "How can the customer be reached?",
        "What message communicates the value?",
        "What observable customer action validates demand?",
        "What would falsify the market hypothesis?",
        "What is the cheapest valid market experiment?",
        "What must not be mistaken for real demand?",
    ),
    required_structured_outputs=(
        "target_user",
        "paying_customer",
        "user_buyer_relationship",
        "market_facts",
        "market_assumptions",
        "current_alternatives",
        "value_proposition",
        "positioning",
        "acquisition_channel_hypothesis",
        "market_validation_experiment",
        "measurable_market_signal",
        "false_positive_market_signals",
        "market_stop_condition",
        "ceo_decision_required",
    ),
    authority=(
        "Propose reversible positioning, channel hypotheses, and market experiments.",
        "Define observable demand signals and false-positive signals.",
        "Classify market claims only from supplied context and Evidence.",
        "May not contact customers, publish claims, spend funds, or make CEO decisions.",
    ),
    escalation_conditions=(
        "Validation requires real outreach, publication, purchasing, or another external action.",
        "User and buyer identity or authority is materially unresolved.",
        "Rights, privacy, regulated claims, or reputational risk is involved.",
        "Evidence is insufficient to distinguish a FACT from an ASSUMPTION.",
        "A material positioning or market choice requires CEO judgment.",
    ),
)


# A read-only mapping makes the exact three-role boundary enforceable at runtime.
ROLE_SPECS: Mapping[str, RoleSpec] = MappingProxyType(
    {"cto": _CTO, "cpo": _CPO, "cmo": _CMO}
)
ROLE_NAMES: tuple[str, ...] = tuple(ROLE_SPECS)


def _normalize_role(role: str) -> str:
    if not isinstance(role, str):
        raise TypeError("role must be a string")
    normalized = role.strip().lower()
    if normalized not in ROLE_SPECS:
        allowed = ", ".join(ROLE_NAMES)
        raise KeyError(f"Unknown executive role {role!r}; expected one of: {allowed}")
    return normalized


def get_role_spec(role: str) -> RoleSpec:
    """Look up one of the three executive specifications, case-insensitively."""

    return ROLE_SPECS[_normalize_role(role)]


def list_role_specs() -> tuple[tuple[str, RoleSpec], ...]:
    """Return role/spec pairs in deterministic council order."""

    return tuple(ROLE_SPECS.items())


def serialize_role_spec(role: str) -> dict[str, Any]:
    """Serialize a named role for a council request."""

    normalized = _normalize_role(role)
    return {"role": normalized, **ROLE_SPECS[normalized].to_dict()}


def serialize_role_specs() -> dict[str, dict[str, Any]]:
    """Serialize all and only the V0.1 roles."""

    return {role: spec.to_dict() for role, spec in ROLE_SPECS.items()}


def _identity(idea_id: str | Any, idea_text: str | None) -> tuple[str, str]:
    """Accept either an Idea-like object or explicit id/text strings."""

    if not isinstance(idea_id, str):
        idea = idea_id
        idea_id = getattr(idea, "id", None)
        if idea_text is None:
            idea_text = getattr(idea, "text", None)
    if not isinstance(idea_id, str) or not idea_id.strip():
        raise ValueError("idea_id must be a non-empty string")
    if idea_text is None:
        idea_text = "Create one deterministic synthetic artifact and verify its content."
    if not isinstance(idea_text, str) or not idea_text.strip():
        raise ValueError("idea_text must be a non-empty string")
    return idea_id.strip(), idea_text.strip()


def _fact(idea_id: str) -> dict[str, Any]:
    return {
        "id": "fact-1",
        "type": "FACT",
        "statement": "The deterministic synthetic input fixture exists.",
        "evidence_refs": ["source-evidence-1"],
        "source_types": ["SYNTHETIC_FIXTURE"],
    }


def _assumption(idea_id: str) -> dict[str, Any]:
    return {
        "id": "assumption-1",
        "type": "ASSUMPTION",
        "statement": "Writing one artifact is sufficient for this experiment.",
        "evidence_refs": [],
        "source_types": ["DETERMINISTIC_TEST_FIXTURE"],
    }


def _metric() -> dict[str, Any]:
    return {
        "name": "verified_artifact_count",
        "formula": "count(artifacts whose sha256 matches expected content)",
        "unit": "artifacts",
        "time_window": "one synthetic run",
        "data_source": "deterministic verifier output",
        "baseline": {"status": "KNOWN", "value": 0},
        "desired_outcome": {"operator": ">=", "value": 1},
    }


def _governance() -> dict[str, dict[str, Any]]:
    return {
        "financial_check": {
            "validation_cost_ceiling": {"amount": 0, "currency": "KRW"},
            "cash_ceiling": {"amount": 0, "currency": "KRW"},
            "time_ceiling": "one local synthetic run",
            "variable_cost_assumptions": ["Local synthetic execution has no marginal cash cost."],
            "unit_economic_unknowns": ["Real venture unit economics are outside this fixture."],
            "financial_stop_threshold": "Stop before any paid service or purchase.",
        },
        "rights_and_legal_check": {
            "rights_owner": "synthetic fixture author",
            "authority_basis": "repository-local synthetic test fixture",
            "consent_or_delegation_status": "NOT_APPLICABLE_SYNTHETIC_DATA",
            "personal_data_involved": False,
            "biometric_data_involved": False,
            "regulated_data_involved": False,
            "prohibited_external_actions": [
                "customer contact",
                "public publication",
                "legal or rights enforcement",
            ],
            "specialist_review_required": False,
            "explicit_ceo_approval_required": False,
        },
        "operations_check": {
            "first_72_hour_actions": [
                "create one deterministic local artifact",
                "run its mechanical verifier",
                "preserve verifier Evidence",
            ],
            "dependencies": ["Python standard library", "writable temporary workspace"],
            "current_bottleneck": "No verified artifact exists yet.",
            "operating_mode": "ON_DEMAND",
            "cadence": "one synthetic run",
            "recovery_plan": "Resume from canonical SQLite state without repeating a completed run.",
        },
        "strategy_check": {
            "why_now": "The operating kernel needs an end-to-end deterministic proof.",
            "fundamental_advantage_hypothesis": "Auditable local state reduces unverifiable handoffs.",
            "strategic_chokepoint_hypothesis": "Mechanical verification is the smallest critical unit.",
            "zero_based_alternative": "Verify the expected content in memory without creating a file.",
            "do_nothing_consequence": "The vertical slice remains unverified.",
            "revisit_condition": "Revisit if the verifier contract or artifact format changes.",
        },
    }


def _contract_contribution(idea_id: str) -> dict[str, Any]:
    fact = _fact(idea_id)
    assumption = _assumption(idea_id)
    contribution: dict[str, Any] = {
        "decision_level": "FP_STANDARD",
        "observable_problem": {
            "statement": "A deterministic synthetic artifact is not yet present.",
            "observable": True,
        },
        "metric": _metric(),
        "claims": [fact, assumption],
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
                "statement": "A web dashboard is customary but unnecessary for this experiment.",
                "type": "INHERITED_CONVENTION",
            }
        ],
        "decomposition": {
            "technology": "local deterministic text-file creation and hashing",
            "data": "repository-local synthetic fixture only",
            "time": "one on-demand run",
            "cash_and_unit_economics": "zero permitted cash spend; real economics unknown",
            "rights_and_authority": "synthetic data only; no third-party rights",
            "human_behavior": "the CEO may later transfer a review request manually",
            "risk_and_reversibility": "workspace artifacts are local and reproducible",
        },
        "zero_based_alternative": "Verify the expected content in memory without creating a file.",
        "do_nothing_option": "Leave the synthetic fixture unprocessed.",
        "highest_risk_assumption": assumption["id"],
        "cheapest_valid_experiment": {
            "description": "Create one deterministic text artifact and hash its content.",
            "measurable_output": "verified_artifact_count",
        },
        "pass_condition": "verified_artifact_count >= 1",
        "fail_condition": "verified_artifact_count == 0",
        "stop_condition": "Stop after one verified artifact.",
        "revisit_condition": "Revisit if the verifier contract or artifact format changes.",
        "decision_basis": [{"claim_id": fact["id"], "type": "FACT"}],
    }
    contribution.update(_governance())
    return contribution


def _role_contract_contribution(role: str, idea_id: str) -> dict[str, Any]:
    """Return realistic independent contributions with stable owned fields."""

    contribution = _contract_contribution(idea_id)
    if role == "cpo":
        contribution["observable_problem"] = {
            "statement": "The operator has no auditable verified artifact yet.",
            "observable": True,
        }
        contribution["metric"] = {
            **contribution["metric"],
            "data_source": "product acceptance plus deterministic verifier output",
        }
    elif role == "cmo":
        contribution["cheapest_valid_experiment"] = {
            "description": "Run one local validation before any external acquisition action.",
            "measurable_output": "verified_artifact_count",
        }
        contribution["strategy_check"] = {
            **contribution["strategy_check"],
            "why_now": "A verified local loop is needed before testing any market channel.",
        }
    return contribution


def _model_metadata() -> dict[str, Any]:
    # Preserve the required configured-session metadata while making the test
    # source explicit; no model is actually run by these functions.
    return {
        "requested_model": "GPT-5.6 Sol",
        "resolved_model": "current_cursor_codex_session",
        "execution_surface": "Cursor Codex",
        "authentication_mode": "ChatGPT subscription",
        "model_executed": False,
        "response_source": "deterministic_synthetic_fixture",
    }


def _cto_outputs(idea_id: str, idea_text: str) -> dict[str, Any]:
    fact = _fact(idea_id)
    assumption = _assumption(idea_id)
    return {
        "technical_facts": [deepcopy(fact)],
        "technical_assumptions": [deepcopy(assumption)],
        "hard_technical_constraints": [
            {
                "type": "HARD_CONSTRAINT",
                "statement": "The experiment must remain local and deterministic.",
            }
        ],
        "inherited_technical_conventions": [
            {
                "type": "INHERITED_CONVENTION",
                "statement": "A web interface is conventional but is not required.",
            }
        ],
        "fundamental_technical_units": [
            "write exact expected bytes",
            "calculate sha256",
            "compare observed and expected content",
        ],
        "architecture_proposal": {
            "summary": "A local one-file artifact with a deterministic verifier.",
            "idea": idea_text,
        },
        "zero_based_alternative": "Verify the expected content in memory without creating a file.",
        "highest_risk_technical_assumption": assumption["id"],
        "cheapest_validating_experiment": {
            "description": "Create and verify one deterministic synthetic text artifact.",
            "measurable_output": "verified_artifact_count",
        },
        "work_orders": [
            {
                "title": "Create the deterministic synthetic artifact",
                "objective": "Produce one local artifact whose exact content can be verified.",
                "verifier": "exact-content and sha256 comparison",
            }
        ],
        "mechanical_acceptance_criteria": [
            "artifact exists at the declared relative path",
            "artifact bytes equal the declared expected content",
            "artifact sha256 equals the verifier result",
        ],
        "technical_risks": ["verifier drift", "interrupted local write"],
        "stop_condition": "Stop after one verified artifact.",
        "revisit_condition": "Revisit if the verifier contract or artifact format changes.",
        "ceo_decision_required": False,
    }


def _cpo_outputs(idea_id: str, idea_text: str) -> dict[str, Any]:
    fact = _fact(idea_id)
    assumption = _assumption(idea_id)
    return {
        "user_definition": "The operator validating the local AI company OS vertical slice.",
        "observable_user_problem": "The operator has no verified end-to-end synthetic artifact.",
        "current_user_behavior": "The operator can inspect files and verifier output manually.",
        "desired_user_outcome": "Obtain one auditable PASS result after an on-demand run.",
        "product_facts": [deepcopy(fact)],
        "product_assumptions": [deepcopy(assumption)],
        "product_hypothesis": {
            "status": "UNTESTED",
            "statement": "A single verified artifact demonstrates the smallest usable execution loop.",
        },
        "minimum_usable_scope": [
            "one synthetic idea",
            "one deterministic artifact",
            "one mechanical PASS or FAIL result",
        ],
        "explicitly_excluded_scope": [
            "web dashboard",
            "real customer data",
            "external messaging",
            "background execution",
        ],
        "user_journey": [
            "submit synthetic idea",
            "approve contract state",
            "execute one Work Order",
            "inspect preserved Evidence",
        ],
        "product_requirements": [
            {"id": "PR-1", "statement": "Execution is local and on-demand."},
            {"id": "PR-2", "statement": "Completion is mechanically verifiable."},
        ],
        "product_acceptance_criteria": [
            "verified_artifact_count is at least one",
            "Evidence identifies the run and verifier output",
        ],
        "product_validation_experiment": {
            "description": "Run the synthetic vertical slice once.",
            "measurable_output": "verified_artifact_count",
            "idea": idea_text,
        },
        "product_stop_condition": "Stop after one verified artifact or the first deterministic failure.",
        "ceo_decision_required": False,
    }


def _cmo_outputs(idea_id: str, idea_text: str) -> dict[str, Any]:
    fact = _fact(idea_id)
    assumption = {
        "id": "market-assumption-1",
        "type": "ASSUMPTION",
        "statement": "An operator values an auditable verified execution loop.",
        "evidence_refs": [],
        "source_types": ["DETERMINISTIC_TEST_FIXTURE"],
    }
    return {
        "target_user": "A synthetic local operator persona used only by this fixture.",
        "paying_customer": "UNKNOWN; payment is outside the synthetic validation scope.",
        "user_buyer_relationship": "UNKNOWN; no real buyer is asserted.",
        "market_facts": [deepcopy(fact)],
        "market_assumptions": [assumption],
        "current_alternatives": ["manual local file creation and inspection"],
        "value_proposition": "Produce traceable verified evidence from an idea with local state.",
        "positioning": "An auditable local execution kernel, not an autonomous company service.",
        "acquisition_channel_hypothesis": {
            "status": "UNTESTED",
            "channel": "UNKNOWN; no external acquisition action is permitted in this fixture.",
        },
        "market_validation_experiment": {
            "description": "No real market experiment is performed; preserve an explicit untested hypothesis.",
            "measurable_output": "verified_artifact_count",
            "idea": idea_text,
        },
        "measurable_market_signal": "A future real user performs the workflow without assistance; not tested here.",
        "false_positive_market_signals": [
            "executive-model agreement",
            "fixture test success",
            "uncited enthusiasm",
        ],
        "market_stop_condition": "Stop before external contact, publication, or spend.",
        "ceo_decision_required": False,
    }


_OUTPUT_BUILDERS = MappingProxyType(
    {"cto": _cto_outputs, "cpo": _cpo_outputs, "cmo": _cmo_outputs}
)


def synthetic_council_response(
    role: str,
    idea_id: str | Any,
    idea_text: str | None = None,
) -> dict[str, Any]:
    """Build one deterministic, schema-complete council response.

    The returned object is new on every call, contains all role-specific output
    fields, and includes a complete FP_STANDARD contract contribution.  Every
    claim classified as FACT has a source Evidence reference.
    """

    normalized = _normalize_role(role)
    normalized_idea_id, normalized_idea_text = _identity(idea_id, idea_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "idea_id": normalized_idea_id,
        "role": normalized,
        "model_metadata": _model_metadata(),
        "outputs": _OUTPUT_BUILDERS[normalized](
            normalized_idea_id, normalized_idea_text
        ),
        "contract_contribution": _role_contract_contribution(
            normalized,
            normalized_idea_id,
        ),
    }


def synthetic_cto_response(
    idea_id: str | Any, idea_text: str | None = None
) -> dict[str, Any]:
    return synthetic_council_response("cto", idea_id, idea_text)


def synthetic_cpo_response(
    idea_id: str | Any, idea_text: str | None = None
) -> dict[str, Any]:
    return synthetic_council_response("cpo", idea_id, idea_text)


def synthetic_cmo_response(
    idea_id: str | Any, idea_text: str | None = None
) -> dict[str, Any]:
    return synthetic_council_response("cmo", idea_id, idea_text)


def synthetic_council_responses(
    idea_id: str | Any, idea_text: str | None = None
) -> dict[str, dict[str, Any]]:
    """Build independent responses for the complete three-role council."""

    normalized_idea_id, normalized_idea_text = _identity(idea_id, idea_text)
    return {
        role: synthetic_council_response(role, normalized_idea_id, normalized_idea_text)
        for role in ROLE_NAMES
    }


# Compatibility-oriented explicit names for callers that prefer "build" verbs.
build_synthetic_council_response = synthetic_council_response
build_synthetic_cto_response = synthetic_cto_response
build_synthetic_cpo_response = synthetic_cpo_response
build_synthetic_cmo_response = synthetic_cmo_response


__all__ = [
    "ROLE_NAMES",
    "ROLE_SPECS",
    "RoleSpec",
    "build_synthetic_cmo_response",
    "build_synthetic_council_response",
    "build_synthetic_cpo_response",
    "build_synthetic_cto_response",
    "get_role_spec",
    "list_role_specs",
    "serialize_role_spec",
    "serialize_role_specs",
    "synthetic_cmo_response",
    "synthetic_council_response",
    "synthetic_council_responses",
    "synthetic_cpo_response",
    "synthetic_cto_response",
]
