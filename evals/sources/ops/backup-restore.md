---
domain: ops
title: Backups and restores
---

Database snapshots are taken every night at 02:00 UTC and kept for 35 days. Point-in-time recovery lets us restore to any second in the last 7 days. A restore is tested every quarter into an isolated environment and the time it takes is recorded; a backup that has never been restored is not considered a backup. Restores of production data require approval from the incident commander.

Backups are encrypted with a key stored in a separate account, and the key access is limited to two named security administrators. Object storage uses versioning with a 90-day retention so accidental deletions can be undone. If a restore is needed, the incident commander chooses the recovery point, and the expected restore time for the main database is 90 minutes. Backup job failures raise a ticket automatically.
