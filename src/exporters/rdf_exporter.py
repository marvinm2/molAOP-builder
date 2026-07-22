"""
RDF/Turtle export for KE-WP and KE-GO mapping datasets.

Pure Python module — no Flask dependency. Uses rdflib Graph for valid
Turtle serialisation with full Phase 2/3 provenance columns.
"""
import logging

from rdflib import Graph, Literal, Namespace, RDF
from rdflib.namespace import DCTERMS, XSD

from src.exporters.confidence import (
    filter_by_exact_confidence,
    filter_by_min_confidence,
)

logger = logging.getLogger(__name__)

VOCAB = Namespace("https://ke-wp-mapping.org/vocab#")
MAPPING = Namespace("https://ke-wp-mapping.org/mapping/")



def _apply_confidence(mappings, min_confidence=None, confidence=None):
    """Apply whichever confidence filter the caller asked for.

    `min_confidence` is a threshold (medium => medium and above) and backs the
    public ?min_confidence= query parameter. `confidence` selects a single tier
    and backs the mutually exclusive _High/_Medium/_Low files in the Zenodo
    deposit and the admin export bundle. Passing both is a caller bug.

    Before #206 there was only `min_confidence`, and it implemented the
    partition — so ?min_confidence=medium silently dropped every high-confidence
    mapping.
    """
    if min_confidence and confidence:
        raise ValueError(
            "Pass either min_confidence (threshold) or confidence (exact tier), not both"
        )
    if confidence:
        return filter_by_exact_confidence(mappings, confidence)
    return filter_by_min_confidence(mappings, min_confidence)

def _to_iso8601_datetime(value):
    """Coerce a SQLite-style "YYYY-MM-DD HH:MM:SS" string into ISO-8601.

    The DB migration in core/models.py normalises legacy rows on startup,
    but this defensive coercion guards against any stragglers (e.g.
    fixtures, tests, future tables not yet listed in the backfill targets)
    so the XSD.dateTime literal is always well-formed. No-op when the
    value already contains 'T' or doesn't match the discriminator.
    """
    if not isinstance(value, str) or len(value) < 11:
        return value
    if value[10] == " ":
        return value[:10] + "T" + value[11:]
    return value


def generate_ke_wp_turtle(mappings, min_confidence=None, confidence=None) -> str:
    """Generate Turtle content for KE-WP mappings.

    Parameters
    ----------
    mappings:
        List of dicts from MappingModel.get_all_mappings(). Each dict is
        expected to contain: uuid, ke_id, ke_title, wp_id, wp_title,
        confidence_level, approved_by_curator, approved_at_curator,
        suggestion_score.
    min_confidence:
        Optional lowercase threshold ("high", "medium" or "low"). Rows below
        it are excluded; "medium" therefore keeps medium *and* high, and "low"
        is equivalent to no filtering. Rows whose own confidence is missing or
        unrecognised are kept, so an export can never silently empty.
        Mutually exclusive with `confidence`.
    confidence:
        Optional lowercase exact tier. Keeps only rows at precisely this level —
        the partition the Zenodo deposit's _High/_Medium/_Low files are built
        on. Mutually exclusive with `min_confidence`.

    Returns
    -------
    str
        Turtle-formatted string parseable by rdflib. Empty graph skeleton if
        no rows survive filtering.
    """
    mappings = _apply_confidence(mappings, min_confidence, confidence)

    g = Graph()
    g.bind("ke-wp", VOCAB)
    g.bind("dcterms", DCTERMS)
    g.bind("mapping", MAPPING)

    for row in mappings:
        if not row.get("uuid"):
            continue

        uri = MAPPING[row["uuid"]]
        g.add((uri, RDF.type, VOCAB.KeyEventPathwayMapping))
        g.add((uri, DCTERMS.identifier, Literal(row["uuid"])))
        g.add((uri, VOCAB.keyEventId, Literal(row["ke_id"])))
        g.add((uri, VOCAB.keyEventName, Literal(row["ke_title"])))
        g.add((uri, VOCAB.pathwayId, Literal(row["wp_id"])))
        g.add((uri, VOCAB.pathwayTitle, Literal(row["wp_title"])))
        g.add((uri, VOCAB.confidenceLevel, Literal(row["confidence_level"])))

        if row.get("approved_by_curator"):
            g.add((uri, DCTERMS.creator, Literal(row["approved_by_curator"])))

        if row.get("approved_at_curator"):
            g.add((
                uri,
                DCTERMS.date,
                Literal(_to_iso8601_datetime(row["approved_at_curator"]), datatype=XSD.dateTime),
            ))

        if row.get("suggestion_score") is not None:
            g.add((
                uri,
                VOCAB.suggestionScore,
                Literal(float(row["suggestion_score"]), datatype=XSD.decimal),
            ))

        # Phase E.1: upstream snapshot provenance per mapping.
        # `wpReleaseDate` is the WikiPathways release the curator was reviewing
        # at approval time; `aopWikiSnapshotDate` is the AOP-Wiki snapshot the
        # KE side was anchored to. Both are nullable (legacy rows that pre-date
        # the backfill have NULL — emit nothing for those).
        if row.get("wp_release_date"):
            g.add((
                uri,
                VOCAB.wpReleaseDate,
                Literal(row["wp_release_date"], datatype=XSD.date),
            ))
        if row.get("aopwiki_snapshot_date"):
            g.add((
                uri,
                VOCAB.aopWikiSnapshotDate,
                Literal(row["aopwiki_snapshot_date"], datatype=XSD.date),
            ))

    return g.serialize(format="turtle")


def generate_ke_go_turtle(mappings, min_confidence=None, confidence=None) -> str:
    """Generate Turtle content for KE-GO mappings.

    Parameters
    ----------
    mappings:
        List of dicts from GoMappingModel.get_all_mappings(). Each dict is
        expected to contain: uuid, ke_id, ke_title, go_id, go_name,
        confidence_level, approved_by_curator, approved_at_curator,
        suggestion_score.
    min_confidence:
        Optional lowercase string for confidence filtering.

    Returns
    -------
    str
        Turtle-formatted string parseable by rdflib.
    """
    mappings = _apply_confidence(mappings, min_confidence, confidence)

    g = Graph()
    g.bind("ke-wp", VOCAB)
    g.bind("dcterms", DCTERMS)
    g.bind("mapping", MAPPING)

    for row in mappings:
        if not row.get("uuid"):
            continue

        uri = MAPPING[row["uuid"]]
        g.add((uri, RDF.type, VOCAB.KeyEventGOMapping))
        g.add((uri, DCTERMS.identifier, Literal(row["uuid"])))
        g.add((uri, VOCAB.keyEventId, Literal(row["ke_id"])))
        g.add((uri, VOCAB.keyEventName, Literal(row["ke_title"])))
        g.add((uri, VOCAB.goTermId, Literal(row["go_id"])))
        g.add((uri, VOCAB.goTermName, Literal(row["go_name"])))
        g.add((uri, VOCAB.confidenceLevel, Literal(row["confidence_level"])))

        if row.get("approved_by_curator"):
            g.add((uri, DCTERMS.creator, Literal(row["approved_by_curator"])))

        if row.get("approved_at_curator"):
            g.add((
                uri,
                DCTERMS.date,
                Literal(_to_iso8601_datetime(row["approved_at_curator"]), datatype=XSD.dateTime),
            ))

        if row.get("suggestion_score") is not None:
            g.add((
                uri,
                VOCAB.suggestionScore,
                Literal(float(row["suggestion_score"]), datatype=XSD.decimal),
            ))

        if row.get("go_direction"):
            g.add((uri, VOCAB.goDirection, Literal(row["go_direction"])))

        if row.get("go_namespace"):
            g.add((uri, VOCAB.goNamespace, Literal(row["go_namespace"])))

        # Phase E.1: upstream snapshot provenance per mapping. See the
        # corresponding block in generate_ke_wp_turtle for shape rationale.
        if row.get("go_release_date"):
            g.add((
                uri,
                VOCAB.goReleaseDate,
                Literal(row["go_release_date"], datatype=XSD.date),
            ))
        if row.get("aopwiki_snapshot_date"):
            g.add((
                uri,
                VOCAB.aopWikiSnapshotDate,
                Literal(row["aopwiki_snapshot_date"], datatype=XSD.date),
            ))

    return g.serialize(format="turtle")


def generate_ke_reactome_turtle(mappings, min_confidence=None, confidence=None, reactome_metadata=None) -> str:
    """Generate Turtle content for KE-Reactome mappings.

    Mirrors generate_ke_go_turtle. Drops goDirection/goNamespace; adds
    species, pathwayDescription (from optional reactome_metadata dict
    keyed by reactome_id).

    Parameters
    ----------
    mappings:
        List of dicts from ReactomeMappingModel.get_all_mappings(). Each
        dict is expected to contain: uuid, ke_id, ke_title, reactome_id,
        pathway_name, species, confidence_level, approved_by_curator,
        approved_at_curator, suggestion_score.
    min_confidence:
        Optional lowercase string for confidence filtering (e.g. "high").
    reactome_metadata:
        Optional dict keyed by reactome_id; each value may carry a
        ``description`` key, which (when present) is emitted as a
        ``vocab#pathwayDescription`` triple.

    Returns
    -------
    str
        Turtle-formatted string parseable by rdflib.
    """
    mappings = _apply_confidence(mappings, min_confidence, confidence)

    g = Graph()
    g.bind("ke-wp", VOCAB)
    g.bind("dcterms", DCTERMS)
    g.bind("mapping", MAPPING)

    for row in mappings:
        if not row.get("uuid"):
            continue

        uri = MAPPING[row["uuid"]]
        g.add((uri, RDF.type, VOCAB.KeyEventReactomeMapping))
        g.add((uri, DCTERMS.identifier, Literal(row["uuid"])))
        g.add((uri, VOCAB.keyEventId, Literal(row["ke_id"])))
        g.add((uri, VOCAB.keyEventName, Literal(row["ke_title"])))
        g.add((uri, VOCAB.reactomeId, Literal(row["reactome_id"])))
        g.add((uri, VOCAB.pathwayName, Literal(row["pathway_name"])))
        g.add((uri, VOCAB.confidenceLevel, Literal(row["confidence_level"])))

        if row.get("species"):
            g.add((uri, VOCAB.species, Literal(row["species"])))

        if row.get("approved_by_curator"):
            g.add((uri, DCTERMS.creator, Literal(row["approved_by_curator"])))

        if row.get("approved_at_curator"):
            g.add((
                uri,
                DCTERMS.date,
                Literal(_to_iso8601_datetime(row["approved_at_curator"]), datatype=XSD.dateTime),
            ))

        if row.get("suggestion_score") is not None:
            g.add((
                uri,
                VOCAB.suggestionScore,
                Literal(float(row["suggestion_score"]), datatype=XSD.decimal),
            ))

        if reactome_metadata:
            meta = reactome_metadata.get(row["reactome_id"])
            if meta and meta.get("description"):
                g.add((uri, VOCAB.pathwayDescription, Literal(meta["description"])))

        # Phase E.1: upstream snapshot provenance per mapping. Reactome
        # carries both an integer release version and a release date.
        if row.get("reactome_release_version"):
            g.add((
                uri,
                VOCAB.reactomeReleaseVersion,
                Literal(row["reactome_release_version"]),
            ))
        if row.get("reactome_release_date"):
            g.add((
                uri,
                VOCAB.reactomeReleaseDate,
                Literal(row["reactome_release_date"], datatype=XSD.date),
            ))
        if row.get("aopwiki_snapshot_date"):
            g.add((
                uri,
                VOCAB.aopWikiSnapshotDate,
                Literal(row["aopwiki_snapshot_date"], datatype=XSD.date),
            ))

    return g.serialize(format="turtle")
