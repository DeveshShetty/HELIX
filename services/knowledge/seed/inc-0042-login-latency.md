---
id: inc-0042-login-latency
type: incident
title: "INC-0042: Login latency spike caused by expired session-cache certificate"
tags: login, latency, slow, redis, certificate, tls
---
Question: Logins are taking 20+ seconds for all users in eu-west.

Resolution: The TLS certificate for the session cache (Redis) expired, so
every login fell back to the slow database session path. Rotated the
certificate with `helixctl certs rotate session-cache --region eu-west`;
latency returned to normal within 2 minutes. Added an alert for certificates
expiring within 14 days.
