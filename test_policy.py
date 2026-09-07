"""Policy tests. Run: python -m pytest test_policy.py -q

This file is the point of the whole exercise. Deterministic policy can be
asserted in CI. A probabilistic content filter cannot -- the best you get
there is an eval suite with a pass rate.
"""

from ndaguard.policy import PolicyEngine

EU = {"tenant": "tenant-eu"}
E = PolicyEngine()


def d(role, tool, **args):
    return E.evaluate(role=role, tool_name=tool, args=args, session=EU)


def test_read_is_open_to_analysts():
    assert d("analyst", "search_ndas", query="acme").allowed


def test_analyst_cannot_write():
    r = d("analyst", "redline_clause", doc_id="NDA-0119", clause_count=1)
    assert r.effect == "deny" and r.rule_id == "R-002"


def test_unknown_tool_is_denied_by_default():
    assert d("legal_counsel", "exec_shell").effect == "deny"


def test_destructive_tool_unreachable_for_every_role():
    for role in E.doc["roles"]:
        assert d(role, "purge_document", doc_id="NDA-0119").effect == "deny"


def test_outbound_requires_approval_even_when_arguments_are_valid():
    r = d("legal_counsel", "request_signature",
          doc_id="NDA-0119", counterparty_domain="acme-corp.com")
    assert r.effect == "needs_approval"


def test_domain_allowlist_beats_approval():
    # An unapproved domain must be denied outright, never queued for a
    # human to rubber-stamp.
    r = d("legal_counsel", "request_signature",
          doc_id="NDA-0119", counterparty_domain="data-exfil.example")
    assert r.effect == "deny" and r.rule_id == "R-014"


def test_cross_tenant_read_is_denied():
    r = d("legal_counsel", "read_clause", doc_id="NDA-9001", repository="tenant-us")
    assert r.effect == "deny" and r.rule_id == "R-021"


def test_bulk_redline_capped():
    assert d("legal_counsel", "redline_clause", doc_id="X", clause_count=50).rule_id == "R-033"
    assert d("legal_counsel", "redline_clause", doc_id="X", clause_count=3).allowed


def test_every_tool_declares_a_blast_radius():
    """Guards against someone adding a tool without classifying it."""
    for name, spec in E.doc["tools"].items():
        assert spec.get("blast_radius") in {"read", "write", "outbound", "destructive"}, name


def test_no_role_holds_a_destructive_tool():
    destructive = {n for n, s in E.doc["tools"].items()
                   if s["blast_radius"] == "destructive"}
    for role, spec in E.doc["roles"].items():
        assert not destructive & set(spec["tools"]), role
