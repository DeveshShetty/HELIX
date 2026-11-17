---
id: inc-0081-dashboard-blank-safari
type: incident
title: "INC-0081: Dashboard shows a blank page in Safari"
tags: dashboard, blank page, safari, browser, white screen
---
Problem: The dashboard showed a blank white page in Safari 16.

Resolution: A JavaScript feature not supported by Safari 16 was shipped in a release. The release was
rolled back and the code was fixed to support Safari 16 and later.
