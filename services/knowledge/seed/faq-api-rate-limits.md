---
id: faq-api-rate-limits
type: faq
title: "What are the API rate limits?"
tags: api, rate limit, limits, requests per second, quota
---
Each API key may make 100 requests per second on Starter and Team plans, and 1,000 requests per
second on Enterprise. Bulk endpoints count as one request per batch. When you exceed the limit the
API returns HTTP 429 with a Retry-After header.
