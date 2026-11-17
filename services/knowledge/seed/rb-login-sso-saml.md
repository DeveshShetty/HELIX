---
id: rb-login-sso-saml
type: runbook
title: "SSO login fails with 'SAML assertion invalid'"
tags: login, sso, saml, identity provider, okta, azure ad, assertion
---
Symptoms: users signing in with single sign-on see "SAML assertion invalid" or are sent back
to the login page.

Steps:
1. In Admin > Security > SSO, check that the Entity ID and ACS URL in your identity provider
   exactly match the values shown on that page.
2. Check that the identity provider's signing certificate uploaded to Helix Cloud has not expired.
3. Make sure the assertion includes the user's email address as the NameID.
4. Admins can always sign in with a password at /login?sso=off to fix the configuration.
