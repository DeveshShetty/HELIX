---
id: rb-payments-webhook-retries
type: runbook
title: "Payment webhooks are not being delivered"
tags: payments, webhooks, delivery, retries, callback
---
Symptoms: your endpoint stops receiving payment.succeeded or payment.failed webhooks;
the Webhooks page shows deliveries as "Failing".

Cause: Helix Cloud stops sending to an endpoint after it returns non-2xx responses
or times out (10 seconds) for 50 consecutive deliveries.

Steps:
1. Open Developers > Webhooks and check the "Last response" column for the error your endpoint returned.
2. Fix the endpoint so it returns HTTP 200 within 10 seconds (do slow work asynchronously).
3. Click "Re-enable endpoint". Failed deliveries from the last 72 hours are retried automatically
   with exponential backoff; older ones can be re-sent with "Resend" on each event.
