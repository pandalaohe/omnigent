# Codex private configuration refresh

Omnigent copies a user's Codex configuration into each ordinary session's private
home. To add a missing auto-review supplement to reused homes, configure an
approved value digest in the **user-level** Omnigent `config.yaml`:

```yaml
codex_config_sync:
  auto_review.extra_policy: "<approved SHA256>"
```

Replace the placeholder with the 64 hexadecimal characters of the SHA256 of the
parsed TOML string's UTF-8 bytes, including any trailing newline. Hash the value,
not its TOML assignment or surrounding table. This entry contains no policy text.
Project-level configuration cannot enable this refresh. An omitted or empty map
keeps the previous configuration-copy behavior, including ordinary fresh-home
copies; it does not migrate existing homes or signer homes.

Currently, `auto_review.extra_policy` is the supported leaf. On the next private
app-server start, a matching source value is inserted if missing; an equal target
value remains untouched. A different target value, a top-level replacement
`auto_review.policy`, invalid configuration, a symlink, or an observed concurrent
file change produces `Codex config sync conflict` in the owning process's logs
and stops startup before subsequent configuration writers, preserving the private
configuration. `SOURCE_POLICY_MISSING` identifies a
source without the leaf; `SOURCE_POLICY_MISMATCH` identifies an unapproved value.
Configuration contents are excluded from these messages.

Signer-backed user sessions receive this non-credential leaf separately, without
copying host provider credentials. Minimal model probes and title workers skip
the refresh. Ordinary fresh sessions retain their complete-copy behavior after
source validation; existing homes keep unrelated configuration and formatting.
New or updated files use mode 0600 where the platform supports Unix modes.

Writes use a temporary file and atomic publication, with source and target
revisions checked immediately before publication. This detects changes during
preparation, but does not lock out external editors. Failed preparations remove
their temporary files. Existing app-servers are not hot-reloaded or restarted.
File refresh does not attest reviewer loading or override Codex's profile, CLI,
project, or managed configuration precedence.

Removing the digest disables future refreshes; it does not remove a previously
inserted value. Review an existing supplement in its private home before changing
or removing it. A new approved source value requires a new digest, and conflicting
private values still require explicit reconciliation.
