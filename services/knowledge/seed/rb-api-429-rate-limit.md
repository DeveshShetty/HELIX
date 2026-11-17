---
id: rb-api-429-rate-limit
type: runbook
title: "API returns 429 Too Many Requests"
tags: api, 429, rate limit, throttling, too many requests
---
Symptoms: API calls fail with HTTP 429 Too Many Requests.

Cause: the API key exceeded its rate limit (see the FAQ on API rate limits for the numbers).

Steps:
1. Read the Retry-After header on the 429 response and wait that many seconds before retrying.
2. Retry with exponential backoff and jitter instead of retrying immediately.
3. Spread batch jobs over time, or use the bulk endpoints (/v2/bulk/*), which count as one request per batch.
4. If you consistently need more, contact sales to raise the limit on an Enterprise plan.
