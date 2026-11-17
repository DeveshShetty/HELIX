---
id: inc-0074-reset-emails
type: incident
title: "INC-0074: Password reset emails not arriving for Outlook users"
tags: email, password reset, outlook, not arriving, spam
---
Problem: Password reset emails were not reaching Outlook and Hotmail addresses.

Resolution: Microsoft's filters rejected our mail after an SPF record change. The SPF record was
corrected and delivery recovered within 3 hours. Users were asked to request a new reset link.
