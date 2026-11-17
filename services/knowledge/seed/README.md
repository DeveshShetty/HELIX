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

Target: ~12 runbooks, ~15 FAQs, ~8 past incidents. Write them so that some
questions are clearly covered, some are near misses (similar words, different
problem), and some topics are deliberately missing (gaps); see
`eval/questions.jsonl`.

`inj-*` files are deliberately poisoned documents for the prompt-injection
demo. The Chat service must not obey instructions found inside them.
