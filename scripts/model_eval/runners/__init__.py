"""One module per kind of model. Each takes a declared candidate, measures it through one path, and returns a `RunOutput`
(per-item data + efficiency) for report.py. A runner is a function of a *backend*, so its logic is tested with a fake backend and no model."""
