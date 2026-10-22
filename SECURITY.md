# Security policy

## Supported versions

This project is experimental. Security fixes are best-effort and may not be backported to every historical snapshot.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Contact the maintainer privately through the GitHub profile associated with this repository and include:

- a short impact summary;
- affected commit/version;
- a synthetic reproduction or minimal proof of concept;
- exact environment and launch mode;
- whether local evidence, credentials, or network access were involved.

Never send:

- source evidence;
- database files;
- auth tokens;
- API keys;
- private screenshots;
- unredacted logs.

If private GitHub reporting is enabled for this repository, use the repository’s Security Advisories flow. Otherwise request a private contact channel before sharing sensitive details.

## High-risk deployment warnings

Do not:

- expose the local backend to a LAN;
- run local-forensics mode as a shared service;
- commit `.env` or `.env.local`;
- ship Supabase service-role keys;
- embed Gemini keys in desktop builds;
- upload evidence to an issue or pull request;
- treat the local hash-linked event log as a tamper-proof legal chain of custody.

## Secret response

If a credential is accidentally exposed:

1. revoke/rotate it immediately at the provider;
2. remove it from the working tree and public artifacts;
3. inspect Git history and release bundles;
4. invalidate sessions/tokens where applicable;
5. document the incident without publishing the secret.
