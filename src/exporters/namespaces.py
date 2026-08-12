"""The one place this project's public URLs and RDF namespaces are stated.

Every exporter minted its own URIs against the string ``ke-wp-mapping.org``,
a domain that **has never been registered** — it has no DNS record at all. It
arrived with the first rdflib rewrite of the RDF exporter as an aspirational
name from when this repository was called ``KE-WP-mapping``, and it spread: the
RDF vocabulary and mapping namespaces, the JSON-LD ``@id`` and dataset URI, the
download and export URLs advertised inside the JSON exports, and the base URL in
every documented API example.

Three separate things were wrong with it, and only the first is cosmetic:

1. It names a project that no longer exists. The repository, the service and the
   container image have all been ``molAOP-builder`` since the rename.
2. Nothing at those URIs resolves, and nothing ever could. Turtle deposited
   under the dataset DOI carries subject URIs that dereference to nothing, which
   is an F1/A1 failure in a deposit whose entire purpose is FAIR re-use — and
   the documented ``curl`` examples sent readers to a dead host.
3. Because the domain is unregistered, a third party could register it and serve
   content at this project's published identifiers.

The replacement is the live service, which is not a new invention: the mapping
namespace points at ``/mappings/<uuid>``, an existing route documented in
``main.mapping_detail`` as a "stable mapping detail page, accessible via
permanent UUID URL". Every mapping URI the RDF emits now dereferences to a real
page describing that mapping, which is what a linked-data identifier is supposed
to do.

**These are constants, deliberately, and must not be derived from the request
host.** An RDF identifier is global: the same mapping has to carry the same URI
whoever serves it, or a self-hosted instance would mint a second identity for
every row and joining two graphs would silently produce duplicates. A local
deployment therefore emits the canonical URIs, which is correct — it is
describing the same mappings, not different ones.

Note ``VOCAB`` still does not resolve: no OWL/SHACL schema is published at
``/vocab#`` yet (#162). That is now a missing document on a domain this project
controls and can fix, rather than a dead name on a domain it does not own.
"""

#: The canonical public base URL of the service. No trailing slash.
BASE_URL = "https://molaop-builder.vhp4safety.nl"

#: RDF vocabulary namespace for project-local predicates and classes.
#: Terminates in '#', so terms read as ``<...>/vocab#keyEventName``.
VOCAB_NS = f"{BASE_URL}/vocab#"

#: Namespace for individual mapping resources. Deliberately matches the live
#: ``/mappings/<uuid>`` route, so each minted URI dereferences to that mapping's
#: detail page rather than to a 404.
MAPPING_NS = f"{BASE_URL}/mappings/"

#: URI identifying the dataset as a whole, for schema.org/DCAT descriptions.
DATASET_URI = f"{BASE_URL}/dataset"
