"""Issue #289: deletion proposals must name the mapping they delete.

A deletion proposal stores no KE/pathway of its own (``ke_id``/``wp_id`` are
NULL by design); the target lives in the joined ``mapping_*`` columns. The GO
queue already fell back to them, but the WikiPathways and Reactome queues
rendered "None → None", and the shared review panel printed blanks — so an
admin was asked to approve a DELETE against a row the page did not name.
"""
import os
import re
import tempfile

import pytest


def _row_html(html, proposal_id):
    """Return the opening <tr ...> tag and Mapping cell for one proposal."""
    match = re.search(
        r'<tr data-proposal-id="%d".*?</td>\s*<td>\s*%d\s*</td>\s*<td>(.*?)</td>'
        % (proposal_id, proposal_id),
        html,
        re.S,
    )
    assert match, f"row for proposal {proposal_id} not rendered"
    return match.group(0), match.group(1)


def _attr(row, name):
    match = re.search(r'%s="([^"]*)"' % re.escape(name), row)
    assert match, f"{name} missing from row"
    return match.group(1)


@pytest.fixture
def admin_app():
    os.environ["ADMIN_USERS"] = "github:testadmin"

    import src.blueprints.admin as admin_mod
    from app import app as flask_app
    from src.core.models import (
        Database,
        MappingModel,
        ProposalModel,
        ReactomeMappingModel,
        ReactomeProposalModel,
    )

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    models = {
        "mapping_model": MappingModel(db),
        "proposal_model": ProposalModel(db),
        "reactome_mapping_model": ReactomeMappingModel(db),
        "reactome_proposal_model": ReactomeProposalModel(db),
    }
    orig = {name: getattr(admin_mod, name) for name in models}
    for name, model in models.items():
        setattr(admin_mod, name, model)

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with flask_app.app_context():
            with client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield client, models

    for name, model in orig.items():
        setattr(admin_mod, name, model)
    os.close(fd)
    os.unlink(db_path)


def test_wp_deletion_proposal_names_its_target(admin_app):
    client, models = admin_app
    mapping_id = models["mapping_model"].create_mapping(
        ke_id="KE 386",
        ke_title="Decrease of neuronal network function",
        wp_id="WP4875",
        wp_title="Disruption of postsynaptic signaling by CNV",
        created_by="github:curator",
    )
    proposal_id = models["proposal_model"].create_proposal(
        mapping_id=mapping_id,
        user_name="Curator",
        user_email="curator@example.com",
        user_affiliation="UM",
        provider_username="github:curator",
        proposed_delete=True,
    )

    resp = client.get("/admin/proposals?status=pending")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    row, cell = _row_html(html, proposal_id)

    assert "None" not in cell
    assert "KE 386" in cell and "WP4875" in cell
    assert "Decrease of neuronal network function" in cell
    assert "Disruption of postsynaptic signaling by CNV" in cell

    assert _attr(row, "data-ke-id") == "KE 386"
    assert _attr(row, "data-pathway-id") == "WP4875"
    assert _attr(row, "data-ke-title") == "Decrease of neuronal network function"
    assert _attr(row, "data-pathway-title") == (
        "Disruption of postsynaptic signaling by CNV"
    )
    # With data-ke-id populated the panel renders from the row instead of
    # fetching, so the row must also say it is a deletion.
    assert _attr(row, "data-proposed-delete") == "1"
    assert _attr(row, "data-mapping-id") == str(mapping_id)


def test_reactome_deletion_proposal_names_its_target(admin_app):
    client, models = admin_app
    mapping_id = models["reactome_mapping_model"].create_mapping(
        ke_id="KE 177",
        ke_title="Mitochondrial dysfunction",
        reactome_id="R-HSA-5357801",
        pathway_name="Programmed Cell Death",
        created_by="github:curator",
    )
    # Rows written before the display columns were populated carry no KE or
    # pathway of their own; the queue must still name the target.
    proposal_id = models["reactome_proposal_model"].create_proposal(
        mapping_id=mapping_id,
        user_name="Curator",
        user_email="curator@example.com",
        user_affiliation="UM",
        provider_username="github:curator",
        proposed_delete=True,
    )

    resp = client.get("/admin/reactome-proposals?status=pending")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    row, cell = _row_html(html, proposal_id)

    assert "None" not in cell
    assert "KE 177" in cell and "R-HSA-5357801" in cell
    assert "Mitochondrial dysfunction" in cell
    assert "Programmed Cell Death" in cell

    assert _attr(row, "data-ke-id") == "KE 177"
    assert _attr(row, "data-pathway-id") == "R-HSA-5357801"
    assert _attr(row, "data-ke-title") == "Mitochondrial dysfunction"
    assert _attr(row, "data-pathway-title") == "Programmed Cell Death"


def test_review_panel_falls_back_to_mapping_fields():
    """The detail-endpoint path of the shared panel must read mapping_* too."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    js_path = os.path.join(
        os.path.dirname(tests_dir), "static", "js", "admin_proposals.js"
    )
    content = open(js_path, encoding="utf-8").read()
    start = content.index("function _renderPanel(proposal)")
    body = content[start:]

    # Everything the panel uses to name the pathway/term side.
    pathway = body[: body.index("var pathwayLabel")]
    for field in [
        "mapping_wp_id",
        "mapping_go_id",
        "mapping_reactome_id",
        "mapping_wp_title",
        "mapping_go_name",
        "mapping_pathway_name",
    ]:
        assert "proposal." + field in pathway, f"panel ignores {field}"

    assert "proposal.mapping_ke_id" in body, "KE line ignores mapping_ke_id"
    assert "proposal.mapping_ke_title" in body, "KE line ignores mapping_ke_title"
