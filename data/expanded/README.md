# Expanded MusicBrainz collection

Source: **MusicBrainz and its contributors**, https://musicbrainz.org/.
Core metadata is CC0. Community tags, genre associations, ratings and search
indexes are supplementary data under **CC BY-NC-SA 3.0**. The raw API cache,
tagged normalized dataset and derived reports retain that attribution and
dataset license. Application code is separate.

- [Official API](https://musicbrainz.org/doc/MusicBrainz_API)
- [Data license](https://musicbrainz.org/doc/About/Data_License)
- [Core/supplementary field definitions](https://musicbrainz.org/doc/MusicBrainz_Database)
- [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/)

`musicbrainz-config.json` declares the bounded sampling plan: five seed artists
(resolved by exact canonical names or aliases) plus eight subgenre query strata,
each split into pre-2010, 2010–2019 and 2020–2026 dates. Artist collections are
kept first; remaining candidates are selected round-robin across the query
strata. No ranking by community rating or popularity is performed at acquisition.
Search relevance and incomplete community tagging still introduce selection
bias: this is not a representative sample of all music or all hip-hop.

Run or resume the explicitly network-enabled collector:

```sh
uv run python -m discovery.catalog.collect_musicbrainz \
  --config data/expanded/musicbrainz-config.json
```

Calls are serialized with at least 1.1 seconds between request starts; transient
HTTP errors receive bounded retries. Run only one MusicBrainz importer process.
The collector locks its workspace, and each successful response is saved before
continuing. Resuming reuses immutable cache records and keeps original capture
times; a resumed request is not relabelled as fresh data.

`musicbrainz/raw/` stores URL, parameters, capture timestamp and the unmodified
JSON response in a wrapper. `musicbrainz/state.json` gives live progress and
errors. A complete run publishes a normalized catalogue, import report,
source manifest and audit report. The old 33-release sample is not modified.

Offline reconstruction fails on a cache miss and never silently accesses the
network:

```sh
uv run python -m discovery.catalog.collect_musicbrainz \
  --config data/expanded/musicbrainz-config.json --offline
```

For a deliberate later refresh use a new workspace/cache path in a copied
configuration; keep historical caches for reproducible decisions.

Only explicit release-group lookup `rating.value` and `rating.votes-count` are
imported as rating signals. Search scores and edition counts are never ratings.
An explicit zero vote count stays zero; a missing count stays null, and a missing
average does not become a zero-star rating. The query's genre is not assigned to
a release: only positive provider tags/genre assertions are mapped. Lookup tags
and search tags are retained as evidence, including unmapped tags.

The tag `trap` is ambiguous between hip-hop and electronic dance music. It is
mapped into hip-hop only when the same release has another mapped hip-hop tag.
Otherwise the raw tag remains in evidence with a withholding reason and does
not qualify the release for hip-hop recommendations. Mapping rules are versioned
as `mb-tags-v2`; this does not retroactively alter the original sample.

These are catalogue metadata and platform aggregate ratings, **not personal
training labels**. No user liking/disliking is fabricated. Product G0 can remain
limited despite a working expanded catalogue, particularly because independent
genre review and sufficient popularity coverage in every comparison group are
separate requirements.
