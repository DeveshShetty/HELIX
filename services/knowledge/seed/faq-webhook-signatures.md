---
id: faq-webhook-signatures
type: faq
title: "How do I verify that a webhook came from Helix Cloud?"
tags: webhooks, signature, verify, security, hmac
---
Every webhook has a Helix-Signature header: an HMAC-SHA256 of the raw request body using your
endpoint's signing secret. Compute the same HMAC and compare in constant time. Reject requests whose
timestamp is more than 5 minutes old to prevent replays.
