"""#245 — the Propose Change modal asks the assessment, not a bare tier.

Static assertions over templates/explore.html. The modal's markup and its
submit handler both live in that template, and the failure mode being guarded
is a silent one: a form that stops sending a field still submits successfully,
and the server would read the answers as absent rather than erroring, so a
drifted client shows up as "revisions mysteriously rejected" rather than as a
test failure.

The same grep-the-asset approach as tests/test_admin_proposals_js.py, for the
same reason — there is no JS test runner in this project.
"""
import os

import pytest

from src.core.schemas import (
    GO_CONNECTION_TYPES,
    GO_DIMENSION_SCORES,
    KE_WP_BASIS_OPTIONS,
    KE_WP_COVERAGE_OPTIONS,
    KE_WP_RELATIONSHIP_OPTIONS,
    KE_WP_SPECIFICITY_OPTIONS,
)

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "explore.html",
)


@pytest.fixture(scope="module")
def explore_html():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------
# The direct tier control is gone
# --------------------------------------------------------------------------

def test_bare_confidence_radio_is_gone(explore_html):
    """The control this whole issue exists to remove.

    It was the only way to move a mapping between tiers without recording why.
    """
    assert "changeConfidence" not in explore_html
    assert 'name="changeConfidence"' not in explore_html


def test_confidence_is_described_as_derived(explore_html):
    assert "calculated from these answers" in explore_html


# --------------------------------------------------------------------------
# Each resource is asked its own instrument
# --------------------------------------------------------------------------

def test_wp_questions_match_the_server_whitelists(explore_html):
    """Every option the modal offers must be one the schema accepts.

    A value present in one and not the other is a 400 the curator cannot act
    on, which is what made the GO connection-type mismatch survive so long.
    """
    for option in KE_WP_RELATIONSHIP_OPTIONS:
        assert f"['{option}'," in explore_html
    for option in KE_WP_BASIS_OPTIONS:
        assert f"['{option}'," in explore_html
    for option in KE_WP_SPECIFICITY_OPTIONS:
        assert f"['{option}'," in explore_html
    for option in KE_WP_COVERAGE_OPTIONS:
        assert f"['{option}'," in explore_html


def test_wp_asks_all_four_questions(explore_html):
    for name in ("step1", "step2", "step3", "step4"):
        assert f"name: '{name}'" in explore_html


def test_go_offers_its_own_connection_vocabulary(explore_html):
    """Not WikiPathways'. The old modal offered causative/responsive/undefined
    for GO, none of which is a valid GO connection type."""
    for go_type in GO_CONNECTION_TYPES:
        assert f"['{go_type}'," in explore_html


def test_go_asks_its_three_dimensions(explore_html):
    for name in ("connection_score", "specificity_score", "evidence_score"):
        assert f"name: '{name}'" in explore_html


def test_go_dimension_options_match_the_server_range(explore_html):
    """The form must not offer a score the schema rejects."""
    go_block = explore_html[explore_html.index("go: ["):explore_html.index("};", explore_html.index("go: ["))]
    for score in GO_DIMENSION_SCORES:
        assert f"['{score}'," in go_block
    assert "['0'," not in go_block
    assert "['4'," not in go_block


# --------------------------------------------------------------------------
# The submit handler sends what the server reads
# --------------------------------------------------------------------------

def test_submit_sends_the_answers_by_question_name(explore_html):
    handler = explore_html[explore_html.index("#proposalForm\").on('submit'"):]
    assert "ASSESSMENT_QUESTIONS[mappingType]" in handler
    assert "formData.append(q.name, value)" in handler


def test_deletion_sends_no_assessment(explore_html):
    """The server rejects answers alongside a deletion, so the client must not
    send them — otherwise every deletion is a 400."""
    handler = explore_html[explore_html.index("#proposalForm\").on('submit'"):]
    assert "if (!deleting)" in handler


def test_deletion_clears_and_hides_the_questions(explore_html):
    assert "function syncDeletionState" in explore_html
    assert "prop('checked', false)" in explore_html


# --------------------------------------------------------------------------
# The proposer can see what they are changing
# --------------------------------------------------------------------------

def test_current_answers_are_shown(explore_html):
    """Previously the modal showed only a JSON dump of the row, which does not
    include the assessment — so a curator revised answers they could not see."""
    assert "function currentAnswers" in explore_html
    assert "currently: " in explore_html


def test_legacy_mappings_say_so_rather_than_rendering_blanks(explore_html):
    """Four empty values read as "no answers given" when the truth is "answers
    were never collectable". Until #234 every approved GO mapping was v1."""
    assert "predates the assessment" in explore_html


def test_reactome_shares_the_wp_questions(explore_html):
    """#245 part 2. Reactome uses the same four columns as WikiPathways, so it
    asks the same four questions rather than a Reactome-shaped copy — a copy is
    how the GO and WP paths drifted apart in the first place."""
    assert "ASSESSMENT_QUESTIONS.reactome = ASSESSMENT_QUESTIONS.wp" in explore_html


def test_reactome_current_answers_read_the_assessment(explore_html):
    assert "mappingType === 'wp' || mappingType === 'reactome'" in explore_html


def test_reactome_entry_comes_from_the_row_not_data_attributes(explore_html):
    """The hand-built entry carried only an id and a tier, which was enough to
    delete by and not enough to revise with — the row already carries the
    stored assessment."""
    handler = explore_html[explore_html.index("propose-change-reactome', function"):]
    assert "DataTable()" in handler[:400]
    assert "data('reactome-id')" not in handler[:400]


def test_go_rows_carry_their_current_scores(explore_html):
    """The GO entry is hand-built from data-* attributes, so the scores have to
    be emitted or currentAnswers() has nothing to read."""
    for attr in ("data-connection-score", "data-specificity-score",
                 "data-evidence-score"):
        assert attr in explore_html
