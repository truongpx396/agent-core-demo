---
domain: ops
title: Incident severity levels
---

A severity 1 incident is an outage or a problem that stops customers from paying or using the core product. The on-call engineer is paged immediately, an incident commander is named within 10 minutes, and the status page is updated every 30 minutes until resolution. Severity 2 is a major degradation with a workaround, handled during working hours within 4 hours. Severity 3 is a minor defect that goes into the normal backlog.

During a severity 1 incident one person acts as incident commander and does not debug; another engineer is named as communications lead and writes the status updates. Customers affected by a severity 1 incident receive an email within 60 minutes. The incident channel is created from a template, and everything said in it is treated as the record for the later postmortem. An incident is declared resolved only after 30 minutes of stable metrics.
