"""core.diagnostics — the "why" path: evidence-gathering behind a metric movement.

Nothing here is wired into the request pipeline yet. `driver_graph.yml` is the
first artifact: a curated, verified map of what mechanically moves each metric and
which dimensions can decompose it. See CLAUDE.md and
`gateway/audit_dimension_coverage.py` for how the dimension lists were verified.
"""
