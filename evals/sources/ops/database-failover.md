---
domain: ops
title: Database failover runbook
---

To fail over the primary database, first confirm the replica's replication lag is under 10 seconds, then promote it with the promote command and wait for it to accept writes. Point the application at the new primary by updating the DNS record, whose TTL is kept at 60 seconds for this reason. The target recovery time is 5 minutes. Afterwards rebuild the old primary as a replica rather than bringing it back as a primary.

Before a planned failover, announce it in the operations channel at least 30 minutes ahead and pause long-running batch jobs, because they would be cut off when connections move. After the switch, verify that application error rates are normal, that replication to the new replicas has started, and that the connection pool has reconnected. Failover drills are run twice a year in a staging copy of production.
