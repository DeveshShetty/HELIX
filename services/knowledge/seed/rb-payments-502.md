---
id: rb-payments-502
type: runbook
title: Payment API returns 502 Bad Gateway
tags: payments, api, 502, gateway, checkout
---
Symptoms: calls to POST /v2/payments return HTTP 502 Bad Gateway; checkout
pages show "Payment could not be processed".

Cause: the payments gateway connection pool is exhausted, usually after a
deploy or a spike in checkout traffic.

Steps:
1. Check the "payments-gateway pool in use" panel. If it is at 100%, the pool is exhausted.
2. Restart the payment workers: `helixctl restart payments-worker --rolling`.
3. If 502s continue after 5 minutes, raise the pool size to 200 in
   `payments.gateway.pool_size` and redeploy.
4. Payments that failed with 502 are NOT charged; customers can safely retry.
