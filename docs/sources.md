# Source status and catalogue evidence

Checked on 2026-10-28. Application code and the data adapters are multi-genre;
the first real sample is a hip-hop pilot. `is_fixture` is never inferred from
file location and synthetic data cannot satisfy product G0.

| Source | Actual integration | Fields and access |
|---|---|---|
| MusicBrainz | Expanded catalogue: 520 releases / 409 artists, serial cached acquisition, offline replay | 463 hip-hop releases, eight subgenres, 109 aggregate ratings, 411 explicit zero vote counts. Source metadata, not personal labels. |
| Wikidata | 10 exact release-group and artist matches; five with genre features | CC0 source claims and revision IDs; separate features, no automatic taxonomy merge or popularity inference. |
| AOTY | 6,703 historical rows from a public GitHub workbook; one diagnostic cached API response | Historical workbook included in the [unified catalogue](unified-catalog.md) with pending identity cases excluded. Unknown capture time/license; counts are unverified historical metrics. API response remains separate. |
| RYM | 5,000 historical rows from a public GitHub CSV | Included by conservative identity rules in the [unified catalogue](unified-catalog.md), with source fields and separate historical ratings retained. Popular-chart bias; identity links are heuristic, not independent verification. |
| ListenBrainz | Listener counts per MusicBrainz release group, stored as a dated overlay next to the catalogue (added 2026-11-10) | Public API without a key; listen statistics are CC0. Counts are ListenBrainz users, not all listeners. Release-group IDs come from exact MusicBrainz title/artist/year search. See [listener popularity](listenbrainz-popularity.md). |
| Last.fm | Research candidate only | [album.getInfo](https://www.last.fm/api/show/album.getInfo) documents listeners/playcount; requires own key and review of [API terms](https://www.last.fm/api/tos). |
| Synthetic demo | Offline interface/test catalogue | 336 invented releases, 336 invented artists, two main genre scopes and R&B crossover; fake ratings are explicitly identified. |

## MusicBrainz access and attribution

The [official API](https://musicbrainz.org/doc/MusicBrainz_API) is free for
noncommercial use, requires a meaningful User-Agent and at most one call/second.
The adapter uses 1.1-second start intervals, bounded retries, an HTTPS API base
and explicit CLI acquisition. No network request occurs in `load_catalog()`.

The [license](https://musicbrainz.org/doc/About/Data_License) and
[database field classification](https://musicbrainz.org/doc/MusicBrainz_Database)
were read successfully from the official pages. Core data is CC0. **Genre
associations, user tags, ratings and search indexes are supplementary data under
CC BY-NC-SA 3.0**, so they must not be described as unrestricted CC0. Attribution
and the dataset-specific terms are retained in
`data/samples/musicbrainz/README.md`, the manifest and every observation.

Raw search `score` measures search relevance, and result/edition `count` is not
user votes. The adapter deliberately ignores these when forming observations.
Only explicit lookup `rating.value` and `rating.votes-count` are accepted;
aggregate API ratings use the five-star scale (see the official
[API examples](https://musicbrainz.org/doc/MusicBrainz_API/Examples)). No personal
user histories or reviews were collected.

## Expanded collection

`data/catalogs/musicbrainz-expanded.json` is the unchanged MusicBrainz base. The app now defaults to `data/unified/community-v1/catalog.json`: 9,903 releases, 408 genre nodes, and separate historical RYM/AOTY observations. Source capture times for MusicBrainz are preserved per response. All five configured favourite artists are represented; explicit aliases resolve Kanye West to Ye. The independent source sidecar is `data/wikidata/expanded/dataset.json`; the earlier MB/Wikidata metadata export lives in `data/training/musicbrainz-wikidata-expanded/`.

Mapping `mb-tags-v2` withholds ambiguous `trap` tags without supporting hip-hop evidence. 37 such tags are withheld in this snapshot. They are not silently converted to hip-hop membership. Search-query labels never become genre labels. The eight supported families have 45–101 releases; genre assertions have not undergone independent listening-based review.

G0 remains limited. Counts are explicitly available for every release, but 411 are zero; the conservative percentile rule finds no strict-rarity candidates. These are local platform counts, not evidence of global obscurity. [Acquisition report](data-update.md), [Wikidata evidence](additional-dataset.md), [fresh RYM/AOTY access investigation](rym-aoty-access.md).

## Original sample and limitations (retained)

`data/samples/musicbrainz.json` contains **33 canonical release groups and 28
credited artists** from two saved search responses: one favourite artist and
one abstract-hip-hop query. One aggregate rating/count was looked up separately.
Search pages, exact raw tags, skipped release types, mapping decisions and
capture details remain in the manifest/import report. The sample does not
represent all hip-hop or the long tail. Search ordering, chosen artist and
query selection produce substantial selection bias.

Raw-to-normalized checks cover IDs, titles, artist credits and explicit tag
mapping; they are not independent listening-based verification of genre. No
release is marked independently reviewed yet. Missing genre tags are retained
as missing. A release with several genres keeps a single canonical ID and is
counted only once within each cohort. Artists do not inherit genre assertions
from albums. Editions do not become separate recommended items.

## Original sample G0 status: limited

Run `uv run python -m discovery.catalog.audit --manifest
data/manifests/musicbrainz.json --as-of 2026-10-28` for the actual report. The
report breaks down scopes, family coverage, source and age cohorts, usable
observations, provenance and rare candidates. Missing, future and stale counts
do not satisfy availability. Equal-count populations do not make every item
rare. Sources' counts are never summed.

The real sample fails the pilot size, genre-review, family and popularity gates.
No strict-rarity comparison population has 30 usable observations. This is a
**working metadata sample**, not a completed low-popularity product. The
synthetic demo stays `limited` regardless of apparent coverage.

Product acceptance requires a larger and more varied permissible catalogue,
independent genre checks, source-specific popularity coverage and sufficient
rare candidates in each advertised group. Requirements are configured per
genre in manifests; a new genre does not inherit hip-hop's acceptance claims.
The audit conservatively requires each reported group to pass, without using
broader ranking fallback to hide thin coverage. No source-contact messages
have been sent. Historical third-party RYM/AOTY files have been acquired; successful direct collection from the original sites is not claimed.
