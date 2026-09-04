# Security and privacy

Music Friend stores its working catalog locally. It has no product-operated account, hosted catalog,
or telemetry service. Provider credentials stay outside the catalog and are never accepted through
MCP inputs.

Use an approved native operating-system credential store when available. The passphrase-protected
vault is for interactive CLI use only and cannot be used by MCP or scheduled refreshes. If a
provider credential appears in a terminal capture, export, backup, issue, or source change, revoke
it with the provider, remove the local connection, and reconnect.

Provider metadata and imported data are untrusted input. Music Friend validates local import data,
redacts operational errors, and limits MCP changes to the local catalog and inbox. It has no network-reachable inbound service; it also has no managed sign-in service or ticket-purchase capability.
Spotify authorization uses a temporary 127.0.0.1 OAuth callback during an interactive local
connection; this is not a network-reachable product service.

Exports and backups can contain personal catalog information even though they exclude credentials.
Store them only where you control access. Before sharing diagnostics, review the output. See
[SECURITY.md](../SECURITY.md) for vulnerability reporting.
