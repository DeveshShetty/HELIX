---
id: rb-deploy-rollback
type: runbook
title: "Roll back a bad deployment of a Helix Cloud app"
tags: deploy, deployment, rollback, release, revert, apps
---
If a new deployment of your app is broken:

1. Open Apps > (your app) > Deployments.
2. Find the last deployment marked "Healthy" and click "Roll back to this version".
3. The rollback reuses the old build, so it takes about 30 seconds and needs no rebuild.
4. Environment variables are NOT rolled back; if the bad release changed them, restore them manually.
