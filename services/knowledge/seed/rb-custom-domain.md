---
id: rb-custom-domain
type: runbook
title: "Custom domain not working or certificate stuck on 'pending'"
tags: domain, custom domain, dns, cname, certificate, tls, https, pending
---
Symptoms: your custom domain does not load, or its TLS certificate stays "Pending".

Steps:
1. Create a CNAME record pointing your domain to apps.helix.example (for a root domain use an
   ALIAS/ANAME record).
2. Remove any CAA record that does not allow letsencrypt.org.
3. DNS changes can take up to 1 hour. The certificate is issued automatically once the record resolves.
4. If it is still pending after 24 hours, remove and re-add the domain under Apps > Domains.
