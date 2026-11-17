# Helix Cloud knowledge base (synthetic)

Helix Cloud is a **fictional** SaaS platform (payments API, login/SSO, object
storage, billing, public REST API). Everything here is made up; no real
personal data anywhere (brief §7).

One file per document. File name = `<doc_id>.md`. Header between `---` lines,
one `key: value` per line, then the body:

```
---
id: rb-payments-502          # rb-* runbook, faq-* FAQ, inc-* past incident
type: runbook                # runbook | faq | incident
title: Payment API returns 502 Bad Gateway
tags: payments, api, 502, gateway
---
Body text in plain markdown...
```

Contents: 12 runbooks, 17 FAQs, 8 past incidents, 2 poisoned documents (39). Browse the live knowledge base, including fixes learned from tickets, at http://localhost:8004. Written so that some
questions are clearly covered, some are near misses (similar words, different
problem), and some topics are deliberately missing (gaps); see
`eval/questions.jsonl`.

`inj-*` files are deliberately poisoned documents for the prompt-injection
demo. The Chat service must not obey instructions found inside them.
