"""business-logic inference from a function dependency graph (figure: build graph ->
hierarchical clustering -> locate roots -> random-walk trajectories -> deduplicate by
cluster granularity -> infer business logic per trajectory).

the number of clusters K is the granularity knob: fewer clusters collapse more
trajectories onto the same cluster sequence, so fewer survive deduplication and each is
described more coarsely.
"""
