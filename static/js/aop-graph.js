/**
 * AOP Explorer Graph — Standalone Adapter
 * Consumes AOPGraphCore (aop-graph-core.js) for all graph rendering logic.
 *
 * Handles page-specific concerns: card grid, Select2 dropdown, AOP selection
 * flow, side panel population, gap filter, OECD badge/filter, and KE redirect.
 *
 * Requires: aop-graph-core.js loaded before this script.
 */

// OECD development-status colour ramp — all 8 OECD buckets enumerated for
// forward-compatibility even if live data only populates a subset.
var OECD_STATUS_CONFIG = {
    ordinal: {
        'Under Development: Contributions and Comments Welcome': 1,
        'Under Development': 2,
        'Open for Adoption': 3,
        'Under Review / Internal Review': 3,
        'Under Review': 3,
        'EAGMST Under Review': 4,
        'EAGMST Approved': 5,
        'WPHA/WNT Endorsed': 6,
        'Unknown': 0
    },
    color: {
        0: '#9e9e9e',
        1: '#d32f2f',
        2: '#e64a19',
        3: '#f57c00',
        4: '#fbc02d',
        5: '#7cb342',
        6: '#388e3c'
    }
};

function oecdBadgeColor(status) {
    var ord = OECD_STATUS_CONFIG.ordinal[status];
    var ordinal = (ord !== undefined) ? ord : 0;
    return OECD_STATUS_CONFIG.color[ordinal] || OECD_STATUS_CONFIG.color[0];
}

var AOPGraph = (function () {
    'use strict';

    // Private state
    var cy = null;
    var kerData = null;
    var currentAOP = null;
    var selectedKEId = null;
    var geneCountMap = {};

    // Mapped KE ID sets (one per resource) — populated by loadMappedKeIds().
    // They start empty so the gap filter can call .has() safely before the
    // fetches land; mappedKeIdsLoaded distinguishes "not loaded yet" from
    // "loaded and genuinely empty", which the coverage dots must not conflate —
    // an empty Set would otherwise render every KE as uncovered (#190).
    var wpMappedKeIds = new Set();
    var goMappedKeIds = new Set();
    var reactomeMappedKeIds = new Set();
    var mappedKeIdsLoaded = false;

    // Gap filter state — sticky across AOP switches
    var currentGapFilter = 'all';

    // OECD status data — keyed by "AOP N"
    var oecdStatusData = {};

    // ---------------------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------------------
    function init() {
        AOPGraphCore.resolveNodeColors();
        wireBackButton();
        wireCloseButton();
        wireGapFilterButtons();
        loadData();
        loadOecdStatus();
    }

    function loadData() {
        fetch('/api/ker-adjacency')
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error('HTTP ' + resp.status);
                }
                return resp.json();
            })
            .then(function (data) {
                kerData = data;
                populateSelect2(data);
                populateCardGrid(data);
                // Both loaders are applied on every subsequent graph render, but
                // a graph may already be on screen when they land (the user can
                // pick an AOP before the fetches finish), so refresh in place too.
                loadGeneCountMap(function () {
                    refreshNodeOverlays();
                });
                loadMappedKeIds(function () {
                    refreshNodeOverlays();
                    applyGapFilter(currentGapFilter);
                });
            })
            .catch(function (err) {
                var grid = document.getElementById('aop-card-grid');
                if (grid) {
                    grid.innerHTML =
                        '<div class="aop-card-loading" style="color:#c0392b;">' +
                        'Failed to load AOP data. Please try refreshing the page.<br>' +
                        '<small>' + err.message + '</small></div>';
                }
                console.error('[AOPGraph] Failed to load /api/ker-adjacency:', err);
            });
    }

    function renderGeneGroups(container, groups) {
        groups.forEach(function (group) {
            var div = document.createElement('div');
            div.className = 'gene-group';
            var header = document.createElement('div');
            header.className = 'gene-group__header';
            var arrow = document.createElement('span');
            arrow.className = 'gene-group__arrow';
            arrow.textContent = '\u25B6';
            var typeBadge = document.createElement('span');
            typeBadge.className = 'gene-group__type-badge gene-group__type-badge--' + group.type;
            typeBadge.textContent = group.type.toUpperCase();
            var nameSpan = document.createElement('span');
            nameSpan.textContent = group.name;
            nameSpan.style.flex = '1';
            nameSpan.style.overflow = 'hidden';
            nameSpan.style.textOverflow = 'ellipsis';
            nameSpan.style.whiteSpace = 'nowrap';
            nameSpan.title = group.name;
            var countSpan = document.createElement('span');
            countSpan.className = 'gene-group__count';
            countSpan.textContent = '(' + group.genes.length + ')';
            var confBadge = document.createElement('span');
            var level = (group.confidence_level || 'low').toLowerCase();
            confBadge.className = 'confidence-' + level;
            confBadge.textContent = level.charAt(0).toUpperCase() + level.slice(1);
            confBadge.style.fontSize = '11px';
            confBadge.style.marginLeft = '4px';
            header.appendChild(arrow);
            header.appendChild(typeBadge);
            header.appendChild(nameSpan);
            header.appendChild(confBadge);
            header.appendChild(countSpan);
            var ul = document.createElement('ul');
            ul.className = 'gene-group__genes';
            group.genes.forEach(function (symbol) {
                var li = document.createElement('li');
                var a = document.createElement('a');
                a.href = 'https://www.genecards.org/cgi-bin/carddisp.pl?gene=' + encodeURIComponent(symbol);
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                a.textContent = symbol;
                li.appendChild(a);
                ul.appendChild(li);
            });
            header.addEventListener('click', function () {
                div.classList.toggle('gene-group--open');
            });
            div.appendChild(header);
            div.appendChild(ul);
            container.appendChild(div);
        });
    }

    function loadGeneCountMap(callback) {
        fetch('/api/ke-gene-counts')
            .then(function (resp) { return resp.ok ? resp.json() : {}; })
            .then(function (data) {
                geneCountMap = data || {};
                callback();
            })
            .catch(function () {
                geneCountMap = {};
                callback();
            });
    }

    // Fetch all three mapped-KE-ID sets from the backend (one request per resource).
    // Mirrors loadGeneCountMap() — caches into module-level Sets, fires callback when done.
    function loadMappedKeIds(callback) {
        var resources = [
            { type: 'wp',       target: 'wpMappedKeIds' },
            { type: 'go',       target: 'goMappedKeIds' },
            { type: 'reactome', target: 'reactomeMappedKeIds' }
        ];
        var pending = resources.length;

        function done() {
            pending -= 1;
            if (pending === 0) {
                mappedKeIdsLoaded = true;
                if (callback) { callback(); }
            }
        }

        resources.forEach(function (res) {
            fetch('/api/mapped-ke-ids?type=' + res.type)
                .then(function (resp) { return resp.ok ? resp.json() : { ke_ids: [] }; })
                .then(function (data) {
                    var ids = Array.isArray(data.ke_ids) ? data.ke_ids : [];
                    if (res.type === 'wp')       { wpMappedKeIds       = new Set(ids); }
                    if (res.type === 'go')       { goMappedKeIds       = new Set(ids); }
                    if (res.type === 'reactome') { reactomeMappedKeIds = new Set(ids); }
                    done();
                })
                .catch(function () {
                    done();
                });
        });
    }

    // Load OECD development-status data and wire the OECD multi-select filter.
    function loadOecdStatus() {
        fetch('/api/aop-oecd-status')
            .then(function (resp) { return resp.ok ? resp.json() : {}; })
            .then(function (data) {
                oecdStatusData = data || {};
                // Render OECD badges on AOP cards after card grid is populated
                // (cards are rendered asynchronously — use a small poll to wait)
                renderOecdBadgesOnCards();
                wireOecdFilter();
            })
            .catch(function () {
                oecdStatusData = {};
            });
    }

    function oecdBadgeHtml(aopId) {
        var entry = oecdStatusData[aopId];
        var status = (entry && entry.status) ? entry.status : 'Unknown';
        var color = oecdBadgeColor(status);
        var label = status.length > 32 ? status.slice(0, 30) + '…' : status;
        return '<span class="aop-oecd-badge" style="background:' + color + ';" ' +
               'title="OECD development status: ' + AOPGraphCore.escapeHtml(status) + '">' +
               AOPGraphCore.escapeHtml(label) + '</span>';
    }

    function renderOecdBadgesOnCards() {
        var grid = document.getElementById('aop-card-grid');
        if (!grid) return;
        var cards = grid.querySelectorAll('.aop-card');
        if (cards.length === 0) {
            // Cards not yet rendered — retry after short delay
            setTimeout(renderOecdBadgesOnCards, 200);
            return;
        }
        cards.forEach(function (card) {
            var aopId = card.getAttribute('data-aop-id');
            if (!aopId) return;
            var existing = card.querySelector('.aop-oecd-badge');
            if (existing) return; // already rendered
            card.insertAdjacentHTML('beforeend', oecdBadgeHtml(aopId));
        });
    }

    function wireOecdFilter() {
        var sel = document.getElementById('oecd-status-filter');
        if (!sel) return;

        // Collect distinct statuses actually present in the loaded data,
        // normalising the "Under Review" alias into "Under Review / Internal Review".
        var presentSet = {};
        Object.keys(oecdStatusData).forEach(function (aopKey) {
            if (aopKey === '_meta') return;
            var entry = oecdStatusData[aopKey];
            if (!entry) return;
            var st = (entry.status && entry.status !== '') ? entry.status : 'Unknown';
            // Collapse alias so both map to the canonical bucket
            if (st === 'Under Review') { st = 'Under Review / Internal Review'; }
            presentSet[st] = true;
        });

        // Always include 'Unknown' when data is empty or any AOP lacks a status
        if (Object.keys(oecdStatusData).filter(function (k) { return k !== '_meta'; }).length === 0) {
            // No data at all — no options; the filter stays empty without erroring
            return;
        }

        // Sort present statuses by OECD_STATUS_CONFIG ordinal so dropdown is logically ordered
        var presentStatuses = Object.keys(presentSet).sort(function (a, b) {
            var ordA = OECD_STATUS_CONFIG.ordinal[a];
            var ordB = OECD_STATUS_CONFIG.ordinal[b];
            if (ordA === undefined) ordA = 0;
            if (ordB === undefined) ordB = 0;
            return ordA - ordB;
        });

        // Populate options — all selected by default
        presentStatuses.forEach(function (status) {
            var opt = document.createElement('option');
            opt.value = status;
            opt.textContent = status;
            opt.selected = true;
            sel.appendChild(opt);
        });

        // Init Select2
        if (window.$ && $.fn.select2) {
            $(sel).select2({
                placeholder: 'Filter by OECD status...',
                width: '100%',
                closeOnSelect: false
            });
            $(sel).on('change', applyOecdFilter);
        } else {
            sel.addEventListener('change', applyOecdFilter);
        }
    }

    function applyOecdFilter() {
        var sel = document.getElementById('oecd-status-filter');
        if (!sel) return;
        var selected = Array.from(sel.options)
            .filter(function (o) { return o.selected; })
            .map(function (o) { return o.value; });

        // Include "Under Review" alias alongside "Under Review / Internal Review"
        if (selected.indexOf('Under Review / Internal Review') > -1) {
            selected.push('Under Review');
        }

        var grid = document.getElementById('aop-card-grid');
        if (!grid) return;
        var cards = grid.querySelectorAll('.aop-card');
        cards.forEach(function (card) {
            var aopId = card.getAttribute('data-aop-id');
            var entry = aopId ? oecdStatusData[aopId] : null;
            var status = (entry && entry.status) ? entry.status : 'Unknown';
            card.style.display = (selected.indexOf(status) > -1) ? '' : 'none';
        });
    }

    // ---------------------------------------------------------------------------
    // Card grid population
    // ---------------------------------------------------------------------------
    function populateCardGrid(data) {
        var grid = document.getElementById('aop-card-grid');
        if (!grid) return;

        // Clear loading placeholder
        grid.innerHTML = '';

        var aopIds = Object.keys(data).filter(function (k) {
            return k !== '_metadata';
        });

        if (aopIds.length === 0) {
            grid.innerHTML = '<div class="aop-card-loading">No AOP data available.</div>';
            return;
        }

        var fragment = document.createDocumentFragment();

        aopIds.forEach(function (aopId) {
            var aop = data[aopId];
            var keCount = Array.isArray(aop.kes) ? aop.kes.length : 0;
            var card = document.createElement('div');
            card.className = 'aop-card';
            card.setAttribute('data-aop-id', aopId);
            card.setAttribute('tabindex', '0');
            card.setAttribute('role', 'button');
            card.setAttribute('aria-label', 'View graph for ' + aopId);
            card.innerHTML =
                '<div class="aop-card__id">' + AOPGraphCore.escapeHtml(aopId) + '</div>' +
                '<div class="aop-card__title">' + AOPGraphCore.escapeHtml(aop.title || '') + '</div>' +
                '<div class="aop-card__ke-count">' + keCount + ' KE' + (keCount !== 1 ? 's' : '') + '</div>';

            card.addEventListener('click', function () {
                selectAOP(aopId);
            });
            card.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    selectAOP(aopId);
                }
            });

            fragment.appendChild(card);
        });

        grid.appendChild(fragment);
    }

    // ---------------------------------------------------------------------------
    // Select2 AOP dropdown
    // ---------------------------------------------------------------------------
    function populateSelect2(data) {
        var aopIds = Object.keys(data).filter(function (k) {
            return k !== '_metadata';
        });

        var selectData = aopIds.map(function (aopId) {
            var aop = data[aopId];
            return {
                id: aopId,
                text: aopId + ' \u2014 ' + (aop.title || aopId)
            };
        });

        var $select = $('#aop-selector');
        if ($select.length === 0) return;

        // Destroy any existing Select2, re-init with data
        if ($select.data('select2')) {
            $select.select2('destroy');
        }

        $select.select2({
            placeholder: 'Search by AOP title or ID...',
            allowClear: true,
            width: '100%',
            data: selectData,
            dropdownCssClass: 'aop-selector-dropdown'
        });

        $select.on('select2:select', function (e) {
            var aopId = e.params.data.id;
            selectAOP(aopId);
        });

        $select.on('select2:clear', function () {
            showCardGrid();
        });
    }

    // ---------------------------------------------------------------------------
    // AOP selection flow
    // ---------------------------------------------------------------------------
    function selectAOP(aopId) {
        if (!kerData || !kerData[aopId]) {
            console.warn('[AOPGraph] Unknown AOP:', aopId);
            return;
        }

        currentAOP = aopId;

        // Update Select2 to reflect selection
        var $select = $('#aop-selector');
        if ($select.length && $select.data('select2')) {
            $select.val(aopId).trigger('change.select2');
        }

        // Hide card grid, show graph section
        var cardSection = document.getElementById('aop-card-grid-section');
        var graphSection = document.getElementById('aop-graph-section');
        if (cardSection) cardSection.style.display = 'none';
        if (graphSection) graphSection.style.display = '';

        // Dismiss any open side panel before rendering new graph
        dismissSidePanel();

        // Render graph (must happen AFTER container is visible — see lazy Cytoscape init note)
        renderGraph(aopId);
    }

    // ---------------------------------------------------------------------------
    // Back to card grid
    // ---------------------------------------------------------------------------
    function showCardGrid() {
        // Destroy existing Cytoscape instance to free memory
        if (cy) {
            cy.destroy();
            cy = null;
        }

        currentAOP = null;
        selectedKEId = null;

        dismissSidePanel();

        // Reset Select2
        var $select = $('#aop-selector');
        if ($select.length && $select.data('select2')) {
            $select.val(null).trigger('change.select2');
        }

        var cardSection = document.getElementById('aop-card-grid-section');
        var graphSection = document.getElementById('aop-graph-section');
        if (graphSection) graphSection.style.display = 'none';
        if (cardSection) cardSection.style.display = '';
    }

    function wireBackButton() {
        var btn = document.getElementById('aop-back-btn');
        if (btn) {
            btn.addEventListener('click', showCardGrid);
        }
    }

    // ---------------------------------------------------------------------------
    // Gap filter — applied within the current graph
    // ---------------------------------------------------------------------------
    function applyGapFilter(filterMode) {
        currentGapFilter = filterMode;
        if (!cy) return;

        cy.batch(function () {
            cy.nodes().forEach(function (node) {
                var keId = node.data('id');
                var show = false;
                switch (filterMode) {
                    case 'all':
                        show = true;
                        break;
                    case 'unmapped':
                        show = !wpMappedKeIds.has(keId) &&
                               !goMappedKeIds.has(keId) &&
                               !reactomeMappedKeIds.has(keId);
                        break;
                    case 'gap-wp':
                        show = !wpMappedKeIds.has(keId);
                        break;
                    case 'gap-go':
                        show = !goMappedKeIds.has(keId);
                        break;
                    case 'gap-reactome':
                        show = !reactomeMappedKeIds.has(keId);
                        break;
                    default:
                        show = true;
                }
                node.style('opacity', show ? 1 : 0.2);
            });
            // Never dim edges
            cy.edges().style('opacity', 1);
        });

        updateGapFilterCounts();
    }

    function updateGapFilterCounts() {
        if (!cy) return;
        var nodes = cy.nodes();
        var counts = {
            all:          nodes.length,
            unmapped:     0,
            'gap-wp':     0,
            'gap-go':     0,
            'gap-reactome': 0
        };
        nodes.forEach(function (node) {
            var keId = node.data('id');
            if (!wpMappedKeIds.has(keId) && !goMappedKeIds.has(keId) && !reactomeMappedKeIds.has(keId)) {
                counts.unmapped++;
            }
            if (!wpMappedKeIds.has(keId))       { counts['gap-wp']++; }
            if (!goMappedKeIds.has(keId))       { counts['gap-go']++; }
            if (!reactomeMappedKeIds.has(keId)) { counts['gap-reactome']++; }
        });

        document.querySelectorAll('.gap-filter-btn').forEach(function (btn) {
            var filter = btn.getAttribute('data-filter');
            var countEl = btn.querySelector('.gap-filter-count');
            if (countEl && counts[filter] !== undefined) {
                countEl.textContent = '(' + counts[filter] + ')';
            }
        });
    }

    function wireGapFilterButtons() {
        document.addEventListener('click', function (e) {
            var btn = e.target.closest('.gap-filter-btn');
            if (!btn) return;
            document.querySelectorAll('.gap-filter-btn').forEach(function (b) {
                b.classList.remove('active');
            });
            btn.classList.add('active');
            applyGapFilter(btn.getAttribute('data-filter'));
        });
    }

    // ---------------------------------------------------------------------------
    // Graph rendering — delegates to AOPGraphCore
    // ---------------------------------------------------------------------------

    /**
     * (Re-)register the node HTML overlays on the current graph: the gene-count
     * badge (AOPX-04) and the per-resource coverage dots (#190).
     *
     * Called both after a render and when either loader lands, since the user
     * can select an AOP before the fetches complete. Both overlays go through
     * one AOPGraphCore.applyNodeOverlays call because the nodeHtmlLabel plugin
     * replaces its whole label set per invocation.
     */
    /**
     * KEs mapped in at least one resource.
     *
     * The node border used to be driven by wpMappedKeIds alone, so a KE mapped
     * only in GO or Reactome was drawn as unmapped (#190). Now that the dots
     * state each resource individually, the border carries the "any coverage"
     * summary and no longer contradicts them.
     */
    function anyMappedKeIds() {
        var union = new Set();
        [wpMappedKeIds, goMappedKeIds, reactomeMappedKeIds].forEach(function (set) {
            if (set && typeof set.forEach === 'function') {
                set.forEach(function (id) { union.add(id); });
            }
        });
        return union;
    }

    function refreshNodeOverlays() {
        if (!cy) return;

        // Re-derive the border state too: the sets may have landed after this
        // graph was rendered, and node data.mapped was fixed at build time.
        if (mappedKeIdsLoaded) {
            var mapped = anyMappedKeIds();
            cy.nodes().forEach(function (node) {
                node.data('mapped', mapped.has(node.id()));
            });
        }

        AOPGraphCore.applyNodeOverlays(cy, {
            geneCountMap: Object.keys(geneCountMap).length > 0 ? geneCountMap : null,
            // Resolved lazily: the three Sets are reassigned when the
            // /api/mapped-ke-ids fetches land, so a captured reference would
            // pin stale coverage. Null until loaded, so a KE is never drawn as
            // "not mapped" merely because the data has not arrived.
            coverage: function () {
                if (!mappedKeIdsLoaded) return null;
                return {
                    wp: wpMappedKeIds,
                    go: goMappedKeIds,
                    reactome: reactomeMappedKeIds
                };
            }
        });
    }

    function renderGraph(aopId) {
        var aopData = kerData[aopId];
        if (!aopData) return;

        // Destroy previous instance if any
        if (cy) {
            cy.destroy();
            cy = null;
        }

        // Pass mappedKeIds so AOPGraphCore adds green-border styling on mapped KEs (AOPX-05)
        cy = AOPGraphCore.renderGraph('cy', aopData, {
            // Border = mapped in ANY resource; the per-resource detail is on
            // the coverage dots (#190). Previously this was WP-only, which drew
            // a GO-or-Reactome-only KE as if it had no mappings at all.
            mappedKeIds: anyMappedKeIds(),
            onNodeTap: function (nodeData, cyInst) {
                cyInst.elements().removeClass('active-node');
                cyInst.$('#' + nodeData.id).addClass('active-node');
                showSidePanel(nodeData);
            },
            onBackgroundTap: function () {
                dismissSidePanel();
            }
        });

        refreshNodeOverlays();

        // Re-apply sticky gap filter and update counts
        applyGapFilter(currentGapFilter);

        // Update OECD badge in graph header
        var headerBadge = document.getElementById('aop-oecd-badge');
        if (headerBadge) {
            headerBadge.innerHTML = oecdBadgeHtml(aopId);
        }
    }

    // ---------------------------------------------------------------------------
    // Side panel
    // ---------------------------------------------------------------------------
    function showSidePanel(nodeData) {
        var panel = document.getElementById('ke-side-panel');
        if (!panel) return;

        selectedKEId = nodeData.id;

        // Populate type badge
        var typeEl = document.getElementById('ke-panel-type');
        if (typeEl) {
            var type = nodeData.type || 'KE';
            var typeClass = 'ke-side-panel__ke-type ke-side-panel__ke-type--' + type.toLowerCase();
            typeEl.className = typeClass;
            typeEl.textContent = type;
        }

        // Populate title
        var titleEl = document.getElementById('ke-panel-title');
        if (titleEl) {
            titleEl.textContent = nodeData.label || nodeData.id;
        }

        // Populate ID
        var idEl = document.getElementById('ke-panel-id');
        if (idEl) {
            idEl.textContent = nodeData.id;
        }

        // Populate "Also in these AOPs" list
        var listEl = document.getElementById('ke-panel-aop-list');
        if (listEl && kerData) {
            var otherAOPs = AOPGraphCore.findAOPsForKE(nodeData.id, currentAOP, kerData);
            listEl.innerHTML = '';
            if (otherAOPs.length === 0) {
                var li = document.createElement('li');
                li.textContent = 'This KE is not in any other AOPs.';
                li.style.color = '#888';
                li.style.fontStyle = 'italic';
                listEl.appendChild(li);
            } else {
                otherAOPs.forEach(function (aopId) {
                    var li = document.createElement('li');
                    li.textContent = aopId;
                    if (kerData[aopId] && kerData[aopId].title) {
                        li.title = kerData[aopId].title;
                    }
                    listEl.appendChild(li);
                });
            }
        }

        // Populate gene list section (grouped by WP/GO term)
        var geneListEl = document.getElementById('ke-panel-gene-list');
        var geneLoadingEl = document.getElementById('ke-panel-gene-loading');
        if (geneListEl) {
            geneListEl.innerHTML = '';
            var count = geneCountMap[nodeData.id] || 0;
            if (count > 0 && geneLoadingEl) {
                geneLoadingEl.textContent = 'Loading ' + count + ' gene(s)...';
                geneLoadingEl.style.display = '';
                fetch('/api/ke-genes/' + encodeURIComponent(nodeData.id))
                    .then(function (resp) { return resp.ok ? resp.json() : { genes: [], groups: [] }; })
                    .then(function (data) {
                        geneLoadingEl.style.display = 'none';
                        var groups = data.groups || [];
                        if (groups.length > 0) {
                            renderGeneGroups(geneListEl, groups);
                        } else {
                            var li = document.createElement('li');
                            li.textContent = 'No mapped genes';
                            li.style.color = '#888';
                            li.style.fontStyle = 'italic';
                            geneListEl.appendChild(li);
                        }
                    })
                    .catch(function () {
                        geneLoadingEl.style.display = 'none';
                    });
            } else {
                if (geneLoadingEl) geneLoadingEl.style.display = 'none';
                var li = document.createElement('li');
                li.textContent = 'No mapped genes';
                li.style.color = '#888';
                li.style.fontStyle = 'italic';
                geneListEl.appendChild(li);
            }
        }

        // Wire the "Map this KE" button — opens the KE in the resource-correct
        // mapper tab via redirectToKE() (tab derived from the active gap filter).
        var selectBtn = document.getElementById('ke-panel-select-btn');
        if (selectBtn) {
            // Replace the button to clear any previous listener
            var newBtn = selectBtn.cloneNode(true);
            selectBtn.parentNode.replaceChild(newBtn, selectBtn);
            newBtn.addEventListener('click', function () {
                redirectToKE(nodeData.id);
            });
        }

        // Open panel
        panel.classList.add('ke-side-panel--open');
    }

    function dismissSidePanel() {
        var panel = document.getElementById('ke-side-panel');
        if (panel) {
            panel.classList.remove('ke-side-panel--open');
        }
        selectedKEId = null;
        if (cy) {
            cy.elements().removeClass('active-node');
        }
    }

    function wireCloseButton() {
        var closeBtn = document.getElementById('ke-side-panel-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', function () {
                dismissSidePanel();
            });
        }
    }

    // ---------------------------------------------------------------------------
    // KE selection redirect
    // ---------------------------------------------------------------------------
    function redirectToKE(keId) {
        var tabParam = 'wp';
        if (currentGapFilter === 'gap-go') {
            tabParam = 'go';
        } else if (currentGapFilter === 'gap-reactome') {
            tabParam = 'reactome';
        }
        window.location.href = '/mapper?ke_id=' + encodeURIComponent(keId) + '&tab=' + tabParam;
    }

    // Public API
    return { init: init };
})();

// Auto-initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function () {
    AOPGraph.init();
});
