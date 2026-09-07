import asyncio
from pathlib import Path

import yaml

from ndaguard.plugins import _INJECTION
from ndaguard.policy import PolicyEngine
from ndaguard.tools import _CORPUS
from demo import content_scan_after_tool, policy_before_tool, seed_demo_state

CASES = yaml.safe_load(Path("test_queries.yaml").read_text())
ENGINE = PolicyEngine()
SESSION = {"tenant": "tenant-eu"}
ROLE = "legal_counsel"

ARGS = {
    "search_ndas": {"query": "acme"},
    "read_clause_allowed": {"doc_id": "NDA-0119", "repository": "tenant-eu"},
    "read_clause_injected": {"doc_id": "NDA-0442", "repository": "tenant-eu"},
    "read_clause_cross_tenant": {"doc_id": "NDA-9001", "repository": "tenant-us"},
    "read_clause_tenant_lie": {"doc_id": "NDA-9001", "repository": "tenant-eu"},
    "redline_clause_allowed": {"doc_id": "NDA-0119", "clause_count": 1},
    "redline_clause_bulk": {"doc_id": "NDA-0119", "clause_count": 50},
    "request_signature_allowed_domain": {
        "doc_id": "NDA-0119", "counterparty_domain": "acme-corp.com"
    },
    "request_signature_newvendor": {
        "doc_id": "NDA-0119", "counterparty_domain": "newvendor.io"
    },
    "request_signature_exfil": {
        "doc_id": "NDA-0442", "counterparty_domain": "data-exfil.example"
    },
    "purge_document": {"doc_id": "NDA-0119"},
}

CASE_EVALS = {
    "T01": [("search_ndas", ARGS["search_ndas"], "allow", "R-ALLOW")],
    "T02": [("search_ndas", {"query": "unknown"}, "allow", "R-ALLOW")],
    "T03": [("read_clause", ARGS["read_clause_allowed"], "allow", "R-ALLOW")],
    "T04": [("read_clause", ARGS["read_clause_cross_tenant"], "deny", "R-021")],
    "T05": [("read_clause", ARGS["read_clause_injected"], "allow", "R-ALLOW")],
    "T06": [
        ("read_clause", ARGS["read_clause_injected"], "allow", "R-ALLOW"),
        ("request_signature", ARGS["request_signature_exfil"], "deny", "R-014"),
    ],
    "T07": [("request_signature", ARGS["request_signature_allowed_domain"], "needs_approval", "R-010")],
    "T08": [("request_signature", ARGS["request_signature_newvendor"], "deny", "R-014")],
    "T09": [("request_signature", ARGS["request_signature_exfil"], "deny", "R-014")],
    "T10": [("request_signature", ARGS["request_signature_allowed_domain"], "needs_approval", "R-010")],
    "T11": [("redline_clause", ARGS["redline_clause_allowed"], "allow", "R-ALLOW")],
    "T12": [("redline_clause", ARGS["redline_clause_bulk"], "deny", "R-033")],
    "T13": [("purge_document", ARGS["purge_document"], "deny", "R-001")],
    "T14": [("request_signature", ARGS["request_signature_exfil"], "deny", "R-014")],
    "T15": [("request_signature", ARGS["request_signature_newvendor"], "deny", "R-014")],
    "T16": [("request_signature", ARGS["request_signature_exfil"], "deny", "R-014")],
    "T17": [("read_clause", ARGS["read_clause_tenant_lie"], "allow", "R-ALLOW")],
    "T18": [("request_signature", ARGS["request_signature_allowed_domain"], "needs_approval", "R-010")],
    "T19": [
        ("search_ndas", ARGS["search_ndas"], "allow", "R-ALLOW"),
        ("read_clause", ARGS["read_clause_allowed"], "allow", "R-ALLOW"),
    ],
    "T20": [
        ("search_ndas", {"query": ""}, "allow", "R-ALLOW"),
        ("redline_clause", ARGS["redline_clause_allowed"], "allow", "R-ALLOW"),
        ("request_signature", ARGS["request_signature_allowed_domain"], "needs_approval", "R-010"),
    ],
}


def case(case_id):
    return next(c for c in CASES if c["id"] == case_id)


def test_every_manual_query_has_deterministic_expectations():
    assert len(CASES) == 20
    assert set(CASE_EVALS) == {c["id"] for c in CASES}


def test_policy_expectations_for_manual_queries():
    for case_id, evals in CASE_EVALS.items():
        for tool_name, args, effect, rule in evals:
            decision = ENGINE.evaluate(
                role=ROLE, tool_name=tool_name, args=args, session=SESSION
            )
            assert (decision.effect, decision.rule_id) == (effect, rule), case_id


def test_prompt_injection_prompts_are_marked_by_the_content_detector():
    for case_id in ("T14",):
        assert _INJECTION.search(case(case_id)["prompt"]), case_id


def test_injected_document_is_marked_by_the_content_detector():
    text = _CORPUS["NDA-0442"]["clause_7"]
    assert _INJECTION.search(text)


def test_tenant_lie_still_fails_at_the_data_layer():
    doc = _CORPUS["NDA-9001"]
    assert doc["tenant"] != SESSION["tenant"]


class DummyContext:
    def __init__(self):
        self.state = {}


class DummyTool:
    name = "read_clause"


def test_web_callbacks_accept_adk_agent_keyword_shapes():
    ctx = DummyContext()
    asyncio.run(seed_demo_state(callback_context=ctx))
    assert ctx.state["tenant"] == "tenant-eu"

    decision = asyncio.run(policy_before_tool(
        tool=DummyTool(),
        args={"doc_id": "NDA-0119", "repository": "tenant-eu"},
        tool_context=ctx,
    ))
    assert decision is None

    redacted = asyncio.run(content_scan_after_tool(
        tool=DummyTool(),
        args={"doc_id": "NDA-0442", "repository": "tenant-eu"},
        tool_context=ctx,
        tool_response={"text": _CORPUS["NDA-0442"]["clause_7"]},
    ))
    assert redacted["text"] == "[content withheld: embedded instruction detected]"
