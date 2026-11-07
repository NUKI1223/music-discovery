"""Ranked album evidence must remain distinct from unordered index membership."""

import pytest

from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    SourceMetadata,
    UserProfile,
)
from discovery.ranking import score_candidates
from discovery.ranking.source_descriptors import descriptor_weights, ordered_vibe_positions


def metadata(labels, *, row='album', date='2026-11-02T08:00:00+00:00', kind='aoty_album_readable_metadata'):
    return SourceMetadata(source='aoty', source_row_id=row, title='Fixture', artist_credit='Fixture',
        vibes=labels, mapping_evidence=[dict(kind=kind, retrieved_at=date,
            url='https://www.albumoftheyear.org/album/123-fixture.php',
            text_sha256='a' * 64, vibe_completeness='visible_subset')])


def release(rows):
    return Release(id='r', title='Fixture', artist_ids=['a'], genre_ids=['jazz'], source_metadata=rows)


@pytest.mark.parametrize('scope', ['jazz', 'electronic', 'hip-hop'])
def test_early_vibe_outranks_late_and_unknown_order_in_each_genre(scope):
    rows = [metadata(['heavy', 'warm', 'energetic']), metadata(['warm', 'energetic', 'heavy']),
            metadata(['heavy', 'warm'], kind='index_membership')]
    cat = Catalog(snapshot_id='ordered-vibes-fixture', is_fixture=True,
        genres=[Genre(id=scope, name=scope)], artists=[Artist(id='a', name='Fixture')],
        releases=[Release(id=f'r{i}', title=f'Fixture {i}', artist_ids=['a'], genre_ids=[scope],
                          source_metadata=[row]) for i, row in enumerate(rows)])
    wanted = score_candidates(cat, RequestIntent(genre_scope_id=scope, preferred_descriptors=['heavy']), UserProfile())
    assert [i.release_id for i in wanted.items] == ['r0', 'r1', 'r2']
    avoided = score_candidates(cat, RequestIntent(genre_scope_id=scope, avoided_descriptors=['heavy']), UserProfile())
    assert [i.release_id for i in avoided.items] == ['r2', 'r1', 'r0']
    assert 'vibe_order_is_heuristic' in wanted.items[0].uncertainty_flags
    assert 'vibe_order_is_heuristic' not in wanted.items[-1].uncertainty_flags
    assert all(i.components['rarity'] is None for i in wanted.items)


def test_index_duplicates_and_alphabetical_order_cannot_override_album_position():
    page = metadata(['warm', 'energetic', 'heavy'])
    r = release([page])
    original = descriptor_weights(r)
    index = metadata(['heavy', 'warm', 'energetic'], row='index', kind='index_membership')
    r.source_metadata += [index, index, page]
    assert descriptor_weights(r) == original
    index.vibes.reverse()
    assert descriptor_weights(r) == original
    r.source_metadata = [index]
    assert ordered_vibe_positions(r) == {}
    assert len(set(descriptor_weights(r)[0].values())) == 1


def test_latest_capture_wins_without_inventing_order_for_hidden_tags():
    old = metadata(['warm', 'heavy', 'hidden'], row='old')
    new = metadata(['heavy', 'warm'], row='new', date='2026-11-02T10:00:00+00:00')
    r = release([new, old])
    weights, positions = descriptor_weights(r)
    assert positions == {'heavy': 1, 'warm': 2}
    assert weights['hidden'] < weights['warm'] < weights['heavy']
    r.source_metadata.reverse()
    assert descriptor_weights(r) == (weights, positions)


@pytest.mark.parametrize('patch', [dict(url='https://example.com/album/123-fake.php'),
    dict(url='https://www.albumoftheyear.org/all/releases/vibe/heavy/'),
    dict(retrieved_at='invalid'), dict(retrieved_at='2026-11-02T08:00:00'),
    dict(text_sha256=''), dict(vibe_completeness='unknown')])
def test_unattributable_order_is_not_used(patch):
    row = metadata(['heavy', 'warm'])
    row.mapping_evidence[0].update(patch)
    assert ordered_vibe_positions(release([row])) == {}


def test_repeated_labels_do_not_shift_positions_or_multiply_strength():
    r = release([metadata([' Heavy ', 'heavy', '', 'warm'])])
    assert ordered_vibe_positions(r) == {'heavy': 1, 'warm': 2}


def test_visible_block_captures_preserve_order_too():
    row = metadata(['heavy', 'warm'])
    row.mapping_evidence[0]['vibe_completeness'] = 'visible_block'
    assert ordered_vibe_positions(release([row])) == {'heavy': 1, 'warm': 2}
