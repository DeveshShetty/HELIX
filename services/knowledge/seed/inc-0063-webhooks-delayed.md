---
id: inc-0063-webhooks-delayed
type: incident
title: "INC-0063: Webhooks delayed by up to 2 hours"
tags: webhooks, delayed, late, backlog, queue
---
Problem: Webhook deliveries arrived up to 2 hours late.

Resolution: The webhook dispatch queue built up a backlog after a traffic spike. Dispatch workers were
scaled from 4 to 16 and the backlog cleared in 25 minutes. All webhooks were eventually delivered;
none were lost.
