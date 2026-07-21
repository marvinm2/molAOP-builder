"""
Static smoke tests for the AOP Explorer per-resource coverage dots (#190).

Grep-style assertions against the graph JS/CSS/template — they do not execute
the JS, but they pin the fix's structure so a refactor cannot silently drop it.

Before this change a KE node carried a single green border derived only from
WikiPathways coverage, so a curator could not tell which resources a KE was
actually mapped in. Each node now carries three dots (WikiPathways, GO,
Reactome).

The subtle parts worth pinning, beyond "the dots exist":

- Both node overlays (gene badge + coverage dots) must register in ONE
  nodeHtmlLabel call, because the plugin replaces its whole label set per
  invocation — two calls would silently drop the first.
- Coverage must resolve lazily. The three Sets in aop-graph.js are REASSIGNED
  when the /api/mapped-ke-ids fetches land, so an eagerly-captured reference
  would pin stale (empty) coverage on a graph rendered before they arrive.
- "Not loaded yet" must not render as "not mapped" — an empty Set is
  indistinguishable from absent coverage unless a load flag guards it.
- Coverage must not be conveyed by colour alone (accessibility criterion in
  the issue).
"""
import os
import re

HERE = os.path.dirname(__file__)
CORE_JS = os.path.join(HERE, "..", "static", "js", "aop-graph-core.js")
GRAPH_JS = os.path.join(HERE, "..", "static", "js", "aop-graph.js")
GRAPH_CSS = os.path.join(HERE, "..", "static", "css", "aop-graph.css")
EXPLORER_HTML = os.path.join(HERE, "..", "templates", "aop-explorer.html")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_core_exposes_coverage_helpers():
    """The core module publishes the overlay API the page module consumes."""
    body = _read(CORE_JS)

    assert "coverageDotsHtml: coverageDotsHtml" in body
    assert "applyNodeOverlays: applyNodeOverlays" in body
    # applyGeneBadges stays exported — existing callers and tests depend on it.
    assert "applyGeneBadges: applyGeneBadges" in body


def test_all_three_resources_are_represented():
    """A dot per resource, each with a distinguishing letter."""
    body = _read(CORE_JS)

    for key, letter in [("wp", "W"), ("go", "G"), ("reactome", "R")]:
        assert "key: '%s'" % key in body, "no coverage dot for %s" % key
        assert "letter: '%s'" % letter in body, "no letter for %s" % key


def test_overlays_register_in_a_single_nodehtmllabel_call():
    """Two nodeHtmlLabel calls would drop one overlay — there must be exactly one."""
    body = _read(CORE_JS)

    calls = re.findall(r"cy\.nodeHtmlLabel\(", body)
    assert len(calls) == 1, (
        "expected exactly one cy.nodeHtmlLabel() call so the gene badge and "
        "coverage dots share a label set; found %d" % len(calls)
    )


def test_coverage_is_resolved_lazily():
    """A captured Set reference would go stale when the fetches reassign it."""
    core = _read(CORE_JS)
    graph = _read(GRAPH_JS)

    assert "typeof coverage === 'function'" in core, (
        "coverageDotsHtml must accept a function so coverage resolves at render time"
    )
    assert re.search(r"coverage:\s*function\s*\(\)", graph), (
        "aop-graph.js must pass coverage as a function, not a captured object"
    )


def test_unloaded_coverage_is_not_rendered_as_uncovered():
    """An empty Set must not be reported as 'this KE has no mappings'."""
    graph = _read(GRAPH_JS)
    core = _read(CORE_JS)

    assert "mappedKeIdsLoaded" in graph, "no load flag guarding the coverage dots"
    assert "if (!mappedKeIdsLoaded) return null;" in graph, (
        "coverage callback must return null until the mapped-KE sets have loaded"
    )
    # And the core must treat a null/absent coverage object as "render nothing".
    assert "if (!coverage) return '';" in core


def test_overlays_refresh_when_data_lands_after_render():
    """The user can pick an AOP before the fetches finish."""
    graph = _read(GRAPH_JS)

    assert "function refreshNodeOverlays()" in graph
    # Called from the render path and from both loader callbacks.
    assert graph.count("refreshNodeOverlays()") >= 3, (
        "refreshNodeOverlays must be called after render and when each loader lands"
    )


def test_coverage_is_not_conveyed_by_colour_alone():
    """Accessibility criterion from #190: fill + border style + letter."""
    css = _read(GRAPH_CSS)

    assert ".ke-coverage-dot.is-covered" in css
    assert ".ke-coverage-dot.is-uncovered" in css
    # The uncovered state differs structurally, not just in hue.
    uncovered = css.split(".ke-coverage-dot.is-uncovered")[1].split("}")[0]
    assert "dashed" in uncovered, (
        "uncovered dots must differ by border style, not colour alone"
    )
    # And the letter is always present in the markup.
    core = _read(CORE_JS)
    assert "escapeHtml(r.letter)" in core


def test_dots_use_the_house_palette():
    """VHP4Safety palette — no new colours introduced."""
    css = _read(GRAPH_CSS)

    assert "--color-primary-blue" in css.split(".ke-coverage-dot--wp.is-covered")[1].split("}")[0]
    assert "--color-primary-pink" in css.split(".ke-coverage-dot--go.is-covered")[1].split("}")[0]
    assert "--color-secondary-teal" in (
        css.split(".ke-coverage-dot--reactome.is-covered")[1].split("}")[0]
    )


def test_legend_explains_the_dots():
    """A legend keyed to the indicator, alongside the existing gene-badge legend."""
    html = _read(EXPLORER_HTML)

    assert "ke-coverage-dot--wp" in html
    assert "ke-coverage-dot--go" in html
    assert "ke-coverage-dot--reactome" in html
    assert "Mapping coverage per resource" in html
    # Both states are shown so the legend teaches the filled/hollow distinction.
    assert "is-covered" in html and "is-uncovered" in html


def test_titles_are_escaped():
    """Tooltip text goes through the shared escaper (Phase 25 sentinel)."""
    core = _read(CORE_JS)

    assert "escapeHtml(title)" in core


def test_coverage_is_announced_to_screen_readers():
    """The dots are the only per-resource statement on the graph, so they must
    be labelled rather than hidden."""
    core = _read(CORE_JS)

    assert 'role="img"' in core, "coverage group should be an labelled image, not hidden"
    assert "aria-label=" in core
    assert "Mapping coverage" in core
    # The individual letters stay out of the accessibility tree — the group
    # label already spells the resources out.
    assert core.count('aria-hidden="true"') >= 1


def test_node_border_reflects_any_resource_not_just_wikipathways():
    """#190's core complaint: the green border was derived from WP coverage
    alone, so a KE mapped only in GO or Reactome looked unmapped."""
    graph = _read(GRAPH_JS)

    assert "function anyMappedKeIds()" in graph
    assert "mappedKeIds: anyMappedKeIds()" in graph, (
        "renderGraph must pass the union of all three resources, not wpMappedKeIds"
    )
    assert not re.search(r"mappedKeIds:\s*wpMappedKeIds", graph), (
        "border is still driven by WikiPathways coverage alone"
    )
    # And the border re-derives when the sets land after a render.
    assert "node.data('mapped'" in graph
