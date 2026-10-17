---
domain: ops
title: Writing good alerts
---

Every alert that pages a human must be actionable, must link to a runbook and must describe a symptom customers feel, not a cause. Alerts that fire without needing action are noisy and are fixed or deleted. The team reviews the ten noisiest alerts every month. A new alert is first sent to a chat channel for two weeks before it is allowed to page anyone.

Dashboards must show the same signals the alerts use, so an engineer paged at night can see what changed. Alert thresholds are set from service-level objectives rather than guesses: a page is raised when the error budget would be exhausted within 6 hours at the current burn rate. Silences for planned maintenance must have an end time and an owner, and silences without an end time are removed automatically after 24 hours.
