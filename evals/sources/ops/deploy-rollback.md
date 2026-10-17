---
domain: ops
title: Deploys and rollbacks
---

Every deploy first goes to a canary receiving 5 percent of traffic for 15 minutes. If the error rate in the canary rises above the baseline the deploy stops automatically. To roll back a bad release run deployctl rollback to the previous version, which takes about 2 minutes. When a service has used 10 percent of its monthly error budget, deploys to it are frozen except for reliability fixes.

Database schema changes must be backwards compatible so a rolled-back release still works with the new schema; destructive changes such as dropping a column are done in a separate release at least one week later. Feature flags are used to switch risky code on gradually, and a flag older than 90 days must be removed. Deploys are not allowed on Fridays after 15:00 unless they fix an active incident.
