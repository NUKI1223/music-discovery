"""Offline tests using authored synthetic HTML; no real AOTY album was downloaded."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from discovery.catalog.web.aoty import parse_aoty
from discovery.catalog.web.common import ParseError, html_digest

URL = "https://www.albumoftheyear.org/album/987654-synthetic-artist-test-release.php"
NOW = datetime(2026, 10, 28, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "aoty_synthetic_album.html"


def parse(html=None, **kwargs):
    return parse_aoty(html if html is not None else FIXTURE.read_text(), url=URL,
                      observed_at=NOW, **kwargs)


def test_parses_user_and_critic_aggregates_separately_with_evidence():
    result = parse()
    assert result.source == "aoty"
    assert result.source_id == "987654"
    assert result.title == "Test & Signal"
    assert result.artists == ["Synthetic Artist"]
    assert result.year == 2023
    assert result.genres == ["Experimental Hip Hop"]
    assert result.secondary_genres == ["Abstract Hip Hop"]
    assert result.rating == 88.45
    assert result.rating_scale == 100
    assert result.rating_count == 1234
    assert result.rating_count_text == "Based on 1,234 ratings"
    assert result.critic_rating == 91.25
    assert result.critic_rating_scale == 100
    assert result.critic_rating_count == 17
    assert result.html_sha256 == html_digest(FIXTURE.read_text())
    assert result.observed_at == NOW
    assert "albumUserScoreBox" in result.field_evidence["rating"]
    assert "LIVE_DOM_COMPATIBILITY_UNVERIFIED" in result.warnings


@pytest.mark.parametrize("count", ["1.2K", "12K", "1M", "1,234+", "-5", "−5", "1/2", "about 1234", "1.234", "12,34"])
def test_abbreviated_approximate_or_malformed_counts_remain_unknown(count):
    result = parse(FIXTURE.read_text().replace("1,234 ratings", f"{count} ratings"))
    assert result.rating_count is None
    assert count in result.rating_count_text
    assert result.rating == 88.45


@pytest.mark.parametrize("text,expected", [("0", 0), ("15", 15), ("1 234", 1234), ("1\u202f234", 1234)])
def test_exact_counts_include_zero_and_grouped_whitespace(text, expected):
    result = parse(FIXTURE.read_text().replace("1,234 ratings", f"{text} ratings"))
    assert result.rating_count == expected
    if expected == 0:
        assert result.rating is None
        assert "USER_SCORE_WITH_ZERO_COUNT_IGNORED" in result.warnings
        assert "rating" not in result.field_evidence


def test_critic_score_with_zero_reviews_is_unknown_without_changing_user_score():
    result = parse(FIXTURE.read_text().replace("17 reviews", "0 reviews"))
    assert result.critic_rating_count == 0
    assert result.critic_rating is None
    assert result.rating == 88.45
    assert "CRITIC_SCORE_WITH_ZERO_COUNT_IGNORED" in result.warnings
    assert "critic_rating" not in result.field_evidence


@pytest.mark.parametrize("heading", [
    '<h1 class="albumTitle">Another Album</h1>',
    '<div class="artistTitle"><a href="/artist/1-other/">Other Artist</a></div>',
])
def test_dom_header_conflicts_with_jsonld_are_rejected(heading):
    with pytest.raises(ParseError) as error:
        parse(FIXTURE.read_text().replace("<body>", "<body>"+heading))
    assert error.value.code == "IDENTITY_CONFLICT"


def test_matching_dom_headers_and_unrelated_recommendation_cards():
    header = ('<h1 class="albumTitle"> Test &amp; Signal </h1>'
              '<div class="artistTitle"><a href="/artist/1-test/">Synthetic Artist</a></div>')
    recommendation = ('<div class="albumBlock"><div class="albumTitle">Other Album</div>'
                      '<div class="artistTitle">Another Artist</div></div>')
    result = parse(FIXTURE.read_text().replace("<body>", "<body>"+header+recommendation))
    assert result.rating == 88.45
    assert "checked page .albumTitle" in result.field_evidence["title"]
    assert "checked page .artistTitle" in result.field_evidence["artists"]


def test_user_review_count_is_not_a_rating_count():
    result = parse(FIXTURE.read_text().replace("1,234 ratings", "1,234 reviews"))
    assert result.rating_count is None
    assert "USER_COUNT_SEMANTICS_UNVERIFIED" in result.warnings
    assert result.critic_rating_count == 17


def test_generic_jsonld_aggregate_is_not_used_when_user_score_is_missing():
    html = FIXTURE.read_text().replace('class="albumUserScoreBox"', 'class="unrelated"')
    result = parse(html)
    assert result.rating is None
    assert result.rating_count is None
    assert result.critic_rating == 91.25
    assert "UNSCOPED_JSON_LD_AGGREGATE_IGNORED" in result.warnings


def test_zero_score_and_nr_are_distinct():
    html = FIXTURE.read_text().replace('title="88.45">88', 'title="0">0')
    assert parse(html).rating == 0
    html = FIXTURE.read_text().replace('title="88.45">88', 'title="">NR')
    assert parse(html).rating is None


@pytest.mark.parametrize("score", ["101", "-5"])
def test_out_of_range_scores_fail_instead_of_getting_clipped(score):
    html = FIXTURE.read_text().replace('title="88.45"', f'title="{score}"')
    with pytest.raises(ParseError) as error:
        parse(html)
    assert error.value.code == "INVALID_RATING"


def test_title_tooltip_can_be_non_numeric_then_display_is_used():
    html = FIXTURE.read_text().replace('title="88.45"', 'title="User score"')
    assert parse(html).rating == 88


def test_conflicting_duplicate_user_score_blocks_fail():
    html = FIXTURE.read_text() + '<div class="albumUserScoreBox"><div class="albumUserScore">70</div></div>'
    with pytest.raises(ParseError) as error:
        parse(html)
    assert error.value.code == "AMBIGUOUS_RATING"


def test_conflicting_counts_are_unknown():
    html = FIXTURE.read_text().replace("Based on <a>1,234 ratings</a>",
                                      "Based on <a>1,234 ratings</a><span> and 567 ratings</span>")
    result = parse(html)
    assert result.rating_count is None
    assert "USER_COUNT_CONFLICT" in result.warnings


def test_graph_album_identity_and_multiple_artists_without_reading_track_ratings():
    album = {"@type": "MusicAlbum", "name": "Synthetic graph", "url": URL,
             "byArtist": [{"name": "A"}, {"name": "B"}], "datePublished": "2020",
             "track": {"@type": "MusicRecording", "name": "Track",
                       "aggregateRating": {"ratingValue": 100, "ratingCount": 999}}}
    payload = {"@graph": [{"@type": "WebSite", "name": "Site"}, album]}
    result = parse('<script type="application/ld+json">'+json.dumps(payload)+"</script>")
    assert result.title == "Synthetic graph"
    assert result.artists == ["A", "B"]
    assert result.year == 2020
    assert result.rating is None
    assert result.rating_count is None


def test_canonical_album_id_conflict_fails():
    html = FIXTURE.read_text().replace('rel="canonical" href="'+URL,
                                      'rel="canonical" href="'+URL.replace("987654", "987655"))
    with pytest.raises(ParseError) as error:
        parse(html)
    assert error.value.code == "IDENTITY_CONFLICT"


@pytest.mark.parametrize("html", ["<title>Just a moment...</title>", "<form id='challenge-form'></form>",
                                  "<title>Log in</title>"])
def test_challenge_or_login_is_not_a_release(html):
    with pytest.raises(ParseError) as error:
        parse(html)
    assert error.value.code == "BLOCKED_PAGE"


def test_missing_album_identity_fails_instead_of_using_unrelated_recommendations():
    with pytest.raises(ParseError) as error:
        parse('<div class="albumTitle">Recommendation</div><div class="artistTitle">Artist</div>')
    assert error.value.code == "MISSING_IDENTITY"


def test_malformed_jsonld_does_not_prevent_independent_valid_album_script():
    result = parse('<script type="application/ld+json">{broken}</script>'+FIXTURE.read_text())
    assert result.title == "Test & Signal"
    assert "MALFORMED_JSON_LD_IGNORED" in result.warnings


def test_unmatched_album_url_never_borrows_another_album():
    html = FIXTURE.read_text().replace('"url":"'+URL, '"url":"'+URL.replace("987654", "987655"))
    with pytest.raises(ParseError) as error:
        parse(html)
    assert error.value.code == "AMBIGUOUS_RELEASE"


def vibe_row(content):
    # Authored synthetic markup based on reference selectors; not a live page.
    return f'<div class="detailRow vibes">{content}<span>/ Vibe</span></div>'


def vibe_link(label, href=None):
    return f'<span class="vibe"><a href="{href or "/all/releases/vibe/" + label + "/"}">{label}</a></span>'


def with_details(content):
    return FIXTURE.read_text().replace('<div class="albumTopBox info">',
                                      '<div class="albumTopBox info">' + content)


def test_vibes_have_separate_source_evidence_without_changing_genres_or_ratings():
    row = vibe_row(vibe_link("energetic") + vibe_link("noisy") +
                   '<span hidden>' + vibe_link("dense") + '</span>' +
                   vibe_link("Energetic") + vibe_link("+12") + '<button>+12</button>')
    result = parse(with_details(row))
    assert result.vibes == ["energetic", "noisy", "dense"]
    assert result.genres == parse().genres
    assert result.descriptors == []
    assert result.rating == parse().rating
    assert result.rating_count == parse().rating_count
    assert result.model_dump()["vibes"] == result.vibes
    assert "detailRow.vibes" in result.field_evidence["vibes"]
    assert result.url == URL
    assert result.observed_at == NOW
    assert result.html_sha256 == html_digest(with_details(row))


@pytest.mark.parametrize("container", ["nav", "aside", "header", "footer",
    'div class="albumBlock"', 'div class="albumReviewRow"', 'div class="userReview"',
    'div class="trackListTable"', 'div class="recommendations"'])
def test_vibes_do_not_leak_from_navigation_recommendations_or_reviews(container):
    tag = container.split()[0]
    decoy = f'<{container}>' + vibe_row(vibe_link("decoy")) + f'</{tag}>'
    # Even an identically shaped row nested inside the album info must be excluded.
    assert parse(with_details(decoy)).vibes == []
    decoy = f'<{container}><div class="albumTopBox info">' + vibe_row(vibe_link("decoy"))
    decoy += f'</div></{tag}>'
    assert parse(FIXTURE.read_text() + decoy).vibes == []


def test_vibes_require_main_details_row_and_same_site_vibe_links():
    valid = vibe_link("real")
    wrong = vibe_row(vibe_link("foreign", "https://example.org/all/releases/vibe/foreign/") +
                     vibe_link("malformed", "http://[broken") +
                     vibe_link("genre", "/genre/1-genre/") +
                     vibe_link("tag", "/tag/tag/") +
                     vibe_link("insecure", "http://www.albumoftheyear.org/all/releases/vibe/x/"))
    html = with_details(wrong + valid) + vibe_row(vibe_link("outside"))
    assert parse(html).vibes == []
    assert "vibes" not in parse(html).field_evidence


def test_empty_vibes_do_not_change_legacy_parsed_release_serialization():
    result = parse()
    assert result.vibes == []
    assert "vibes" not in result.model_dump()
    assert "vibes" not in json.loads(result.model_dump_json())
