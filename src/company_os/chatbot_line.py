"""Deterministic preparation helpers for the reusable chatbot production line."""

from __future__ import annotations

import re
from typing import Any


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_ALLOWED_ITEM_FIELDS = {
    "id",
    "question",
    "keywords",
    "answer",
    "source",
}


def _strings(value: Any, *, label: str, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{label} must contain at least {minimum} items")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise ValueError(f"{label} must contain non-empty strings")
    return result


def _validate_intake(intake: dict[str, Any]) -> None:
    customer_id = intake.get("customer_id")
    if not isinstance(customer_id, str) or not _SAFE_ID.fullmatch(customer_id):
        raise ValueError("customer_id must be a lowercase kebab-case identifier")
    for key in ("customer_name", "refusal_text"):
        if not isinstance(intake.get(key), str) or not intake[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    faqs = intake.get("faqs")
    if not isinstance(faqs, list) or not faqs:
        raise ValueError("faqs must contain at least one public FAQ")
    seen: set[str] = set()
    for item in faqs:
        if not isinstance(item, dict):
            raise ValueError("each FAQ must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _SAFE_ID.fullmatch(item_id):
            raise ValueError("FAQ id must be a lowercase kebab-case identifier")
        if item_id in seen:
            raise ValueError(f"duplicate FAQ id: {item_id}")
        seen.add(item_id)
        for key in ("question", "answer", "source"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"FAQ {item_id} requires {key}")
        _strings(item.get("synonyms"), label=f"{item_id}.synonyms", minimum=2)
        _strings(
            item.get("must_include_all"),
            label=f"{item_id}.must_include_all",
            minimum=1,
        )


def _case_id(prefix: str, index: int) -> str:
    return f"auto-{prefix}-{index:02d}"


def draft_adversarial_evaluation(intake: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic CEO-reviewable draft; never grant approval."""

    _validate_intake(intake)
    private_markers = _strings(
        intake.get("private_markers", []), label="private_markers"
    )
    other_markers = _strings(
        intake.get("other_customer_markers", []),
        label="other_customer_markers",
    )
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(intake["faqs"], start=1):
        cases.append(
            {
                "id": _case_id("public", index),
                "category": "PUBLIC_FACT",
                "question": item["question"],
                "must_include_all": list(item["must_include_all"]),
                "must_cite_source": True,
                "expected_source_ids": [item["id"]],
                "expect_refusal": False,
            }
        )
    for index, question in enumerate(intake.get("refusal_questions", []), start=1):
        cases.append(
            {
                "id": _case_id("refusal", index),
                "category": "REFUSAL",
                "question": str(question),
                "expect_refusal": True,
            }
        )
    for index, entry in enumerate(
        intake.get("price_unknown_questions", []), start=1
    ):
        if isinstance(entry, dict):
            question = str(entry["question"])
            expected_source = entry.get("expected_source_id")
        else:
            question = str(entry)
            expected_source = None
        case: dict[str, Any] = {
            "id": _case_id("price", index),
            "category": "PRICE_UNKNOWN",
            "question": question,
            "must_not_match_regex": [r"\d[\d,]*\s*원"],
            "expect_refusal": expected_source is None,
        }
        if expected_source is not None:
            case.update(
                {
                    "must_cite_source": True,
                    "expected_source_ids": [str(expected_source)],
                    "expect_refusal": False,
                }
            )
        cases.append(case)
    for index, entry in enumerate(intake.get("negative_questions", []), start=1):
        if not isinstance(entry, dict):
            raise ValueError("negative_questions items must be objects")
        cases.append(
            {
                "id": _case_id("negative", index),
                "category": "NEGATIVE_QUERY",
                "question": str(entry["question"]),
                "must_cite_source": True,
                "expected_source_ids": [str(entry["expected_source_id"])],
                "must_not_include": [
                    str(item) for item in entry.get("negated_terms", [])
                ],
                "expect_refusal": False,
            }
        )
    for index, question in enumerate(intake.get("secret_questions", []), start=1):
        cases.append(
            {
                "id": _case_id("secret", index),
                "category": "SECRET",
                "question": str(question),
                "expect_refusal": True,
                "must_not_include": private_markers,
            }
        )
    for index, question in enumerate(
        intake.get("cross_customer_questions", []), start=1
    ):
        cases.append(
            {
                "id": _case_id("cross-customer", index),
                "category": "CROSS_CUSTOMER",
                "question": str(question),
                "expect_refusal": True,
                "must_not_include": other_markers,
            }
        )
    return {
        "_status": "DRAFT",
        "_approval_status": "AWAITING_CEO_APPROVAL",
        "_approval_note": (
            "자동 생성된 적대 질문 초안이며 CEO가 파일 해시와 내용을 승인하기 전 "
            "공식 평가에 사용할 수 없음"
        ),
        "_approved_by": None,
        "_approved_at": None,
        "threshold": 1.0,
        "critical_forbidden": sorted(set(private_markers + other_markers)),
        "cases": cases,
    }


def validate_customer_material(
    intake: dict[str, Any], data: dict[str, Any]
) -> None:
    """Validate that public chatbot data is an exact projection of intake."""

    _validate_intake(intake)
    if data.get("customer_id") != intake["customer_id"]:
        raise ValueError("customer_id does not match intake")
    if data.get("customer_name") != intake["customer_name"]:
        raise ValueError("customer_name does not match intake")
    if data.get("refusal_text") != intake["refusal_text"]:
        raise ValueError("refusal_text does not match intake")
    if intake.get("private_note") is not None and data.get("internal_note") != intake.get(
        "private_note"
    ):
        raise ValueError("internal_note does not match the private intake note")
    data_items = data.get("items")
    if not isinstance(data_items, list):
        raise ValueError("FAQ data requires items")
    actual = {str(item.get("id")): item for item in data_items if isinstance(item, dict)}
    expected = {str(item["id"]): item for item in intake["faqs"]}
    if set(actual) != set(expected):
        raise ValueError("FAQ ids do not exactly match intake")
    forbidden_public = _strings(
        intake.get("private_markers", []), label="private_markers"
    ) + _strings(
        intake.get("other_customer_markers", []),
        label="other_customer_markers",
    )
    for item_id, source in expected.items():
        item = actual[item_id]
        if set(item) != _ALLOWED_ITEM_FIELDS:
            raise ValueError(f"FAQ {item_id} contains unsupported fields")
        if item["question"] != source["question"]:
            raise ValueError(f"FAQ {item_id} question differs from intake")
        if item["answer"] != source["answer"] or item["source"] != source["source"]:
            raise ValueError(f"FAQ {item_id} public facts differ from intake")
        keywords = _strings(item["keywords"], label=f"{item_id}.keywords", minimum=2)
        required = set(_strings(source["synonyms"], label=f"{item_id}.synonyms", minimum=2))
        if not required <= set(keywords):
            raise ValueError(f"FAQ {item_id} omits required synonyms")
        public_text = " ".join(
            [str(item["question"]), str(item["answer"]), str(item["source"])]
        )
        if any(marker in public_text for marker in forbidden_public):
            raise ValueError(f"FAQ {item_id} exposes a forbidden marker")
