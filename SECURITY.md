# Security policy

## Supported versions

The `0.9.x` line is a pre-release. Security fixes are handled on the latest
reviewed commit; older commits are not promised support.

## Report a vulnerability

Do not open a public issue for a suspected credential leak, authentication
bypass, cross-tenant data exposure, browser profile escape, or confirmation-gate
bypass. Send a private report to the repository owner through the private
channel configured for the final publication. If no private channel has been
published yet, do not include exploit details in a public issue; ask the owner
to configure one first.

Include:

- affected commit or version;
- exact reproduction steps without real credentials;
- impact and affected execution mode;
- redacted logs or a minimal fixture;
- whether the issue is still reproducible on the current main branch.

Never attach cookies, OAuth tokens, passwords, browser profiles, or unredacted
screenshots. Revoke any credential that was accidentally exposed before
reporting the incident.

## Security boundaries

The repository is not a hosted multi-tenant service. The local browser path is
an operator-controlled integration and must not be exposed directly to the
public Internet. Credentials stay in the host environment or owner profile;
they are not valid release assets.
