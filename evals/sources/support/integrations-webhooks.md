---
domain: support
title: Webhook delivery and retries
---

Webhooks are sent as signed HTTP POST requests. A delivery that does not receive a 2xx response is retried 5 times with exponential backoff, for up to 1 hour in total, and then marked failed. Each request carries an HMAC-SHA256 signature computed with the endpoint's signing secret, which customers should verify. Customers who restrict inbound traffic can allow the published webhook IP ranges.

Webhook endpoints must respond within 10 seconds, or the delivery counts as failed and is retried. Events are delivered at least once, so receivers must tolerate duplicates by checking the event identifier in the payload. After 20 consecutive failures an endpoint is disabled automatically and the account owner is emailed; re-enabling it is done from the integrations page.
