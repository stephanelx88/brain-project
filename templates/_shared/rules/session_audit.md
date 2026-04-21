Do not list every item, do not derail the user's task, do not nag on
subsequent turns. If they ignore it, drop it. If they say `audit` /
`show`, call `brain_audit(limit=10)` and walk through them one at a time.

If `brain doctor` flags the session-start hook as unwired, fall back
to calling `brain_audit(limit=3)` yourself on the first user message
and apply the same surface-once rule.
