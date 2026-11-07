"""Explicitly synthetic data for offline demonstrations, never product evidence."""

from datetime import UTC, datetime

from discovery.contracts import Artist, Catalog, Genre, Observation, Release


def make_demo_catalog() -> Catalog:
    genres = [
        Genre(id="hip-hop", name="Hip-hop", aliases=["hip hop", "rap"]),
        Genre(id="jazz", name="Jazz"),
        Genre(id="rnb", name="R&B"),
        Genre(id="experimental", name="Experimental hip-hop", parent_ids=["hip-hop"]),
        Genre(id="abstract", name="Abstract hip-hop", parent_ids=["hip-hop"]),
        Genre(id="boom-bap", name="Boom bap", parent_ids=["hip-hop"]),
        Genre(id="jazz-rap", name="Jazz rap", parent_ids=["hip-hop", "jazz"]),
        Genre(id="hardcore", name="Hardcore hip-hop", parent_ids=["hip-hop"]),
        Genre(id="trap", name="Trap", parent_ids=["hip-hop"]),
        Genre(id="modal-jazz", name="Modal jazz", parent_ids=["jazz"]),
    ]
    artists, releases = [], []
    for group in [genre for genre in genres if genre.parent_ids]:
        for index in range(48):
            artist_id = f"demo-artist-{group.id}-{index}"
            release_id = f"demo-release-{group.id}-{index}"
            tags = [group.id]
            if index % 9 == 0 and group.id in {"trap", "jazz-rap"}:
                tags.append("rnb")
            artists.append(Artist(
                id=artist_id, name=f"[DEMO] {group.name} artist {index + 1}", genre_ids=tags,
            ))
            releases.append(Release(
                id=release_id, title=f"[DEMO] {group.name} session {index + 1}",
                artist_ids=[artist_id], genre_ids=tags, year=2012 + index % 5,
                release_type="ep" if index % 7 == 0 else "album",
                observations=[Observation(
                    source="synthetic-demo", url="", observed_at=datetime(2026, 10, 28, tzinfo=UTC),
                    rating=65 + index % 30, rating_scale=100,
                    rating_count=None if index == 47 else 10 + index * 25,
                    access_basis="Invented test data; not real releases, ratings or popularity.",
                )],
            ))
    return Catalog(
        snapshot_id="synthetic-demo-v1-2026-10-28", taxonomy_version="demo-v1",
        is_fixture=True, genres=genres, artists=artists, releases=releases,
    )
