---
id: inc-0051-double-charge
type: incident
title: "INC-0051: Some customers were charged twice for one payment"
tags: payments, double charge, duplicate, charged twice, idempotency
---
Problem: Some customers saw two identical charges after a checkout timed out and was retried.

Resolution: Retries were sent without an Idempotency-Key, so each retry created a new charge. Duplicate
charges were refunded automatically within 24 hours. Fix for integrators: always send an
Idempotency-Key header on POST /v2/payments so retries return the original payment.
