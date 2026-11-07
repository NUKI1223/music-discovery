"""Offline coverage audit. A synthetic or unreviewed catalogue cannot pass product G0."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

from discovery.contracts import Catalog

from .io import load_catalog


def _age_group(year: int | None, as_of: date) -> str:
    if year is None or year > as_of.year:
        return "unknown"
    age = as_of.year - year
    return "under-1" if age < 1 else "1-5" if age <= 5 else "over-5"


def audit_catalog(
    catalog: Catalog,
    *,
    as_of: date | None = None,
    review: dict | None = None,
    requirements_by_genre: dict | None = None,
) -> dict:
    """Inspect actual rows; never equate declared tags with an independent genre review.

    G0 thresholds are initial project requirements. ``review`` is a manifest of
    independently checked release IDs and source access, not an inferred approval.
    Cohort coverage is conservative: no silently widened comparison population.
    """
    catalog = Catalog.model_validate(catalog.model_dump())
    as_of = as_of or datetime.now(UTC).date()
    review = review or {}
    requirements_by_genre = requirements_by_genre or {}
    reviewed_ids = set(review.get("confirmed_genre_release_ids", []))
    access_checked = set(review.get("access_verified_sources", []))
    invalid_review_ids = sorted(reviewed_ids - {release.id for release in catalog.releases})
    roots = [genre for genre in catalog.genres if not genre.parent_ids]
    reports = {}
    for scope in roots:
        allowed = catalog.descendant_ids(scope.id)
        releases = [r for r in catalog.releases if set(r.genre_ids) & allowed]
        if not releases:
            reports[scope.id] = {"status": "no_data", "release_count": 0, "cohorts": []}
            continue
        sources = sorted({o.source for r in releases for o in r.observations})
        latest = {}
        for release in releases:
            for obs in release.observations:
                if obs.observed_at.date() > as_of:
                    continue
                key = (release.id, obs.source)
                if key not in latest or obs.observed_at > latest[key].observed_at:
                    latest[key] = obs

        def suitable(release, source):
            observation = latest.get((release.id, source))
            return (
                observation is not None and observation.rating_count is not None
                and (as_of - observation.observed_at.date()).days <= 90
                and _age_group(release.year, as_of) != "unknown"
            )

        usable = {r.id for r in releases if any(suitable(r, s) for s in sources)}
        provenance_ids = {
            r.id for r in releases if r.observations
            and all(o.url and o.access_basis for o in r.observations)
        }
        cohorts = []
        for genre_id in sorted(allowed):
            members = catalog.descendant_ids(genre_id)
            genre_releases = [r for r in releases if set(r.genre_ids) & members]
            for source in sources:
                for age in ("under-1", "1-5", "over-5", "unknown"):
                    denominator = [r for r in genre_releases if _age_group(r.year, as_of) == age]
                    if not denominator:
                        continue
                    observed = [r for r in denominator if suitable(r, source)]
                    rare = []
                    if len(observed) >= 30:
                        counts = [latest[(r.id, source)].rating_count for r in observed]
                        rare = [r for r in observed if sum(
                            count <= latest[(r.id, source)].rating_count for count in counts
                        ) / len(counts) <= .4]
                    rare_artists = {artist for r in rare for artist in r.artist_ids}
                    coverage = len(observed) / len(denominator)
                    cohorts.append({
                        "genre_id": genre_id, "source": source, "age_group": age,
                        "release_count": len(denominator), "usable_observation_count": len(observed),
                        "coverage": coverage, "rare_release_count": len(rare),
                        "rare_artist_count": len(rare_artists),
                        "status": "available" if coverage >= .8 and len(rare_artists) >= 10
                        else "limited",
                    })
        family_counts = {
            genre.id: sum(bool(set(r.genre_ids) & catalog.descendant_ids(genre.id)) for r in releases)
            for genre in catalog.genres if scope.id in genre.parent_ids
        }
        reasons = []
        requirements = requirements_by_genre.get(scope.id)
        if requirements is None:
            reasons.append("genre_product_requirements_not_configured")
        requirements = requirements or {}
        if catalog.is_fixture:
            reasons.append("synthetic_data_is_not_product_evidence")
        if len(releases) < requirements.get("min_releases", 0):
            reasons.append("release_count_below_genre_requirement")
        if sum(count >= requirements.get("min_releases_per_family", 30)
               for count in family_counts.values()) < requirements.get("min_families", 0):
            reasons.append("family_coverage_below_genre_requirement")
        confirmed = len({r.id for r in releases} & reviewed_ids) / len(releases)
        if confirmed < .95:
            reasons.append("independent_genre_review_below_95_percent")
        if len(usable) / len(releases) < .8:
            reasons.append("usable_popularity_below_80_percent")
        if len(provenance_ids) != len(releases):
            reasons.append("missing_provenance")
        if not sources or not set(sources) <= access_checked:
            reasons.append("source_access_not_verified_in_manifest")
        if not cohorts or any(row["status"] != "available" for row in cohorts):
            reasons.append("one_or_more_comparison_groups_limited")
        if invalid_review_ids:
            reasons.append("review_contains_unknown_release_ids")
        reports[scope.id] = {
            "status": "limited" if reasons else "product_pass",
            "release_count": len(releases),
            "artist_count": len({a for r in releases for a in r.artist_ids}),
            "family_counts": family_counts,
            "confirmed_genre_coverage": confirmed,
            "usable_popularity_coverage": len(usable) / len(releases),
            "provenance_coverage": len(provenance_ids) / len(releases),
            "reasons": reasons, "cohorts": cohorts,
        }
    return {
        "snapshot_id": catalog.snapshot_id, "taxonomy_version": catalog.taxonomy_version,
        "as_of": as_of.isoformat(), "is_fixture": catalog.is_fixture,
        "release_count": len(catalog.releases), "scope_reports": reports,
        "g0_status": "limited" if catalog.is_fixture or not reports or not any(
            row["status"] == "product_pass" for row in reports.values()
        ) else "available_scopes_only",
        "measurement_scope": "local_catalogue_not_global_popularity",
        "invalid_review_release_ids": invalid_review_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = audit_catalog(
            load_catalog(args.manifest), as_of=args.as_of,
            review=payload.get("independent_review", {}),
            requirements_by_genre=payload.get("requirements_by_genre", {}),
        )
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
