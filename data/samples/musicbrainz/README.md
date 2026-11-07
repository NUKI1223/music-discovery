# MusicBrainz API sample and attribution

Data supplied by **MusicBrainz and its contributors**, https://musicbrainz.org/.
Retrieved serially on 2026-10-28 with an identifying User-Agent and less than one
request per second. The metadata was obtained from the official API, not page scraping.

Core entity metadata is [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
Supplementary tags, genre associations, ratings and search data are
[CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/).
This tagged sample and its derived import/audit reports are supplied under
CC BY-NC-SA 3.0 with attribution to MusicBrainz contributors. These data terms do
not set the license of application code. See the official
[data license](https://musicbrainz.org/doc/About/Data_License) and
[field classification](https://musicbrainz.org/doc/MusicBrainz_Database).

`raw/` retains exact JSON responses. `../musicbrainz.json` is the normalized
catalogue; `../musicbrainz.import-report.json` records alias-based genre mappings,
unmapped source tags, duplicates and skipped records. The source URL of each
accepted release is preserved. Artist genres are not inferred from release tags.

The sample has 33 canonical release groups and 28 credited artists. It is biased
toward Death Grips and a single abstract-hip-hop search page. It is **not** a
representative catalogue or evidence of musical usefulness. Source tags are not
independently adjudicated genre labels. Some artist credits are collaborations.

Only The Money Store has an aggregate rating lookup: the API response contained
4.4/5 from 18 votes when retrieved. Search `score` is search relevance and search
`count` is not a vote count; neither is imported as a musical rating. Other vote
counts remain null. No release in this sample has a supported rarity comparison
cohort, so strict rarity must return no recommendations.

Reproduce normalization offline:

```sh
uv run python -m discovery.catalog.musicbrainz normalize \
  --input data/samples/musicbrainz/raw/death-grips-release-groups.json data/samples/musicbrainz/raw/abstract-release-groups.json \
  --details data/samples/musicbrainz/raw/details.json \
  --observed-at 2026-10-28T11:46:25+00:00 \
  --output data/samples/musicbrainz.json
uv run python -m discovery.catalog.audit \
  --manifest data/manifests/musicbrainz.json --as-of 2026-10-28
```

Live acquisition is an explicit optional command, never part of loading a catalogue:

```sh
uv run python -m discovery.catalog.musicbrainz fetch \
  --query 'tag:"abstract hip hop" AND primarytype:album' \
  --limit 25 --offset 0 --output data/new-source-page.json
```

Run only one importer process at a time. The client serializes calls across its
instances in one process; multiple processes do not share its limiter. Refresh
uses new raw files and a new snapshot; it must not silently change historical
decisions. Existing-ID conflicts from `merge_catalogs` are reported for review.
