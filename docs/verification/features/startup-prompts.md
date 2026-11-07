# Starting prompts verification

Use [the feature recipe](../../../feature-map/startup-prompts.md) for entry
points, setup, actions and expected results. It covers session text, owner
versions, restoration, reconnect and ACP agents that build their own prompt.

Run Python checks through the project's isolated fork check wrapper. Run web
component checks under Node 24. Record command, checkout/diff, exit and log.
The database migration also needs the release's database lanes; SQLite alone
does not prove MySQL or PostgreSQL behavior.
