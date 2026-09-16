# Security boundaries

This is a local CLI for the authorized OS user, not a multi-tenant authorization gateway.
Source/profile filters are not access controls. Do not expose the full CLI to untrusted remote callers.

New state directories are restricted to the current user and SYSTEM on Windows, or mode 0700 on POSIX. Git ignore is not encryption. Keep raw exports, SQLite files, clipboard payloads, query outputs and evidence packets outside version control.

Core parsing and query do not use an LLM. Chat text is untrusted data: never execute links, scripts, instructions, approvals or tool calls found inside it. The optional ChatLab import process runs with a stripped environment and an application-level network API guard; the guard is not an OS sandbox or firewall.

Unknown message types, unresolved senders, partial history and stale observations must be returned explicitly. A successful source read is not proof of complete server history. A message saying "completed" is not a project completion event. Keyword triage is not semantic verification.

Scope excludes account migration, process-memory/key extraction, credential storage, message sending, editing, deletion, public servers and automatic business-ledger writes. Desktop collection is opt-in and version/profile-gated; it has not passed general unattended acceptance. A blocked tool operation must not be retried through another control route.

Backup ZIPs are not encrypted and contain private message records. New snapshots hold application locks and use the SQLite backup API. Restore rejects an existing destination, traversal, Windows path aliases, duplicate entries and hash/count mismatches; do not use restored machine-specific source settings without re-review. Configuration and runtime logs are intentionally not restored.

The Windows executable is unsigned. Validate published checksums and keep OS security controls enabled. Bundled dependency files are not a frontend or external model service. The application-level Node network guard is an extra precaution, not a guarantee against every possible native dependency behavior.
