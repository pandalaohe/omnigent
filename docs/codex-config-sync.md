# Codex private configuration refresh

Omnigent copies a user's Codex configuration into each ordinary session's private
home. Before each private app-server start, permission defaults
(`approval_policy`, `sandbox_mode`, `approvals_reviewer`, `default_permissions`)
mirror the main config, including removal of keys absent there. Explicit session
launch arguments still override these defaults. Minimal probes and title workers
skip this refresh. Existing app-servers are not hot-reloaded or restarted.

To add a missing auto-review supplement to reused homes, configure an
approved value digest in the **user-level** Omnigent `config.yaml`:

```yaml
codex_config_sync:
  auto_review.extra_policy: "<approved SHA256>"
```

Replace the placeholder with the 64 hexadecimal characters of the SHA256 of the
parsed TOML string's UTF-8 bytes, including any trailing newline. Hash the value,
not its TOML assignment or surrounding table. This entry contains no policy text.
Project-level configuration cannot enable this refresh. An omitted or empty map
disables the optional policy refresh; permission defaults still refresh in
ordinary and signer-backed user homes.

For digest-pinned refresh, `auto_review.extra_policy` is the supported leaf. On
the next private app-server start, a matching source value is inserted if missing; an equal target
value remains untouched. A different target value, a top-level replacement
`auto_review.policy`, or invalid policy pins produce `Codex config sync conflict`
and skip the optional policy refresh while permission defaults still refresh.
A missing main config clears the four copied permission keys. Unsafe files,
parse failures, or observed concurrent changes skip the entire publication.
The existing startup configuration steps continue, including lane
role defaults, Windows sandbox refresh, and generated provider configuration.
`SOURCE_POLICY_MISSING` identifies a
source without the leaf; `SOURCE_POLICY_MISMATCH` identifies an unapproved value.
Configuration contents are excluded from these messages.

Signer-backed user sessions receive this non-credential leaf separately, without
copying host provider credentials. Minimal model probes and title workers skip
the refresh. Ordinary fresh sessions retain their pre-existing complete-copy
behavior, including after an optional sync conflict. A copied source value is
part of that initial configuration, not an incremental policy migration. Existing
homes keep their policy values while permission defaults and the original startup
writers update their own settings. Source deletion or edits do not introduce a sync startup gate.
New or updated files use mode 0600 where the platform supports Unix modes.

Writes use a temporary file and atomic publication, with source and target
revisions checked immediately before publication. This detects changes during
preparation, but does not lock out external editors. Failed preparations remove
their temporary files. After a detected concurrent change, subsequent startup
writers read the current configuration as before. An unparseable target retains
the existing writers' parse-error behavior; sync adds no startup exception.
File refresh does not attest reviewer loading or override Codex's profile, CLI,
project, or managed configuration precedence.

Removing the digest disables future policy refreshes; it does not remove a previously
inserted value. Review an existing supplement in its private home before changing
or removing it. A new approved source value requires a new digest, and conflicting
private values still require explicit reconciliation.
