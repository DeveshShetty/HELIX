---
id: rb-storage-quota-exceeded
type: runbook
title: "Uploads fail with 507 Insufficient Storage (quota exceeded)"
tags: storage, quota, 507, insufficient storage, limit, full
---
Symptoms: uploads fail with HTTP 507 Insufficient Storage and the error QuotaExceeded.

Cause: the organisation has reached the storage quota of its plan (Starter 100 GB, Team 1 TB,
Enterprise custom).

Steps:
1. Check usage under Storage > Usage; the largest buckets are listed first.
2. Delete unneeded objects, or set a lifecycle rule to expire old versions automatically.
3. To raise the quota, upgrade the plan under Billing > Plan. The new quota applies within 5 minutes.
