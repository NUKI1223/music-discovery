"""Authored synthetic inputs; these tests do not claim live-site verification."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.catalog.web.common import ParseError
from discovery.catalog.web.rym import parse_rym

FIXTURES = Path(__file__).parent / "fixtures" / "rym"
URL = "https://rateyourmusic.com/release/album/fixture/copper-and-rain/"
STAMP = datetime(2026, 10, 28, tzinfo=UTC)


def parse(html):
    return parse_rym(html, url=URL, observed_at=STAMP)


def fixture(name="release.html"):
    return (FIXTURES / name).read_text()


def test_scoped_release_fields_ignore_friends_recommendations_reviews_and_scenes():
    result = parse(fixture())
    assert result.title == "[FIXTURE] Copper & Rain"
    assert result.artists == ["[FIXTURE] Artist A", "[FIXTURE] Artist B"]
    assert result.year == 2019
    assert result.genres == ["Abstract Hip Hop"]
    assert result.secondary_genres == ["Jazz Rap"]
    assert (result.rating, result.rating_scale, result.rating_count) == (3.75, 5, 1234)
    assert result.rating_count_text == "1,234"
    assert result.observed_at == STAMP
    assert len(result.html_sha256) == 64
    assert result.field_evidence["rating"].startswith("RYM Rating row")
    assert "not been verified" in result.warnings[0]


@pytest.mark.parametrize("text", ["1.2K", "1.2k", "1M", "about 1234", "12,34", "1234 ratings"])
def test_nonexact_counts_remain_unknown(text):
    result = parse(fixture().replace("1,234", text))
    assert result.rating_count is None
    assert result.rating_count_text == text
    assert any("exact integer" in warning for warning in result.warnings)


@pytest.mark.parametrize("text", ["1\u00a0234", "1\u202f234", "1 234", "1234"])
def test_exact_counts_allow_grouped_spaces(text):
    assert parse(fixture().replace("1,234", text)).rating_count == 1234


def test_missing_rating_count_does_not_use_reviews_or_friends():
    html = fixture().replace('<span class="num_ratings"><b>1,234</b> ratings</span>', "")
    assert parse(html).rating_count is None


def test_unrated_release_retains_explicit_zero():
    result = parse(fixture().replace("3.75", "—").replace("1,234", "0"))
    assert result.rating is None
    assert result.rating_count == 0


@pytest.mark.parametrize("score", ["0", "3.75"])
def test_mean_rating_with_zero_votes_is_rejected(score):
    with pytest.raises(ParseError) as exc:
        parse(fixture().replace("3.75", score).replace("1,234", "0"))
    assert exc.value.code == "INVALID_RATING"


def test_schema_mean_rating_with_zero_votes_is_rejected():
    with pytest.raises(ParseError) as exc:
        parse(fixture("schema.html").replace('"ratingCount":"2,345"', '"ratingCount":0'))
    assert exc.value.code == "INVALID_RATING"


@pytest.mark.parametrize("value", ["6", "-1", "nan", "inf", "bad"])
def test_invalid_rating_is_controlled_error(value):
    with pytest.raises(ParseError, match="rating"):
        parse(fixture().replace("3.75", value))


def test_invalid_scale_is_not_converted():
    with pytest.raises(ParseError) as exc:
        parse(fixture().replace("/ 5.0", "/ 10"))
    assert exc.value.code == "INVALID_RATING"


def test_schema_album_scopes_aggregate_and_ignores_website_rating():
    result = parse(fixture("schema.html"))
    assert result.title == "[FIXTURE] Structured release"
    assert result.artists == ["[FIXTURE] Ensemble"]
    assert result.year == 2004
    assert (result.rating, result.rating_count) == (4.2, 2345)
    assert result.genres == ["Experimental Hip Hop", "Industrial Hip Hop"]
    assert "MusicAlbum.aggregateRating" in result.field_evidence["rating"]


def test_schema_review_count_is_never_rating_count():
    result = parse(fixture("schema.html").replace('"ratingCount":"2,345",', ""))
    assert result.rating_count is None
    assert any("reviewCount" in warning for warning in result.warnings)


def test_schema_wrong_album_identity_rejected():
    html = fixture("schema.html").replace('"@type":"MusicAlbum",',
        '"@type":"MusicAlbum","url":"https://rateyourmusic.com/release/album/other/album/",')
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "IDENTITY_MISMATCH"


def test_page_canonical_wrong_identity_rejected():
    html = fixture().replace("<head>", '<head><link rel="canonical" '
        'href="https://rateyourmusic.com/release/album/some/other/">')
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "IDENTITY_MISMATCH"


def test_duplicate_rating_rows_are_ambiguous():
    html = fixture().replace("</table>", '<tr><th class="info_hdr">RYM Rating</th>'
        '<td><span class="avg_rating">1</span></td></tr></table>')
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "AMBIGUOUS_RELEASE"


def test_global_name_metadata_does_not_supply_missing_release_title():
    html = fixture().replace(
        '<h1 class="album_title"><span itemprop="name">[FIXTURE] Copper &amp; Rain</span></h1>',
        '<meta itemprop="name" content="Unrelated recommended artist">')
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "UNSUPPORTED_LAYOUT"


def test_duplicate_release_titles_are_ambiguous():
    html = fixture() + '<h1 class="album_title">Unrelated recommendation</h1>'
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "AMBIGUOUS_RELEASE"


def test_schema_multiple_albums_rejected():
    html = fixture("schema.html").replace('"@graph":[',
        '"@graph":[{"@type":"MusicAlbum","name":"Another"},')
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "AMBIGUOUS_RELEASE"


@pytest.mark.parametrize("html", ["<html><h1>Unknown layout</h1></html>",
    '<script type="application/ld+json">{"@type":"MusicGroup","name":"Artist",'
    '"aggregateRating":{"ratingValue":5,"ratingCount":123}}</script>',
    '<script type="application/ld+json">{broken</script>'])
def test_unsupported_or_artist_pages_are_not_releases(html):
    with pytest.raises(ParseError) as exc:
        parse(html)
    assert exc.value.code == "UNSUPPORTED_LAYOUT"


@pytest.mark.parametrize("html", ["<title>Access denied</title>",
    "<title>Just a moment...</title>", "<title>Log in - RYM</title>",
    '<form id="challenge-form"></form>'])
def test_blocked_inputs_stop_even_with_embedded_release(html):
    with pytest.raises(ParseError) as exc:
        parse(html + fixture())
    assert exc.value.code == "BLOCKED_PAGE"


def test_naive_timestamp_is_controlled_error():
    with pytest.raises(ParseError) as exc:
        parse_rym(fixture(), url=URL, observed_at=datetime(2026, 10, 28))
    assert exc.value.code == "INVALID_FIELDS"


@pytest.mark.parametrize("url", ["https://rateyourmusic.com/artist/example/",
    "https://rateyourmusic.com/charts/top/album/all-time/",
    "https://evil.example/release/album/artist/title/"])
def test_source_url_must_be_release_page(url):
    with pytest.raises(ParseError) as exc:
        parse_rym(fixture(), url=url, observed_at=STAMP)
    assert exc.value.code == "INVALID_URL"
