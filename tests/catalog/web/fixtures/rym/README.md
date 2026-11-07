# RYM authored test fixtures

Every HTML fixture here is authored synthetic markup. Titles, artists and ratings
are invented. These are not saved RYM pages and do not prove compatibility with
the currently deployed RYM website.

The ordinary primary release URL was checked on 2026-10-28:
https://rateyourmusic.com/release/album/death-grips/the-money-store/
The browser retrieval was denied by robots.txt. No alternate access was attempted.

Selector evidence comes from the original source of these browser tools, not
from independently retrieved RYM HTML:

- https://greasyfork.org/en/scripts/583372-modern-rym-release-page/code
  (author's MIT-licensed userscript): album information rows, scoped RYM rating,
  primary and secondary genre links.
- https://gist.github.com/FOSSforlife/e71c8ddcba4d70830b2805d9ce3dbdb0
  (author's clipper template): artist, itemprop name, issue year selectors.
- https://schema.org/MusicAlbum and https://schema.org/AggregateRating:
  optional structured-data semantics; ratingCount differs from reviewCount.

The parser uses an independently written implementation. JSON-LD fixtures test
the documented schema format, not a claim that RYM currently emits JSON-LD.
