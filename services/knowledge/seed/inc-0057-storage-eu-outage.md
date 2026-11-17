---
id: inc-0057-storage-eu-outage
type: incident
title: "INC-0057: Storage errors (HTTP 500) in eu-west"
tags: storage, 500, internal server error, eu-west, outage, upload failing
---
Problem: Uploads and downloads in eu-west failed with HTTP 500 for 40 minutes.

Resolution: A storage node ran out of disk space and the cluster stopped accepting writes. The node was
replaced and writes resumed. No data was lost. Clients that retried with backoff recovered automatically.
