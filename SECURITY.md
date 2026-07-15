# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

- Preferred: open a private report through GitHub Security Advisories - the
  **"Report a vulnerability"** button on this repository's **Security** tab.
- Alternatively: email <enterprise.code.developer@gmail.com>.

Please include a description of the issue, and steps to reproduce or a proof of concept.
We aim to acknowledge reports within a few business days and will keep you updated on remediation.

## Scope

This policy covers the published `agent-skill-description-optimizer` package and its
release supply chain: the source in this repository, the distributed sdist/wheel, and the
GitHub Actions workflows under `.github/` that build, sign, and publish releases. The tool
shells out to the `claude` CLI and requires no API key; it stores no secrets. Reports about
the automation itself (workflow token scope, SHA-pinning, OIDC publishing) are in scope.

## Supported versions

Only the latest released version is supported. Fixes ship as new releases rather than as
patches to older versions.
