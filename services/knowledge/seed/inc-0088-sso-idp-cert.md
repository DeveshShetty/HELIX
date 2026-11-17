---
id: inc-0088-sso-idp-cert
type: incident
title: "INC-0088: SSO logins failing for one organisation"
tags: sso, saml, login, certificate expired, identity provider
---
Problem: All users of one organisation got "SAML assertion invalid" when signing in.

Resolution: The organisation's identity-provider signing certificate had expired. Their admin signed in
with /login?sso=off, uploaded the new certificate under Admin > Security > SSO, and logins recovered.
