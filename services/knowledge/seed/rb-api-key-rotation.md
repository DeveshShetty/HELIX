---
id: rb-api-key-rotation
type: runbook
title: "Rotate a leaked or compromised API key"
tags: api key, rotate, leaked, compromised, secret, security
---
If an API key may have leaked (for example it was committed to a public repository):

1. Go to Developers > API keys and click "Roll key" on the affected key.
2. Choose an expiry for the old key: "Now" if it is actively abused, otherwise up to 24 hours
   so you can deploy the new key without downtime.
3. Deploy the new key to your services.
4. Review Developers > Logs for requests made with the old key that you do not recognise.
Rolled keys cannot be restored.
