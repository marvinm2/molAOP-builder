"""The one place the dataset's licence is stated.

Every export declared the licence independently, and they disagreed: the
JSON and JSON-LD exports said **CC BY 4.0** while the Zenodo deposit, the GMT
header, the Turtle export and every document said **CC0 1.0** (#165).

The disagreement had a direction, which is what made it more than untidy. The
Zenodo deposit under concept DOI ``10.5281/zenodo.20184643`` has already
published this dataset under CC0, and **CC0 is irrevocable** — the public
domain dedication cannot be narrowed after the fact. So the JSON-LD was not
merely inconsistent, it was asserting *more* restriction than had already been
granted, in the machine-readable representation most likely to be harvested and
propagated by a third party. A downstream consumer reading the JSON-LD would
have believed attribution was legally required when it was not.

CC0 is also the documented intent: ``docs/DMP.md`` §2.4 states it, and
``docs/DATASET_DOCUMENTATION.md`` gives the rationale (attribution
requirements on derivative datasets are operationally awkward in regulatory and
commercial re-use).

Note the two licences in this project are deliberately different and must not
be conflated:

* the **dataset** — the curated KE → WikiPathways / GO / Reactome mappings and
  every export of them — is CC0 1.0, defined here;
* the **application source** is GPL-2.0, defined by the repository ``LICENSE``.
"""

# Human-readable name, for prose headers.
DATASET_LICENCE_NAME = "CC0 1.0 Universal (Public Domain Dedication)"

# Short form for a comment line where space is tight (GMT headers).
DATASET_LICENCE_SHORT = "CC0 1.0 - Public Domain Dedication"

# SPDX identifier, for machine-readable exports that expect one.
DATASET_LICENCE_SPDX = "CC0-1.0"

# Canonical URI. schema.org/Dataset `license` expects a URL, so this is what
# the JSON-LD emits.
DATASET_LICENCE_URI = "https://creativecommons.org/publicdomain/zero/1.0/"

# Zenodo's own vocabulary for the same licence. Zenodo will not accept an SPDX
# identifier or a URL here.
DATASET_LICENCE_ZENODO = "cc-zero"
