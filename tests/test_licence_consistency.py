"""Every export must state the same dataset licence (#165).

The exports each declared a licence independently and they disagreed: JSON and
JSON-LD said CC BY 4.0 while the Zenodo deposit, the GMT header, the Turtle
export and every document said CC0 1.0.

The disagreement had a direction, which is what made it more than untidy. The
Zenodo deposit under concept DOI 10.5281/zenodo.20184643 has already published
this dataset under CC0, and **CC0 is irrevocable** — a public domain dedication
cannot be narrowed afterwards. So the JSON-LD was asserting *more* restriction
than had already been granted, in the machine-readable representation most
likely to be harvested and propagated. A downstream consumer would have
believed attribution was legally required when it was not.

These tests pin the single source and the absence of any competing literal, so
the next exporter added cannot quietly reintroduce the drift.
"""
import os
import re

import pytest

from src.exporters.licence import (
    DATASET_LICENCE_NAME,
    DATASET_LICENCE_SHORT,
    DATASET_LICENCE_SPDX,
    DATASET_LICENCE_URI,
    DATASET_LICENCE_ZENODO,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that legitimately mention CC BY: they describe the licences of the
# *upstream* sources (AOP-Wiki, GO and Reactome are CC BY), which govern
# redistribution of unmodified source data — not the curated mappings.
UPSTREAM_LICENCE_DOCS = {
    "docs/DATASET_DOCUMENTATION.md",
    "docs/DMP.md",
}


def test_every_form_of_the_licence_agrees():
    """The five representations exist because different consumers demand
    different vocabularies, not because they may differ."""
    assert DATASET_LICENCE_SPDX == "CC0-1.0"
    assert DATASET_LICENCE_ZENODO == "cc-zero"
    assert "publicdomain/zero/1.0" in DATASET_LICENCE_URI
    assert "CC0" in DATASET_LICENCE_NAME
    assert "CC0" in DATASET_LICENCE_SHORT


@pytest.mark.parametrize(
    "module",
    [
        "src/exporters/json_exporter.py",
        "src/exporters/zenodo_assembly.py",
        "src/blueprints/main.py",
    ],
)
def test_exporters_use_the_constant_rather_than_a_literal(module):
    source = open(os.path.join(REPO, module), encoding="utf-8").read()
    assert "DATASET_LICENCE" in source, f"{module} must import the shared licence"


def test_no_exporter_declares_cc_by_for_the_dataset():
    """The specific regression: json_exporter declared CC-BY-4.0 in both the
    plain JSON header and the JSON-LD `license` field."""
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(REPO, "src")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            relative = os.path.relpath(path, REPO)
            # The licence module itself necessarily names CC BY: its docstring
            # is what records why the dataset is not under it.
            if relative == os.path.join("src", "exporters", "licence.py"):
                continue
            text = open(path, encoding="utf-8").read()
            if re.search(r"CC[-_ ]?BY|licenses/by/", text, re.IGNORECASE):
                offenders.append(relative)
    assert not offenders, (
        "these modules assert CC BY for the dataset, which contradicts the "
        f"irrevocable CC0 already published on Zenodo: {offenders}"
    )


def test_the_json_ld_license_is_a_url():
    """schema.org/Dataset `license` expects a URL. An SPDX identifier here
    would validate but resolve to nothing for a harvester."""
    assert DATASET_LICENCE_URI.startswith("https://")


def test_the_zenodo_form_is_zenodos_own_vocabulary():
    """Zenodo rejects both an SPDX identifier and a URL in this field, so this
    one genuinely cannot be shared with the others."""
    assert DATASET_LICENCE_ZENODO == DATASET_LICENCE_ZENODO.lower()
    assert " " not in DATASET_LICENCE_ZENODO


def test_the_code_licence_is_not_the_dataset_licence():
    """Deliberately different, and conflating them is the likeliest way to get
    this wrong: the application source is GPL-2.0, the curated dataset CC0."""
    licence_file = os.path.join(REPO, "LICENSE")
    assert os.path.exists(licence_file)
    head = open(licence_file, encoding="utf-8").read(200)
    assert "GNU GENERAL PUBLIC LICENSE" in head
    assert "CC0" not in head
