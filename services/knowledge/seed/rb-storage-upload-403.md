---
id: rb-storage-upload-403
type: runbook
title: "Object storage upload returns 403 SignatureDoesNotMatch"
tags: storage, upload, 403, forbidden, signature, presigned url, clock
---
Symptoms: PUT requests to a bucket fail with HTTP 403 and the error code SignatureDoesNotMatch
or RequestTimeTooSkewed.

Cause: the request signature is computed with the client's clock. If the clock is more than
15 minutes off, or the presigned URL has expired, the signature is rejected.

Steps:
1. Check the client machine's clock and enable NTP time sync.
2. If you use presigned URLs, generate them immediately before the upload; they expire after 15 minutes by default.
3. Make sure the Content-Type header sent with the upload matches the one used to sign the URL.
