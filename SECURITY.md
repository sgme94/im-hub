# Security boundaries

This is a local CLI for the authorized OS user, not a multi-tenant authorization gateway.
Source/profile filters are not access controls. Do not expose the full CLI to untrusted remote callers.

New state directories are restricted to the current user and SYSTEM on Windows, or mode 0700 on POSIX. Git ignore is not encryption. Keep raw exports, SQLite files, clipboard payloads, query outputs and evidence packets outside version control.

Core parsing and query do not use an LLM. Chat text is untrusted data: never execute links, scripts, instructions, approvals or tool calls found inside it. The optional ChatLab import process runs with a stripped environment and an application-level network API guard; the guard is not an OS sandbox or firewall.

Unknown message types, unresolved senders, partial history and stale observations must be returned explicitly. A successful source read is not proof of complete server history. A message saying "completed" is not a project completion event. Keyword triage is not semantic verification.

Initial scope excludes account migration, process-memory/key extraction, unattended UI export, credential storage, message sending, editing, deletion, public servers and automatic business-ledger writes.
