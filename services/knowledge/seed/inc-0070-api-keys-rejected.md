---
id: inc-0070-api-keys-rejected
type: incident
title: "INC-0070: Valid API keys rejected with 401 after a key roll"
tags: api key, 401, unauthorized, rejected, rotation, roll
---
Problem: After rolling an API key, requests with the NEW key were rejected with HTTP 401 for about
10 minutes.

Resolution: The key cache in one region was not invalidated on roll. Cache invalidation was fixed;
new keys now work within 5 seconds of being created.
