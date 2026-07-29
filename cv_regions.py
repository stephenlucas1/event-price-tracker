"""
cv_regions.py
-----------------------------------------------------------
Map a CrowdVolt slug to a city/region.

CrowdVolt slugs carry location as free text, and it is NOT consistent —
the same market shows up as "new-york", "nyc", "brooklyn", "bushwick",
"montauk", or as a bare venue ("sunset-park-rooftop"). The old scanner
scoped itself with a raw substring test for "new-york", which silently
dropped NYC events whose slug happened to spell the city differently:

    resolute-...-daytime-rooftop-buschwick-sat-aug-8-nyc      -> missed
    valentino-khan-marquee-skydeck-at-edge-hudson-yards-...   -> missed
    bunt-fri-jul-31-the-surf-lodge-montauk                    -> missed

So location is resolved through a token map instead, and every region is
scanned by default. `region_of` is best-effort labelling for the alert
emails; scanning no longer depends on it being right.

Token order matters: the FIRST region with a matching token wins, so
specific tokens (inglewood, san-francisco) must beat the vague state
tokens (california) that appear in both LA and Bay Area slugs.

A copy of this module ships alongside the alerting scripts, which run
separately and must label regions the same way. Keep the two in sync;
they are pure static data, so a diff is the whole review.
-----------------------------------------------------------
"""

# Ordered most-specific-first. Values are substrings matched against the slug.
REGION_TOKENS = (
    ("nyc", (
        "new-york", "-nyc", "nyc-", "brooklyn", "bushwick", "buschwick",
        "williamsburg", "greenpoint", "ridgewood", "queens", "manhattan",
        "montauk", "hudson-yards", "knockdown-center", "forest-hills",
        "long-island", "sunset-park", "elsewhere", "public-records",
    )),
    ("chicago", ("chicago", "chicag", "illinois", "lollapalooza", "lolla-")),
    ("los-angeles", (
        "los-angeles", "hollywood", "inglewood", "palm-springs",
        "orange-county", "long-beach", "santa-monica", "downtown-la",
        "shrine-expo", "kia-forum", "indio", "empire-polo",
    )),
    ("san-diego", ("san-diego",)),
    ("san-francisco", (
        "san-francisco", "san-franscisco", "-sf", "oakland", "berkeley",
        "bay-area", "pier-80",
    )),
    ("miami", (
        "miami", "florida", "fort-lauderdale", "wynwood", "jolene",
        "zey-zey", "zeyzey", "club-space",
    )),
    ("las-vegas", ("las-vegas", "vegas", "nevada")),
    ("denver", ("denver", "colorado", "morrison", "red-rocks")),
    ("austin", ("austin", "texas", "dallas", "houston")),
    ("seattle", ("seattle", "washington-state", "the-gorge")),
    ("new-jersey", ("new-jersey", "atlantic-city")),
    ("boston", ("boston", "massachusetts")),
    ("atlanta", ("atlanta", "georgia")),
)

OTHER = "other"


def region_of(slug: str) -> str:
    """Best-effort region label for a slug. Never raises; unknown -> 'other'."""
    s = (slug or "").lower()
    for region, tokens in REGION_TOKENS:
        if any(t in s for t in tokens):
            return region
    return OTHER


def parse_regions(value: str) -> set:
    """Parse a CV_CITIES / CV_HOME_REGION env value into a set of regions.

    Accepts region names ('nyc,chicago') and tolerates the legacy raw-token
    form ('new-york') by resolving it through the same token map, so an old
    CV_CITIES=new-york keeps meaning "the NYC market" instead of silently
    matching nothing.
    """
    out = set()
    for part in (value or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        known = {r for r, _ in REGION_TOKENS}
        if part in known or part == OTHER:
            out.add(part)
        else:
            out.add(region_of(part))
    return out


def known_regions() -> list:
    return [r for r, _ in REGION_TOKENS] + [OTHER]
