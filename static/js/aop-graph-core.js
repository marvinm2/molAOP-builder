/**
 * AOPGraphCore — Shared IIFE module for AOP graph rendering.
 *
 * Provides: resolveNodeColors, renderGraph, buildCytoscapeStyle, buildElements,
 *           findAOPsForKE, escapeHtml
 *
 * Consumed by:
 *   - aop-graph.js (standalone /aop-network adapter)
 *   - Future inline mapper adapter (Plan 02)
 *
 * Requires: Cytoscape.js to be loaded before this module.
 */
var AOPGraphCore = (function () {
    'use strict';

    // Register cytoscape-node-html-label plugin if available (CDN-loaded)
    if (typeof cytoscape !== 'undefined' && typeof cytoscapeNodeHtmlLabel !== 'undefined') {
        cytoscape.use(cytoscapeNodeHtmlLabel);
    }

    // Node color palette — resolved from CSS variables at init time
    // (Cytoscape style objects do not support CSS custom properties)
    var NODE_COLORS = { MIE: '#E6007E', KE: '#307BBF', AO: '#005A6C' };
    var EDGE_COLOR = '#29235C';

    // ---------------------------------------------------------------------------
    // CSS variable resolution
    // ---------------------------------------------------------------------------
    function getCSSVar(name) {
        var val = getComputedStyle(document.documentElement)
            .getPropertyValue(name)
            .trim();
        return val || undefined;
    }

    /**
     * Resolve NODE_COLORS and EDGE_COLOR from CSS custom properties.
     * Must be called after DOM is ready (usually in an adapter's init()).
     */
    function resolveNodeColors() {
        var mie  = getCSSVar('--color-primary-pink');
        var ke   = getCSSVar('--color-primary-blue');
        var ao   = getCSSVar('--color-secondary-teal');
        var edge = getCSSVar('--color-primary-dark');
        if (mie)  NODE_COLORS.MIE = mie;
        if (ke)   NODE_COLORS.KE  = ke;
        if (ao)   NODE_COLORS.AO  = ao;
        if (edge) EDGE_COLOR      = edge;
    }

    // ---------------------------------------------------------------------------
    // Style builder
    // ---------------------------------------------------------------------------

    /**
     * Build the Cytoscape style array.
     *
     * @param {Object} [options]
     * @param {Set}    [options.mappedKeIds]  When provided, add mapped/unmapped border selectors.
     * @returns {Array} Cytoscape style array
     */
    function buildCytoscapeStyle(options) {
        var styles = [
            {
                selector: 'node',
                style: {
                    'label': 'data(label)',
                    'text-wrap': 'wrap',
                    'text-max-width': '120px',
                    'font-size': '10px',
                    'text-valign': 'center',
                    'text-halign': 'center',
                    'width': 60,
                    'height': 60,
                    'shape': 'round-rectangle',
                    'color': '#fff',
                    'text-outline-width': 2,
                    'text-outline-color': '#333'
                }
            },
            {
                selector: 'node[type="MIE"]',
                style: {
                    'background-color': NODE_COLORS.MIE,
                    'text-outline-color': NODE_COLORS.MIE
                }
            },
            {
                selector: 'node[type="KE"]',
                style: {
                    'background-color': NODE_COLORS.KE,
                    'text-outline-color': NODE_COLORS.KE
                }
            },
            {
                selector: 'node[type="AO"]',
                style: {
                    'background-color': NODE_COLORS.AO,
                    'text-outline-color': NODE_COLORS.AO
                }
            },
            {
                selector: 'node.active-node',
                style: {
                    'border-width': 4,
                    'border-color': '#FFD700',
                    'border-style': 'solid',
                    'shadow-blur': 10,
                    'shadow-color': '#FFD700',
                    'shadow-opacity': 0.6
                }
            },
            {
                selector: 'edge',
                style: {
                    'width': 2,
                    'line-color': EDGE_COLOR,
                    'target-arrow-color': EDGE_COLOR,
                    'target-arrow-shape': 'triangle',
                    'curve-style': 'bezier',
                    'arrow-scale': 1.2
                }
            }
        ];

        // Optional mapping-status border indicators
        if (options && options.mappedKeIds) {
            styles.push({
                selector: 'node[?mapped]',
                style: {
                    'border-width': 4,
                    'border-color': '#2ecc71',
                    'border-style': 'solid'
                }
            });
            styles.push({
                selector: 'node[!mapped]',
                style: {
                    'border-width': 2,
                    'border-color': '#ccc',
                    'border-style': 'dashed'
                }
            });
        }

        return styles;
    }

    // ---------------------------------------------------------------------------
    // Element builder
    // ---------------------------------------------------------------------------

    /**
     * Build the Cytoscape elements array from an AOP entry.
     *
     * @param {Object} aopData      Single AOP entry from ker_adjacency JSON ({ kes, kers, title, ... })
     * @param {Set}    [mappedKeIds] When provided, sets data.mapped = true/false on each node element.
     * @returns {Array} Cytoscape elements array
     */
    function buildElements(aopData, mappedKeIds) {
        var elements = [];

        var kes = Array.isArray(aopData.kes) ? aopData.kes : [];
        kes.forEach(function (ke) {
            var nodeData = {
                id: ke.id,
                label: ke.title || ke.id,
                type: ke.type || 'KE'
            };
            if (mappedKeIds) {
                nodeData.mapped = mappedKeIds.has(ke.id);
            }
            elements.push({ data: nodeData });
        });

        var kers = Array.isArray(aopData.kers) ? aopData.kers : [];
        kers.forEach(function (ker, idx) {
            elements.push({
                data: {
                    id: 'ker-' + idx,
                    source: ker.upstream,
                    target: ker.downstream
                }
            });
        });

        return elements;
    }

    // ---------------------------------------------------------------------------
    // Graph renderer
    // ---------------------------------------------------------------------------

    /**
     * Create (or replace) a Cytoscape instance on the given container.
     *
     * @param {string} containerId  DOM id of the Cytoscape container element
     * @param {Object} aopData      Single AOP entry ({ kes, kers, title, ... })
     * @param {Object} [options]
     * @param {Set}              [options.mappedKeIds]    Passed to buildElements and buildCytoscapeStyle
     * @param {Function}         [options.onNodeTap]     Called with (nodeData, cyInstance) on node tap
     * @param {Function}         [options.onBackgroundTap] Called on tap on background
     * @param {Object}           [options.layoutOptions] Merged into the dagre layout config
     * @returns {Object} The Cytoscape instance (cy)
     */
    function renderGraph(containerId, aopData, options) {
        options = options || {};

        var container = document.getElementById(containerId);
        if (!container) {
            console.warn('[AOPGraphCore] Container not found:', containerId);
            return null;
        }

        // Destroy any previous Cytoscape instance on this container
        if (container._cy) {
            container._cy.destroy();
            container._cy = null;
        }

        var elements = buildElements(aopData, options.mappedKeIds || null);

        var layoutDefaults = {
            name: 'dagre',
            rankDir: 'LR',
            padding: 30,
            nodeSep: 50,
            rankSep: 100,
            animate: false
        };

        var layoutOptions = Object.assign({}, layoutDefaults, options.layoutOptions || {});

        var cy = cytoscape({
            container: container,
            elements: elements,
            style: buildCytoscapeStyle({ mappedKeIds: options.mappedKeIds || null }),
            layout: layoutOptions,
            userPanningEnabled: true,
            userZoomingEnabled: true,
            boxSelectionEnabled: false,
            maxZoom: 3,
            minZoom: 0.3
        });

        cy.on('layoutstop', function () {
            cy.fit(undefined, 30);
        });
        cy.fit(undefined, 30);
        cy.resize();

        // Wire tap events
        if (typeof options.onNodeTap === 'function') {
            cy.on('tap', 'node', function (evt) {
                var node = evt.target;
                cy.elements().removeClass('active-node');
                node.addClass('active-node');
                options.onNodeTap(node.data(), cy);
            });
        }

        if (typeof options.onBackgroundTap === 'function') {
            cy.on('tap', function (evt) {
                if (evt.target === cy) {
                    options.onBackgroundTap(cy);
                }
            });
        }

        // Store reference on container for cleanup on next call
        container._cy = cy;

        return cy;
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    /**
     * Find all AOP IDs that contain a given KE ID, excluding the specified AOP.
     *
     * @param {string} keId         KE ID to look up
     * @param {string} excludeAopId AOP ID to exclude (usually the currently-shown AOP)
     * @param {Object} kerData      Full ker_adjacency data object
     * @returns {string[]} Matching AOP IDs
     */
    function findAOPsForKE(keId, excludeAopId, kerData) {
        if (!kerData) return [];
        return Object.keys(kerData).filter(function (aopId) {
            if (aopId === '_metadata') return false;
            if (aopId === excludeAopId) return false;
            var aop = kerData[aopId];
            if (!Array.isArray(aop.kes)) return false;
            return aop.kes.some(function (ke) {
                return ke.id === keId;
            });
        });
    }

    /**
     * Minimal HTML escape to prevent XSS when setting innerHTML.
     *
     * @param {string} str
     * @returns {string}
     */
    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Apply gene-count badge overlays to graph nodes via cytoscape-node-html-label.
     * Call AFTER renderGraph() returns a cy instance.
     *
     * @param {Object} cy            Cytoscape instance
     * @param {Object} geneCountMap  {ke_id: count} — only KEs with genes
     */
    function applyGeneBadges(cy, geneCountMap) {
        applyNodeOverlays(cy, { geneCountMap: geneCountMap });
    }

    /**
     * Build the coverage-dot markup for one KE (#190).
     *
     * Three dots, one per resource, in a fixed left-to-right order so position
     * alone identifies the resource. A covered resource gets a filled dot, an
     * uncovered one a hollow dashed dot — coverage is encoded by fill AND by
     * the letter inside, never by colour alone, so the indicator survives any
     * form of colour blindness and greyscale printing.
     *
     * @param {string} keId
     * @param {Object|Function} coverage  { wp: Set, go: Set, reactome: Set }, or a
     *        function returning that object. The caller's Sets are REASSIGNED when
     *        the /api/mapped-ke-ids fetches land, so a captured reference goes
     *        stale; pass a function to have them resolved at render time instead.
     * @returns {string} HTML, or '' when no coverage data has loaded yet
     */
    function coverageDotsHtml(keId, coverage) {
        var RESOURCES = [
            { key: 'wp',       letter: 'W', name: 'WikiPathways' },
            { key: 'go',       letter: 'G', name: 'GO' },
            { key: 'reactome', letter: 'R', name: 'Reactome' }
        ];

        coverage = (typeof coverage === 'function') ? coverage() : coverage;
        if (!coverage) return '';

        // Nothing to say until at least one resource set has arrived.
        var haveData = RESOURCES.some(function (r) {
            return coverage[r.key] && typeof coverage[r.key].has === 'function';
        });
        if (!haveData) return '';

        var summary = [];
        var dots = RESOURCES.map(function (r) {
            var set = coverage[r.key];
            var known = set && typeof set.has === 'function';
            var covered = known && set.has(keId);
            var cls = 'ke-coverage-dot ke-coverage-dot--' + r.key +
                      (covered ? ' is-covered' : ' is-uncovered');
            var title = r.name + ': ' + (covered ? 'mapped' : 'not mapped');
            summary.push(title);
            // The letter is not announced separately — it is a visual shorthand
            // for the resource, and the group's aria-label already spells it out.
            return '<span class="' + cls + '" title="' + escapeHtml(title) + '"' +
                   ' aria-hidden="true">' + escapeHtml(r.letter) + '</span>';
        }).join('');

        // Labelled as a single image rather than hidden: the dots are the only
        // place per-resource coverage is stated on the graph, so hiding them
        // would drop that information for screen-reader users entirely.
        return '<div class="ke-coverage-dots" role="img" aria-label="' +
               escapeHtml('Mapping coverage — ' + summary.join('; ')) + '">' +
               dots + '</div>';
    }

    /**
     * Apply the node HTML overlays: the gene-count badge and the per-resource
     * coverage dots (#190).
     *
     * Both overlays go through a single nodeHtmlLabel() call. The plugin
     * replaces its label set on each invocation, so registering them
     * separately would silently drop whichever was registered first.
     *
     * @param {Object} cy
     * @param {Object} [options]
     * @param {Object} [options.geneCountMap]  { keId: count }
     * @param {Object|Function} [options.coverage]  { wp: Set, go: Set, reactome: Set },
     *        or a function returning it (preferred — see coverageDotsHtml).
     */
    function applyNodeOverlays(cy, options) {
        options = options || {};
        var geneCountMap = options.geneCountMap;
        var coverage = options.coverage;

        if (!cy) return;
        if (!geneCountMap && !coverage) return;
        if (typeof cy.nodeHtmlLabel !== 'function') {
            console.warn('[AOPGraphCore] nodeHtmlLabel not available — badge plugin not loaded');
            return;
        }

        var labels = [];

        if (geneCountMap) {
            labels.push({
                query: 'node',
                halign: 'right',
                valign: 'top',
                halignBox: 'left',
                valignBox: 'bottom',
                cssClass: 'gene-badge-container',
                tpl: function (data) {
                    var count = geneCountMap[data.id];
                    if (!count || count === 0) return '';
                    return '<div class="gene-badge">' + escapeHtml(String(count)) + '</div>';
                }
            });
        }

        if (coverage) {
            labels.push({
                query: 'node',
                halign: 'center',
                valign: 'bottom',
                halignBox: 'center',
                valignBox: 'bottom',
                cssClass: 'ke-coverage-container',
                tpl: function (data) {
                    return coverageDotsHtml(data.id, coverage);
                }
            });
        }

        cy.nodeHtmlLabel(labels);
    }

    // ---------------------------------------------------------------------------
    // Public API
    // ---------------------------------------------------------------------------
    return {
        resolveNodeColors: resolveNodeColors,
        renderGraph: renderGraph,
        buildCytoscapeStyle: buildCytoscapeStyle,
        buildElements: buildElements,
        findAOPsForKE: findAOPsForKE,
        escapeHtml: escapeHtml,
        applyGeneBadges: applyGeneBadges,
        applyNodeOverlays: applyNodeOverlays,
        coverageDotsHtml: coverageDotsHtml
    };
})();
