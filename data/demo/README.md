# Synthetic demo

Every artist, release, score and count in `catalog.json` is invented. These are
offline fixtures for interaction, genre boundaries and ranking tests, not musical
recommendations. The `[DEMO]` names, `synthetic-demo` source and `is_fixture=true`
flag must remain visible. Product G0 cannot pass on these records.

There are 336 releases across hip-hop and jazz, including jazz-rap with two parents
and occasional R&B crossover tags. Observation dates are fixed for reproducibility;
after 90 days strict rarity may correctly stop finding suitable observations.

Regenerate locally with `make_demo_catalog()` from `discovery.catalog.demo` and
`save_catalog()` from `discovery.catalog`. No network or API key is required.
