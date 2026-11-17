---
id: faq-storage-limits
type: faq
title: "What is the maximum file size I can upload?"
tags: storage, file size, upload, limit, multipart, large files
---
A single PUT upload can be up to 5 GB. Larger files, up to 5 TB, must use multipart upload with
parts between 5 MB and 5 GB. The SDKs switch to multipart automatically for files over 100 MB.
