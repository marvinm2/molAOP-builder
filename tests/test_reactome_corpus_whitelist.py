"""
Test the Reactome corpus umbrella-pathway whitelist (#196).

Umbrella pathways (Programmed Cell Death, Apoptosis, DNA Repair, Detox of ROS)
exceed the MAX_GENES ceiling and were dropped, so curators could neither suggest
nor search them for generic KEs. filter_annotations must now force-include them
past the upper bound while still applying the disease exclusion and MIN_GENES floor.
"""
from scripts import download_reactome_annotations as dra


def test_umbrella_pathway_bypasses_max_genes():
    umbrella = "R-HSA-5357801"  # Programmed Cell Death (whitelisted)
    assert umbrella in dra.UMBRELLA_WHITELIST
    big_gene_set = [f"GENE{i}" for i in range(dra.MAX_GENES + 200)]
    raw = {umbrella: big_gene_set}
    out = dra.filter_annotations(raw, disease_ids=set())
    assert umbrella in out, "whitelisted umbrella pathway should survive the ceiling"


def test_non_whitelisted_big_pathway_still_dropped():
    big = "R-HSA-9999999"  # not whitelisted
    raw = {big: [f"GENE{i}" for i in range(dra.MAX_GENES + 200)]}
    out = dra.filter_annotations(raw, disease_ids=set())
    assert big not in out, "non-whitelisted oversized pathway should be dropped"


def test_whitelist_does_not_bypass_disease_or_min_floor():
    umbrella = "R-HSA-109581"  # Apoptosis (whitelisted)
    # In the disease branch -> excluded even though whitelisted.
    out = dra.filter_annotations(
        {umbrella: [f"G{i}" for i in range(dra.MAX_GENES + 50)]},
        disease_ids={umbrella},
    )
    assert umbrella not in out
    # Below the MIN_GENES floor -> excluded even though whitelisted.
    out2 = dra.filter_annotations({umbrella: ["G1", "G2"]}, disease_ids=set())
    assert umbrella not in out2


def test_normal_in_range_pathway_kept():
    pid = "R-HSA-1234567"
    raw = {pid: [f"G{i}" for i in range(50)]}
    out = dra.filter_annotations(raw, disease_ids=set())
    assert pid in out
