# Security policy

## Supported versions

The v0.1.x release line receives security fixes.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Do not include credentials, tokens,
listening history, calendar or email content, exports, databases, personal data, or
machine-specific paths in a report.

Use this repository's private GitHub security advisory feature to report it. If private security
advisories are unavailable, wait for a maintainer-approved private reporting method rather than
posting details publicly.

## Credential exposure

If a provider credential may have been exposed, revoke it with the provider first. Remove the
local credential through the operating-system credential store, then authorize the connection
again. Deleting a file or Git commit is not a substitute for revocation.
