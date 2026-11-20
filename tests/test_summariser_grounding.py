"""Summariser: an AI draft whose steps the agent never gave is rejected."""
from conftest import load_service

summ = load_service("summariser")

MSGS = [
    {"seq": 1, "role": "user", "text": "My invoices show the wrong currency after I moved to Ireland"},
    {"seq": 2, "role": "bot", "text": "I'm connecting you with a support agent."},
    {"seq": 3, "role": "agent", "text": "Open Billing > Settings and change the billing country to Ireland. "
                                        "Then click Save and reload the invoices page."},
    {"seq": 4, "role": "user", "text": "That fixed it, the invoices are in euro now, thanks"},
]


def fake_llm(steps, cause="Not confirmed"):
    def llm_draft(msgs):
        return {"title": "Invoices show the wrong currency", "symptoms": "Invoices show the wrong currency",
                "cause": cause, "steps": steps}, "raw"
    return llm_draft


def check(checks, name):
    return next(c for c in checks if c["check"] == name)


def test_ungrounded_ai_draft_is_rejected(monkeypatch):
    monkeypatch.setattr(summ, "llm_draft", fake_llm(["Reinstall the mobile application from the store",
                                                     "Contact your bank about currency conversion fees"]))
    monkeypatch.setattr(summ.budget, "allow", lambda *a: True)
    title, body, method, checks, alt = summ.draft(MSGS)
    assert check(checks, "grounding")["ok"] is False
    assert method.startswith("rule-based")               # the agent's own words are used instead
    assert "Billing" in body and "bank" not in body
    assert alt is not None                               # the rejected AI version is kept for review


def test_grounded_ai_draft_is_used(monkeypatch):
    monkeypatch.setattr(summ, "llm_draft", fake_llm(["Open Billing > Settings and change the billing country.",
                                                     "Click Save and reload the invoices page."]))
    monkeypatch.setattr(summ.budget, "allow", lambda *a: True)
    title, body, method, checks, alt = summ.draft(MSGS)
    assert check(checks, "grounding")["ok"] is True
    assert not method.startswith("rule-based")


def test_invented_cause_is_replaced(monkeypatch):
    monkeypatch.setattr(summ, "llm_draft", fake_llm(["Open Billing > Settings and change the billing country.",
                                                     "Click Save and reload the invoices page."],
                                                    cause="A database migration corrupted the currency table"))
    monkeypatch.setattr(summ.budget, "allow", lambda *a: True)
    _, body, _, checks, _ = summ.draft(MSGS)
    assert check(checks, "cause")["ok"] is False
    assert "database migration" not in body


def test_budget_exhausted_degrades_to_rule_based(monkeypatch):
    called = []
    monkeypatch.setattr(summ, "llm_draft", lambda m: called.append(1))
    monkeypatch.setattr(summ.budget, "allow", lambda *a: False)
    _, _, method, checks, _ = summ.draft(MSGS)
    assert not called and method.startswith("rule-based")
    assert check(checks, "ai_budget")["ok"] is False
