---
domain: ops
title: Capacity planning
---

Add capacity when average CPU stays above 70 percent for 10 minutes. The platform keeps 30 percent headroom above peak traffic so one availability zone can be lost without an outage. Capacity is reviewed every quarter against the traffic forecast and the cost report. Memory-bound services are scaled on memory, not CPU, and databases are scaled vertically before they are sharded.

Load tests are run before each major launch at 2 times the expected peak, and the results are attached to the launch checklist. Storage is forecast from the last 90 days of growth and extended when less than 25 percent of free space remains. Cost reviews flag any service whose monthly spend grew more than 20 percent without matching traffic growth, and the owning team explains it in the next review.
