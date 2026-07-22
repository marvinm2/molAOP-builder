"""POST /admin/exports/regenerate must rebuild all three resources (#227).

The handler rmtree'd the whole export directory and then wrote KE-WP and KE-GO
only, so the Reactome GMT and Turtle were deleted and never written back. The
admin dashboard lists Reactome alongside the other two, so pressing "rebuild
the on-disk export cache" silently produced two thirds of a cache.
"""
import os
import tempfile

import pytest

import src.blueprints.admin as admin_mod
import src.blueprints.main as main_mod
from app import app as flask_app
from src.core.models import (
    Database, GoMappingModel, MappingModel, ReactomeMappingModel,
)


@pytest.fixture
def regen_env(tmp_path, monkeypatch):
    os.environ["ADMIN_USERS"] = "github:regenadmin"

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm, gmm, rmm = MappingModel(db), GoMappingModel(db), ReactomeMappingModel(db)

    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO ke_go_mappings (ke_id, ke_title, go_id, go_name, "
            "connection_type, confidence_level) "
            "VALUES ('KE 149', 'Inflammation', 'GO:0006954', 'inflammatory response', "
            "'describes', 'high')"
        )
        conn.execute(
            "INSERT INTO ke_reactome_mappings (uuid, ke_id, ke_title, reactome_id, "
            "pathway_name, species, confidence_level) "
            "VALUES ('u1', 'KE 1194', 'DNA damage', 'R-HSA-100', 'p53', "
            "'Homo sapiens', 'high')"
        )
        conn.commit()
    finally:
        conn.close()

    cache_dir = tmp_path / "exports"
    monkeypatch.setattr(main_mod, "EXPORT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(admin_mod, "mapping_model", mm)
    monkeypatch.setattr(admin_mod, "go_mapping_model", gmm)
    monkeypatch.setattr(admin_mod, "reactome_mapping_model", rmm)

    from src.exporters import gmt_exporter
    monkeypatch.setattr(
        gmt_exporter, "_load_go_annotations_merged",
        lambda bp_path=None, mf_path=None: {"GO:0006954": ["IL6", "TNF"]},
    )
    monkeypatch.setattr(
        gmt_exporter, "_load_reactome_annotations",
        lambda path=None: {"R-HSA-100": ["TP53", "MDM2"]},
    )

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = {"username": "github:regenadmin", "email": "a@e"}
        with flask_app.app_context():
            yield {"client": client, "cache_dir": cache_dir}

    os.close(fd)
    os.unlink(db_path)


def test_regenerate_writes_reactome_gmt_and_turtle(regen_env):
    resp = regen_env["client"].post("/admin/exports/regenerate")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["status"] == "ok"

    written = payload["files"]
    assert any(f.startswith("KE-REACTOME_") for f in written), written
    assert "ke-reactome-mappings.ttl" in written, written

    cache_dir = regen_env["cache_dir"]
    gmts = sorted(p.name for p in cache_dir.glob("KE-REACTOME_*.gmt"))
    assert gmts, "no Reactome GMT on disk after a full regenerate"
    assert "TP53" in (cache_dir / gmts[0]).read_text()
    assert (cache_dir / "ke-reactome-mappings.ttl").read_text().strip()


def test_regenerate_still_writes_wp_and_go(regen_env):
    """Guard against the Reactome arm being added at the others' expense."""
    resp = regen_env["client"].post("/admin/exports/regenerate")
    written = resp.get_json()["files"]
    assert any(f.startswith("KE-GO_") for f in written), written
    assert "ke-go-mappings.ttl" in written, written
