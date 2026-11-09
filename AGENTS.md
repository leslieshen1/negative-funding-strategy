# Maintenance contract

Read README.md and docs/STATUS.md before changing a release. CURRENT_RELEASE.json identifies the accepted baseline and its restrictions.

Preserve frozen formulas, model bytes, feature ordering, event/receive clocks, quantity units and evidence. Put proposed changes in a new version with an explicit comparison; keep the current baseline reproducible. Treat example accounts as synthetic and historical replays as research.

Run scripts/verify_release.py and the documented focused checks before claiming a handoff works. Keep credentials, real account states, private inventories and live SQLite ledgers outside Git. Public-data acquisition obeys provider cooldowns and access restrictions. Source recovery never creates backdated forecasts.

Strategy outputs are decisions; execution receipts are facts. Keep submission_allowed=false until an explicitly authorized production acceptance changes the version. Orders, borrowing, cancellations, leg repair and transfers belong to the execution team.
