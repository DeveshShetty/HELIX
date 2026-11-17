---
id: rb-api-503-database
type: runbook
title: "API returns 503 'database unavailable'"
tags: api, 503, service unavailable, database, outage
---
Symptoms: API requests fail with HTTP 503 and the message "database unavailable".

Cause: the regional database is failing over or is overloaded. Failover usually completes
within 2 minutes.

Steps:
1. Check status.helix.example for an ongoing incident in your region.
2. Retry with exponential backoff; requests that failed with 503 were not applied and are safe to retry.
3. If 503s last more than 10 minutes and the status page shows no incident, open a support ticket
   with the request IDs from the X-Request-Id header.
