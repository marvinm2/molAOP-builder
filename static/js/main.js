/**
 * Main JavaScript functionality for KE-WP Mapping Application
 */

/**
 * PathwayEmbed - Utility object for embedding WikiPathways diagrams via Toolforge iframe.
 * Exposed at window.PathwayEmbed for use in explore.html and future plans.
 */
var PathwayEmbed = {
    /**
     * Constructs the Toolforge embed URL for a given WP ID.
     * If genes array is non-empty, appends ?yellow=... for gene highlighting.
     */
    buildEmbedUrl: function(wpId, genes) {
        var base = 'https://pathway-viewer.toolforge.org/embed/' + wpId;
        if (genes && genes.length > 0) {
            var encoded = genes.map(function(g) { return encodeURIComponent(g); }).join(',');
            return base + '?yellow=' + encoded;
        }
        return base;
    },

    /**
     * Clears container, shows loading spinner, creates iframe with 5s timeout.
     * On load: removes spinner, shows iframe.
     * On timeout: removes iframe, shows error state with fallback link.
     */
    mountIframe: function(container, wpId, genes) {
        var $container = $(container);
        $container.empty();
        $container.html('<div class="wp-embed-loading"><div class="loading-spinner"></div><span>Loading pathway...</span></div>');

        var src = PathwayEmbed.buildEmbedUrl(wpId, genes);
        var $iframe = $('<iframe>', {
            src: src,
            frameborder: 0,
            allowfullscreen: true
        }).css({ width: '100%', height: '100%', border: 'none', visibility: 'hidden', position: 'absolute', top: 0, left: 0 });

        var timeoutId = setTimeout(function() {
            $iframe.off('load');
            $container.html(PathwayEmbed.buildErrorState(wpId));
        }, 10000);

        $iframe.on('load', function() {
            clearTimeout(timeoutId);
            $container.find('.wp-embed-loading').remove();
            $iframe.css({ visibility: 'visible', position: 'static' });
        });

        $container.append($iframe);
    },

    /**
     * Returns HTML for the error state with a fallback link to WikiPathways.
     */
    buildErrorState: function(wpId) {
        return '<div class="wp-embed-error">'
            + '<p>Pathway viewer unavailable.</p>'
            + '<a href="https://www.wikipathways.org/pathways/' + wpId + '" target="_blank">View on WikiPathways</a>'
            + '</div>';
    }
};

window.PathwayEmbed = PathwayEmbed;

/**
 * ReactomeDiagramEmbed — Utility object for embedding the Reactome DiagramJS widget.
 *
 * Lifecycle:
 *   1. loadScriptOnce() — lazily injects the un-versioned CDN bundle on first use (D-07).
 *      Reactome publishes only a rolling `diagram.nocache.js`; no version-pinning is possible
 *      and no SRI hash is feasible. See RESEARCH §1 / Finding 1.
 *   2. init(containerId) — shows the parent and constructs the Diagram instance EXACTLY ONCE
 *      (D-04 reuse-instance). The parent must be visible before construct, otherwise the
 *      widget reads width=0 and renders into an empty canvas (Pitfall 6).
 *   3. load(reactomeId, genes) — diagram.loadDiagram(stId), then on the diagram-loaded
 *      signal, resetFlaggedItems() + per-gene flagItems loop (D-05 corrected: flagItems
 *      accepts a single string per call; arrays silently fail).
 *   4. hide() — $('#reactome-inline-embed').hide(); instance stays alive (no destroy()
 *      method exists in DiagramJS).
 *
 * Three-layer failure detection (D-08):
 *   (a) <script>.onerror — hard CDN unreachable
 *   (b) 10s setTimeout fallback — stalled load (matches WP timeout at main.js:43)
 *   (c) try/catch around Diagram.create() and loadDiagram() — runtime exceptions
 *
 * Exposed at window.ReactomeDiagramEmbed for parity with window.PathwayEmbed.
 */
var ReactomeDiagramEmbed = {
    _scriptPromise: null,
    // Failure-flag contract (Phase 31 / D-09):
    //   _scriptFailed   — sticky for the session. Set on CDN script-tag failure,
    //                     load-timeout, or Diagram.create() throw. Once true, all
    //                     load() calls reject immediately. NOT reset on KE change.
    //   _lastLoadFailed — per-attempt. Set on loadDiagram runtime exception or
    //                     per-load timeout. Reset at the start of every fresh
    //                     load() and on resetForNewKe().
    _scriptFailed: false,    // sticky session-level — CDN script tag fail or load-timeout
    _lastLoadFailed: false,  // per-attempt — reset on every fresh load() / resetForNewKe()
    _diagram: null,
    // Phase 31 Plan 02 — Promise/token/handler-accumulation fields:
    _pendingFlags: [],            // genes for the most recent load() — read by the bind-once onDiagramLoaded handler (D-05)
    _loadToken: 0,                // monotonic per load() — older onDiagramLoaded fires whose closure's token mismatches no-op (D-05)
    _flagGenesInvocations: 0,     // verification counter — manual playbook reads this (D-08)
    _resolveCurrentLoad: null,    // shared resolver for the in-flight load Promise; bound handler invokes when token matches
    _rejectCurrentLoad: null,     // matching reject — used by the per-load timeout (D-03)
    _loadTimeoutId: null,         // per-load timeout handle, cleared on resolve / on next load()
    // CDN URL is intentionally un-versioned — Reactome publishes only a rolling
    // `diagram.nocache.js` build; no version pin and no SRI hash are available.
    // See .planning/phases/27-reactome-pathway-viewer/27-RESEARCH.md §1.
    _CDN_URL: 'https://reactome.org/DiagramJs/diagram/diagram.nocache.js',
    _CONTAINER_ID: 'reactome-inline-embed',
    _FRAME_ID: 'reactome-inline-embed-frame',
    _LOAD_TIMEOUT_MS: 10000,

    /**
     * Idempotent script-tag injection. Returns a memoized Promise that resolves when
     * window.Reactome.Diagram.create is callable. Subsequent calls return the same
     * Promise. After a hard script failure, returns a fast-rejecting Promise without
     * re-injecting (D-09 sticky-script-failure).
     */
    loadScriptOnce: function() {
        if (this._scriptPromise) return this._scriptPromise;
        if (this._scriptFailed) return Promise.reject(new Error('Reactome CDN previously failed'));

        var self = this;
        this._scriptPromise = new Promise(function(resolve, reject) {
            var settled = false;
            var timer = null;
            var fail = function(err) {
                if (settled) return;
                settled = true;
                if (timer) clearTimeout(timer);
                self._scriptFailed = true;
                self._scriptPromise = null;
                reject(err);
            };
            var ok = function() {
                if (settled) return;
                settled = true;
                if (timer) clearTimeout(timer);
                resolve();
            };
            // GWT bundle calls this global once Reactome.Diagram.create is callable.
            // Distinct from script.onload, which fires too early. RESEARCH §2.6.
            window.onReactomeDiagramReady = ok;

            var s = document.createElement('script');
            s.src = self._CDN_URL;
            s.async = true;
            s.onerror = function() { fail(new Error('Reactome CDN unreachable')); };
            document.head.appendChild(s);

            // Stall fallback — matches WP iframe timeout at main.js:43.
            timer = setTimeout(function() {
                if (typeof window.Reactome === 'undefined' ||
                    typeof window.Reactome.Diagram === 'undefined' ||
                    typeof window.Reactome.Diagram.create !== 'function') {
                    fail(new Error('Reactome CDN load timed out'));
                }
            }, self._LOAD_TIMEOUT_MS);
        });
        return this._scriptPromise;
    },

    /**
     * Show the parent container, then construct the Diagram instance exactly once.
     * Registers onDiagramLoaded EXACTLY ONCE at construction (D-05 bind-once);
     * subsequent calls are a no-op past the cached-instance return.
     * Returns the diagram instance.
     * IMPORTANT: caller must invoke this only after loadScriptOnce() resolves.
     */
    init: function() {
        // Pitfall 6: parent must be visible BEFORE create() so width is read correctly.
        $('#' + this._CONTAINER_ID).show();
        if (this._diagram) return this._diagram;

        var self = this;
        try {
            var width = $('#' + this._FRAME_ID).width() || 950;
            this._diagram = window.Reactome.Diagram.create({
                placeHolder: this._FRAME_ID,
                width: width,
                height: 500,
                toHide: []
            });
        } catch (e) {
            this._scriptFailed = true;
            throw e;
        }

        // D-05 bind-once. Handler reads the latest _pendingFlags and checks the
        // captured-at-call-time token against the current module token; older
        // fires (from prior pathway swaps) no-op. The signature does not give us
        // a token per fire, so we capture via the closure of _resolveCurrentLoad.
        this._diagram.onDiagramLoaded(function(/* loadedStId */) {
            // Apply gene highlights for whatever the most recent load() set up.
            // _flagGenesInvocations increments inside flagGenes for the manual
            // verification playbook (D-08).
            self.flagGenes();
            // Resolve the in-flight load Promise (if any). _resolveCurrentLoad
            // already encodes the token-match check — see load() below.
            if (typeof self._resolveCurrentLoad === 'function') {
                self._resolveCurrentLoad();
            }
        });

        return this._diagram;
    },

    /**
     * Apply gene highlights for the most recent load(). Reads _pendingFlags
     * (set by load(), possibly updated by Plan 03's gene-Promise resolver).
     * Increments _flagGenesInvocations for the D-08 verification playbook.
     * No-arg: genes come from _pendingFlags, not from the caller.
     */
    flagGenes: function() {
        this._flagGenesInvocations += 1;
        if (!this._diagram) return;
        try { this._diagram.resetFlaggedItems(); } catch (_) { /* defensive */ }
        var diagram = this._diagram;
        (this._pendingFlags || []).forEach(function(g) {
            if (!g) return;
            try { diagram.flagItems(g); } catch (_) { /* per-gene swallow */ }
        });
    },

    /**
     * Top-level orchestration. Returns a Promise that:
     *   - resolves when DiagramJS fires onDiagramLoaded for THIS attempt
     *     (token match per D-05),
     *   - rejects on per-load timeout (D-03), synchronous loadDiagram throw,
     *     or sticky _scriptFailed.
     * Failure path always sets _lastLoadFailed = true so the caller-side
     * .catch can render the sibling error overlay (D-01).
     */
    load: function(reactomeId, genes) {
        var self = this;
        return this.loadScriptOnce().then(function() {
            // Begin a fresh attempt: clear per-attempt fail flag, advance token,
            // store the pending flags so the bind-once onDiagramLoaded handler
            // applies them (D-05, D-06).
            self._lastLoadFailed = false;
            self._loadToken += 1;
            self._pendingFlags = genes || [];
            var myToken = self._loadToken;

            // Cancel any in-flight timeout from a prior swap so the new
            // attempt cleanly supersedes it.
            if (self._loadTimeoutId) {
                clearTimeout(self._loadTimeoutId);
                self._loadTimeoutId = null;
            }

            return new Promise(function(resolve, reject) {
                var settled = false;

                self._resolveCurrentLoad = function() {
                    if (settled) return;
                    // Token guard: an older onDiagramLoaded fire (from a previous
                    // load() that hasn't fired yet) must NOT resolve this new
                    // attempt. The bind-once handler always invokes the latest
                    // _resolveCurrentLoad — but if a later load() supersedes
                    // this one, _loadToken has moved past myToken and we no-op.
                    if (myToken !== self._loadToken) return;
                    settled = true;
                    if (self._loadTimeoutId) {
                        clearTimeout(self._loadTimeoutId);
                        self._loadTimeoutId = null;
                    }
                    resolve();
                };
                self._rejectCurrentLoad = function(err) {
                    if (settled) return;
                    settled = true;
                    if (self._loadTimeoutId) {
                        clearTimeout(self._loadTimeoutId);
                        self._loadTimeoutId = null;
                    }
                    self._lastLoadFailed = true;
                    // D-07 defensive: clear any stale flags from this aborted
                    // attempt so a later successful retry starts clean.
                    self._pendingFlags = [];
                    try { if (self._diagram) self._diagram.resetFlaggedItems(); } catch (_) { /* defensive */ }
                    reject(err);
                };

                var diagram;
                try {
                    diagram = self.init();   // bind-once handler installed here
                } catch (e) {
                    self._rejectCurrentLoad(e);
                    return;
                }

                // D-03 per-load timeout — same 10s ceiling as the script-load
                // timeout. Symmetric.
                self._loadTimeoutId = setTimeout(function() {
                    self._rejectCurrentLoad(new Error('Reactome diagram load timed out'));
                }, self._LOAD_TIMEOUT_MS);

                // D-02 async surfacing — wrap loadDiagram. Sync throws route to
                // reject; the async resolution is driven by the bind-once
                // handler invoking _resolveCurrentLoad above.
                try {
                    diagram.loadDiagram(reactomeId);
                } catch (e) {
                    self._rejectCurrentLoad(e);
                }
            });
        });
    },

    /**
     * Hide the inline embed and clear flags. Instance stays alive — DiagramJS exports
     * no destroy() method (RESEARCH §2.4 / §6). Next load() will reuse the same diagram.
     * Also hides the sibling error overlay and resets per-attempt state (Plan 02);
     * _scriptFailed is NOT touched — that is session-level (D-09).
     */
    hide: function() {
        $('#' + this._CONTAINER_ID).hide();
        $('#reactome-inline-embed-error').hide().empty();
        $('#' + this._FRAME_ID).show();   // restore frame visibility for next load()
        this._lastLoadFailed = false;
        this._pendingFlags = [];
        if (this._diagram) {
            try { this._diagram.resetFlaggedItems(); } catch (_) { /* defensive */ }
        }
    },

    /**
     * Reset per-KE state so a new KE selection starts with a clean attempt.
     * Resets the per-attempt failure flag, load token, pending flags, and sibling
     * error overlay. _scriptFailed is NOT touched (session-level per D-09).
     * _diagram is NOT touched (instance is reused per D-12 / Phase 27 D-04).
     *
     * Called from the KE-id change handler in KEWPApp.
     */
    resetForNewKe: function() {
        this._lastLoadFailed = false;
        this._pendingFlags = [];
        this._loadToken += 1;     // invalidate any in-flight handler closures
        if (this._loadTimeoutId) {
            clearTimeout(this._loadTimeoutId);
            this._loadTimeoutId = null;
        }
        $('#reactome-inline-embed-error').hide().empty();
        $('#' + this._FRAME_ID).show();
    },

    /**
     * Failure-state HTML. Mirrors PathwayEmbed.buildErrorState shape (main.js:57-62)
     * with the Reactome PathwayBrowser fallback link (Phase 25 D-15 / Phase 26 D-15
     * convention). The reactomeId is HTML-escaped on principle even though
     * server-validated `^R-HSA-[0-9]+$` IDs cannot contain metacharacters.
     */
    buildErrorState: function(reactomeId) {
        var safe = String(reactomeId || '').replace(/[<>&"']/g, function(c) {
            return ({
                '<': '&lt;', '>': '&gt;', '&': '&amp;',
                '"': '&quot;', "'": '&#39;'
            })[c];
        });
        return '<div class="reactome-embed-error">'
            + '<p>Pathway viewer unavailable.</p>'
            + '<a href="https://reactome.org/PathwayBrowser/#/' + safe
            + '" target="_blank" rel="noopener noreferrer">Open in Reactome PathwayBrowser</a>'
            + '</div>';
    }
};

window.ReactomeDiagramEmbed = ReactomeDiagramEmbed;

class KEWPApp {
    constructor() {
        this.isLoggedIn = false;
        this.csrfToken = null;
        this.stepAnswers = {};
        this.scoringConfig = null;
        this.configLoaded = false;

        // Method filter state
        this.currentMethodFilter = 'all';
        this.currentKEContext = null;

        // GO mapping state
        this.activeTab = 'wp';
        this.goScoringConfig = null;
        this.goMethodFilter = 'all';
        this.goAspectFilter = 'all';
        this.selectedGoTerm = null;
        this.goAssessmentAnswers = {};
        this.goMappingResult = null;

        // GO suggestions pagination
        this.goSuggestionsData = null;
        this.goSuggestionsPage = 0;
        this.goSuggestionsPerPage = 10;

        // Reactome mapping state
        this.selectedReactomePathway = null;     // {reactomeId, pathwayName, species, suggestionScore}
        this.selectedReactomeConfidence = null;  // 'low' | 'medium' | 'high'

        // Gene pre-fetch cache for mapping modal highlights
        this._cachedKeGenes = {};

        // Load scoring configs, then initialize
        Promise.all([
            this.loadScoringConfig(),
            this.loadGoScoringConfig()
        ]).then(() => {
            this.init();
        });
    }

    async loadScoringConfig() {
        try {
            const response = await fetch('/api/scoring-config');
            if (response.ok) {
                const data = await response.json();
                this.scoringConfig = data.ke_pathway_assessment;
                this.configLoaded = true;
                console.log('Scoring configuration loaded:', data.metadata);
            } else {
                throw new Error('Failed to fetch config');
            }
        } catch (error) {
            console.warn('Failed to load scoring config, using defaults:', error);
            this.scoringConfig = this.getDefaultScoringConfig();
            this.configLoaded = true;
        }
    }

    getDefaultScoringConfig() {
        // Return current hardcoded values as fallback
        return {
            evidence_quality: { known: 3, likely: 2, possible: 1, uncertain: 0 },
            pathway_specificity: { specific: 2, includes: 1, loose: 0 },
            ke_coverage: { complete: 1.5, keysteps: 1.0, minor: 0.5 },
            biological_level: {
                bonus: 1.0,
                qualifying_levels: ['molecular', 'cellular', 'tissue']
            },
            confidence_thresholds: { high: 5.0, medium: 2.5 },
            max_scores: {
                with_bio_bonus: 7.5,
                without_bio_bonus: 6.5
            }
        };
    }

    async loadGoScoringConfig() {
        try {
            const response = await fetch('/api/go-scoring-config');
            if (response.ok) {
                const data = await response.json();
                this.goScoringConfig = data.ke_go_assessment;
                console.log('GO scoring configuration loaded:', data.metadata);
            } else {
                throw new Error('Failed to fetch GO config');
            }
        } catch (error) {
            console.warn('Failed to load GO scoring config, using defaults:', error);
            this.goScoringConfig = this.getDefaultGoScoringConfig();
        }
    }

    getDefaultGoScoringConfig() {
        return {
            term_specificity: { exact: 3, parent_child: 2, related: 1, broad: 0 },
            evidence_support: { experimental: 3, curated: 2, inferred: 1, assumed: 0 },
            gene_overlap: {
                high_threshold: 0.5, high_score: 2,
                moderate_threshold: 0.2, moderate_score: 1,
                low_score: 0
            },
            bio_level_bonus: {
                molecular_process: 1.0,
                cellular_process: 1.0,
                general_process: 0.5
            },
            confidence_thresholds: { high: 6, medium: 3 },
            max_scores: { with_bio_bonus: 9.0, without_bio_bonus: 8.0 },
            connection_types: ['describes', 'involves', 'related', 'context']
        };
    }

    init() {
        this.setupCSRF();
        this.setupEventListeners();

        // URL param pre-fill: read ?ke_id= and store for after dropdown populates
        const urlParams = new URLSearchParams(window.location.search);
        this.preselectedKE = urlParams.get('ke_id') || null;
        const tabParam = urlParams.get('tab') || null;
        if (this.preselectedKE && window.history.replaceState) {
            // Clean URL without triggering navigation
            const cleanUrl = window.location.pathname;
            window.history.replaceState({}, '', cleanUrl);
        }

        // Activate the requested tab if provided via ?tab= deep-link
        if (tabParam && ['wp', 'go', 'reactome'].includes(tabParam)) {
            // Defer until after setupEventListeners completes so handleTabSwitch listeners are wired
            setTimeout(() => { this.handleTabSwitch(tabParam); }, 0);
        }

        this.loadDropdownOptions();

        // Initialize form validation

        // Restore form state if returning from login
        this.restoreFormState();

        // Show v1.5 pure-semantic migration banner (dismissible)
        this.initV15Banner();

        // Show Reactome under-development notice (dismissible)
        this.initReactomeDevBanner();
    }

    setupCSRF() {
        // Get CSRF token from meta tag or input field
        this.csrfToken = $('meta[name="csrf-token"]').attr('content') || $('input[name="csrf_token"]').val();
        
        if (!this.csrfToken) {
            console.warn('CSRF token not found');
            return;
        }
        
        // Setup CSRF token for all AJAX requests
        $.ajaxSetup({
            beforeSend: (xhr, settings) => {
                if (!/^(GET|HEAD|OPTIONS|TRACE)$/i.test(settings.type) && !this.crossDomain) {
                    xhr.setRequestHeader("X-CSRFToken", this.csrfToken);
                }
            }
        });
        
        // CSRF token configured successfully
    }

    setupEventListeners() {
        // Retrieve login status
        this.isLoggedIn = $("body").data("is-logged-in") === true;
        // User login status retrieved

        // Form submission handler
        $("#mapping-form").on('submit', (e) => {
            this.handleFormSubmission(e);
        });

        // Confidence assessment handlers (WP only - exclude GO assessment buttons)
        $(document).on("click", ".btn-group:not(.go-btn-group) .btn-option:not(.go-assess-btn)", (e) => this.handleConfidenceAssessment(e));

        // Dropdown change handlers
        $("#ke_id").on('change', () => this.toggleAssessmentSection());
        
        // KE selection change handler for preview
        $("#ke_id").on('change', (e) => this.handleKESelection(e));
        
        // Setup pathway event handlers
        this.setupPathwayEventHandlers();
        
        // Assessment completion handler
        $(document).on('click', '#complete-all-assessments', () => this.handleCompleteAllAssessments());
        
        // Pathway search functionality
        this.setupPathwaySearch();
        
        // Direct button click handler fallback
        $("#mapping-form button[type='submit']").on('click', (e) => {
            e.preventDefault();
            $("#mapping-form").trigger('submit');
        });
        
        // Save form state before login (using delegation)
        $(document).on('click', 'a[href*="/login"]', (e) => {
            this.saveFormState();
        });

        // Tab switching for WP / GO mapping
        $(document).on('click', '.mapping-tab', (e) => {
            this.handleTabSwitch($(e.currentTarget).data('tab'));
        });

        // GO mapping form submission
        $("#go-mapping-form").on('submit', (e) => {
            this.handleGoFormSubmission(e);
        });

        // Step 2 sub-tab switching (#96)
        this.setupStep2SubTabs();
        this.setupGoStep2SubTabs();
        this.setupGoTermSearch();

        // Confidence select-button group wiring
        $(document).on('click', '#confidence-select-group .btn-option', (e) => {
            $('#confidence-select-group .btn-option').removeClass('selected');
            $(e.currentTarget).addClass('selected');
            const level = $(e.currentTarget).data('value');
            $('#confidence_level').val(level);
            $('#confidence-select-error').hide();
        });

        // ---- Reactome tab event wiring (Phase 25 Plan 05) ----

        // Reactome Step 2 sub-tab toggle (Suggested / Search)
        $(document).on('click', '.reactome-step2-subtab', function() {
            const sub = $(this).data('subtab');
            $('.reactome-step2-subtab').removeClass('active');
            $(this).addClass('active');
            $('#reactome-step2-panel-suggested, #reactome-step2-panel-search').hide();
            $('#reactome-step2-panel-' + sub).show();
        });

        // Click "Select" on a suggestion card -> set selected pathway
        $(document).on('click', '#reactome-suggestions-container .btn-select-reactome', (e) => {
            const $card = $(e.currentTarget).closest('.suggestion-card');
            const reactomeId = $card.data('reactome-id');
            const pathwayName = $card.data('pathway-name');
            const species = $card.data('species');
            const score = $card.data('score');
            const matchingGenesAttr = $card.attr('data-matching-genes') || '';
            const matchingGenes = matchingGenesAttr.split(',').filter(Boolean);
            const geneScore = $card.attr('data-gene-score') || '0';
            this.selectReactomePathway({
                reactomeId: reactomeId,
                pathwayName: pathwayName,
                species: species,
                suggestionScore: (score === '' || score == null) ? null : Number(score),
                matchingGenes: matchingGenes,
                genePercent: geneScore,
            });
        });

        // Reactome search input — debounced type-ahead, min 2 chars
        let reactomeSearchTimer = null;
        $(document).on('input', '#reactome-pathway-search', (e) => {
            const query = String(e.target.value || '').trim();
            clearTimeout(reactomeSearchTimer);
            if (query.length < 2) {
                $('#reactome-search-results').empty().hide();
                return;
            }
            reactomeSearchTimer = setTimeout(() => {
                $.getJSON('/search_reactome', { q: query, threshold: 0.4, limit: 10 })
                    .done((data) => {
                        this.renderReactomeSearchResults((data && data.results) || []);
                    })
                    .fail(() => {
                        $('#reactome-search-results').empty().hide();
                    });
            }, 250);
        });

        // Click a Reactome search result -> populate input and select pathway
        $(document).on('click', '.reactome-search-result-item', (e) => {
            const $item = $(e.currentTarget);
            const reactomeId = $item.data('reactome-id');
            const pathwayName = $item.data('pathway-name');
            const species = $item.data('species');
            const relevance = $item.data('relevance');
            $('#reactome-search-results').empty().hide();
            $('#reactome-pathway-search').val(`${reactomeId} — ${pathwayName}`);
            this.selectReactomePathway({
                reactomeId: reactomeId,
                pathwayName: pathwayName,
                species: species,
                suggestionScore: (relevance === '' || relevance == null) ? null : Number(relevance),
            });
        });

        // Reactome confidence confirm/override button click
        // (#reactome-confidence-select-group was the old 3-button selector — removed in 37-01.
        //  #reactome-confidence-confirm-group is the new confirm/override control.)
        $(document).on('click', '#reactome-confidence-confirm-group .btn-option', (e) => {
            const $btn = $(e.currentTarget);
            $('#reactome-confidence-confirm-group .btn-option').removeClass('selected');
            $btn.addClass('selected');
            this.selectedReactomeConfidence = $btn.data('value');
            $('#reactome-confidence-confirm-error').hide();
            $('#reactome-step-submit').show();
            this.enableReactomeSubmitIfReady();
        });

        // Reactome submit form
        $(document).on('submit', '#reactome-mapping-form', (e) => {
            e.preventDefault();
            this.handleReactomeFormSubmission(e);
        });

        // WikiPathways mapping modal close handlers
        $('#wpMappingModalClose').on('click', () => this.closeMappingModal());
        $('#wpMappingOverlay').on('click', () => this.closeMappingModal());
        $(document).on('keydown', (e) => {
            if (e.key === 'Escape' && $('#wpMappingModal').hasClass('is-visible')) {
                this.closeMappingModal();
            }
        });

        // Expand button: open mapping modal with gene highlighting
        $('#wp-expand-modal-btn').on('click', () => {
            var pathwayId = $('#wp_id').val();
            var pathwayTitle = $('#wp_id option:selected').data('title') || $('#wp_id option:selected').text();
            if (pathwayId) {
                this.openMappingModal(pathwayId, pathwayTitle);
            }
        });
    }

    setupStep2SubTabs() {
        $(document).on('click', '.step2-subtab', (e) => {
            const $btn = $(e.currentTarget);
            const subtab = $btn.data('subtab');

            // Update active state
            $('.step2-subtab').removeClass('active');
            $btn.addClass('active');

            // Show/hide panels
            $('.step2-panel').hide();
            $(`#step2-panel-${subtab}`).show();
        });
    }

    switchToSubTab(subtab) {
        $('.step2-subtab').removeClass('active');
        $(`.step2-subtab[data-subtab="${subtab}"]`).addClass('active');
        $('.step2-panel').hide();
        $(`#step2-panel-${subtab}`).show();
    }

    loadDropdownOptions() {
        this.loadAOPOptions();
        this.loadKEOptions();
        this.loadPathwayOptions();
    }

    loadAOPOptions() {
        $.getJSON("/get_aop_options")
            .done((data) => {
                const dropdown = $("#aop_filter");
                dropdown.empty();
                // Empty placeholder option required for Select2 allowClear to work
                dropdown.append('<option></option>');

                data.forEach(aop => {
                    dropdown.append(
                        `<option value="${aop.aopId}">${aop.aopId} - ${aop.aopTitle}</option>`
                    );
                });

                // Initialize Select2 for searchable AOP dropdown
                dropdown.select2({
                    placeholder: 'All Key Events (click to filter by AOP)',
                    allowClear: true,
                    width: '100%',
                    matcher: this.customMatcher
                });

                // Add change handler for AOP filter
                dropdown.on('change', () => this.handleAOPFilterChange());

                // Add clear button handler
                $("#clear_aop_filter").on('click', () => this.clearAOPFilter());
            })
            .fail((xhr, status, error) => {
                console.error("Failed to load AOP options:", error);
                // Don't show error to user - AOP filter is optional
            });
    }

    clearAOPFilter() {
        // Reset the AOP dropdown to empty (show all KEs)
        $("#aop_filter").val(null).trigger('change');
    }

    handleAOPFilterChange() {
        const aopId = $("#aop_filter").val();

        if (!aopId) {
            // "All KEs" selected - restore full KE list
            this.populateKEDropdown(this.allKEOptions);
            // Hide the clear button
            $("#clear_aop_filter").hide();
            return;
        }

        // Show the clear button
        $("#clear_aop_filter").show();

        // Show loading state
        const dropdown = $("#ke_id");
        if (dropdown.hasClass("select2-hidden-accessible")) {
            dropdown.select2('destroy');
        }
        dropdown.empty();
        dropdown.append('<option value="" disabled selected>Loading KEs for selected AOP...</option>');

        // Fetch KEs for specific AOP
        $.getJSON(`/get_aop_kes/${encodeURIComponent(aopId)}`)
            .done((data) => {
                if (data.length === 0) {
                    dropdown.empty();
                    dropdown.append('<option value="" disabled selected>No Key Events found for this AOP</option>');
                    dropdown.select2({
                        placeholder: 'No Key Events found',
                        allowClear: true,
                        width: '100%'
                    });
                } else {
                    this.populateKEDropdown(data);
                }
            })
            .fail((xhr, status, error) => {
                console.error("Failed to load KEs for AOP:", error);
                // Fallback to full KE list
                this.populateKEDropdown(this.allKEOptions);
                this.showMessage("Failed to filter KEs by AOP. Showing all Key Events.", "error");
            });
    }

    populateKEDropdown(data) {
        // Sort data by KE Label numerically
        data.sort((a, b) => {
            const matchA = a.KElabel.match(/\d+/);
            const matchB = b.KElabel.match(/\d+/);
            const idA = matchA ? parseInt(matchA[0]) : 0;
            const idB = matchB ? parseInt(matchB[0]) : 0;
            return idA - idB;
        });

        // Populate KE ID dropdown
        const dropdown = $("#ke_id");

        // Destroy existing Select2 if initialized
        if (dropdown.hasClass("select2-hidden-accessible")) {
            dropdown.select2('destroy');
        }

        dropdown.empty();
        dropdown.append('<option value="" disabled selected>Select a Key Event</option>');
        data.forEach(option => {
            dropdown.append(
                `<option value="${option.KElabel}"
                 data-title="${option.KEtitle}"
                 data-description="${option.KEdescription || ''}"
                 data-biolevel="${option.biolevel || ''}"
                 data-kepage="${option.KEpage || ''}">${option.KElabel} - ${option.KEtitle}</option>`
            );
        });

        // Initialize Select2 for searchable dropdown
        dropdown.select2({
            placeholder: 'Search for a Key Event...',
            allowClear: true,
            width: '100%',
            matcher: this.customMatcher
        });

        // Apply URL param pre-fill if set
        if (this.preselectedKE) {
            const keToSelect = this.preselectedKE;
            this.preselectedKE = null;
            // Small delay to ensure Select2 is ready
            setTimeout(() => {
                $('#ke_id').val(keToSelect).trigger('change');
            }, 100);
        }
    }

    loadKEOptions() {
        $.getJSON("/get_ke_options")
            .done((data) => {
                // Store all KE options for later filtering restoration
                this.allKEOptions = data;

                // Populate the dropdown
                this.populateKEDropdown(data);
            })
            .fail((xhr, status, error) => {
                console.error("Failed to load KE options:", error);
                const errorMsg = xhr.responseJSON?.error || "Unable to load Key Events. Please check your internet connection and try refreshing the page.";
                this.showMessage(errorMsg, "error");
            });
    }

    customMatcher(params, data) {
        // If there are no search terms, return all data
        if ($.trim(params.term) === '') {
            return data;
        }

        // Do not display the item if there is no 'text' property
        if (typeof data.text === 'undefined') {
            return null;
        }

        // Search term matching - case insensitive, matches anywhere in text
        const searchTerm = params.term.toLowerCase();
        const text = data.text.toLowerCase();

        // Match if search term is found in the text
        if (text.indexOf(searchTerm) > -1) {
            return data;
        }

        // Return null if term not found
        return null;
    }

    loadPathwayOptions() {
        $.getJSON("/get_pathway_options")
            .done((data) => {
                
                // Sort data by Pathway ID numerically
                data.sort((a, b) => {
                    const matchA = a.pathwayID.match(/\d+/);
                    const matchB = b.pathwayID.match(/\d+/);
                    const idA = matchA ? parseInt(matchA[0]) : 0;
                    const idB = matchB ? parseInt(matchB[0]) : 0;
                    return idA - idB;
                });

                // Store pathway options for later use
                this.pathwayOptions = data;
                
                // Populate all pathway dropdowns
                this.populatePathwayDropdowns();
            })
            .fail((xhr, status, error) => {
                console.error("Failed to load Pathway options:", error);
                const errorMsg = xhr.responseJSON?.error || "Unable to load Pathways. Please check your internet connection and try refreshing the page.";
                this.showMessage(errorMsg, "error");
            });
    }

    handleFormSubmission(event) {
        event.preventDefault();

        // Get pathway title from the actual visible dropdown, not the hidden input
        const selectedPathwayOption = $("select[name='wp_id'] option:selected").first();
        const wpTitle = selectedPathwayOption.data("title") || selectedPathwayOption.text() || $("#wp_id").val();
        
        const pathwayId = $("#wp_id").val();
        // Phase 37 ASMT-06: append step1-4 from the assessment answers so new
        // WP proposals persist them (closes the verified formData gap — without
        // this, every new proposal lands with NULL step columns and the admin
        // modal shows em-dashes for all of them). jQuery $.post skips undefined
        // values, preserving Marshmallow optional semantics and the Drop-None
        // filter on the server side.
        const assessmentAnswers = (this.pathwayResults && this.pathwayResults[pathwayId])
            ? this.pathwayResults[pathwayId].answers : {};

        const formData = {
            ke_id: $("#ke_id").val(),
            ke_title: $("#ke_id option:selected").data("title"),
            wp_id: pathwayId,
            wp_title: wpTitle,
            connection_type: this.mapConnectionTypeForServer($("#connection_type").val()),
            confidence_level: $("#confidence_level").val(),
            csrf_token: this.csrfToken,
            step1: assessmentAnswers.step1 || undefined,
            step2: assessmentAnswers.step2 || undefined,
            step3: assessmentAnswers.step3 || undefined,
            step4: assessmentAnswers.step4 || undefined,
        };

        // Form data prepared for submission
        console.log('Form submission debug:', {
            formData: formData,
            selectedPathwayOption: selectedPathwayOption.text(),
            originalConnectionType: $("#connection_type").val(),
            mappedConnectionType: formData.connection_type
        });

        // Validate required fields
        if (!formData.ke_id || !formData.wp_id) {
            this.showMessage("Please select both a Key Event and at least one Pathway before submitting.", "error");
            return;
        }

        // Confidence level is required — enforce via the select-button group UI
        if (!formData.confidence_level) {
            $('#confidence-select-error').show();
            $('#confidence-confirm')[0] && $('#confidence-confirm')[0].scrollIntoView({ behavior: 'smooth' });
            return;
        }

        // Single pathway submission
        if (!formData.connection_type) {
            this.showMessage("Please complete the confidence assessment for all selected pathways before submitting.", "error");
            return;
        }

        // First, check for duplicates
        this.checkEntry(formData);
    }

    checkEntry(formData) {
        $.post("/check", formData)
            .done((response) => {
                $("#existing-entries").html(""); // Clear previous content
                if (response.pair_exists) {
                    this.showMessage(response.message, "error");
                } else if (response.ke_exists) {
                    this.showExistingEntries(response, formData);
                } else {
                    this.showMappingPreview(formData);
                }
            })
            .fail((xhr) => {
                const errorMsg = xhr.responseJSON?.error || "Unable to verify mapping. Please try again or contact support if the problem persists.";
                this.showMessage(errorMsg, "error");
            });
    }

    showExistingEntries(response, formData) {
        let tableHTML = `
            <div class="existing-entries-container">
                <p>${response.message}</p>
                <table class="existing-entries-table">
                    <thead>
                        <tr>
                            <th>KE ID</th>
                            <th>WP ID</th>
                            <th>Connection Type</th>
                            <th>Confidence Level</th>
                            <th>Created At</th>
                        </tr>
                    </thead>
                    <tbody>
        `;
        
        response.ke_matches.forEach(entry => {
            tableHTML += `
                <tr>
                    <td>${entry.ke_id || entry.KE_ID}</td>
                    <td>${entry.wp_id || entry.WP_ID}</td>
                    <td>${entry.connection_type || entry.Connection_Type}</td>
                    <td>${entry.confidence_level || entry.Confidence_Level}</td>
                    <td>${(entry.created_at || entry.Timestamp || '').split('.')[0]}</td>
                </tr>
            `;
        });
        
        tableHTML += `
                    </tbody>
                </table>
                <hr style="margin: 20px 0;">
            </div>
        `;
        
        $("#existing-entries").html(tableHTML);
        
        // Also show the mapping preview for the new entry
        this.showMappingPreviewAfterTable(formData);
    }

    showMappingPreviewAfterTable(formData) {
        // This is similar to showMappingPreview but appends to existing content
        const selectedKE = $("#ke_id option:selected");
        const selectedPW = $("#wp_id option:selected");
        const keDescription = selectedKE.data('description') || '';
        const pwDescription = selectedPW.data('description') || '';
        const biolevel = selectedKE.data('biolevel') || '';
        
        // Get user information from body attributes or session
        const isLoggedIn = $("body").data("is-logged-in") === true;
        let userInfo = 'Anonymous';
        if (isLoggedIn) {
            // Try to get username from the welcome message in the header
            const welcomeText = $('header nav p').text();
            const usernameMatch = welcomeText.match(/Welcome,\s*([^(]+)/);
            if (usernameMatch) {
                userInfo = `GitHub: ${usernameMatch[1].trim()}`;
            } else {
                userInfo = 'GitHub user (logged in)';
            }
        }
        const currentDate = new Date().toLocaleString();
        
        // Create collapsible descriptions
        const keDescHtml = this.createCollapsibleDescription(keDescription, 'preview-ke-desc-table');
        const pwDescHtml = this.createCollapsibleDescription(pwDescription, 'preview-pw-desc-table');
        
        let previewHTML = `
                <h3>New Mapping Preview</h3>
                <p>Review your new mapping that will be added:</p>
                
                <div class="mapping-preview" style="display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 20px; margin: 20px 0; width: 100%;">
                    <div class="preview-section ke-section preview-section-ke">
                        <h4 class="preview-header-ke">Key Event Information</h4>
                        <p><strong>KE ID:</strong> ${formData.ke_id}</p>
                        <p><strong>KE Title:</strong> ${formData.ke_title}</p>
                        <p><strong>Biological Level:</strong> <span class="biolevel-chip">${biolevel || 'Not specified'}</span></p>
                        <div><strong>Description:</strong><br/>${keDescHtml}</div>
                    </div>

                    <div class="preview-section wp-section preview-section-wp">
                        <h4 class="preview-header-wp">Pathway Information</h4>
                        <p><strong>WP ID:</strong> ${formData.wp_id}</p>
                        <p><strong>WP Title:</strong> ${formData.wp_title}</p>
                        <div><strong>Description:</strong><br/>${pwDescHtml}</div>
                    </div>
                </div>

                <div class="preview-section preview-section-metadata">
                    <h4 class="preview-header-metadata">Mapping Metadata</h4>
                    <div class="grid-two-column">
                        <div>
                            <p><strong>Connection Type:</strong> <span class="metadata-chip">${formData.connection_type.charAt(0).toUpperCase() + formData.connection_type.slice(1)}</span></p>
                            <p><strong>Confidence Level:</strong> <span class="metadata-chip">${formData.confidence_level.charAt(0).toUpperCase() + formData.confidence_level.slice(1)}</span></p>
                        </div>
                        <div>
                            <p><strong>Submitted by:</strong> ${userInfo}</p>
                            <p><strong>Submission time:</strong> ${currentDate}</p>
                            <p><strong>Entry status:</strong> <span class="entry-status-new">New mapping</span></p>
                            <p><strong>Data sources:</strong> <span class="text-muted" style="font-size: 12px;">AOP-Wiki, WikiPathways</span></p>
                        </div>
                    </div>
                </div>
                
                <div class="confirmation-section">
                    <p class="confirmation-title"><strong>Do you want to add this new KE-WP mapping?</strong></p>
                    <p class="confirmation-subtitle">This will be added alongside the existing mappings shown above.</p>
                    <button id="confirm-submit" class="btn-success-custom">Yes, Add Entry</button>
                    <button id="cancel-submit" class="btn-secondary-custom">Cancel</button>
                </div>
        `;
        
        // Append to existing content
        $("#existing-entries").append(previewHTML);
        
        // Handle confirmation buttons
        $("#confirm-submit").on('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            this.submitEntry(formData);
        });

        $("#cancel-submit").on('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            $("#existing-entries").html("");
        });
    }

    showMappingPreview(formData) {
        // Get additional data from selected options
        const selectedKE = $("#ke_id option:selected");
        const selectedPW = $("#wp_id option:selected");
        const keDescription = selectedKE.data('description') || '';
        const pwDescription = selectedPW.data('description') || '';
        const biolevel = selectedKE.data('biolevel') || '';
        
        // Get user information from body attributes or session
        const isLoggedIn = $("body").data("is-logged-in") === true;
        let userInfo = 'Anonymous';
        if (isLoggedIn) {
            // Try to get username from the welcome message in the header
            const welcomeText = $('header nav p').text();
            const usernameMatch = welcomeText.match(/Welcome,\s*([^(]+)/);
            if (usernameMatch) {
                userInfo = `GitHub: ${usernameMatch[1].trim()}`;
            } else {
                userInfo = 'GitHub user (logged in)';
            }
        }
        const currentDate = new Date().toLocaleString();
        
        // Create collapsible descriptions
        const keDescHtml = this.createCollapsibleDescription(keDescription, 'preview-ke-desc');
        const pwDescHtml = this.createCollapsibleDescription(pwDescription, 'preview-pw-desc');
        
        let previewHTML = `
            <div class="existing-entries-container">
                <h3>Mapping Preview & Confirmation</h3>
                <p>Please carefully review your mapping details before submitting:</p>
                
                <div class="mapping-preview" style="display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 20px; margin: 20px 0; width: 100%;">
                    <div class="preview-section ke-section preview-section-ke">
                        <h4 class="preview-header-ke">Key Event Information</h4>
                        <p><strong>KE ID:</strong> ${formData.ke_id}</p>
                        <p><strong>KE Title:</strong> ${formData.ke_title}</p>
                        <p><strong>Biological Level:</strong> <span class="biolevel-chip">${biolevel || 'Not specified'}</span></p>
                        <div><strong>Description:</strong><br/>${keDescHtml}</div>
                    </div>

                    <div class="preview-section wp-section preview-section-wp">
                        <h4 class="preview-header-wp">Pathway Information</h4>
                        <p><strong>WP ID:</strong> ${formData.wp_id}</p>
                        <p><strong>WP Title:</strong> ${formData.wp_title}</p>
                        <div><strong>Description:</strong><br/>${pwDescHtml}</div>
                    </div>
                </div>

                <div class="preview-section preview-section-metadata">
                    <h4 class="preview-header-metadata">Mapping Metadata</h4>
                    <div class="grid-two-column">
                        <div>
                            <p><strong>Connection Type:</strong> <span class="metadata-chip">${formData.connection_type.charAt(0).toUpperCase() + formData.connection_type.slice(1)}</span></p>
                            <p><strong>Confidence Level:</strong> <span class="metadata-chip">${formData.confidence_level.charAt(0).toUpperCase() + formData.confidence_level.slice(1)}</span></p>
                        </div>
                        <div>
                            <p><strong>Submitted by:</strong> ${userInfo}</p>
                            <p><strong>Submission time:</strong> ${currentDate}</p>
                            <p><strong>Entry status:</strong> <span class="entry-status-new">New mapping</span></p>
                            <p><strong>Data sources:</strong> <span class="text-muted" style="font-size: 12px;">AOP-Wiki, WikiPathways</span></p>
                        </div>
                    </div>
                </div>
                
                <div class="confirmation-section">
                    <p class="confirmation-title"><strong>Are you sure you want to submit this mapping?</strong></p>
                    <p class="confirmation-subtitle">This action will add the mapping to the database and make it available for other researchers.</p>
                    <button id="confirm-final-submit" class="btn-success-custom">Yes, Submit Mapping</button>
                    <button id="cancel-final-submit" class="btn-secondary-custom">Cancel</button>
                </div>
            </div>
        `;
        
        $("#existing-entries").html(previewHTML);

        // Handle confirmation buttons
        $("#confirm-final-submit").on('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            this.submitEntry(formData);
        });

        $("#cancel-final-submit").on('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            $("#existing-entries").html("");
        });
    }

    submitEntry(formData) {
        // Check authentication before submitting
        if (!this.isLoggedIn) {
            this.showMessage("Please log in with GitHub to submit mappings.", "error");
            setTimeout(() => {
                // Save form state before redirecting to login
                this.saveFormState();
                window.location.href = '/auth/login';
            }, 2000);
            return;
        }
        
        // Show loading state
        this.showMessage("Submitting your mapping...", "info");
        
        // Handle multiple pathway IDs
        const pathwayIds = formData.wp_id.split(',').filter(id => id.trim());
        
        console.log('Submitting form data:', formData);
        console.log('Number of pathways:', pathwayIds.length);
        
        if (pathwayIds.length === 1) {
            // Single pathway - use existing logic
            $.post("/submit", formData)
                .done((response) => {
                    console.log('Submission successful:', response);
                    this.showSuccessMessage(response.message, formData);
                    $("#existing-entries").html("");
                    this.resetForm();
                })
                .fail((xhr) => {
                    console.error('Submission failed:', xhr);
                    console.log('Status:', xhr.status, 'Response:', xhr.responseText);
                    
                    if (xhr.status === 401 || xhr.status === 403) {
                        const expired = xhr.responseJSON?.error === "session_expired";
                        this.showMessage(
                            expired
                                ? "Your session has expired — please log in again. Your assessment has been saved."
                                : "Please log in to submit mappings.",
                            "error");
                        setTimeout(() => {
                            this.saveFormState();
                            window.location.href = '/auth/login';
                        }, 2000);
                    } else {
                        const errorMsg = xhr.responseJSON?.error || "Unable to submit mapping. Please try again or check your internet connection.";
                        this.showMessage(errorMsg, "error");
                    }
                });
        } else {
            // Multiple pathways - submit each separately
            this.submitMultiplePathways(formData, pathwayIds);
        }
    }

    async submitMultiplePathways(baseFormData, pathwayIds) {
        let successCount = 0;
        let failureCount = 0;
        const errors = [];
        
        // Show loading state for multiple pathway submission
        const loadingHtml = `
            <div class="loading-container">
                <div class="loading-title">
                    Submitting Multiple Pathway Mappings
                </div>
                <div class="loading-subtitle">
                    Processing ${pathwayIds.length} pathway mapping(s)...
                </div>
                <div class="spinner spinner--lg"></div>
                <div id="submission-progress" class="loading-progress">
                    Preparing submissions...
                </div>
            </div>
        `;
        $("#existing-entries").html(loadingHtml);
        
        for (const pathwayId of pathwayIds) {
            // Update progress
            $("#submission-progress").text(`Processing pathway ${successCount + failureCount + 1} of ${pathwayIds.length}: ${pathwayId}`);
            
            // Get pathway title for this ID
            const pathwayOption = this.pathwayOptions.find(opt => opt.pathwayID === pathwayId.trim());
            const pathwayTitle = pathwayOption ? pathwayOption.pathwayTitle : pathwayId;
            
            // Create individual submission
            const individualFormData = {
                ...baseFormData,
                wp_id: pathwayId.trim(),
                wp_title: pathwayTitle
            };
            
            try {
                await new Promise((resolve, reject) => {
                    $.post("/submit", individualFormData)
                        .done(() => {
                            successCount++;
                            $("#submission-progress").text(`Successfully submitted ${successCount} of ${pathwayIds.length} mappings`);
                            resolve();
                        })
                        .fail((xhr) => {
                            failureCount++;
                            const errorMsg = xhr.responseJSON?.error || `Unable to submit mapping for ${pathwayId}. Please try again.`;
                            errors.push(`${pathwayId}: ${errorMsg}`);
                            $("#submission-progress").text(`Completed ${successCount + failureCount} of ${pathwayIds.length} mappings (${failureCount} failed)`);
                            reject();
                        });
                });
                
                // Small delay to show progress
                await new Promise(resolve => setTimeout(resolve, 200));
            } catch (e) {
                // Error already handled above
            }
        }
        
        // Show summary message
        if (successCount > 0 && failureCount === 0) {
            this.showMessage(`Successfully submitted ${successCount} mapping(s)!`, "success");
            $("#existing-entries").html("");
            this.resetForm();
        } else if (successCount > 0 && failureCount > 0) {
            this.showMessage(`Successfully submitted ${successCount} mapping(s), but ${failureCount} failed. Please review the errors and try again for the failed mappings.`, "warning");
        } else {
            this.showMessage(`All mapping submissions failed. Please check your internet connection and try again. If the problem persists, contact support.`, "error");
        }
    }

    handleConfidenceAssessment(event) {
        const $btn = $(event.target).closest('.btn-option');
        if (!$btn.length) return;
        const $group = $btn.closest(".btn-group");
        const stepId = $group.data("step");
        const assessmentId = $group.data("assessment");
        const selectedValue = $btn.data("value");
        const $pathwayAssessment = $btn.closest(".pathway-assessment");
        const pathwayId = $pathwayAssessment.data("pathway-id");

        // Initialize pathway-specific answers if not exists
        if (!this.pathwayAssessments) {
            this.pathwayAssessments = {};
        }
        if (!this.pathwayAssessments[pathwayId]) {
            this.pathwayAssessments[pathwayId] = {};
        }

        // Save the value for this specific pathway
        this.pathwayAssessments[pathwayId][stepId] = selectedValue;
        
        // Debug logging
        console.log('handleConfidenceAssessment debug:', {
            stepId: stepId,
            selectedValue: selectedValue,
            pathwayId: pathwayId,
            currentAnswers: this.pathwayAssessments[pathwayId]
        });

        // Update UI
        $group.find(".btn-option").removeClass("selected");
        $btn.addClass("selected");

        // Show/hide next steps based on logic
        this.handlePathwayStepProgression($pathwayAssessment, pathwayId);
        
        // Update overall assessment status
        this.updateAssessmentStatus();
    }

    handlePathwayStepProgression($pathwayAssessment, pathwayId) {
        const answers = this.pathwayAssessments[pathwayId];
        const s1 = answers["step1"];
        const s2 = answers["step2"];
        const s3 = answers["step3"];
        const s4 = answers["step4"];

        const $steps = $pathwayAssessment.find(".assessment-step");
        const stepLabels = {
            step1: { num: 1, label: "Relationship", values: { causative: "Causative", responsive: "Responsive", bidirectional: "Bidirectional", unclear: "Unclear" } },
            step2: { num: 2, label: "Basis", values: { known: "Known connection", likely: "Likely connection", possible: "Possible connection", uncertain: "Uncertain connection" } },
            step3: { num: 3, label: "Specificity", values: { specific: "KE-specific", includes: "Includes KE", loose: "Loosely related" } },
            step4: { num: 4, label: "Coverage", values: { complete: "Complete mechanism", keysteps: "Key steps only", minor: "Minor aspects" } }
        };

        // Remove any existing collapsed summaries
        $pathwayAssessment.find('.assessment-step-collapsed').remove();

        // Reset visibility
        $steps.filter("[data-step='step2'], [data-step='step3'], [data-step='step4']").hide();
        $steps.show(); // show all active steps first

        // Determine which step is the current (latest unanswered)
        const answeredSteps = [];
        if (s1) answeredSteps.push('step1');
        if (s1 && s2) answeredSteps.push('step2');
        if (s1 && s2 && s3) answeredSteps.push('step3');
        if (s1 && s2 && s3 && s4) answeredSteps.push('step4');

        // Determine the next step to show
        const allStepKeys = ['step1', 'step2', 'step3', 'step4'];
        const currentStepIdx = answeredSteps.length; // index of next unanswered step

        // Collapse answered steps and show the current one
        allStepKeys.forEach((stepKey, idx) => {
            const $step = $steps.filter(`[data-step='${stepKey}']`);
            const answer = answers[stepKey];

            if (answer && idx < currentStepIdx) {
                // This step is answered and not the latest — collapse it
                $step.hide();
                const info = stepLabels[stepKey];
                const displayValue = info.values[answer] || answer;
                const collapsedHtml = `
                    <div class="assessment-step-collapsed" data-collapsed-step="${stepKey}">
                        <span class="collapsed-summary">Q${info.num}: <strong>${info.label}</strong> &mdash; ${this.escapeHtml(displayValue)}</span>
                        <button type="button" class="collapsed-edit-btn" data-edit-step="${stepKey}" data-pathway-id="${pathwayId}">Edit</button>
                    </div>
                `;
                $step.before(collapsedHtml);
            } else if (idx === currentStepIdx) {
                // This is the next step to answer — show it
                $step.show();
            } else {
                // Future steps — hide
                $step.hide();
            }
        });

        // Bind edit handlers
        $pathwayAssessment.find('.collapsed-edit-btn').off('click').on('click', (e) => {
            const editStep = $(e.currentTarget).data('edit-step');
            const editPathwayId = $(e.currentTarget).data('pathway-id');
            this.editAssessmentStep($pathwayAssessment, editPathwayId, editStep);
        });

        if (s1 && s2 && s3 && s4) {
            this.evaluatePathwayConfidence($pathwayAssessment, pathwayId);
            // If this is the Reactome assessment, reveal the confirm/override step
            if (this.selectedReactomePathway && pathwayId === this.selectedReactomePathway.reactomeId) {
                this.revealReactomeConfirmStep(pathwayId);
            }
        }
    }

    editAssessmentStep($pathwayAssessment, pathwayId, stepKey) {
        // Clear this step and all subsequent answers
        const allStepKeys = ['step1', 'step2', 'step3', 'step4'];
        const stepIdx = allStepKeys.indexOf(stepKey);

        for (let i = stepIdx; i < allStepKeys.length; i++) {
            delete this.pathwayAssessments[pathwayId][allStepKeys[i]];
        }

        // Clear selected buttons for affected steps
        allStepKeys.slice(stepIdx).forEach(sk => {
            $pathwayAssessment.find(`.btn-group[data-step="${sk}"] .btn-option`).removeClass('selected');
        });

        // Hide result
        $pathwayAssessment.find('.assessment-result').hide();

        // Remove stored result
        if (this.pathwayResults) {
            delete this.pathwayResults[pathwayId];
        }

        // If editing a Reactome assessment step, hide the confirm/override step again
        if (this.selectedReactomePathway && pathwayId === this.selectedReactomePathway.reactomeId) {
            $('#reactome-confidence-confirm').hide();
            $('#reactome-step-submit').hide();
            this.selectedReactomeConfidence = null;
        }

        // Re-run progression to rebuild collapsed/expanded state
        this.handlePathwayStepProgression($pathwayAssessment, pathwayId);
        this.updateAssessmentStatus();
    }

    evaluatePathwayConfidence($pathwayAssessment, pathwayId) {
        const answers = this.pathwayAssessments[pathwayId];
        const config = this.scoringConfig;

        console.log('evaluatePathwayConfidence called:', {
            pathwayId: pathwayId,
            answers: answers,
            $pathwayAssessment: $pathwayAssessment
        });

        // Use existing confidence evaluation logic
        let baseScore = 0;
        let connectionType = "undefined";

        // Connection type from step 1 (now first question)
        connectionType = answers["step1"] || "unclear";

        // Evidence quality scoring (now step 2) - use config
        baseScore += config.evidence_quality[answers["step2"]] || 0;

        // Pathway specificity scoring (now step 3) - use config
        baseScore += config.pathway_specificity[answers["step3"]] || 0;

        // Coverage comprehensiveness scoring (now step 4) - use config
        baseScore += config.ke_coverage[answers["step4"]] || 0;

        // Apply biological level bonus - use config
        const bioLevel = this.selectedBiolevel ? this.selectedBiolevel.toLowerCase() : '';
        const qualifyingLevels = config.biological_level.qualifying_levels;
        const isMolecularLevel = qualifyingLevels.some(level => bioLevel.includes(level));

        if (isMolecularLevel) {
            baseScore += config.biological_level.bonus;
        }

        // Determine confidence level - use config thresholds
        let confidence;
        if (baseScore >= config.confidence_thresholds.high) {
            confidence = "high";
        } else if (baseScore >= config.confidence_thresholds.medium) {
            confidence = "medium";
        } else {
            confidence = "low";
        }

        // Update pathway assessment result - use config max scores
        const $result = $pathwayAssessment.find(".assessment-result");
        const maxScore = isMolecularLevel ?
            config.max_scores.with_bio_bonus :
            config.max_scores.without_bio_bonus;

        $result.find(".confidence-result").text(`${confidence} confidence`);
        $result.find(".connection-result").text(connectionType);
        $result.find(".score-details").text(`Score: ${baseScore.toFixed(1)}/${maxScore}${isMolecularLevel ? ' with biological level bonus' : ''}`);
        $result.show();

        // Store results for submission
        if (!this.pathwayResults) {
            this.pathwayResults = {};
        }
        this.pathwayResults[pathwayId] = {
            confidence: confidence,
            connection_type: connectionType,
            score: baseScore,
            answers: answers
        };

        console.log('evaluatePathwayConfidence completed:', {
            pathwayId: pathwayId,
            confidence: confidence,
            connectionType: connectionType,
            baseScore: baseScore,
            resultShown: $result.is(':visible')
        });
    }

    handleStepProgression() {
        const s1 = this.stepAnswers["step1"];  // Relationship type
        const s2 = this.stepAnswers["step2"];  // Evidence quality
        const s3 = this.stepAnswers["step3"];  // Pathway specificity
        const s4 = this.stepAnswers["step4"];  // Coverage comprehensiveness

        // Reset visibility for new 4-step workflow
        $("#step2, #step3, #step4").hide();
        $("#evaluateBtn").hide();

        if (s1) {
            $("#step2").show();
        }

        if (s2) {
            $("#step3").show();
        }

        if (s3) {
            $("#step4").show();
        }

        // Check if all required steps are completed
        const ready = s1 && s2 && s3 && s4;
        if (ready) {
            $("#evaluateBtn").show();
        }
    }

    handleKESelection(event) {
        const selectedOption = $(event.target).find('option:selected');
        const keId = selectedOption.val();
        const title = selectedOption.data('title') || '';
        const biolevel = selectedOption.data('biolevel') || '';

        // Load unified KE context panel (replaces showKEPreview + loadKEContext)
        if (keId) {
            this.loadKEDetail(keId);
        } else {
            this.removeKEContextPanel();
        }

        // Store biological level for later use in assessment
        this.selectedBiolevel = biolevel;

        // Store selected KE info for assessment info cards (#103)
        this.selectedKEInfo = keId ? { keId, title, biolevel } : null;

        // Pre-fetch KE genes for mapping modal gene highlighting
        if (keId) {
            this.prefetchKeGenes(keId);
            // Phase 31 / D-10: reset Reactome embed per-attempt state on KE change.
            // _scriptFailed is intentionally NOT reset (session-level resource).
            if (window.ReactomeDiagramEmbed) {
                window.ReactomeDiagramEmbed.resetForNewKe();
            }
        }

        // Load suggestions for the active tab
        if (keId && title) {
            if (this.activeTab === 'wp') {
                this.currentMethodFilter = 'all';
                this.loadPathwaySuggestions(keId, title, 'all');
            } else if (this.activeTab === 'go') {
                this.goMethodFilter = 'all';
                this.goAspectFilter = 'all';
                this.loadGoSuggestions(keId, title, 'all', 'all');
            } else if (this.activeTab === 'reactome') {
                this.loadReactomeSuggestions(keId, title);
            }
        } else {
            this.hidePathwaySuggestions();
            this.hideGoSuggestions();
            // Reset upstream pathway link when KE is cleared
            this.updatePathwayUpstreamLink(null, null);
            // Reset Reactome suggestions panel to default if KE is cleared
            $('#reactome-suggestions-container').html(
                '<p class="text-muted-italic">Select a Key Event above to see Reactome pathway suggestions.</p>'
            );
        }
    }

    loadKEDetail(keId) {
        this.removeKEContextPanel();
        const encodedKeId = encodeURIComponent(keId);
        $.getJSON(`/api/ke_detail/${encodedKeId}`)
            .done((data) => {
                this.renderKEContextPanel(data);
            })
            .fail((xhr, status, error) => {
                console.warn('Failed to load KE detail:', error);
            });
    }

    removeKEContextPanel() {
        $('#ke-context-panel').remove();
        $('#ke-preview').remove();
    }

    renderKEContextPanel(data) {
        this.removeKEContextPanel();

        const biolevelBadge = data.biolevel
            ? `<span class="ke-biolevel-badge ke-biolevel-badge--${this.escapeHtml(data.biolevel.toLowerCase())}">${this.escapeHtml(data.biolevel)}</span>`
            : '';

        const aopWikiLink = data.ke_page
            ? `<a href="${this.escapeHtml(data.ke_page)}" target="_blank" rel="noopener noreferrer" style="font-size: 13px;">View on AOP-Wiki &rarr;</a>`
            : '';

        // Description with collapsible truncation
        const descriptionHTML = data.ke_description
            ? this.createCollapsibleDescription(data.ke_description, 'ke-context-description')
            : '<em class="text-muted-italic">No description available</em>';

        // AOP membership list
        let aopSection = '';
        if (data.aop_membership && data.aop_membership.length > 0) {
            aopSection = `<details style="margin-top:8px;"><summary style="font-size:13px;font-weight:600;">AOP Membership (${data.aop_membership.length})</summary>
            <table class="context-table"><thead><tr><th>AOP</th><th>Title</th></tr></thead><tbody>`;
            data.aop_membership.forEach(aop => {
                const aopNum = aop.aop_id.replace('AOP ', '');
                aopSection += `<tr>
                    <td><a href="https://aopwiki.org/aops/${aopNum}" target="_blank">${this.escapeHtml(aop.aop_id)}</a></td>
                    <td>${this.escapeHtml(aop.aop_title)}</td>
                </tr>`;
            });
            aopSection += `</tbody></table></details>`;
        } else {
            aopSection = `<p style="font-size:13px;margin:4px 0;" class="text-muted">No AOP membership found for this KE.</p>`;
        }

        const html = `
            <details id="ke-context-panel" class="ke-context-panel" open>
                <summary class="ke-context-title">
                    <strong>${this.escapeHtml(data.ke_title)}</strong> ${biolevelBadge}<span id="ke-direction-badge"></span>
                </summary>
                <div style="margin-top:10px;">
                    <div style="margin-bottom:8px;">${descriptionHTML}</div>
                    ${aopSection}
                    ${aopWikiLink ? `<div style="margin-top:10px;">${aopWikiLink}</div>` : ''}
                </div>
            </details>
        `;

        // Insert after KE dropdown container
        $('#ke_id').closest('.form-group, .field-group, div').first().after(html);
    }

    updateKEDirectionBadge(keDirection) {
        const $badge = $('#ke-direction-badge');
        if (!$badge.length) return;
        if (keDirection === 'positive' || keDirection === 'negative') {
            const arrow = keDirection === 'positive' ? '&#8593;' : '&#8595;';
            const label = keDirection === 'positive' ? 'Positive' : 'Negative';
            $badge.html(`<span class="badge-direction-ke badge-direction--${keDirection}">${arrow} KE: ${label}</span>`);
        } else {
            $badge.empty();
        }
    }

    getBiolevelColor(level) {
        const root = getComputedStyle(document.documentElement);
        const colorMap = {
            'molecular': '--color-primary-blue',
            'cellular': '--color-teal-accent',
            'tissue': '--color-secondary-teal',
            'organ': '--color-secondary-purple',
            'individual': '--color-secondary-orange',
            'population': '--color-secondary-magenta'
        };
        const token = colorMap[level.toLowerCase()] || '--color-text-gray';
        return root.getPropertyValue(token).trim();
    }
    
    handlePathwaySelection(event) {
        const $select = $(event.target);
        const $group = $select.closest('.pathway-selection-group');
        const $pathwayInfo = $group.find('.pathway-info');

        const selectedOption = $select.find('option:selected');
        const title = selectedOption.data('title') || '';
        const description = selectedOption.data('description') || '';
        const svgUrl = selectedOption.data('svg-url') || '';
        const pathwayId = selectedOption.val();

        // Show pathway information within the group
        if (title) {
            this.showPathwayInfoInGroup($pathwayInfo, pathwayId, title, description, svgUrl);
        } else {
            $pathwayInfo.hide();
        }

        // Fire live duplicate check when a pathway is selected from the browse panel
        if (pathwayId) {
            // Browse panel selections have no suggestion score
            $('#suggestion_score').val('');
            setTimeout(() => this.checkForDuplicatePair(), 100);
            this.loadInlineEmbed(pathwayId);
            this.updatePathwayUpstreamLink('wp', pathwayId);
        } else {
            this.updatePathwayUpstreamLink(null, null);
        }
    }

    showPathwayInfoInGroup($container, pathwayId, title, description, svgUrl) {
        // Create collapsible description HTML
        const descriptionHTML = this.createCollapsibleDescription(description, `pathway-description-${pathwayId}`);

        // Create figure preview button (replaces SVG thumbnail — inline embed handles preview)
        const figureHTML = `
            <div style="margin-top: 6px;">
                <button type="button" class="btn-link-blue" style="font-size: 12px; padding: 4px 10px;"
                        onclick="window.KEWPApp.showPathwayPreview('${this.escapeHtml(pathwayId)}', '${this.escapeHtml(title)}', '')">
                    Preview pathway
                </button>
            </div>
        `;

        // Create preview HTML with side-by-side layout
        const infoHTML = `
            <div style="padding: 10px;">
                <p style="margin: 5px 0;"><strong>Pathway:</strong> ${this.escapeHtml(title)}</p>
                <p class="text-muted" style="margin: 5px 0; font-size: 13px;">
                    <strong>ID:</strong> ${pathwayId} |
                    <a href="https://www.wikipathways.org/pathways/${pathwayId}" target="_blank">View on WikiPathways</a>
                </p>

                <!-- Side-by-side container -->
                <div style="display: flex; gap: 15px; margin-top: 10px; align-items: flex-start;">
                    <!-- Description column (left) - 60% width -->
                    <div style="flex: 3; min-width: 0;">
                        <div class="text-subtle" style="font-size: 13px; line-height: 1.5;">
                            ${description ? descriptionHTML : '<div class="text-muted-italic">No description available</div>'}
                        </div>
                    </div>

                    <!-- Figure column (right) - 40% width -->
                    <div style="flex: 2; min-width: 0;">
                        ${figureHTML || '<div class="text-muted-italic" style="font-size: 12px;">No diagram available</div>'}
                    </div>
                </div>
            </div>
        `;

        $container.html(infoHTML).show();
    }

    showPathwayDetails(title, description, svgUrl = '') {
        // Remove existing preview
        $("#pathway-preview").remove();
        
        // Create collapsible description HTML
        const descriptionHTML = this.createCollapsibleDescription(description, 'pathway-description');
        
        // Create figure preview button (replaces SVG thumbnail)
        const figureHTML = `
            <div style="margin: 10px 0;">
                <button type="button" class="btn-link-blue" style="font-size: 12px; padding: 4px 10px;"
                        onclick="window.KEWPApp.showPathwayPreview($('#wp_id').val(), '${this.escapeHtml(title)}', '')">
                    Preview pathway
                </button>
            </div>
        `;

        // Create preview HTML
        const previewHTML = `
            <div id="pathway-preview" class="panel-outlined" style="margin-top: 10px; padding: 15px;">
                <h4 class="text-dark-heading" style="margin: 0 0 8px 0;">Pathway Details:</h4>
                <p style="margin: 0 0 8px 0;"><strong>Title:</strong> ${this.escapeHtml(title)}</p>
                ${description ? `<div style="margin-bottom: 10px;"><strong>Description:</strong><br/>${descriptionHTML}</div>` : '<p class="text-muted-italic" style="margin: 0 0 10px 0;">No description available</p>'}
                ${figureHTML}
            </div>
        `;
        
        // Insert after pathway dropdown
        $("#wp_id").parent().after(previewHTML);
    }

    hidePathwayPreview() {
        $("#pathway-preview").remove();
    }

    populatePathwayDropdowns() {
        // Populate all existing pathway dropdowns while preserving current selections
        $("select[name='wp_id']").each((index, dropdown) => {
            const $dropdown = $(dropdown);
            const currentValue = $dropdown.val(); // Store current selection
            
            $dropdown.empty();
            $dropdown.append('<option value="" disabled selected>Select a Pathway</option>');
            
            this.pathwayOptions.forEach(option => {
                const svgUrl = `https://www.wikipathways.org/wikipathways-assets/pathways/${option.pathwayID}/${option.pathwayID}.svg`;
                $dropdown.append(
                    `<option value="${this.escapeHtml(option.pathwayID)}"
                     data-title="${this.escapeHtml(option.pathwayTitle)}"
                     data-description="${this.escapeHtml(option.pathwayDescription || '')}"
                     data-svg-url="${svgUrl}">${this.escapeHtml(option.pathwayID)} - ${this.escapeHtml(option.pathwayTitle)}</option>`
                );
            });
            
            // Restore the previous selection if it existed
            if (currentValue && currentValue !== '') {
                $dropdown.val(currentValue);
            }
        });
    }


    updateSelectedPathways() {
        // Get the single pathway selection
        const value = $("select[name='wp_id']").val();

        // Update the hidden field
        $("#wp_id").val(value || '');
    }

    setupPathwayEventHandlers() {
        // Remove existing handlers to prevent duplication
        $(document).off('change', "select[name='wp_id']");
        
        // Add pathway selection change handlers
        $(document).on('change', "select[name='wp_id']", (e) => {
            this.handlePathwaySelection(e);
            this.updateSelectedPathways();
            this.toggleAssessmentSection();
        });
    }

    createCollapsibleDescription(description, id) {
        if (!description) return '<span class="text-muted-italic">No description available</span>';
        
        const maxLength = 300;
        const isLong = description.length > maxLength;
        
        if (!isLong) {
            return `<div style="max-width: 100%; word-wrap: break-word; line-height: 1.4;">${description}</div>`;
        }
        
        const shortText = description.substring(0, maxLength) + '...';
        
        return `
            <div id="${id}" style="max-width: 100%; word-wrap: break-word; line-height: 1.4;">
                <div class="description-short">
                    ${shortText}
                    <br/><a href="#" onclick="KEWPApp.toggleDescription('${id}'); return false;" class="text-link-blue" style="font-weight: bold;">Show full description</a>
                </div>
                <div class="description-full" style="display: none;">
                    ${description}
                    <br/><a href="#" onclick="KEWPApp.toggleDescription('${id}'); return false;" class="text-link-blue" style="font-weight: bold;">Show less</a>
                </div>
            </div>
        `;
    }

    static toggleDescription(id) {
        const container = $(`#${id}`);
        const shortDiv = container.find('.description-short');
        const fullDiv = container.find('.description-full');
        
        if (shortDiv.is(':visible')) {
            shortDiv.hide();
            fullDiv.show();
        } else {
            shortDiv.show();
            fullDiv.hide();
        }
    }

    hideKEPreview() {
        this.removeKEContextPanel();
        this.selectedBiolevel = '';
    }

    toggleAssessmentSection() {
        const keSelected = $("#ke_id").val();
        const selectedPathways = [];
        $("select[name='wp_id']").each((index, dropdown) => {
            const value = $(dropdown).val();
            if (value) {
                const option = $(dropdown).find('option:selected');
                selectedPathways.push({
                    id: value,
                    title: option.data('title') || value,
                    description: option.data('description') || '',
                    svgUrl: option.data('svg-url') || '',
                    index: $(dropdown).closest('.pathway-selection-group').data('index')
                });
            }
        });
        
        // Debug logging
        console.log('toggleAssessmentSection called:', {
            keSelected: keSelected,
            selectedPathwaysCount: selectedPathways.length,
            selectedPathways: selectedPathways
        });
        
        if (keSelected && selectedPathways.length > 0) {
            // Show the confidence guide section
            $("#confidence-guide").show();

            // Build Step 1 summary: "KE{N}: {title}"
            const keIdRaw = $('#ke_id').val() || '';
            const keTitle = $('#ke_id option:selected').data('title') || $('#ke_id option:selected').text() || keIdRaw;
            const keNum = keIdRaw.replace(/\D/g, '');
            const step1SummaryText = keNum ? ` \u2014 KE${keNum}: ${keTitle}` : '';

            // Build Step 2 summary from selected pathway
            let step2SummaryText = '';
            if (selectedPathways.length > 0) {
                const firstPathway = selectedPathways[0];
                const wpId = firstPathway.id || '';
                const wpTitle = firstPathway.title || wpId;
                step2SummaryText = wpId ? ` \u2014 ${wpId}: ${wpTitle}` : '';
            }

            // Helper to inject/refresh Step 1 summary
            const injectStep1Summary = () => {
                $('#step1-header .step-summary').remove();
                if (step1SummaryText) {
                    $('#step1-header').append($('<span class="step-summary"></span>').text(step1SummaryText));
                }
            };

            // Helper to inject/refresh Step 2 summary
            const injectStep2Summary = () => {
                $('#step2-header .step-summary').remove();
                if (step2SummaryText) {
                    $('#step2-header').append($('<span class="step-summary"></span>').text(step2SummaryText));
                }
            };

            injectStep1Summary();
            injectStep2Summary();

            // Collapse Steps 1 & 2 to save screen space during assessment
            $('#step1-content').slideUp(300);
            $('#step2-content').slideUp(300);
            $('#step1-header').addClass('collapsible collapsed').off('click.collapse').on('click.collapse', function() {
                const $header = $(this);
                const isNowExpanded = $header.hasClass('collapsed');
                $header.toggleClass('collapsed');
                if (isNowExpanded) {
                    // Expanding — remove summary
                    $header.find('.step-summary').remove();
                    $('#step1-content').slideDown(200);
                } else {
                    // Collapsing — re-inject summary
                    injectStep1Summary();
                    $('#step1-content').slideUp(200);
                }
            });
            $('#step2-header').addClass('collapsible collapsed').off('click.collapse').on('click.collapse', function() {
                const $header = $(this);
                const isNowExpanded = $header.hasClass('collapsed');
                $header.toggleClass('collapsed');
                if (isNowExpanded) {
                    // Expanding — remove summary
                    $header.find('.step-summary').remove();
                    $('#step2-content').slideDown(200);
                } else {
                    // Collapsing — re-inject summary
                    injectStep2Summary();
                    $('#step2-content').slideUp(200);
                }
            });

            // Show loading state in the pathway assessments area
            $("#pathway-assessments").html(`
                <div style="text-align: center; padding: 20px;">
                    <div class="text-muted" style="margin-bottom: 10px;">Generating confidence assessments...</div>
                    <div class="spinner spinner--sm"></div>
                </div>
            `);

            // Hide assessment completion section while loading
            $("#assessment-completion").hide();

            // Delay to show loading state, then generate assessments
            setTimeout(() => {
                this.generatePathwayAssessments(selectedPathways);
                // Pre-fill biological level if available
                this.preFillBiologicalLevel();
            }, 300);
        } else {
            $("#confidence-guide").hide();
            // Re-expand Steps 1 & 2
            $('#step1-content').slideDown(200);
            $('#step2-content').slideDown(200);
            $('#step1-header, #step2-header').removeClass('collapsible collapsed').off('click.collapse');
            $('#step1-header .step-summary, #step2-header .step-summary').remove();
            this.resetGuide();
        }
    }

    generatePathwayAssessments(selectedPathways) {
        console.log('generatePathwayAssessments called with:', selectedPathways);
        
        const $assessments = $("#pathway-assessments");
        $assessments.empty();
        
        selectedPathways.forEach((pathway, index) => {
            console.log(`Creating assessment ${index} for pathway:`, pathway);
            const assessmentHTML = this.createPathwayAssessment(pathway, index);
            $assessments.append(assessmentHTML);
        });
        
        // Show completion button if assessments exist
        if (selectedPathways.length > 0) {
            $("#assessment-completion").show();
        } else {
            $("#assessment-completion").hide();
        }
        
        // Update assessment status
        this.updateAssessmentStatus();
    }

    // -------------------------------------------------------------------------
    // Shared 4-question assessment card renderer (WP + Reactome)
    // -------------------------------------------------------------------------

    /**
     * Build the HTML string for a 4-question KE-pathway assessment card.
     *
     * @param {Object} opts
     * @param {string}   opts.assessmentId      - Prefix for data-assessment attributes
     *                                            (e.g. "assessment-0" for WP, "assessment-reactome" for Reactome)
     * @param {string}   opts.pathwayId         - Canonical pathway identifier stored on the
     *                                            .pathway-assessment wrapper data-pathway-id
     * @param {string}   opts.pathwayIndex      - Value for data-pathway-index (WP uses numeric index;
     *                                            Reactome can reuse pathwayId)
     * @param {string}   opts.pathwayTitle      - Human-readable pathway title for the heading
     * @param {Object}   opts.keInfo            - { keId, keTitle, keBiolevel }
     * @param {string}   opts.pathwayCardHtml   - Pre-rendered HTML for the resource-specific
     *                                            pathway info card (slots into .assessment-info-card.pw-card)
     * @param {Object}   opts.geneOverlap       - { matchingGenes: string[], genePercent: number|string }
     *                                            Caller resolves this; renderer does NOT read DOM attributes.
     */
    buildAssessmentCard({ assessmentId, pathwayId, pathwayIndex, pathwayTitle, keInfo, pathwayCardHtml, geneOverlap }) {
        const { keId = '', keTitle = '', keBiolevel = '' } = keInfo || {};
        const { matchingGenes = [], genePercent = 0 } = geneOverlap || {};
        const hasOverlapData = geneOverlap !== null && geneOverlap !== undefined;

        // Gene overlap HTML
        let geneOverlapHtml = '';
        if (hasOverlapData && matchingGenes.length > 0) {
            geneOverlapHtml = `
                <div class="assessment-gene-overlap gene-overlap-found">
                    <strong>Gene Overlap:</strong> ${matchingGenes.length} shared gene${matchingGenes.length !== 1 ? 's' : ''} (${genePercent}%)
                    <details style="margin-top: 4px;"><summary style="cursor: pointer; font-size: 12px;">View genes</summary>
                        <span style="font-size: 11px; word-break: break-word;">${this.escapeHtml(matchingGenes.join(', '))}</span>
                    </details>
                </div>`;
        } else if (hasOverlapData) {
            geneOverlapHtml = `
                <div class="assessment-gene-overlap gene-overlap-empty">
                    <strong>Gene Overlap:</strong> No shared genes detected
                </div>`;
        } else {
            geneOverlapHtml = `
                <div class="assessment-gene-overlap gene-overlap-loading">
                    Gene overlap data not available (pathway selected manually)
                </div>`;
        }

        return `
            <div class="pathway-assessment pathway-assessment-container" data-pathway-id="${pathwayId}" data-pathway-index="${pathwayIndex}">

                <!-- Info Cards (#103) -->
                <div class="assessment-info-cards">
                    <div class="assessment-info-card ke-card">
                        <h4>Key Event</h4>
                        <p><strong>${this.escapeHtml(keId)} — ${this.escapeHtml(keTitle)}</strong></p>
                        ${keBiolevel ? `<p class="text-muted" style="font-size: 12px;">Level: ${this.escapeHtml(keBiolevel)}</p>` : ''}
                        <a href="/ke-details?ke_id=${encodeURIComponent(keId)}" target="_blank">View details &rarr;</a>
                    </div>
                    <div class="assessment-info-card pw-card">
                        ${pathwayCardHtml}
                    </div>
                </div>
                ${geneOverlapHtml}

                <h3 style="margin: 0 0 15px 0; border-bottom: 1px solid var(--color-border-light); padding-bottom: 8px;" class="text-dark-heading">
                    Assessment for: ${this.escapeHtml(pathwayTitle)}
                    <span class="text-muted" style="font-size: 14px; font-weight: normal;">(${this.escapeHtml(pathwayId)})</span>
                </h3>

                <div class="assessment-steps" data-assessment-id="${assessmentId}">
                    <div class="assessment-step" data-step="step1">
                        <h4>1. What is the relationship between the pathway and Key Event?
                            <span class="tooltip" data-tooltip="• Causative: The pathway directly causes or leads to the Key Event
• Responsive: The Key Event triggers or activates the pathway
• Bidirectional: Both causative and responsive relationships exist
• Unclear: The relationship exists but directionality is uncertain">❓</span>
                        </h4>
                        <div class="btn-group" data-step="step1" data-assessment="${assessmentId}">
                            <button class="btn-option" data-value="causative"><img class="btn-option-icon" src="/static/images/assessment/q1/causative.svg" alt="Causative"><span class="btn-option-label">Causative</span></button>
                            <button class="btn-option" data-value="responsive"><img class="btn-option-icon" src="/static/images/assessment/q1/responsive.svg" alt="Responsive"><span class="btn-option-label">Responsive</span></button>
                            <button class="btn-option" data-value="bidirectional"><img class="btn-option-icon" src="/static/images/assessment/q1/bidirectional.svg" alt="Bidirectional"><span class="btn-option-label">Bidirectional</span></button>
                            <button class="btn-option" data-value="unclear"><img class="btn-option-icon" src="/static/images/assessment/q1/unclear.svg" alt="Unclear"><span class="btn-option-label">Unclear</span></button>
                        </div>
                    </div>

                    <div class="assessment-step" data-step="step2" style="display: none;">
                        <h4>2. What is the basis for this mapping?
                            <span class="tooltip" data-tooltip="Base your answer on your existing knowledge:
• Known: You've seen this documented in literature or databases
• Likely: Strong biological reasoning supports this connection
• Possible: Plausible hypothesis that makes biological sense
• Uncertain: Speculative or requires investigation

You don't need to search papers - answer based on what you already know.">❓</span>
                        </h4>
                        <div class="btn-group" data-step="step2" data-assessment="${assessmentId}">
                            <button class="btn-option" data-value="known"><img class="btn-option-icon" src="/static/images/assessment/q2/known.svg" alt="Known"><span class="btn-option-label">Known connection</span></button>
                            <button class="btn-option" data-value="likely"><img class="btn-option-icon" src="/static/images/assessment/q2/likely.svg" alt="Likely"><span class="btn-option-label">Likely connection</span></button>
                            <button class="btn-option" data-value="possible"><img class="btn-option-icon" src="/static/images/assessment/q2/possible.svg" alt="Possible"><span class="btn-option-label">Possible connection</span></button>
                            <button class="btn-option" data-value="uncertain"><img class="btn-option-icon" src="/static/images/assessment/q2/uncertain.svg" alt="Uncertain"><span class="btn-option-label">Uncertain connection</span></button>
                        </div>
                    </div>

                    <div class="assessment-step" data-step="step3" style="display: none;">
                        <h4>3. How specific is the pathway to this Key Event?
                            <span class="tooltip" data-tooltip="Consider pathway scope:
• KE-specific: The pathway is specifically about this Key Event
• Includes KE: The pathway covers this KE plus other related processes
• Loosely related: The pathway is very broad or the connection is indirect

This helps identify which pathways need to be more specific.">❓</span>
                        </h4>
                        <div class="btn-group" data-step="step3" data-assessment="${assessmentId}">
                            <button class="btn-option" data-value="specific"><img class="btn-option-icon" src="/static/images/assessment/q3/specific.svg" alt="KE-specific"><span class="btn-option-label">KE-specific</span></button>
                            <button class="btn-option" data-value="includes"><img class="btn-option-icon" src="/static/images/assessment/q3/includes.svg" alt="Includes KE"><span class="btn-option-label">Includes KE</span></button>
                            <button class="btn-option" data-value="loose"><img class="btn-option-icon" src="/static/images/assessment/q3/loose.svg" alt="Loosely related"><span class="btn-option-label">Loosely related</span></button>
                        </div>
                    </div>

                    <div class="assessment-step" data-step="step4" style="display: none;">
                        <h4>4. How much of the KE mechanism is captured by the pathway?
                            <span class="tooltip" data-tooltip="Evaluate pathway completeness:
• Complete: All major biological steps/aspects of the KE are in the pathway
• Key steps: The pathway covers important parts but is missing some aspects
• Minor aspects: Only a small portion of the KE mechanism is represented

This helps identify gaps in existing pathways for future development.">❓</span>
                        </h4>
                        <div class="btn-group" data-step="step4" data-assessment="${assessmentId}">
                            <button class="btn-option" data-value="complete"><img class="btn-option-icon" src="/static/images/assessment/q4/complete.svg" alt="Complete"><span class="btn-option-label">Complete mechanism</span></button>
                            <button class="btn-option" data-value="keysteps"><img class="btn-option-icon" src="/static/images/assessment/q4/keysteps.svg" alt="Key steps"><span class="btn-option-label">Key steps only</span></button>
                            <button class="btn-option" data-value="minor"><img class="btn-option-icon" src="/static/images/assessment/q4/minor.svg" alt="Minor"><span class="btn-option-label">Minor aspects</span></button>
                        </div>
                    </div>
                </div>

                <div class="assessment-result" style="display: none;">
                    <p><strong>Result:</strong> <span class="confidence-result">—</span></p>
                    <p><strong>Connection:</strong> <span class="connection-result">—</span></p>
                    <p class="score-details text-muted" style="font-size: 12px; margin: 5px 0 0 0;">—</p>
                </div>
            </div>
        `;
    }

    // Thin WP-side caller of buildAssessmentCard.
    // Resolves WP-specific pathway card HTML and gene overlap from DOM, then delegates.
    createPathwayAssessment(pathway, index) {
        const keInfo = this.selectedKEInfo || {};
        const keId = keInfo.keId || $('#ke_id').val() || '';
        const keTitle = keInfo.title || $('#ke_id option:selected').data('title') || '';
        const keBiolevel = keInfo.biolevel || this.selectedBiolevel || '';

        // Gene overlap: WP suggestion items carry data-matching-genes on the DOM element
        const $suggItem = $(`.suggestion-item[data-pathway-id="${pathway.id}"]`);
        const matchingGenesStr = $suggItem.length > 0 ? ($suggItem.attr('data-matching-genes') || '') : '';
        const matchingGenes = matchingGenesStr.split(',').filter(Boolean);
        const geneScore = $suggItem.length > 0 ? ($suggItem.attr('data-gene-score') || '0') : '0';
        // null signals "data not available" (manually selected); [] signals "found, but empty"
        const geneOverlap = $suggItem.length > 0
            ? { matchingGenes, genePercent: geneScore }
            : null;

        // WP-specific pathway card content
        const diagramHtml = `
            <div style="margin-top: 6px;">
                <button type="button" class="btn-link-blue" style="font-size: 12px; padding: 4px 10px;"
                        onclick="window.KEWPApp.showPathwayPreview('${this.escapeHtml(pathway.id)}', '${this.escapeHtml(pathway.title)}', '')">
                    Preview pathway
                </button>
            </div>`;
        const descriptionHtml = pathway.description ? `
            <details style="margin-top: 10px;">
                <summary class="text-dark-heading" style="cursor: pointer; font-weight: bold; font-size: 13px;">Pathway Description</summary>
                <div class="text-subtle" style="margin-top: 6px; font-size: 13px; line-height: 1.5; max-height: 150px; overflow-y: auto;">
                    ${this.escapeHtml(pathway.description)}
                </div>
            </details>` : '';
        const pathwayCardHtml = `
                        <h4>Pathway</h4>
                        <p><strong>${this.escapeHtml(pathway.id)} — ${this.escapeHtml(pathway.title)}</strong></p>
                        ${diagramHtml}
                        ${descriptionHtml}
                        <a href="/pw-details?pathway_id=${encodeURIComponent(pathway.id)}" target="_blank">View details &rarr;</a>`;

        return this.buildAssessmentCard({
            assessmentId: `assessment-${pathway.index}`,
            pathwayId: pathway.id,
            pathwayIndex: pathway.index,
            pathwayTitle: pathway.title,
            keInfo: { keId, keTitle, keBiolevel },
            pathwayCardHtml,
            geneOverlap,
        });
    }

    updateAssessmentStatus() {
        const totalAssessments = $('.pathway-assessment').length;
        const completedAssessments = $('.pathway-assessment .assessment-result:visible').length;
        
        const statusText = `${completedAssessments}/${totalAssessments} assessments completed`;
        $('#assessment-status').text(statusText);
        
        if (completedAssessments === totalAssessments && totalAssessments > 0) {
            $('#complete-all-assessments').text('Proceed to Submission').addClass('btn-create').removeClass('btn-secondary-custom');
        } else {
            $('#complete-all-assessments').text('Complete All Assessments').addClass('btn-secondary-custom').removeClass('btn-create');
        }
    }

    handleCompleteAllAssessments() {
        const totalAssessments = $('.pathway-assessment').length;
        const completedAssessments = $('.pathway-assessment .assessment-result:visible').length;
        
        if (completedAssessments !== totalAssessments) {
            this.showMessage("Please complete all pathway assessments before proceeding.", "warning");
            return;
        }
        
        // Show Step 4 results instead of jumping to confirmation
        this.populateStep4Results();
        this.showStep4();
    }

    populateStep4Results() {
        // Get the single pathway selection
        const value = $("select[name='wp_id']").val();
        if (!value) return;

        const option = $("select[name='wp_id'] option:selected");
        const pathway = {
            id: value,
            title: option.data('title') || value
        };

        const result = this.pathwayResults[pathway.id];

        console.log('populateStep4Results debug:', {
            pathway: pathway,
            result: result
        });

        if (result) {
            // Update the Step 4 results display
            $('#auto-confidence').text(result.confidence.charAt(0).toUpperCase() + result.confidence.slice(1));
            $('#auto-connection').text(result.connection_type.charAt(0).toUpperCase() + result.connection_type.slice(1));

            // Also update the hidden form fields for submission
            $('#confidence_level').val(result.confidence);
            $('#connection_type').val(result.connection_type);

            // Show confidence confirm section with the recommended level pre-selected
            const recommended = result.confidence.toLowerCase();
            $('#confidence-recommendation').text(result.confidence.charAt(0).toUpperCase() + result.confidence.slice(1));
            $('#confidence-select-group .btn-option').removeClass('selected');
            $(`#confidence-select-group .btn-option[data-value="${recommended}"]`).addClass('selected');
            $('#confidence-confirm').show();
            $('#confidence-select-error').hide();

            console.log('Updated pathway display:', {
                confidence: $('#auto-confidence').text(),
                connection: $('#auto-connection').text()
            });
        }
    }

    showStep4() {
        console.log('showStep4 called');

        // Show Step 4 section
        $('#step-3-result').show();

        console.log('Step 4 elements visibility:', {
            'step-3-result_visible': $('#step-3-result').is(':visible'),
            'single-pathway-results_exists': $('#single-pathway-results').length,
            'multi-pathway-results_exists': $('#multi-pathway-results').length
        });

        // Collapse the assessment section (Step 3) and show confidence summary in header
        const confidenceLevel = $('#auto-confidence').text().trim();
        const $cgHeader = $('#confidence-guide-header');
        $cgHeader.find('.step-summary').remove();
        if (confidenceLevel && confidenceLevel !== '--') {
            $cgHeader.append($('<span class="step-summary"></span>').text(` \u2014 Confidence: ${confidenceLevel}`));
        }
        $('#confidence-guide-content').slideUp(300);
        $cgHeader.addClass('collapsible collapsed').off('click.collapse').on('click.collapse', function() {
            const $h = $(this);
            const isNowExpanded = $h.hasClass('collapsed');
            $h.toggleClass('collapsed');
            if (isNowExpanded) {
                // Expanding — remove summary
                $h.find('.step-summary').remove();
                $('#confidence-guide-content').slideDown(200);
            } else {
                // Re-collapsing — re-inject current confidence
                const currentConfidence = $('#auto-confidence').text().trim();
                $h.find('.step-summary').remove();
                if (currentConfidence && currentConfidence !== '--') {
                    $h.append($('<span class="step-summary"></span>').text(` \u2014 Confidence: ${currentConfidence}`));
                }
                $('#confidence-guide-content').slideUp(200);
            }
        });

        // Scroll to Step 4
        $('html, body').animate({
            scrollTop: $('#step-3-result').offset().top - 20
        }, 500);

        // Enable submit button inside Step 4
        $('#step-3-result').find('button[type="submit"]').prop('disabled', false).text('Review & Submit Mappings');

        this.showMessage("Assessment completed! Review your results and submit.", "success");
    }

    // Map UI connection types to server-accepted values
    mapConnectionTypeForServer(uiConnectionType) {
        const mapping = {
            'causative': 'causative',
            'responsive': 'responsive', 
            'bidirectional': 'other',
            'unclear': 'undefined'
        };
        return mapping[uiConnectionType] || 'undefined';
    }


    showMultiPathwayConfirmation() {
        const keTitle = $("#ke_id option:selected").text();
        const keId = $("#ke_id").val();
        
        let confirmationHTML = `
            <div class="confirmation-dialog">
                <div class="confirmation-dialog__panel">
                    <h2 style="margin-top: 0;" class="text-dark-heading">Confirm Multiple Pathway Mappings</h2>
                    <p><strong>Key Event:</strong> ${keTitle}</p>
                    <div style="margin: 20px 0;">
        `;
        
        // Add each pathway mapping summary
        $('.pathway-assessment').each((index, element) => {
            const $assessment = $(element);
            const pathwayId = $assessment.data('pathway-id');
            const pathwayTitle = this.pathwayOptions.find(p => p.pathwayID === pathwayId)?.pathwayTitle || pathwayId;
            const result = this.pathwayResults[pathwayId];
            
            if (!result || result.skipped) {
                confirmationHTML += `
                    <div class="confirmation-dialog__skipped">
                        <h4>${pathwayTitle} (${pathwayId})</h4>
                        <p><strong>Status:</strong> Skipped (not biologically relevant)</p>
                    </div>
                `;
            } else {
                confirmationHTML += `
                    <div class="suggestion-panel-container">
                        <h4 style="margin: 0 0 8px 0;" class="text-dark-heading">${pathwayTitle} (${pathwayId})</h4>
                        <p style="margin: 5px 0;"><strong>Confidence:</strong> ${result.confidence}</p>
                        <p style="margin: 5px 0;"><strong>Connection Type:</strong> ${result.connection_type}</p>
                        <p style="margin: 5px 0;"><strong>Score:</strong> ${result.score.toFixed(1)}</p>
                    </div>
                `;
            }
        });
        
        confirmationHTML += `
                    </div>
                    <div style="text-align: center; margin-top: 25px;">
                        <button id="confirm-multi-submit" class="btn-create" style="padding: 15px 30px; border-radius: 6px; margin-right: 10px;">
                            Submit All Mappings
                        </button>
                        <button id="cancel-multi-submit" class="btn-clear" style="padding: 15px 30px; border-radius: 6px;">
                            Cancel
                        </button>
                    </div>
                </div>
            </div>
        `;
        
        $('body').append(confirmationHTML);
        
        // Add event handlers
        $('#confirm-multi-submit').on('click', () => {
            $('.confirmation-dialog').remove();
            // Note: This old popup workflow has been replaced with the Step 4 results & submit workflow
            this.showMessage("Please use the Step 4 results & submit workflow instead.", "info");
        });
        
        $('#cancel-multi-submit').on('click', () => {
            $('.confirmation-dialog').remove();
        });
    }

    // Old submitMultiplePathwayMappings function removed - replaced with new workflow

    // Old submitIndividualMappings function removed - replaced with new workflow

    resetGuide() {
        const sections = ["#step2", "#step3", "#step4"];
        sections.forEach(id => {
            $(id).hide().find("select").val("");
            $(id).find(".btn-option").removeClass("selected");
        });
        $("#evaluateBtn").hide();
        $("#ca-result").text("");
        $("#auto-confidence").text("—");
        $("#auto-connection").text("—");
        $("#confidence_level").val("");
        $("#connection_type").val("");
        // Hide confidence confirm section and reset its state
        $("#confidence-confirm").hide();
        $("#confidence-select-group .btn-option").removeClass("selected");
        $("#confidence-select-error").hide();
        // Hide duplicate warning
        $("#duplicate-warning").hide().empty();
        this.stepAnswers = {};

        // Restore assessment section (Step 3) to its default expanded state
        $('#confidence-guide-header').removeClass('collapsible collapsed').off('click.collapse');
        $('#confidence-guide-header .step-summary').remove();
        $('#confidence-guide-content').show();
    }

    preFillBiologicalLevel() {
        // Biological level is now automatically considered in the confidence scoring
        // No need to pre-fill UI elements, but we store it for use in evaluateConfidence
        if (this.selectedBiolevel) {
            // Biological level detected for confidence scoring
        }
    }

    resetForm() {
        $("#ke_id").val("").trigger('change');

        // Reset the pathway dropdown
        $("select[name='wp_id']").val("").trigger('change');
        $(".pathway-info").hide();
        $("#wp_id").val("");

        // Reset assessment data
        this.pathwayAssessments = {};
        this.pathwayResults = {};
        $("#pathway-assessments").empty();
        $("#assessment-completion").hide();

        // Reset Step 4
        $("#step-3-result").hide();
        $("#mapping-form button[type='submit']").prop('disabled', true).text('Complete Assessment First');

        // Clear existing entries
        $("#existing-entries").html("");

        // Clear suggestion score and duplicate warning
        $("#suggestion_score").val("");
        $("#duplicate-warning").hide().empty();

        // Hide inline embed and close mapping modal on reset
        $('#wp-inline-embed').hide();
        this.closeMappingModal();

        this.hideKEPreview();
        this.hidePathwayPreview();
        this.resetGuide();
        $("#message").text("");
        this.selectedBiolevel = '';
    }

    showMessage(message, type = "info") {
        const colorClass = type === "error" ? "login-warning" : type === "success" ? "entry-status-new" : "text-link-blue";
        $("#message").text(message).removeClass("login-warning entry-status-new text-link-blue").addClass(colorClass).show();
        
        // Auto-hide success messages after 5 seconds
        if (type === "success") {
            setTimeout(() => {
                $("#message").fadeOut();
            }, 5000);
        }
    }

    showSuccessMessage(message, formData) {
        // Populate submission summary
        const summaryHtml = `
            <div style="margin-bottom: 15px;">
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">Key Event:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${formData.ke_id}</span><br>
                    <span style="font-size: 14px;">${formData.ke_title}</span>
                </div>
                <div style="text-align: center; margin: 10px 0; font-size: 20px;" class="text-link-blue">&#8595;</div>
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">WikiPathway:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${formData.wp_id}</span><br>
                    <span style="font-size: 14px;">${formData.wp_title}</span>
                </div>
            </div>
            <div style="border-top: 1px solid var(--color-border-light); padding-top: 15px; display: flex; justify-content: space-around; font-size: 14px;">
                <div>
                    <strong class="text-dark-heading">Connection:</strong><br>
                    <span class="text-muted">${formData.connection_type.charAt(0).toUpperCase() + formData.connection_type.slice(1)}</span>
                </div>
                <div>
                    <strong class="text-dark-heading">Confidence:</strong><br>
                    <span class="text-muted">${formData.confidence_level.charAt(0).toUpperCase() + formData.confidence_level.slice(1)}</span>
                </div>
            </div>
        `;

        $("#submissionSummary").html(summaryHtml);

        // Display the modal
        const modal = $("#thankYouModal");
        modal.css("display", "flex");

        // Close modal handlers
        $("#closeThankYouModal").off("click").on("click", () => {
            modal.hide();
        });

        // Close on background click
        modal.off("click").on("click", (e) => {
            if (e.target.id === "thankYouModal") {
                modal.hide();
            }
        });

        // Auto-close modal after 10 seconds
        setTimeout(() => {
            modal.fadeOut();
        }, 10000);
    }

    loadPathwaySuggestions(keId, keTitle, methodFilter = null) {
        // Loading pathway suggestions

        // Use provided method filter or current state
        const filter = methodFilter !== null ? methodFilter : this.currentMethodFilter;

        // Store KE context for re-filtering
        this.currentKEContext = {
            keId: keId,
            keTitle: keTitle,
            bioLevel: this.selectedBiolevel || ''
        };
        this.currentMethodFilter = filter;

        // Show loading indicator
        this.showPathwaySuggestionsLoading();

        // Encode parameters for URL
        const encodedKeId = encodeURIComponent(keId);
        const encodedKeTitle = encodeURIComponent(keTitle);
        const encodedBioLevel = encodeURIComponent(this.selectedBiolevel || '');
        const encodedMethodFilter = encodeURIComponent(filter);

        // Make AJAX request for suggestions with biological level context
        $.getJSON(`/suggest_pathways/${encodedKeId}?ke_title=${encodedKeTitle}&bio_level=${encodedBioLevel}&limit=8`)
            .done((data) => {
                // Pathway suggestions loaded successfully
                this.displayPathwaySuggestions(data, filter);
            })
            .fail((xhr, status, error) => {
                console.error('Failed to load pathway suggestions:', error);
                this.showPathwaySuggestionsError('Unable to load pathway suggestions. You can still browse pathways manually using the dropdown below.');
            });
    }

    showPathwaySuggestionsLoading() {
        $("#pathway-suggestions").html(`
            <div style="padding: 20px; text-align: center;">
                <div class="spinner spinner--md"></div>
                <p class="text-muted" style="margin-top: 10px;">Loading pathway suggestions...</p>
            </div>
        `);

        // Auto-switch to Suggested tab
        this.switchToSubTab('suggested');
    }

    displayPathwaySuggestions(data, filter = 'all') {
        // Handle different response structures
        const suggestions = filter === 'all'
            ? (data.combined_suggestions || [])
            : (data.suggestions || []);
        const totalCount = data.total_count || data.total_suggestions || 0;
        const filteredCount = data.filtered_count || suggestions.length;

        if (!data || suggestions.length === 0) {
            this.showNoSuggestions(data, filter);
            return;
        }

        let suggestionsHtml = `
                <h3 style="margin: 0 0 15px 0;" class="text-dark-heading">Suggested Pathways for Selected KE</h3>

                <!-- Scoring Information Box -->
                <details class="panel-outlined" style="margin: 0 0 15px 0; padding: 10px;">
                    <summary style="cursor: pointer; font-weight: bold; font-size: 14px;" class="text-dark-heading">
                        How are suggestions scored?
                    </summary>
                    <div style="margin-top: 10px; font-size: 13px; line-height: 1.6;" class="text-subtle">
                        <p style="margin: 8px 0;">Suggestions are ranked by BioBERT semantic similarity to the Key Event. Gene overlap is shown for context but does not influence rank order.</p>
                    </div>
                </details>
        `;

        // Show gene information if available
        if (data.genes_found > 0) {
            suggestionsHtml += `
                <div class="gene-info-panel">
                    <strong>Associated Genes:</strong> ${data.gene_list.join(', ')} (${data.genes_found} gene${data.genes_found !== 1 ? 's' : ''} found)
                </div>
            `;
        }

        // Display suggestions with all three scoring signals
        if (suggestions && suggestions.length > 0) {
            suggestionsHtml += `
                <div class="suggestion-section">
                    <h4 style="margin: 0 0 10px 0;" class="text-link-blue">Pathway Suggestions (${suggestions.length})</h4>
                    <div class="suggestion-list">
            `;

            suggestions.forEach((suggestion, index) => {
                const matchTypeBadges = this.getMatchTypeBadges(suggestion.match_types || []);
                const geneOverlapChip = this.renderGeneOverlapChip(suggestion, data.genes_found || 0);
                const geneSetChip = this.renderGeneSetSizeChip(suggestion.pathway_total_genes);
                const borderClass = this.getBorderClassForMatch(suggestion.match_types || []);
                const finalScoreBar = this.createFinalScoreBar(suggestion);
                const primaryEvidence = this.formatPrimaryEvidence(suggestion.primary_evidence);
                const hiddenClass = index >= 3 ? 'suggestion-item-hidden' : '';

                // Prepare ontology tags HTML
                const ontologyTagsHtml = suggestion.ontologyTags && suggestion.ontologyTags.length > 0
                    ? `<div style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px;">
                        ${suggestion.ontologyTags.map(tag =>
                            `<span class="ontology-tag-chip">${this.escapeHtml(tag)}</span>`
                        ).join('')}
                       </div>`
                    : '';

                // Prepare publications HTML
                const publicationsHtml = suggestion.publications && suggestion.publications.length > 0
                    ? `<div class="text-muted" style="margin-top: 6px; font-size: 11px;">
                        <strong>${suggestion.publications.length}</strong> reference${suggestion.publications.length > 1 ? 's' : ''}:
                        ${suggestion.publications.slice(0, 3).map(pub =>
                            `<a href="${pub.url}" target="_blank" onclick="event.stopPropagation();">PMID:${pub.pmid}</a>`
                        ).join(', ')}${suggestion.publications.length > 3 ? `, +${suggestion.publications.length - 3} more` : ''}
                       </div>`
                    : '';

                suggestionsHtml += `
                    <div class="suggestion-item ${borderClass} ${hiddenClass}" data-pathway-id="${this.escapeHtml(suggestion.pathwayID)}" data-pathway-title="${this.escapeHtml(suggestion.pathwayTitle)}" data-pathway-svg="${this.escapeHtml(suggestion.pathwaySvgUrl || '')}" data-matching-genes="${this.escapeHtml((suggestion.matching_genes || []).join(','))}" data-gene-score="${Math.round((suggestion.gene_overlap_ratio || 0) * 100)}" data-score="${(suggestion.scores && suggestion.scores.final_score !== undefined) ? suggestion.scores.final_score : (suggestion.confidence_score || '')}">
                        <div style="display: flex; gap: 12px; align-items: flex-start;">
                            <div style="flex: 1;">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                    <div>
                                        <strong style="font-size: 14px;">${suggestion.pathwayTitle}</strong>
                                        ${matchTypeBadges}
                                        ${geneOverlapChip}${geneSetChip}
                                    </div>
                                    ${finalScoreBar}
                                </div>
                                ${ontologyTagsHtml}
                                <div class="text-muted" style="font-size: 12px; margin-bottom: 8px;">
                                    ID: ${suggestion.pathwayID} | Primary: ${primaryEvidence} | <a href="https://www.wikipathways.org/pathways/${suggestion.pathwayID}" target="_blank" onclick="event.stopPropagation();">View on WikiPathways</a>
                                </div>
                                ${publicationsHtml}
                                <button class="pathway-preview-btn pathway-preview-trigger">
                                    Preview Pathway
                                </button>
                            </div>
                        </div>
                    </div>
                `;
            });

            suggestionsHtml += '</div>';

            // Add "Show more" button if there are more than 3 suggestions
            if (suggestions.length > 3) {
                suggestionsHtml += `
                    <button class="show-more-suggestions show-more-btn" type="button">
                        Show ${suggestions.length - 3} more suggestions
                    </button>
                `;
            }

            suggestionsHtml += '</div>';
        }

        suggestionsHtml += `
                <div class="text-muted" style="margin-top: 15px; padding-top: 10px; border-top: 1px solid var(--color-border-light); font-size: 12px; text-align: center;">
                    Click any suggestion to auto-select it as your pathway
                </div>
        `;

        // Write into the static container
        $("#pathway-suggestions").html(suggestionsHtml);

        // Auto-switch to Suggested tab
        this.switchToSubTab('suggested');

        // Bind click handlers using data attributes instead of inline onclick
        $('.suggestion-item').off('click').on('click', (e) => {
            // Ignore clicks on preview buttons/thumbnails
            if ($(e.target).closest('.pathway-preview-btn').length) return;
            const $item = $(e.currentTarget);
            this.selectSuggestedPathway($item.data('pathway-id'), $item.data('pathway-title'));
        });
        $('.pathway-preview-btn').off('click').on('click', (e) => {
            e.stopPropagation();
            const $item = $(e.target).closest('.suggestion-item');
            this.showPathwayPreview($item.data('pathway-id'), $item.data('pathway-title'), $item.data('pathway-svg'));
        });

        // Bind show more/less toggle
        $('.show-more-suggestions').off('click').on('click', function() {
            const $button = $(this);
            const $allItems = $('.suggestion-item');
            const isExpanded = $button.data('expanded') === true;

            if (isExpanded) {
                // Collapse: hide items after index 2 (show only first 3)
                $allItems.each((index, item) => {
                    if (index >= 3) $(item).hide();
                });
                const hiddenCount = $allItems.length - 3;
                $button.text(`Show ${hiddenCount} more suggestions`);
                $button.data('expanded', false);

                // Scroll back to top of suggestions
                $('html, body').animate({
                    scrollTop: $('#pathway-suggestions').offset().top - 100
                }, 300);
            } else {
                // Expand: show all items
                $allItems.show();
                $button.text('Show less');
                $button.data('expanded', true);
            }
        });
    }

    setupMethodFilterButtons(currentFilter, totalCount, filteredCount) {
        // Update active button state
        $('.method-filter-btn').removeClass('active method-filter-btn--active').addClass('method-filter-btn--inactive');
        $(`.method-filter-btn[data-method="${currentFilter}"]`).removeClass('method-filter-btn--inactive').addClass('active method-filter-btn--active');

        // Update filter info text
        let filterInfoText = '';
        if (currentFilter === 'all') {
            filterInfoText = `Showing all ${filteredCount} suggestions (combined ranking)`;
        } else {
            const methodName = currentFilter.charAt(0).toUpperCase() + currentFilter.slice(1);
            filterInfoText = `Showing ${filteredCount} ${currentFilter}-based suggestions (${totalCount} total)`;
        }
        $('#filterInfo').text(filterInfoText);

        // Add click event listeners
        $('.method-filter-btn').off('click').on('click', (e) => {
            const method = $(e.currentTarget).data('method');

            // Update button states
            $('.method-filter-btn').removeClass('active method-filter-btn--active').addClass('method-filter-btn--inactive');
            $(e.currentTarget).removeClass('method-filter-btn--inactive').addClass('active method-filter-btn--active');

            // Re-fetch suggestions with new filter
            if (this.currentKEContext) {
                this.loadPathwaySuggestions(
                    this.currentKEContext.keId,
                    this.currentKEContext.keTitle,
                    method
                );
            }
        });
    }

    showNoSuggestions(data, filter = 'all') {
        let message = "No pathway suggestions found for this Key Event.";
        let details = "";

        if (data && data.genes_found === 0) {
            details = "No associated genes were found in the AOP-Wiki data. Try using the Search or Browse All tabs.";
        } else if (data && data.genes_found > 0) {
            details = `Found ${data.genes_found} associated gene${data.genes_found !== 1 ? 's' : ''} (${data.gene_list.join(', ')}) but no matching pathways were identified.`;
        }

        let noSuggestionsHtml = `
                <h3 style="margin: 0 0 15px 0;" class="text-dark-heading">Suggested Pathways for Selected KE</h3>
        `;

        // Show gene information if available
        if (data && data.genes_found > 0) {
            noSuggestionsHtml += `
                <div class="gene-info-panel">
                    <strong>Associated Genes:</strong> ${data.gene_list.join(', ')} (${data.genes_found} gene${data.genes_found !== 1 ? 's' : ''} found)
                </div>
            `;
        }

        noSuggestionsHtml += `
                <div class="text-muted panel-outlined" style="text-align: center; padding: 20px;">
                    <div style="margin-bottom: 8px; font-weight: bold;">${message}</div>
                    ${details ? `<div style="font-size: 12px; margin-bottom: 15px;">${details}</div>` : ''}
                    <div style="margin-top: 15px; font-size: 12px;" class="text-link-blue">
                        <em>Try using the Search or Browse All tabs to find pathways manually</em>
                    </div>
                </div>
        `;

        // Write into the static container
        $("#pathway-suggestions").html(noSuggestionsHtml);

        // Auto-switch to Suggested tab
        this.switchToSubTab('suggested');
    }

    showPathwaySuggestionsError(errorMessage) {
        $("#pathway-suggestions").html(`
            <h3 style="margin: 0 0 10px 0;" class="text-dark-heading">Pathway Suggestions</h3>
            <div class="login-warning" style="text-align: center; padding: 20px;">
                <div class="text-muted" style="font-size: 16px; margin-bottom: 10px; font-weight: bold;">Warning</div>
                <div>${errorMessage}</div>
                <div class="text-muted" style="margin-top: 10px; font-size: 12px;">
                    Try using the Search or Browse All tabs to find pathways manually
                </div>
            </div>
        `);

        // Auto-switch to Suggested tab
        this.switchToSubTab('suggested');
    }

    createConfidenceBar(score) {
        const percentage = Math.round(score * 100);
        let tier = 'low';
        if (percentage >= 70) {
            tier = 'high';
        } else if (percentage >= 40) {
            tier = 'medium';
        }

        return `
            <div class="confidence-bar-box confidence-bar-box--${tier}">
                <div class="confidence-bar-track">
                    <div class="confidence-bar-fill confidence-bar-fill--${tier}" style="width: ${percentage}%;"></div>
                </div>
                <div class="confidence-bar-value confidence-bar-value--${tier}">${percentage}%</div>
            </div>
        `;
    }

    getMatchTypeBadges(matchTypes) {
        const badges = [];

        if (matchTypes.includes('gene')) {
            badges.push('<span class="badge-match--gene" style="margin-left: 8px;">Gene</span>');
        }

        if (matchTypes.includes('embedding')) {
            badges.push('<span class="badge-match--semantic" style="margin-left: 8px;">Semantic</span>');
        }

        return badges.join(' ');
    }

    /**
     * Render a muted chip showing matched/total KE genes for a suggestion card.
     * @param {object} suggestion - suggestion item from API response
     * @param {number} totalKeGenes - denominator (data.genes_found)
     * @returns {string} HTML string for the chip (empty string if no KE genes)
     */
    renderGeneOverlapChip(suggestion, totalKeGenes) {
        const matchedGenes = suggestion.matching_genes || [];
        const matchedCount = matchedGenes.length;
        const total = totalKeGenes || 0;
        if (total === 0) return '';  // No KE genes → no chip
        const fractionLabel = `${matchedCount}/${total}`;
        const tooltipText = matchedCount > 0
            ? `Matched HGNC: ${matchedGenes.join(', ')}`
            : 'No KE genes overlap this pathway';
        const emptyClass = matchedCount === 0 ? ' gene-overlap-chip--empty' : '';
        return `<span class="gene-overlap-chip${emptyClass}" title="${this.escapeHtml(tooltipText)}">Genes: ${fractionLabel}</span>`;
    }

    /**
     * Gene-set size of the candidate itself — how many genes this term or
     * pathway resolves to (#210).
     *
     * Deliberately labelled "Set:" rather than "Genes:", because
     * renderGeneOverlapChip already renders "Genes: m/n" on the same badge row
     * and means something different: how much of the *Key Event's* gene list
     * this candidate matches. Two numbers, two questions.
     *
     * Why it matters: the molAOP Analyser refuses to test a Key Event that
     * resolves to fewer than five genes, so a mapping can be semantically
     * perfect and still leave its Key Event silently excluded from every
     * analysis. That is not hypothetical — KE 1097 was mapped to GO:0097300,
     * the correct term, which resolves to five genes.
     *
     * `direct` is the count annotated to the term itself, shown alongside the
     * propagated count for GO. A term with 891 propagated and 7 direct is
     * well-populated but only indirectly evidenced, and the curator should see
     * both rather than have one stand in for the other.
     */
    renderGeneSetSizeChip(count, direct) {
        // Distinguish "unknown" (no annotation corpus loaded) from "zero".
        // Showing a warning on every candidate because a data file is missing
        // would be worse than showing nothing.
        if (count === null || count === undefined) return '';

        const n = Number(count);
        if (!Number.isFinite(n)) return '';

        const hasDirect = direct !== null && direct !== undefined
            && Number.isFinite(Number(direct)) && Number(direct) !== n;
        const label = hasDirect ? `Set: ${n} genes (${Number(direct)} direct)` : `Set: ${n} genes`;

        const low = n < 5;
        const tooltip = low
            ? `Resolves to ${n} gene${n === 1 ? '' : 's'} — below the 5-gene minimum the molAOP Analyser requires to test a Key Event, so this mapping may not be testable.`
            : `Resolves to ${n} genes.${hasDirect ? ` ${Number(direct)} annotated to this term directly, the rest inherited from its descendants.` : ''}`;

        const cls = low ? ' gene-set-chip--warn' : '';
        return `<span class="gene-set-chip${cls}" title="${this.escapeHtml(tooltip)}">${this.escapeHtml(label)}</span>`;
    }

    getScoreDetails(scores, suggestion) {
        // Check if this is a combined suggestion or individual method
        if (suggestion.scores) {
            // Combined view - show all scores
            return this.getCombinedScoreDetails(suggestion);
        }

        // Individual method views - show method-specific scores
        const type = suggestion.suggestion_type;

        if (type === 'gene_based') {
            return this.getGeneScoreDetails(suggestion);
        } else if (type === 'embedding_based') {
            return this.getEmbeddingScoreDetails(suggestion);
        }

        return '';
    }

    getCombinedScoreDetails(suggestion) {
        // Existing complex score display for combined view
        const scores = suggestion.scores || {};
        const embDetails = suggestion.embedding_details || {};
        let details = [];

        // Gene-based details
        if (scores.gene_confidence && scores.gene_confidence > 0) {
            const geneInfo = suggestion.matching_gene_count
                ? `${suggestion.matching_gene_count}/${suggestion.matching_genes?.length || 0} KE genes`
                : 'Gene match';
            const pathwayInfo = suggestion.pathway_total_genes
                ? ` (${suggestion.matching_gene_count}/${suggestion.pathway_total_genes} pathway genes)`
                : '';

            details.push(`
                <div class="method-detail--gene">
                    <strong>Gene Score: ${Math.round(scores.gene_confidence * 100)}%</strong> - ${geneInfo}${pathwayInfo}
                    ${suggestion.matching_genes ? `<br><span style="font-size: 10px;">Matching genes: ${suggestion.matching_genes.join(', ')}</span>` : ''}
                </div>
            `);
        }

        // Embedding-based details
        if (scores.embedding_similarity && scores.embedding_similarity > 0) {
            // Extract all three scores
            const titleSim = embDetails.title_similarity || scores.embedding_similarity;
            const descSim = embDetails.description_similarity || scores.embedding_similarity;
            const combinedSim = embDetails.combined || scores.embedding_similarity;

            // Format percentages
            const titlePct = Math.round(titleSim * 100);
            const descPct = Math.round(descSim * 100);
            const combinedPct = Math.round(combinedSim * 100);

            // Build breakdown line
            const breakdown = `Title: ${titlePct}% | Description: ${descPct}% | Combined: ${combinedPct}%`;

            details.push(`
                <div class="method-detail--semantic">
                    <strong>Semantic Score: ${combinedPct}%</strong>
                    <br><span style="font-size: 10px;">${breakdown}</span>
                    <br><span style="font-size: 9px; font-style: italic;">BioBERT semantic similarity (directionality-neutral)</span>
                </div>
            `);
        }

        return details.join('');
    }

    getGeneScoreDetails(suggestion) {
        const geneCount = suggestion.matching_gene_count || 0;
        const overlapRatio = suggestion.gene_overlap_ratio || 0;
        const genes = suggestion.matching_genes || [];

        return `
            <div class="method-detail--gene-sm">
                <div><strong>Gene Overlap:</strong> ${Math.round(overlapRatio * 100)}%</div>
                <div style="margin-top: 4px;"><strong>Matching Genes:</strong> ${genes.length > 0 ? genes.join(', ') : 'None'} (${geneCount} genes)</div>
            </div>
        `;
    }

    getEmbeddingScoreDetails(suggestion) {
        const titleSim = suggestion.title_similarity || 0;
        const descSim = suggestion.description_similarity || 0;
        const embeddingSim = suggestion.embedding_similarity || 0;

        return `
            <div class="method-detail--semantic-sm">
                <div><strong>Semantic Similarity:</strong> ${Math.round(embeddingSim * 100)}%</div>
                <div style="margin-left: 10px; margin-top: 2px;">Title: ${Math.round(titleSim * 100)}%</div>
                <div style="margin-left: 10px;">Description: ${Math.round(descSim * 100)}%</div>
                <div style="font-size: 9px; font-style: italic; margin-top: 4px;">BioBERT semantic similarity</div>
            </div>
        `;
    }

    getBorderClassForMatch(matchTypes) {
        if (matchTypes.length >= 3) {
            return 'suggestion-item--multi-match';
        } else if (matchTypes.includes('gene') && matchTypes.includes('embedding')) {
            return 'suggestion-item--gene-semantic';
        } else if (matchTypes.includes('gene')) {
            return 'suggestion-item--gene';
        } else if (matchTypes.includes('embedding')) {
            return 'suggestion-item--semantic';
        } else {
            return 'suggestion-item--other';
        }
    }

    /** @deprecated Use getBorderClassForMatch — kept for backwards compat */
    getBorderColorForMatch(matchTypes) {
        const root = getComputedStyle(document.documentElement);
        if (matchTypes.length >= 3) {
            return root.getPropertyValue('--color-secondary-magenta').trim();
        } else if (matchTypes.includes('gene') && matchTypes.includes('embedding')) {
            return root.getPropertyValue('--color-secondary-purple').trim();
        } else if (matchTypes.includes('gene')) {
            return root.getPropertyValue('--color-status-high').trim();
        } else if (matchTypes.includes('embedding')) {
            return root.getPropertyValue('--color-method-semantic').trim();
        } else {
            return root.getPropertyValue('--color-teal-accent').trim();
        }
    }

    createFinalScoreBar(suggestion) {
        // Determine which score to display based on suggestion type
        let score = 0;
        let label = 'Score';

        if (suggestion.scores && suggestion.scores.final_score !== undefined) {
            // Combined view - use final combined score
            score = suggestion.scores.final_score;
            label = 'Combined';
        } else if (suggestion.suggestion_type === 'gene_based') {
            // Gene-based view - use gene overlap ratio
            score = suggestion.gene_overlap_ratio || 0;
            label = 'Gene Overlap';
        } else if (suggestion.suggestion_type === 'embedding_based') {
            // Embedding-based view - use semantic similarity
            score = suggestion.embedding_similarity || 0;
            label = 'Semantic';
        } else {
            // Fallback to confidence_score if available
            score = suggestion.confidence_score || 0;
            label = 'Confidence';
        }

        const percentage = Math.round(score * 100);
        const tier = this.getConfidenceClass(score);
        const qualityBadge = this.getQualityBadge(score);

        return `
            <div style="text-align: right;">
                <div class="text-muted" style="font-size: 11px; margin-bottom: 2px;">${label}</div>
                <div style="display: flex; align-items: center; gap: 8px; justify-content: flex-end;">
                    <div class="score-bar-track">
                        <div class="score-bar-fill score-bar-fill--${tier}" style="width: ${percentage}%;"></div>
                    </div>
                    <span class="confidence-bar-value--${tier}" style="font-weight: bold; font-size: 13px;">${percentage}%</span>
                </div>
                ${qualityBadge ? `<div style="margin-top: 4px;">${qualityBadge}</div>` : ''}
            </div>
        `;
    }

    formatPrimaryEvidence(primary) {
        const labels = {
            'gene_overlap': 'Gene Overlap',
            'semantic_similarity': 'Semantic Match',
            'ontology_tags': 'Ontology Match'
        };
        return labels[primary] || primary || 'Unknown';
    }

    getConfidenceColor(score) {
        const root = getComputedStyle(document.documentElement);
        if (score >= 0.8) return root.getPropertyValue('--color-status-high').trim();
        if (score >= 0.5) return root.getPropertyValue('--color-status-medium').trim();
        return root.getPropertyValue('--color-status-low').trim();
    }

    getConfidenceClass(score) {
        if (score >= 0.8) return 'high';
        if (score >= 0.5) return 'medium';
        return 'low';
    }

    /**
     * Get quality tier badge based on score thresholds
     * Thresholds from scoring_config.yaml quality_tiers:
     *   excellent: 0.70+, good: 0.50+, moderate: 0.30+
     * @param {number} score - The suggestion score (0-1)
     * @returns {string} HTML badge element
     */
    getQualityBadge(score) {
        // Get thresholds from config or use defaults
        const config = this.scoringConfig?.pathway_suggestion?.quality_tiers || {};
        const excellentThreshold = config.excellent_threshold || 0.70;
        const goodThreshold = config.good_threshold || 0.50;
        const moderateThreshold = config.moderate_threshold || 0.30;

        if (score >= excellentThreshold) {
            return '<span class="quality-badge excellent">Excellent Match</span>';
        }
        if (score >= goodThreshold) {
            return '<span class="quality-badge good">Good Match</span>';
        }
        if (score >= moderateThreshold) {
            return '<span class="quality-badge moderate">Possible Match</span>';
        }
        return '';
    }

    /**
     * Update the #pw-upstream-link anchor to point at the resource-correct upstream page
     * for the currently selected pathway/GO term. Pass null/falsy to hide the link.
     *
     * @param {string|null} resourceType - 'wp' | 'go' | 'reactome'
     * @param {string|null} resourceId   - WPxxxx | GO:xxxxxxx | R-HSA-xxx
     */
    updatePathwayUpstreamLink(resourceType, resourceId) {
        const $link = $('#pw-upstream-link');
        if (!resourceType || !resourceId) {
            $link.hide();
            return;
        }
        let href = '';
        let label = '';
        if (resourceType === 'wp') {
            href = 'https://www.wikipathways.org/pathways/' + resourceId + '.html';
            label = 'View on WikiPathways ↗';
        } else if (resourceType === 'go') {
            href = 'https://amigo.geneontology.org/amigo/term/' + resourceId;
            label = 'View on AmiGO ↗';
        } else if (resourceType === 'reactome') {
            href = 'https://reactome.org/content/detail/' + resourceId;
            label = 'View on Reactome ↗';
        }
        if (href) {
            $link.attr('href', href).text(label).show();
        } else {
            $link.hide();
        }
    }

    escapeHtml(text) {
        if (!text) return '';
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    selectSuggestedPathway(pathwayId, pathwayTitle) {
        // Get the single pathway dropdown
        const $dropdown = $("select[name='wp_id']");

        // Find the pathway option in the dropdown
        const $option = $dropdown.find(`option[value="${pathwayId}"]`);

        if ($option.length > 0) {
            // Capture suggestion score from the suggestion item before selecting
            const $suggItem = $(`.suggestion-item[data-pathway-id="${pathwayId}"]`);
            const score = $suggItem.data('score') !== undefined ? $suggItem.data('score') : '';
            $('#suggestion_score').val(score);

            // Select the pathway
            $dropdown.val(pathwayId).trigger('change');

            // Show selected pathway banner
            const $banner = $('#selected-pathway-banner');
            $banner.html(`
                <span>Selected: <strong>${this.escapeHtml(pathwayTitle)}</strong> (${this.escapeHtml(pathwayId)})</span>
                <button type="button" class="banner-dismiss" title="Dismiss">&times;</button>
            `).show();
            $banner.find('.banner-dismiss').on('click', () => {
                $banner.hide();
                $('#wp-inline-embed').hide();
            });

            // Load inline pathway embed
            this.loadInlineEmbed(pathwayId);

            // Update upstream pathway link
            this.updatePathwayUpstreamLink('wp', pathwayId);

            // Ensure the assessment section is triggered with a slight delay
            setTimeout(() => {
                this.updateSelectedPathways();
                this.toggleAssessmentSection();

                // Fire duplicate check — scroll happens in callback
                const keId = $('#ke_id').val();
                const wpId = $('#wp_id').val();
                if (keId && wpId) {
                    $('#duplicate-warning').hide().empty();
                    $.post('/check', { ke_id: keId, wp_id: wpId }, (result) => {
                        if (result.pair_exists && result.blocking_type) {
                            this.renderDuplicateWarning(result);
                        } else if ($('#confidence-guide').is(':visible')) {
                            $('html, body').animate({
                                scrollTop: $('#confidence-guide').offset().top - 20
                            }, 500);
                        }
                    }).fail(() => {
                        if ($('#confidence-guide').is(':visible')) {
                            $('html, body').animate({
                                scrollTop: $('#confidence-guide').offset().top - 20
                            }, 500);
                        }
                    });
                } else if ($('#confidence-guide').is(':visible')) {
                    $('html, body').animate({
                        scrollTop: $('#confidence-guide').offset().top - 20
                    }, 500);
                }
            }, 50);
        } else {
            console.error(`Pathway option not found in dropdown: ${pathwayId}`);
            this.showMessage(`Selected pathway is not available in the dropdown. Please try refreshing the page or selecting a different pathway.`, "warning");
        }
    }

    checkForDuplicatePair() {
        const keId = $('#ke_id').val();
        const wpId = $('#wp_id').val();
        if (!keId || !wpId) return;

        $('#duplicate-warning').hide().empty();

        $.post('/check', { ke_id: keId, wp_id: wpId }, (result) => {
            if (result.pair_exists && result.blocking_type) {
                this.renderDuplicateWarning(result);
            }
        });
    }

    renderDuplicateWarning(result) {
        const ex = result.existing;
        let html = '<div class="alert alert-warning" style="border: 2px solid var(--color-status-medium); padding: 16px; border-radius: 6px; margin: 12px 0;">';

        if (result.blocking_type === 'approved_mapping') {
            html += '<h4 style="margin-top:0;">This pair already has an approved mapping</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + (ex.ke_id || '') + ' — ' + (ex.ke_title || '') + '</dd>';
            html += '<dt>Pathway</dt><dd>' + (ex.wp_id || '') + ' — ' + (ex.wp_title || '') + '</dd>';
            html += '<dt>Confidence</dt><dd>' + (ex.confidence_level || '') + '</dd>';
            html += '<dt>Curator</dt><dd>' + (ex.approved_by_curator || 'unknown') + '</dd>';
            html += '</dl>';
            html += '<button type="button" class="btn-submit-revision" data-mapping-id="' + ex.id + '" style="margin-top:8px;">Submit Revision Proposal</button>';
        } else if (result.blocking_type === 'pending_proposal') {
            html += '<h4 style="margin-top:0;">A pending proposal already exists for this pair</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + (ex.ke_id || '') + ' — ' + (ex.ke_title || '') + '</dd>';
            html += '<dt>Pathway</dt><dd>' + (ex.wp_id || '') + ' — ' + (ex.wp_title || '') + '</dd>';
            html += '<dt>Submitted by</dt><dd>' + (ex.submitted_by || 'unknown') + '</dd>';
            html += '<dt>Submitted</dt><dd>' + (ex.submitted_at || '') + '</dd>';
            html += '</dl>';
            html += '<button type="button" class="btn-flag-stale" data-proposal-id="' + ex.proposal_id + '" data-mapping-type="wp" style="margin-top:8px;">Flag as Stale for Admin Review</button>';
        }

        html += '</div>';
        $('#duplicate-warning').html(html).show();
        $('#duplicate-warning')[0].scrollIntoView({ behavior: 'smooth', block: 'nearest' });

        $('#duplicate-warning').off('click', '.btn-flag-stale').on('click', '.btn-flag-stale', function() {
            const btn = $(this);
            const proposalId = btn.data('proposal-id');
            const mappingType = btn.data('mapping-type');
            btn.prop('disabled', true).text('Flagging...');
            $.post('/flag_proposal_stale', { proposal_id: proposalId, mapping_type: mappingType }, function() {
                btn.text('Flagged — admin has been notified');
            }).fail(function() {
                btn.prop('disabled', false).text('Flag as Stale for Admin Review');
                alert('Failed to flag proposal. Please try again.');
            });
        });

        $('#duplicate-warning').off('click', '.btn-submit-revision').on('click', '.btn-submit-revision', function() {
            alert('To submit a revision, go to the Explore page, find this mapping, and use the Propose Change button.');
        });
    }

    checkForDuplicatePair_go() {
        const keId = $('#ke_id').val();
        const goId = this.selectedGoTerm ? this.selectedGoTerm.goId : '';
        if (!keId || !goId) return;

        $('#duplicate-warning-go').hide().empty();

        $.post('/check_go_entry', { ke_id: keId, go_id: goId }, (result) => {
            if (result.pair_exists && result.blocking_type) {
                this.renderDuplicateWarning_go(result);
            }
        });
    }

    renderDuplicateWarning_go(result) {
        const ex = result.existing;
        let html = '<div class="alert alert-warning" style="border: 2px solid var(--color-status-medium); padding: 16px; border-radius: 6px; margin: 12px 0;">';

        if (result.blocking_type === 'approved_mapping') {
            html += '<h4 style="margin-top:0;">This pair already has an approved GO mapping</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + (ex.ke_id || '') + ' — ' + (ex.ke_title || '') + '</dd>';
            html += '<dt>GO Term</dt><dd>' + (ex.go_id || '') + ' — ' + (ex.go_name || '') + '</dd>';
            html += '<dt>Confidence</dt><dd>' + (ex.confidence_level || '') + '</dd>';
            html += '<dt>Curator</dt><dd>' + (ex.approved_by_curator || 'unknown') + '</dd>';
            html += '</dl>';
            html += '<button type="button" class="btn-submit-revision" data-mapping-id="' + ex.id + '" style="margin-top:8px;">Submit Revision Proposal</button>';
        } else if (result.blocking_type === 'pending_proposal') {
            html += '<h4 style="margin-top:0;">A pending proposal already exists for this GO pair</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + (ex.ke_id || '') + ' — ' + (ex.ke_title || '') + '</dd>';
            html += '<dt>GO Term</dt><dd>' + (ex.go_id || '') + ' — ' + (ex.go_name || '') + '</dd>';
            html += '<dt>Submitted by</dt><dd>' + (ex.submitted_by || 'unknown') + '</dd>';
            html += '<dt>Submitted</dt><dd>' + (ex.submitted_at || '') + '</dd>';
            html += '</dl>';
            html += '<button type="button" class="btn-flag-stale" data-proposal-id="' + ex.proposal_id + '" data-mapping-type="go" style="margin-top:8px;">Flag as Stale for Admin Review</button>';
        }

        html += '</div>';
        $('#duplicate-warning-go').html(html).show();
        $('#duplicate-warning-go')[0].scrollIntoView({ behavior: 'smooth', block: 'nearest' });

        $('#duplicate-warning-go').off('click', '.btn-flag-stale').on('click', '.btn-flag-stale', function() {
            const btn = $(this);
            const proposalId = btn.data('proposal-id');
            const mappingType = btn.data('mapping-type');
            btn.prop('disabled', true).text('Flagging...');
            $.post('/flag_proposal_stale', { proposal_id: proposalId, mapping_type: mappingType }, function() {
                btn.text('Flagged — admin has been notified');
            }).fail(function() {
                btn.prop('disabled', false).text('Flag as Stale for Admin Review');
                alert('Failed to flag proposal. Please try again.');
            });
        });

        $('#duplicate-warning-go').off('click', '.btn-submit-revision').on('click', '.btn-submit-revision', function() {
            alert('To submit a revision, go to the Explore page, find this GO mapping, and use the Propose Change button.');
        });
    }

    /**
     * Prefetch the gene list for a KE and memoise the Promise.
     *
     * Phase 31 / D-13, D-16:
     *   _cachedKeGenes[keId] is a Promise<string[]> resolved with the gene array
     *   (or [] on fetch failure — silent per D-16, the viewer itself is fine).
     *   Distinguishes "in-flight" from "empty-result" — the v1.4 array-cache
     *   conflated the two and caused VIEWFIX-05.
     *
     * Returns the memoised Promise so callers can `await` directly:
     *   const genes = await this.prefetchKeGenes(keId);
     */
    prefetchKeGenes(keId) {
        if (!keId) return Promise.resolve([]);
        if (this._cachedKeGenes[keId] !== undefined) {
            return this._cachedKeGenes[keId];
        }
        const p = new Promise((resolve) => {
            $.getJSON('/ke_genes/' + encodeURIComponent(keId))
                .done((data) => resolve((data && data.genes) || []))
                .fail(() => resolve([]));   // D-16: silent fail, resolve to [] not reject
        });
        this._cachedKeGenes[keId] = p;
        return p;
    }

    loadInlineEmbed(pathwayId) {
        var $container = $('#wp-inline-embed');
        var $frame = $('#wp-inline-embed-frame');
        if (!pathwayId) {
            $container.hide();
            return;
        }
        $container.show();
        PathwayEmbed.mountIframe($frame, pathwayId, []);  // No gene highlighting for inline
    }

    openMappingModal(wpId, wpTitle) {
        const keId = $('#ke_id').val();

        $('#wpMappingModalTitle').text(wpTitle || wpId);
        $('#wpMappingModalExtLink').attr('href', 'https://www.wikipathways.org/pathways/' + wpId);

        // Phase 31 / D-14: open modal chrome immediately; resolve genes async.
        // Initial meta + gene-list shows a loading state; the iframe mount is
        // deferred until the gene Promise resolves so the embed gets the right
        // ?yellow=... params.
        $('#wpMappingModalMeta').text('ID: ' + wpId + ' | Loading genes…');
        const $genesDiv = $('#wpMappingModalGenes');
        $genesDiv.html('<em>Loading highlighted genes…</em>').show();

        $('#wpMappingModal').addClass('is-visible');
        $('#wpMappingOverlay').show();

        // Empty the iframe body until genes resolve so we don't mount once
        // with no highlights then re-mount — single mount per modal open.
        $('#wpMappingModalBody').empty();

        const genePromise = keId ? this.prefetchKeGenes(keId) : Promise.resolve([]);
        genePromise.then((genes) => {
            const geneList = genes || [];
            const geneCountText = geneList.length > 0
                ? geneList.length + ' gene' + (geneList.length !== 1 ? 's' : '') + ' highlighted'
                : 'No gene highlighting';
            $('#wpMappingModalMeta').text('ID: ' + wpId + ' | ' + geneCountText);

            if (geneList.length > 0) {
                $genesDiv.html('<strong>Highlighted genes:</strong> <span class="wp-gene-list">' + geneList.join(', ') + '</span>').show();
            } else {
                $genesDiv.hide().empty();
            }

            // Mount iframe AFTER modal is visible AND genes resolved.
            PathwayEmbed.mountIframe('#wpMappingModalBody', wpId, geneList);
        });
    }

    closeMappingModal() {
        $('#wpMappingModal').removeClass('is-visible');
        $('#wpMappingOverlay').hide();
    }

    hidePathwaySuggestions() {
        $("#pathway-suggestions").html(`
            <p class="text-muted-italic" style="text-align: center; padding: 20px;">Select a Key Event in Step 1 to see pathway suggestions.</p>
        `);
        $('#selected-pathway-banner').hide();
        $('#wp-inline-embed').hide();
    }

    setupPathwaySearch() {
        const $searchInput = $("#pathway-search");
        const $searchResults = $("#search-results");
        let searchTimeout;
        
        // Handle search input with debouncing
        $searchInput.on('input', (e) => {
            clearTimeout(searchTimeout);
            const query = $(e.target).val().trim();
            
            if (query.length < 2) {
                $searchResults.hide();
                return;
            }
            
            // Debounce search requests
            searchTimeout = setTimeout(() => {
                this.performPathwaySearch(query);
            }, 300);
        });
        
        // Handle focus/blur events
        $searchInput.on('focus', () => {
            if ($searchResults.children().length > 0) {
                $searchResults.show();
            }
        });
        
        $searchInput.on('blur', (e) => {
            // Delay hiding to allow for clicks on results
            setTimeout(() => {
                $searchResults.hide();
            }, 150);
        });
        
        // Handle keyboard navigation
        $searchInput.on('keydown', (e) => {
            const $items = $searchResults.find('.search-result-item');
            const $active = $items.filter('.active');
            
            switch(e.key) {
                case 'ArrowDown':
                    e.preventDefault();
                    if ($active.length === 0) {
                        $items.first().addClass('active');
                    } else {
                        $active.removeClass('active');
                        const $next = $active.next('.search-result-item');
                        if ($next.length > 0) {
                            $next.addClass('active');
                        } else {
                            $items.first().addClass('active');
                        }
                    }
                    break;
                    
                case 'ArrowUp':
                    e.preventDefault();
                    if ($active.length === 0) {
                        $items.last().addClass('active');
                    } else {
                        $active.removeClass('active');
                        const $prev = $active.prev('.search-result-item');
                        if ($prev.length > 0) {
                            $prev.addClass('active');
                        } else {
                            $items.last().addClass('active');
                        }
                    }
                    break;
                    
                case 'Enter':
                    e.preventDefault();
                    if ($active.length > 0) {
                        $active.click();
                    }
                    break;
                    
                case 'Escape':
                    $searchResults.hide();
                    $searchInput.blur();
                    break;
            }
        });
    }

    performPathwaySearch(query) {
        const $searchResults = $("#search-results");
        
        // Show enhanced loading state
        $searchResults.html(`
            <div class="search-loading">
                <div style="margin-bottom: 10px;">Searching pathways...</div>
                <div class="spinner spinner--xs"></div>
            </div>
        `).show();
        
        // Make search request
        $.getJSON(`/search_pathways?q=${encodeURIComponent(query)}&threshold=0.2&limit=10`)
            .done((data) => {
                this.displaySearchResults(data.results, query);
            })
            .fail((xhr, status, error) => {
                console.error('Search failed:', error);
                $searchResults.html('<div class="login-warning" style="padding: 10px;">Unable to search pathways. Please check your internet connection and try again.</div>');
            });
    }

    displaySearchResults(results, query) {
        const $searchResults = $("#search-results");
        
        if (results.length === 0) {
            $searchResults.html(`
                <div class="text-muted" style="padding: 15px; text-align: center;">
                    <div style="margin-bottom: 8px;">No pathways found</div>
                    <div style="font-size: 12px;">Try different keywords or reduce specificity</div>
                </div>
            `).show();
            return;
        }
        
        let resultsHtml = '';
        results.forEach(result => {
            const relevancePercentage = Math.round(result.relevance_score * 100);
            const titleHighlighted = this.highlightSearchTerms(result.pathwayTitle, query);
            const descriptionSnippet = result.pathwayDescription 
                ? this.truncateText(result.pathwayDescription, 100) 
                : 'No description available';
            
            resultsHtml += `
                <div class="search-result-item"
                     data-pathway-id="${this.escapeHtml(result.pathwayID)}" data-pathway-title="${this.escapeHtml(result.pathwayTitle)}" data-pathway-description="${this.escapeHtml(result.pathwayDescription || '')}">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;">
                        <div style="flex: 1;">
                            <div class="text-dark-heading" style="display: flex; align-items: center; gap: 6px; font-weight: bold; margin-bottom: 4px; flex-wrap: wrap;">
                                ${titleHighlighted}
                                ${this.renderGeneSetSizeChip(result.pathway_total_genes)}
                            </div>
                            <div class="text-muted" style="font-size: 11px; margin-bottom: 4px;">
                                ID: ${result.pathwayID}
                            </div>
                            <div class="text-muted" style="font-size: 12px; margin-bottom: 8px;">
                                ${descriptionSnippet}
                            </div>
                            <button onclick="event.stopPropagation(); window.KEWPApp.showPathwayPreview('${this.escapeHtml(result.pathwayID)}', '${this.escapeHtml(result.pathwayTitle)}', '${this.escapeHtml(result.pathwaySvgUrl || '')}')"
                                    class="pathway-preview-trigger" style="font-size: 10px; padding: 3px 6px; border-radius: 2px;">
                                Preview
                            </button>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <div class="center-text">
                                <div class="biolevel-chip" style="font-size: 10px; font-weight: bold;">
                                    ${relevancePercentage}%
                                </div>
                                <div class="text-muted" style="font-size: 9px; margin-top: 2px;">match</div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });
        
        $searchResults.html(resultsHtml).show();
        
        // Add click handlers
        $searchResults.find('.search-result-item').on('click', (e) => {
            const $item = $(e.currentTarget);
            const pathwayId = $item.data('pathway-id');
            const pathwayTitle = $item.data('pathway-title');
            const pathwayDescription = $item.data('pathway-description');

            this.selectSearchResult(pathwayId, pathwayTitle, pathwayDescription);
        });
    }

    selectSearchResult(pathwayId, pathwayTitle, pathwayDescription) {
        // Clear search
        $("#pathway-search").val('');
        $("#search-results").hide();

        // Search/browse selections have no suggestion score — clear the field
        $('#suggestion_score').val('');

        // Find the first available pathway dropdown (select[name='wp_id'])
        const $dropdown = $("select[name='wp_id']").first();
        const $option = $dropdown.find(`option[value="${pathwayId}"]`);

        if ($option.length > 0) {
            // Select the pathway
            $dropdown.val(pathwayId).trigger('change');
            // Pathway selected from search results
            this.showMessage(`Selected pathway: ${pathwayTitle}`, "success");
        } else {
            // Pathway not in dropdown - need to add it dynamically
            // Adding pathway to dropdown

            // Add option to dropdown with description
            const svgUrl = `https://www.wikipathways.org/wikipathways-assets/pathways/${pathwayId}/${pathwayId}.svg`;
            $dropdown.append(`<option value="${pathwayId}" data-title="${pathwayTitle}" data-description="${this.escapeHtml(pathwayDescription || '')}" data-svg-url="${svgUrl}">${pathwayId} - ${pathwayTitle}</option>`);

            // Select the newly added pathway
            $dropdown.val(pathwayId).trigger('change');

            this.showMessage(`Added and selected pathway: ${pathwayTitle}`, "success");
        }

        // Fire live duplicate check now that pathway is selected
        setTimeout(() => this.checkForDuplicatePair(), 100);
    }

    // --- GO Term Search ---

    setupGoStep2SubTabs() {
        $(document).on('click', '.go-step2-subtab', (e) => {
            const $btn = $(e.currentTarget);
            const subtab = $btn.data('subtab');

            $('.go-step2-subtab').removeClass('active');
            $btn.addClass('active');

            $('.go-step2-panel').hide();
            $(`#go-step2-panel-${subtab}`).show();
        });
    }

    setupGoTermSearch() {
        const $searchInput = $("#go-term-search");
        const $searchResults = $("#go-search-results");
        let searchTimeout;

        $searchInput.on('input', (e) => {
            clearTimeout(searchTimeout);
            const query = $(e.target).val().trim();

            if (query.length < 2) {
                $searchResults.hide();
                return;
            }

            searchTimeout = setTimeout(() => {
                this.performGoTermSearch(query);
            }, 300);
        });

        $searchInput.on('focus', () => {
            if ($searchResults.children().length > 0) {
                $searchResults.show();
            }
        });

        $searchInput.on('blur', () => {
            setTimeout(() => {
                $searchResults.hide();
            }, 150);
        });

        $searchInput.on('keydown', (e) => {
            const $items = $searchResults.find('.search-result-item');
            const $active = $items.filter('.active');

            switch(e.key) {
                case 'ArrowDown':
                    e.preventDefault();
                    if ($active.length === 0) {
                        $items.first().addClass('active');
                    } else {
                        $active.removeClass('active');
                        const $next = $active.next('.search-result-item');
                        ($next.length > 0 ? $next : $items.first()).addClass('active');
                    }
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    if ($active.length === 0) {
                        $items.last().addClass('active');
                    } else {
                        $active.removeClass('active');
                        const $prev = $active.prev('.search-result-item');
                        ($prev.length > 0 ? $prev : $items.last()).addClass('active');
                    }
                    break;
                case 'Enter':
                    e.preventDefault();
                    if ($active.length > 0) {
                        $active.click();
                    }
                    break;
                case 'Escape':
                    $searchResults.hide();
                    $searchInput.blur();
                    break;
            }
        });
    }

    performGoTermSearch(query) {
        const $searchResults = $("#go-search-results");

        $searchResults.html(`
            <div class="search-loading">
                <div style="margin-bottom: 10px;">Searching GO terms...</div>
                <div class="spinner spinner--xs"></div>
            </div>
        `).show();

        $.getJSON(`/search_go_terms?q=${encodeURIComponent(query)}&threshold=0.2&limit=10`)
            .done((data) => {
                this.displayGoSearchResults(data.results, query);
            })
            .fail(() => {
                $searchResults.html('<div class="login-warning" style="padding: 10px;">Unable to search GO terms. Please try again.</div>');
            });
    }

    displayGoSearchResults(results, query) {
        const $searchResults = $("#go-search-results");

        if (results.length === 0) {
            $searchResults.html(`
                <div class="text-muted" style="padding: 15px; text-align: center;">
                    <div style="margin-bottom: 8px;">No GO terms found</div>
                    <div style="font-size: 12px;">Try different keywords or broader terms</div>
                </div>
            `).show();
            return;
        }

        let html = '';
        results.forEach(result => {
            const relevancePct = Math.round(result.relevance_score * 100);
            const nameHighlighted = this.highlightSearchTerms(result.go_name, query);
            const defSnippet = result.go_definition
                ? this.truncateText(result.go_definition, 120)
                : 'No definition available';
            const searchNs = result.go_namespace || 'BP';
            const searchNsBadge = searchNs === 'MF'
                ? '<span class="badge-go-mf">MF</span>'
                : '<span class="badge-go-bp">BP</span>';

            html += `
                <div class="search-result-item"
                     data-go-id="${this.escapeHtml(result.go_id)}"
                     data-go-name="${this.escapeHtml(result.go_name)}"
                     data-go-namespace="${this.escapeHtml(searchNs)}"
                     data-gene-count="${result.go_gene_count != null ? result.go_gene_count : ''}"
                     data-gene-count-direct="${result.go_gene_count_direct != null ? result.go_gene_count_direct : ''}">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;">
                        <div style="flex: 1;">
                            <div class="text-dark-heading" style="display: flex; align-items: center; gap: 6px; font-weight: bold; margin-bottom: 4px; flex-wrap: wrap;">
                                ${searchNsBadge}
                                ${nameHighlighted}
                                ${this.renderGeneSetSizeChip(result.go_gene_count, result.go_gene_count_direct)}
                            </div>
                            <div class="text-muted" style="font-size: 11px; margin-bottom: 4px;">
                                ${result.go_id}
                                &middot;
                                <a href="${this.escapeHtml(result.quickgo_link)}" target="_blank" rel="noopener" onclick="event.stopPropagation();" style="font-size: 11px;">QuickGO</a>
                            </div>
                            <div class="text-muted" style="font-size: 12px;">
                                ${defSnippet}
                            </div>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <div class="center-text">
                                <div class="biolevel-chip" style="font-size: 10px; font-weight: bold;">
                                    ${relevancePct}%
                                </div>
                                <div class="text-muted" style="font-size: 9px; margin-top: 2px;">match</div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });

        $searchResults.html(html).show();

        $searchResults.find('.search-result-item').on('click', (e) => {
            const $item = $(e.currentTarget);
            this.selectGoSearchResult($item.data('go-id'), $item.data('go-name'), $item.data('go-namespace') || 'BP',
                { count: $item.data('gene-count'), direct: $item.data('gene-count-direct') });
        });
    }

    selectGoSearchResult(goId, goName, goNamespace = 'BP', geneSet = null) {
        $("#go-term-search").val('');
        $("#go-search-results").hide();
        this.selectGoTerm(goId, goName, goNamespace, geneSet);
    }

    highlightSearchTerms(text, query) {
        if (!query || query.length < 2) return text;
        
        const words = query.toLowerCase().split(/\s+/).filter(word => word.length > 1);
        let highlightedText = text;
        
        words.forEach(word => {
            const regex = new RegExp(`(${word})`, 'gi');
            highlightedText = highlightedText.replace(regex, '<mark class="search-highlight">$1</mark>');
        });
        
        return highlightedText;
    }

    truncateText(text, maxLength) {
        if (!text || text.length <= maxLength) return text;
        return text.substring(0, maxLength) + '...';
    }

    showPathwayPreview(pathwayID, pathwayTitle, svgUrl) {
        this.openMappingModal(pathwayID, pathwayTitle);
    }

    // =========================================================================
    // KE-GO Mapping Tab Methods
    // =========================================================================

    handleTabSwitch(tab) {
        if (tab === this.activeTab) return;

        this.activeTab = tab;

        // Update tab button styles
        $('.mapping-tab').each(function() {
            const isActive = $(this).data('tab') === tab;
            if (isActive) {
                $(this).addClass('active');
            } else {
                $(this).removeClass('active');
            }
        });

        // Toggle tab content
        const keId = $('#ke_id').val();
        const keTitle = $('#ke_id option:selected').data('title');

        // Hide all three panels first; show the selected one below
        $('#wp-tab-content, #go-tab-content, #reactome-tab-content').hide();

        // Reset upstream pathway link on tab switch — a fresh tab has no selection yet
        this.updatePathwayUpstreamLink(null, null);

        if (tab === 'wp') {
            $('#wp-tab-content').show();
            // Reload WP suggestions if a KE is selected
            if (keId && keTitle) {
                this.loadPathwaySuggestions(keId, keTitle, this.currentMethodFilter);
            }
        } else if (tab === 'go') {
            $('#go-tab-content').show();
            // Load GO suggestions if a KE is already selected
            if (keId && keTitle) {
                this.loadGoSuggestions(keId, keTitle, this.goMethodFilter, this.goAspectFilter);
            }
        } else if (tab === 'reactome') {
            $('#reactome-tab-content').show();
            // Load Reactome suggestions if a KE is already selected
            if (keId && keTitle) {
                this.loadReactomeSuggestions(keId, keTitle);
            }
        }
    }

    loadGoSuggestions(keId, keTitle, methodFilter = 'all', aspectFilter = 'all') {
        this.goMethodFilter = methodFilter;
        this.goAspectFilter = aspectFilter;
        const $container = $('#go-suggestions-container');

        // Show loading state
        $container.html(`
            <div style="text-align: center; padding: 20px;">
                <div class="spinner spinner--md"></div>
                <p class="text-muted" style="margin-top: 10px;">Loading GO term suggestions for this Key Event...</p>
            </div>
        `);

        const encodedKeId = encodeURIComponent(keId);
        const encodedKeTitle = encodeURIComponent(keTitle);

        $.getJSON(`/suggest_go_terms/${encodedKeId}?ke_title=${encodedKeTitle}&limit=20&aspect_filter=${encodeURIComponent(aspectFilter)}`)
            .done((data) => {
                this.displayGoSuggestions(data, methodFilter, aspectFilter);
            })
            .fail((xhr, status, error) => {
                console.error('Failed to load GO suggestions:', error);
                $container.html(`
                    <div class="login-warning" style="padding: 15px; text-align: center;">
                        <p style="font-weight: bold;">Unable to load GO term suggestions.</p>
                        <p class="text-muted" style="font-size: 13px;">The GO suggestion service may not be available. Please try again later.</p>
                    </div>
                `);
            });
    }

    displayGoSuggestions(data, filter = 'all', aspectFilter = 'all') {
        // Store full data for pagination
        this.goSuggestionsData = data;
        this.goSuggestionsFilter = filter;
        this.goSuggestionsAspectFilter = aspectFilter;
        this.goSuggestionsPage = 0;

        this.renderGoSuggestionsPage();
    }

    renderGoSuggestionsPage() {
        const $container = $('#go-suggestions-container');
        const data = this.goSuggestionsData;
        const filter = this.goSuggestionsFilter || 'all';
        const aspectFilter = this.goSuggestionsAspectFilter || 'all';
        const suggestions = data.suggestions || [];

        if (suggestions.length === 0) {
            $container.html(`
                <div style="padding: 20px; text-align: center;">
                    ${this.buildGoMethodFilterHtml(filter, aspectFilter)}
                    ${data.genes_found > 0 ? `
                        <div class="gene-info-panel">
                            <strong>Associated Genes:</strong> ${data.gene_list.join(', ')} (${data.genes_found} gene${data.genes_found !== 1 ? 's' : ''} found)
                        </div>
                    ` : ''}
                    <div class="text-muted panel-outlined" style="padding: 20px;">
                        <p style="font-weight: bold;">No GO term suggestions found for this Key Event.</p>
                        <p style="font-size: 13px;">Try a different method filter or aspect, or select a different Key Event.</p>
                    </div>
                </div>
            `);
            this.setupGoFilterButtons(filter, aspectFilter);
            return;
        }

        // Pagination
        const perPage = this.goSuggestionsPerPage;
        const totalPages = Math.ceil(suggestions.length / perPage);
        const page = Math.min(this.goSuggestionsPage, totalPages - 1);
        const startIdx = page * perPage;
        const endIdx = Math.min(startIdx + perPage, suggestions.length);
        const pageSuggestions = suggestions.slice(startIdx, endIdx);

        // Update KE direction badge in context panel (ke_direction is same for all suggestions)
        const keDirectionFromSuggestion = suggestions.length > 0 ? (suggestions[0].ke_direction || 'unspecified') : 'unspecified';
        this.updateKEDirectionBadge(keDirectionFromSuggestion);

        let html = `
            <div style="margin-bottom: 15px;">
                ${this.buildGoMethodFilterHtml(filter, aspectFilter)}
            </div>
        `;

        // Gene info
        if (data.genes_found > 0) {
            html += `
                <div class="gene-info-panel">
                    <strong>Associated Genes:</strong> ${data.gene_list.join(', ')} (${data.genes_found} gene${data.genes_found !== 1 ? 's' : ''} found)
                </div>
            `;
        }

        html += `<div class="text-muted" style="font-size: 13px; margin-bottom: 10px;">Showing ${startIdx + 1}-${endIdx} of ${suggestions.length} suggestions</div>`;

        // Suggestion list (current page only)
        pageSuggestions.forEach((suggestion, index) => {
            const matchBadges = this.getGoMatchBadges(suggestion.match_types || []);
            const depthBadge = (suggestion.depth !== undefined && suggestion.depth > 0)
                ? `<span class="badge-match--depth">Depth: ${suggestion.depth}</span>`
                : '';
            const scorePercent = Math.round((suggestion.hybrid_score || 0) * 100);
            const scoreTier = scorePercent >= 60 ? 'high' : scorePercent >= 30 ? 'medium' : 'low';
            const rawDefinition = suggestion.go_definition
                ? (suggestion.go_definition.length > 200 ? suggestion.go_definition.substring(0, 200) + '...' : suggestion.go_definition)
                : 'No definition available';
            const definition = this.escapeHtml(rawDefinition);

            // Namespace badge (BP blue, MF purple)
            const ns = suggestion.go_namespace || 'BP';
            const namespaceBadge = ns === 'MF'
                ? '<span class="badge-go-mf">MF</span>'
                : '<span class="badge-go-bp">BP</span>';

            // Direction badge for GO term
            const goDir = suggestion.go_direction;
            const keDir = suggestion.ke_direction;
            let directionBadge = '';
            if (goDir === 'positive' || goDir === 'negative') {
                const arrow = goDir === 'positive' ? '&#8593;' : '&#8595;';
                const label = goDir === 'positive' ? 'Positive' : 'Negative';
                directionBadge = `<span class="badge-direction badge-direction--${goDir}">${arrow} ${label}</span>`;
                // Alignment indicator: only when both KE and GO directions are specified
                if (keDir && keDir !== 'unspecified' && goDir !== 'unspecified') {
                    if (keDir === goDir) {
                        directionBadge += `<span class="badge-direction-align badge-direction-align--match">&#10003;</span>`;
                    } else {
                        directionBadge += `<span class="badge-direction-align badge-direction-align--mismatch">&#10007;</span>`;
                    }
                }
            }

            const goGeneOverlapChip = this.renderGeneOverlapChip(suggestion, data.genes_found || 0);
            const goGeneSetChip = this.renderGeneSetSizeChip(
                suggestion.go_gene_count, suggestion.go_gene_count_direct);

            html += `
                <div class="go-suggestion-item go-suggestion-item--${scoreTier}" data-go-id="${suggestion.go_id}" data-go-name="${this.escapeHtml(suggestion.go_name)}" data-go-namespace="${this.escapeHtml(ns)}" data-gene-count="${suggestion.go_gene_count != null ? suggestion.go_gene_count : ''}" data-gene-count-direct="${suggestion.go_gene_count_direct != null ? suggestion.go_gene_count_direct : ''}">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                        <div style="flex: 1;">
                            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap;">
                                ${namespaceBadge}
                                <strong style="font-size: 14px;" class="text-dark-heading">${this.escapeHtml(suggestion.go_name)}</strong>
                                ${matchBadges}${depthBadge}${directionBadge}
                                ${goGeneOverlapChip}${goGeneSetChip}
                            </div>
                            <div class="text-muted" style="font-size: 12px; margin-bottom: 6px;">
                                <a href="${suggestion.quickgo_link}" target="_blank" onclick="event.stopPropagation();">${suggestion.go_id}</a>
                            </div>
                            <div class="text-subtle" style="font-size: 12px; margin-bottom: 8px; line-height: 1.4;">
                                ${definition}
                            </div>
            `;

            // Synonyms (EXACT only, max 5)
            const exactSynonyms = (suggestion.synonyms || [])
                .filter(s => s.type === 'EXACT')
                .map(s => s.text)
                .slice(0, 5);
            if (exactSynonyms.length > 0) {
                html += `
                    <div class="text-xsmall-muted" style="margin-bottom: 6px;">
                        Also known as: ${exactSynonyms.map(s => this.escapeHtml(s)).join(', ')}
                    </div>
                `;
            }

            html += `
                        </div>
                        <div style="text-align: right; min-width: 80px;">
                            <div class="text-muted" style="font-size: 11px; margin-bottom: 2px;">Score</div>
                            <div style="display: flex; align-items: center; gap: 6px; justify-content: flex-end;">
                                <div class="score-bar-track--sm">
                                    <div class="score-bar-fill score-bar-fill--${scoreTier}" style="width: ${scorePercent}%;"></div>
                                </div>
                                <span class="confidence-bar-value--${scoreTier}" style="font-weight: bold; font-size: 13px;">${scorePercent}%</span>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });

        // Pagination controls (only if more than one page)
        if (totalPages > 1) {
            html += `
                <div style="display: flex; justify-content: center; align-items: center; gap: 12px; margin-top: 15px; padding-top: 10px; border-top: 1px solid var(--color-border-light);">
                    <button class="go-page-prev pagination-btn ${page === 0 ? 'pagination-btn--disabled' : 'pagination-btn--active'}" ${page === 0 ? 'disabled' : ''}>
                        Previous
                    </button>
                    <span class="text-muted" style="font-size: 13px;">Page ${page + 1} of ${totalPages}</span>
                    <button class="go-page-next pagination-btn ${page >= totalPages - 1 ? 'pagination-btn--disabled' : 'pagination-btn--active'}" ${page >= totalPages - 1 ? 'disabled' : ''}>
                        Next
                    </button>
                </div>
            `;
        }

        html += `
            <div class="text-muted" style="margin-top: 15px; padding-top: 10px; border-top: 1px solid var(--color-border-light); font-size: 12px; text-align: center;">
                Click a GO term to select it for confidence assessment
            </div>
        `;

        $container.html(html);
        this.setupGoFilterButtons(filter, aspectFilter);
        this.setupGoPaginationButtons();

        // Bind click handlers using data attributes instead of inline onclick
        $container.find('.go-suggestion-item').off('click').on('click', (e) => {
            const $item = $(e.currentTarget);
            this.selectGoTerm($item.data('go-id'), $item.data('go-name'), $item.data('go-namespace') || 'BP',
                { count: $item.data('gene-count'), direct: $item.data('gene-count-direct') });
        });
    }

    setupGoPaginationButtons() {
        $('.go-page-prev').off('click').on('click', () => {
            if (this.goSuggestionsPage > 0) {
                this.goSuggestionsPage--;
                this.renderGoSuggestionsPage();
                $('#go-suggestions-container')[0].scrollIntoView({ behavior: 'smooth' });
            }
        });
        $('.go-page-next').off('click').on('click', () => {
            const totalPages = Math.ceil((this.goSuggestionsData?.suggestions?.length || 0) / this.goSuggestionsPerPage);
            if (this.goSuggestionsPage < totalPages - 1) {
                this.goSuggestionsPage++;
                this.renderGoSuggestionsPage();
                $('#go-suggestions-container')[0].scrollIntoView({ behavior: 'smooth' });
            }
        });
    }

    buildGoMethodFilterHtml(currentFilter, currentAspect = 'all') {
        const aspectFilters = [
            { value: 'all', label: 'All GO' },
            { value: 'bp', label: 'BP only' },
            { value: 'mf', label: 'MF only' }
        ];

        let html = `
            <div style="padding: 10px; background: var(--color-white); border: 1px solid var(--color-border-light); border-radius: 6px; margin-bottom: 10px;">
                <div style="display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start;">
                    <div>
                        <label style="font-weight: bold; display: block; margin-bottom: 8px; font-size: 12px;" class="text-dark-heading">Aspect:</label>
                        <div class="btn-group" role="group" style="display: flex; gap: 6px; flex-wrap: wrap;">
        `;

        aspectFilters.forEach(f => {
            const isActive = f.value === currentAspect;
            html += `
                <button type="button" class="go-aspect-filter-btn method-filter-btn ${isActive ? 'method-filter-btn--active active' : 'method-filter-btn--inactive'}" data-aspect="${f.value}">
                    ${f.label}
                </button>
            `;
        });

        html += `
                        </div>
                    </div>
                </div>
            </div>
        `;
        return html;
    }

    setupGoFilterButtons(currentFilter, currentAspect = 'all') {
        // Aspect filter buttons
        $('.go-aspect-filter-btn').off('click').on('click', (e) => {
            const aspect = $(e.currentTarget).data('aspect');
            this.goAspectFilter = aspect;
            $('.go-aspect-filter-btn').removeClass('active method-filter-btn--active').addClass('method-filter-btn--inactive');
            $(e.currentTarget).removeClass('method-filter-btn--inactive').addClass('active method-filter-btn--active');

            const keId = $('#ke_id').val();
            const keTitle = $('#ke_id option:selected').data('title');
            if (keId && keTitle) {
                this.loadGoSuggestions(keId, keTitle, this.goMethodFilter, aspect);
            }
        });
    }

    getGoMatchBadges(matchTypes) {
        const badges = [];
        if (matchTypes.includes('text')) {
            badges.push('<span class="badge-match--semantic">Semantic</span>');
        }
        if (matchTypes.includes('gene')) {
            badges.push('<span class="badge-match--gene">Gene</span>');
        }
        return badges.join(' ');
    }

    selectGoTerm(goId, goName, goNamespace = 'BP', geneSet = null) {
        this.selectedGoTerm = { goId, goName, goNamespace };

        // Highlight selected item
        $('.go-suggestion-item').removeClass('go-suggestion-item--selected');
        $(`.go-suggestion-item[data-go-id="${goId}"]`).addClass('go-suggestion-item--selected');

        // Update upstream AmiGO link
        this.updatePathwayUpstreamLink('go', goId);

        // Fire live duplicate check for the KE-GO pair
        this.checkForDuplicatePair_go();

        // Show GO confidence assessment
        this.showGoAssessmentForm(goId, goName, geneSet);

        // Scroll to confidence assessment section
        setTimeout(() => {
            const section = document.getElementById('go-confidence-guide');
            if (section) {
                section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }, 100);
    }

    showGoAssessmentForm(goId, goName, geneSet = null) {
        const $section = $('#go-confidence-guide');
        const $form = $('#go-assessment-form');

        // Reset GO assessment state
        this.goAssessmentAnswers = {};
        this.goMappingResult = null;

        const config = this.goScoringConfig;
        const connectionTypes = config.connection_types || ['describes', 'involves', 'related', 'context'];

        const dimensionTooltips = {
            connection: 'How directly does this GO term relate to the key event\'s biological mechanism? High: direct mechanistic link. Medium: functionally related process. Low: broadly associated.',
            specificity: 'How precisely does this GO term describe the key event? High: exact match to the biological process. Medium: correct but broader/narrower. Low: tangentially relevant.',
            evidence: 'How strong is the literature evidence linking this GO term to this key event? High: multiple experimental studies. Medium: curated or computational evidence. Low: inferred or assumed.'
        };

        // Testability caution, rendered full-width rather than as a chip (#210).
        // This form is the last moment before the curator commits, and a mapping
        // below the Analyser's five-gene floor is one whose Key Event will be
        // silently dropped from every analysis. Advisory, never blocking — the
        // semantically correct term is still worth recording when the annotation
        // data is what is deficient.
        const geneCount = geneSet && geneSet.count !== '' && geneSet.count != null
            ? Number(geneSet.count) : null;
        let geneSetNotice = '';
        if (geneCount !== null && Number.isFinite(geneCount) && geneCount < 5) {
            geneSetNotice = `
                <div class="go-geneset-caution" role="status">
                    <strong>Resolves to ${geneCount} gene${geneCount === 1 ? '' : 's'}.</strong>
                    The molAOP Analyser needs at least 5 to test a Key Event, so this
                    mapping may leave the Key Event untestable. Record it anyway if it
                    is the most faithful term &mdash; but prefer a term that also
                    carries a usable gene set where one exists.
                </div>
            `;
        }

        let html = `
            <div class="go-assessment go-assessment-wrapper" data-go-id="${goId}">
                <h4 style="margin: 0 0 15px 0; border-bottom: 1px solid var(--color-border-light); padding-bottom: 8px;" class="text-dark-heading">
                    Assessment for: ${this.escapeHtml(goName)}
                    <span class="text-muted" style="font-size: 13px; font-weight: normal;">(${goId})</span>
                </h4>
                ${geneSetNotice}

                <!-- Connection Type dropdown (separate metadata field) -->
                <div style="margin-bottom: 16px;">
                    <label style="font-weight: 600; display: block; margin-bottom: 6px;">Connection Type
                        <span class="tooltip" data-tooltip="describes: GO term directly describes KE mechanism; involves: KE involves this process; related: Related biological process; context: Provides context" style="cursor: help; font-size: 14px;">&#9432;</span>
                    </label>
                    <select id="go-connection-type-select" style="padding: 6px 10px; border: 1px solid var(--color-border-light); border-radius: var(--radius-sm); font-size: var(--font-size-sm);">
                        <option value="">-- Select connection type --</option>
                        ${connectionTypes.map(ct => `<option value="${ct}">${ct.charAt(0).toUpperCase() + ct.slice(1)}</option>`).join('')}
                    </select>
                </div>

                <!-- Dimension: Connection score -->
                <div class="go-dimension-row" style="margin-bottom: 14px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                        <span style="font-weight: 600; min-width: 200px;">Connection (biological relevance)
                            <span class="tooltip" data-tooltip="${dimensionTooltips.connection}" style="cursor: help; font-size: 14px;">&#9432;</span>
                        </span>
                        <div class="btn-group go-btn-group" data-dimension="connection" style="margin: 0;">
                            <button type="button" class="btn-option go-dim-btn" data-dimension="connection" data-score="3">High</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="connection" data-score="2">Medium</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="connection" data-score="1">Low</button>
                        </div>
                    </div>
                </div>

                <!-- Dimension: Specificity score -->
                <div class="go-dimension-row" style="margin-bottom: 14px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                        <span style="font-weight: 600; min-width: 200px;">Specificity (term precision)
                            <span class="tooltip" data-tooltip="${dimensionTooltips.specificity}" style="cursor: help; font-size: 14px;">&#9432;</span>
                        </span>
                        <div class="btn-group go-btn-group" data-dimension="specificity" style="margin: 0;">
                            <button type="button" class="btn-option go-dim-btn" data-dimension="specificity" data-score="3">High</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="specificity" data-score="2">Medium</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="specificity" data-score="1">Low</button>
                        </div>
                    </div>
                </div>

                <!-- Dimension: Evidence score -->
                <div class="go-dimension-row" style="margin-bottom: 14px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                        <span style="font-weight: 600; min-width: 200px;">Evidence (literature support)
                            <span class="tooltip" data-tooltip="${dimensionTooltips.evidence}" style="cursor: help; font-size: 14px;">&#9432;</span>
                        </span>
                        <div class="btn-group go-btn-group" data-dimension="evidence" style="margin: 0;">
                            <button type="button" class="btn-option go-dim-btn" data-dimension="evidence" data-score="3">High</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="evidence" data-score="2">Medium</button>
                            <button type="button" class="btn-option go-dim-btn" data-dimension="evidence" data-score="1">Low</button>
                        </div>
                    </div>
                </div>

                <!-- Live preview badge -->
                <div id="go-dimension-preview" style="display: none; margin-top: 12px; padding: 10px 14px; background: var(--color-bg-subtle, #f8f9fa); border-radius: var(--radius-sm); border: 1px solid var(--color-border-light);">
                    Computed confidence: <span id="go-dimension-badge" class="badge-status-medium" style="font-size: 14px;">--</span>
                </div>
            </div>
        `;

        $form.html(html);
        $section.show();

        // Setup dimension button handlers
        $(document).off('click', '.go-dim-btn').on('click', '.go-dim-btn', (e) => {
            this.handleDimensionSelection(e);
        });

        // Disable submit button until all dimensions selected
        $('#go-mapping-form button[type="submit"]').prop('disabled', true).text('Complete GO Assessment First');
    }

    handleDimensionSelection(event) {
        const $btn = $(event.target).closest('.btn-option');
        if (!$btn.length) return;
        const dimension = $btn.data('dimension');
        const score = parseInt($btn.data('score'), 10);

        // Update button selection within this dimension group
        $btn.closest('.go-btn-group').find('.btn-option').removeClass('selected');
        $btn.addClass('selected');

        // Store dimension score
        if (!this.goAssessmentAnswers) this.goAssessmentAnswers = {};
        this.goAssessmentAnswers[dimension] = score;

        this.updateDimensionPreview();
    }

    updateDimensionPreview() {
        const answers = this.goAssessmentAnswers || {};
        const connScore = answers['connection'];
        const specScore = answers['specificity'];
        const evScore = answers['evidence'];

        const $preview = $('#go-dimension-preview');
        const $badge = $('#go-dimension-badge');
        const $submitBtn = $('#go-mapping-form button[type="submit"]');

        if (connScore && specScore && evScore) {
            const config = this.goScoringConfig;
            const result = this.computeDimensionConfidence(connScore, specScore, evScore, config);

            $badge
                .text(result.label.charAt(0).toUpperCase() + result.label.slice(1))
                .removeClass('badge-status-high badge-status-medium badge-status-low')
                .addClass(`badge-status-${result.label}`);
            $preview.show();

            $submitBtn.prop('disabled', false).text('Review & Submit GO Mapping');

            this.goMappingResult = {
                confidence: result.label,
                connection_type: $('#go-connection-type-select').val(),
                connection_score: connScore,
                specificity_score: specScore,
                evidence_score: evScore,
                score: result.score
            };

            // Update Step 4 display
            const goConfidenceDisplay = result.label.charAt(0).toUpperCase() + result.label.slice(1);
            $('#go-auto-confidence').text(goConfidenceDisplay);
            $('#go-auto-connection').text((this.goMappingResult.connection_type || '').charAt(0).toUpperCase() + (this.goMappingResult.connection_type || '').slice(1));
            $('#go-step-result').show();
            $('#go-step-submit').show();
        } else {
            $preview.hide();
            $submitBtn.prop('disabled', true).text('Complete GO Assessment First');
            this.goMappingResult = null;
            $('#go-step-result').hide();
            $('#go-step-submit').hide();
        }
    }

    computeDimensionConfidence(connScore, specScore, evScore, config) {
        const weights = (config && config.dimension_weights) || { connection: 0.33, specificity: 0.33, evidence: 0.34 };
        const thresholds = (config && config.dimension_thresholds) || { high: 2.5, medium: 1.5 };

        const weightedAvg = (connScore * weights.connection) + (specScore * weights.specificity) + (evScore * weights.evidence);

        let label;
        if (weightedAvg >= thresholds.high) {
            label = 'high';
        } else if (weightedAvg >= thresholds.medium) {
            label = 'medium';
        } else {
            label = 'low';
        }
        return { label, score: weightedAvg };
    }

    handleGoFormSubmission(event) {
        event.preventDefault();

        if (!this.selectedGoTerm || !this.goMappingResult) {
            this.showGoMessage("Please select a GO term and complete the assessment.", "error");
            return;
        }

        if (!this.isLoggedIn) {
            this.showGoMessage("Please log in with GitHub to submit mappings.", "error");
            setTimeout(() => {
                this.saveFormState();
                window.location.href = '/auth/login';
            }, 2000);
            return;
        }

        // Sync connection_type from dropdown at submission time
        if (this.goMappingResult) {
            this.goMappingResult.connection_type = $('#go-connection-type-select').val() || this.goMappingResult.connection_type;
        }

        // Map UI namespace label to DB value
        const nsLabel = this.selectedGoTerm.goNamespace || 'BP';
        const goNamespaceDb = nsLabel === 'MF' ? 'molecular_function' : 'biological_process';

        const formData = {
            ke_id: $('#ke_id').val(),
            ke_title: $('#ke_id option:selected').data('title'),
            go_id: this.selectedGoTerm.goId,
            go_name: this.selectedGoTerm.goName,
            go_namespace: goNamespaceDb,
            connection_type: this.goMappingResult.connection_type,
            confidence_level: this.goMappingResult.confidence,
            connection_score: this.goMappingResult.connection_score || '',
            specificity_score: this.goMappingResult.specificity_score || '',
            evidence_score: this.goMappingResult.evidence_score || '',
            suggestion_score: this.goMappingResult.score || '',
            csrf_token: this.csrfToken
        };

        if (!formData.ke_id || !formData.go_id) {
            this.showGoMessage("Please select a Key Event and a GO term.", "error");
            return;
        }

        // Check for duplicates first
        this.showGoMessage("Checking for duplicates...", "info");

        $.post("/check_go_entry", { ke_id: formData.ke_id, go_id: formData.go_id, csrf_token: this.csrfToken })
            .done((response) => {
                if (response.pair_exists) {
                    this.showGoMessage("This KE-GO mapping already exists in the dataset.", "error");
                } else {
                    this.showGoMappingPreview(formData);
                }
            })
            .fail((xhr) => {
                console.error('Error checking GO entry:', xhr);
                // Proceed to preview anyway
                this.showGoMappingPreview(formData);
            });
    }

    showGoMappingPreview(formData) {
        const $entries = $('#go-existing-entries');
        const bioLevel = $('#ke_id option:selected').data('biolevel') || 'Not specified';

        let userInfo = 'Anonymous';
        if (this.isLoggedIn) {
            const welcomeText = $('header nav p').text();
            const usernameMatch = welcomeText.match(/Welcome,\s*([^(]+)/);
            if (usernameMatch) userInfo = `GitHub: ${usernameMatch[1].trim()}`;
        }

        const previewHtml = `
            <div class="existing-entries-container" style="margin-top: 15px;">
                <h3>GO Mapping Preview & Confirmation</h3>
                <div class="mapping-preview" style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 20px 0;">
                    <div class="preview-section preview-section-card">
                        <h4 class="text-dark-heading" style="margin-top: 0;">Key Event</h4>
                        <p><strong>ID:</strong> ${formData.ke_id}</p>
                        <p><strong>Title:</strong> ${formData.ke_title}</p>
                        <p><strong>Bio Level:</strong> <span class="biolevel-chip">${bioLevel}</span></p>
                    </div>
                    <div class="preview-section preview-section-card">
                        <h4 class="text-dark-heading" style="margin-top: 0;">GO Term</h4>
                        <p><strong>ID:</strong> <a href="https://www.ebi.ac.uk/QuickGO/term/${formData.go_id}" target="_blank">${formData.go_id}</a></p>
                        <p><strong>Name:</strong> ${formData.go_name}</p>
                    </div>
                </div>
                <div class="preview-section preview-section-card" style="margin-bottom: 15px;">
                    <h4 class="text-dark-heading" style="margin-top: 0;">Mapping Details</h4>
                    <p><strong>Connection:</strong> ${formData.connection_type.charAt(0).toUpperCase() + formData.connection_type.slice(1)}</p>
                    <p><strong>Confidence:</strong> ${formData.confidence_level.charAt(0).toUpperCase() + formData.confidence_level.slice(1)}</p>
                    <p><strong>Submitted by:</strong> ${userInfo}</p>
                </div>
                <div class="confirmation-section confirmation-section--warning">
                    <p style="font-weight: bold;">Submit this KE-GO mapping?</p>
                    <button id="confirm-go-submit" class="btn-create" style="padding: 12px 24px; border-radius: 6px; margin-right: 10px;">
                        Yes, Submit Mapping
                    </button>
                    <button id="cancel-go-submit" class="btn-clear" style="padding: 12px 24px; border-radius: 6px;">
                        Cancel
                    </button>
                </div>
            </div>
        `;

        $entries.html(previewHtml);

        $('html, body').animate({
            scrollTop: $entries.offset().top - 20
        }, 500);

        $('#confirm-go-submit').on('click', (e) => {
            e.preventDefault();
            this.submitGoMapping(formData);
        });

        $('#cancel-go-submit').on('click', (e) => {
            e.preventDefault();
            $entries.html('');
        });
    }

    submitGoMapping(formData) {
        this.showGoMessage("Submitting GO mapping...", "info");

        $.post("/submit_go_mapping", formData)
            .done((response) => {
                this.showGoSuccessMessage(formData);
                $('#go-existing-entries').html('');
                this.resetGoForm();
            })
            .fail((xhr) => {
                if (xhr.status === 401 || xhr.status === 403) {
                    const expired = xhr.responseJSON?.error === "session_expired";
                    this.showGoMessage(
                        expired
                            ? "Your session has expired — please log in again, then resubmit."
                            : "Please log in to submit mappings.",
                        "error");
                    setTimeout(() => {
                        this.saveFormState();
                        window.location.href = '/auth/login';
                    }, 2000);
                } else {
                    const errorMsg = xhr.responseJSON?.error || "Failed to submit GO mapping. Please try again.";
                    this.showGoMessage(errorMsg, "error");
                }
            });
    }

    showGoSuccessMessage(formData) {
        // Use the thank you modal
        const nsLabel = formData.go_namespace === 'molecular_function' ? 'MF' : 'BP';
        const nsBadgeClass = formData.go_namespace === 'molecular_function' ? 'badge-go-mf' : 'badge-go-bp';
        const nsBadge = `<span class="${nsBadgeClass}">${nsLabel}</span>`;

        const summaryHtml = `
            <div style="margin-bottom: 15px;">
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">Key Event:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${formData.ke_id}</span><br>
                    <span style="font-size: 14px;">${formData.ke_title}</span>
                </div>
                <div style="text-align: center; margin: 10px 0; font-size: 20px;" class="text-link-blue">&#8595;</div>
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">GO Term:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${formData.go_id}</span><br>
                    <span style="font-size: 14px;">${formData.go_name}</span><br>
                    <div style="margin-top: 4px;"><strong class="text-dark-heading">Namespace:</strong> ${nsBadge}</div>
                </div>
            </div>
            <div style="border-top: 1px solid var(--color-border-light); padding-top: 15px; display: flex; justify-content: space-around; font-size: 14px;">
                <div>
                    <strong class="text-dark-heading">Connection:</strong><br>
                    <span class="text-muted">${formData.connection_type.charAt(0).toUpperCase() + formData.connection_type.slice(1)}</span>
                </div>
                <div>
                    <strong class="text-dark-heading">Confidence:</strong><br>
                    <span class="text-muted">${formData.confidence_level.charAt(0).toUpperCase() + formData.confidence_level.slice(1)}</span>
                </div>
            </div>
        `;

        $("#submissionSummary").html(summaryHtml);
        const modal = $("#thankYouModal");
        modal.css("display", "flex");

        $("#closeThankYouModal").off("click").on("click", () => modal.hide());
        modal.off("click").on("click", (e) => {
            if (e.target.id === "thankYouModal") modal.hide();
        });
        setTimeout(() => modal.fadeOut(), 10000);
    }

    showReactomeSuccessMessage(payload) {
        // Bind the Thank-You modal to the just-submitted Reactome record.
        // Mirrors showGoSuccessMessage; Reactome has no namespace badge, and
        // its connection type (relationship from step1) is only shown when present.
        const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "—");
        const connectionBlock = payload.connection_type
            ? `<div>
                    <strong class="text-dark-heading">Connection:</strong><br>
                    <span class="text-muted">${cap(payload.connection_type)}</span>
                </div>`
            : "";

        const summaryHtml = `
            <div style="margin-bottom: 15px;">
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">Key Event:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${payload.ke_id}</span><br>
                    <span style="font-size: 14px;">${payload.ke_title}</span>
                </div>
                <div style="text-align: center; margin: 10px 0; font-size: 20px;" class="text-link-blue">&#8595;</div>
                <div style="margin-bottom: 10px;">
                    <strong class="text-dark-heading">Reactome Pathway:</strong><br>
                    <span class="text-muted" style="font-family: monospace; font-size: 13px;">${payload.reactome_id}</span><br>
                    <span style="font-size: 14px;">${payload.pathway_name}</span>
                </div>
            </div>
            <div style="border-top: 1px solid var(--color-border-light); padding-top: 15px; display: flex; justify-content: space-around; font-size: 14px;">
                ${connectionBlock}
                <div>
                    <strong class="text-dark-heading">Confidence:</strong><br>
                    <span class="text-muted">${cap(payload.confidence_level)}</span>
                </div>
            </div>
        `;

        $("#submissionSummary").html(summaryHtml);
        const modal = $("#thankYouModal");
        modal.css("display", "flex");

        $("#closeThankYouModal").off("click").on("click", () => modal.hide());
        modal.off("click").on("click", (e) => {
            if (e.target.id === "thankYouModal") modal.hide();
        });
        setTimeout(() => modal.fadeOut(), 10000);
    }

    resetGoForm() {
        this.selectedGoTerm = null;
        this.goAssessmentAnswers = {};
        this.goMappingResult = null;

        $('#go-confidence-guide').hide();
        $('#go-assessment-form').html('');
        $('#go-dimension-preview').hide();
        $('#go-step-result').hide();
        $('#go-step-submit').hide();
        $('#go-mapping-form button[type="submit"]').prop('disabled', true).text('Complete GO Assessment First');
        $('#go-existing-entries').html('');
        $('#go-message').text('');

        // Restore GO assessment header to default expanded state
        $('#go-confidence-guide-header').removeClass('collapsible collapsed').off('click.collapse');
        $('#go-confidence-guide-header .step-summary').remove();
        $('#go-confidence-guide-content').show();

        // Reset suggestion highlighting
        $('.go-suggestion-item').removeClass('go-suggestion-item--selected');
    }

    hideGoSuggestions() {
        $('#go-suggestions-container').html(`
            <p class="text-muted-italic">Select a Key Event above to see GO Biological Process term suggestions.</p>
        `);
        this.resetGoForm();
    }

    showGoMessage(message, type = "info") {
        const colorClass = type === "error" ? "login-warning" : type === "success" ? "entry-status-new" : "text-link-blue";
        $("#go-message").text(message).removeClass("login-warning entry-status-new text-link-blue").addClass(colorClass).show();
        if (type === "success") {
            setTimeout(() => { $("#go-message").fadeOut(); }, 5000);
        }
    }

    // =========================================================================
    // KE-Reactome Mapping Tab Methods (Phase 25 Plan 05)
    // =========================================================================

    loadReactomeSuggestions(keId, keTitle) {
        const $container = $('#reactome-suggestions-container');
        $container.html(`
            <div style="text-align: center; padding: 20px;">
                <div class="spinner spinner--md"></div>
                <p class="text-muted" style="margin-top: 10px;">Loading Reactome pathway suggestions for this Key Event...</p>
            </div>
        `);
        const encodedKeId = encodeURIComponent(keId);
        const encodedKeTitle = encodeURIComponent(keTitle || '');
        $.getJSON(`/suggest_reactome/${encodedKeId}?ke_title=${encodedKeTitle}&limit=20`)
            .done((data) => { this.displayReactomeSuggestions(data); })
            .fail((xhr, status, error) => {
                console.error('Failed to load Reactome suggestions:', error || (xhr && xhr.statusText));
                $container.html(`
                    <div class="login-warning" style="padding: 15px; text-align: center;">
                        <p style="font-weight: bold;">Failed to load suggestions. Check your connection and try again.</p>
                    </div>
                `);
            });
    }

    displayReactomeSuggestions(data) {
        const $container = $('#reactome-suggestions-container');
        const suggestions = (data && data.suggestions) || [];
        const esc = (v) => this.escapeHtml(v == null ? '' : String(v));

        if (suggestions.length === 0) {
            $container.html(`
                <div class="empty-state panel-outlined" style="padding: 20px; text-align: center;">
                    <h4 style="margin-top: 0;">No Reactome pathway suggestions found</h4>
                    <p class="text-muted">Try searching for a pathway by name using the Search tab, or select a different Key Event.</p>
                </div>
            `);
            return;
        }

        let cardsHtml = '';
        suggestions.forEach((s, index) => {
            const reactomeId = esc(s.reactome_id || '');
            const pathwayName = esc(s.pathway_name || '');
            const species = esc(s.species || 'Homo sapiens');
            const scoreNumeric = (s.suggestion_score != null) ? Number(s.suggestion_score)
                               : (s.hybrid_score != null ? Number(s.hybrid_score) : null);

            // Adapter: inject scores.final_score so createFinalScoreBar reads the right field
            s.scores = { final_score: scoreNumeric != null ? scoreNumeric : 0 };

            const matchTypeBadges  = this.getMatchTypeBadges([]);         // Reactome has no match_types — pure-semantic
            const borderClass      = this.getBorderClassForMatch([]);     // constant — WP "no badges" treatment
            const finalScoreBar    = this.createFinalScoreBar(s);
            const reactomeGeneChip = this.renderGeneOverlapChip(s, data.genes_found || 0);
            const reactomeSetChip = this.renderGeneSetSizeChip(s.reactome_pathway_gene_count);
            const hiddenClass      = index >= 3 ? 'suggestion-item-hidden' : '';

            const matchingGenesStr = esc((s.matching_genes || []).join(','));
            const geneScorePct = Math.round(((s.gene_overlap_ratio || 0) * 100));

            cardsHtml += `
                <div class="suggestion-card panel-outlined ${borderClass} ${hiddenClass}"
                     style="padding: 12px; margin-bottom: 10px; border-radius: 6px;"
                     data-reactome-id="${reactomeId}"
                     data-pathway-name="${pathwayName}"
                     data-species="${species}"
                     data-score="${scoreNumeric != null ? scoreNumeric : ''}"
                     data-matching-genes="${matchingGenesStr}"
                     data-gene-score="${geneScorePct}">
                    <div style="display: flex; gap: 12px; align-items: flex-start;">
                        <div style="flex: 1;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <div>
                                    <strong style="font-size: 14px;">${pathwayName}</strong>
                                    ${matchTypeBadges}
                                    ${reactomeGeneChip}${reactomeSetChip}
                                </div>
                                ${finalScoreBar}
                            </div>
                            <div class="text-muted" style="font-size: 12px; margin-bottom: 8px;">
                                ID: <span style="font-family: monospace;">${reactomeId}</span> | <a href="https://reactome.org/PathwayBrowser/#/${reactomeId}" target="_blank" onclick="event.stopPropagation();">View on Reactome</a>
                            </div>
                            <button type="button" class="btn-select-reactome" style="margin-top: 4px;">Select</button>
                        </div>
                    </div>
                </div>
            `;
        });

        let html = `<div class="reactome-suggestions-list">${cardsHtml}</div>`;

        if (suggestions.length > 3) {
            html += `
                <button class="show-more-reactome-suggestions show-more-btn" type="button">
                    Show ${suggestions.length - 3} more suggestions
                </button>
            `;
        }

        $container.html(html);

        // Bind Reactome-scoped show-more handler (off+on prevents accumulation across KE swaps)
        $('.show-more-reactome-suggestions').off('click').on('click', function() {
            const $button = $(this);
            const $allCards = $('#reactome-suggestions-container .suggestion-card');
            const isExpanded = $button.data('expanded') === true;

            if (isExpanded) {
                // Collapse: hide cards after index 2
                $allCards.each((index, card) => {
                    if (index >= 3) $(card).addClass('suggestion-item-hidden');
                });
                const hiddenCount = $allCards.length - 3;
                $button.text(`Show ${hiddenCount} more suggestions`);
                $button.data('expanded', false);

                // Scroll back to top of Reactome suggestions container
                const $rc = $('#reactome-suggestions-container');
                if ($rc.length && $rc.offset()) {
                    $('html, body').animate({ scrollTop: $rc.offset().top - 100 }, 300);
                }
            } else {
                // Expand: show all cards
                $allCards.removeClass('suggestion-item-hidden');
                $button.text('Show less');
                $button.data('expanded', true);
            }
        });
    }

    // -------------------------------------------------------------------------
    // Reactome duplicate detection
    // -------------------------------------------------------------------------

    checkForDuplicatePair_reactome() {
        const keId = $('#ke_id').val();
        const reactomeId = this.selectedReactomePathway ? this.selectedReactomePathway.reactomeId : '';
        if (!keId || !reactomeId) return;
        $('#duplicate-warning-reactome').hide().empty();
        $.post('/check_reactome_entry', {
            ke_id: keId,
            reactome_id: reactomeId,
            csrf_token: this.csrfToken
        }, (result) => {
            if (result && result.pair_exists && result.blocking_type) {
                this.renderDuplicateWarning_reactome(result);
                this.disableReactomeSubmit();
            } else {
                this.enableReactomeSubmitIfReady();
            }
        }).fail((xhr) => {
            console.warn('Reactome duplicate check failed:', xhr && xhr.statusText);
        });
    }

    renderDuplicateWarning_reactome(result) {
        const ex = result.existing || {};
        const esc = (v) => this.escapeHtml(v == null ? '' : String(v));

        let html = '<div class="alert alert-warning" style="border: 2px solid var(--color-status-medium); padding: 16px; border-radius: 6px; margin: 12px 0;">';
        if (result.blocking_type === 'approved_mapping') {
            html += '<h4 style="margin-top:0;">This KE-Reactome pair already has an approved mapping.</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + esc(ex.ke_id) + ' &mdash; ' + esc(ex.ke_title) + '</dd>';
            html += '<dt>Pathway</dt><dd>' + esc(ex.reactome_id) + ' &mdash; ' + esc(ex.pathway_name) + '</dd>';
            html += '<dt>Confidence</dt><dd>' + esc(ex.confidence_level) + '</dd>';
            html += '<dt>Curator</dt><dd>' + esc(ex.approved_by_curator || 'unknown') + '</dd>';
            html += '</dl>';
            html += '<p style="margin-top:8px; font-style: italic;">To request a change, contact an admin.</p>';
        } else if (result.blocking_type === 'pending_proposal') {
            html += '<h4 style="margin-top:0;">A pending proposal already exists for this pair.</h4>';
            html += '<dl style="margin: 8px 0;">';
            html += '<dt>KE</dt><dd>' + esc(ex.ke_id) + ' &mdash; ' + esc(ex.ke_title) + '</dd>';
            html += '<dt>Pathway</dt><dd>' + esc(ex.reactome_id) + ' &mdash; ' + esc(ex.pathway_name) + '</dd>';
            html += '<dt>Submitted by</dt><dd>' + esc(ex.submitted_by || 'unknown') + '</dd>';
            html += '<dt>Submitted</dt><dd>' + esc(ex.submitted_at) + '</dd>';
            html += '</dl>';
        }
        html += '</div>';
        $('#duplicate-warning-reactome').html(html).show();
        const el = $('#duplicate-warning-reactome')[0];
        if (el && el.scrollIntoView) {
            el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    disableReactomeSubmit() {
        $('#reactome-mapping-form button[type=submit]').prop('disabled', true);
    }

    enableReactomeSubmitIfReady() {
        const dupVisible = $('#duplicate-warning-reactome').is(':visible');
        const ready = !!(this.selectedReactomePathway && this.selectedReactomeConfidence) && !dupVisible;
        const $btn = $('#reactome-mapping-form button[type=submit]');
        $btn.prop('disabled', !ready);
        if (ready) {
            $btn.text('Submit KE-Reactome Mapping');
        } else if (!this.selectedReactomePathway || !this.selectedReactomeConfidence) {
            $btn.text('Complete Steps 2–3 First');
        }
    }

    // -------------------------------------------------------------------------
    // Reactome pathway selection (from Suggested or Search) and step reveal
    // -------------------------------------------------------------------------

    selectReactomePathway({ reactomeId, pathwayName, species, suggestionScore, matchingGenes, genePercent }) {
        this.selectedReactomePathway = {
            reactomeId: reactomeId,
            pathwayName: pathwayName,
            species: species || 'Homo sapiens',
            suggestionScore: suggestionScore != null ? suggestionScore : null,
            // Gene overlap resolved from the suggestion card's data attributes at selection time.
            // null means no card data (e.g. search result with no gene data attribute).
            matchingGenes: matchingGenes != null ? matchingGenes : null,
            genePercent: genePercent != null ? genePercent : '0',
        };
        // Highlight the matching suggestion card if present
        $('#reactome-suggestions-container .suggestion-card').removeClass('go-suggestion-item--selected');
        $(`#reactome-suggestions-container .suggestion-card[data-reactome-id="${this.escapeHtml(reactomeId)}"]`)
            .addClass('go-suggestion-item--selected');

        // Update upstream Reactome link
        this.updatePathwayUpstreamLink('reactome', reactomeId);

        this.checkForDuplicatePair_reactome();
        this.revealReactomeConfidenceStep();

        // Phase 27 (RVIEW-01): inline DiagramJS embed.
        // - D-03: single hook covers both suggestion-card and search-result selection paths.
        // - D-06: race-tolerant flagging — pass cached genes (possibly empty) and let
        //   ReactomeDiagramEmbed apply flags inside its onDiagramLoaded callback.
        // - D-09: any failure (CDN unreachable, stalled, runtime exception) renders the
        //   error card; submission flow is never blocked. RVIEW-01 #3.
        // Phase 31 / D-01: clear the sibling error overlay and show the frame before
        // attempting load(); the next outcome (success render or fresh error card)
        // replaces the previous state cleanly. The mount itself is never destroyed
        // (D-04 reuse-instance preserved).
        if (window.ReactomeDiagramEmbed) {
            const keId = $('#ke_id').val();
            // Phase 31 / D-01: clear sibling overlay and show frame before load().
            $('#reactome-inline-embed-error').hide().empty();
            $('#reactome-inline-embed-frame').show();
            $('#reactome-inline-embed').show();

            // Phase 31 / D-15: fire load() with [] immediately so the diagram mounts
            // without waiting on the gene SPARQL Promise. flagGenes will pick up the
            // resolved genes via the race-tolerant update below.
            const loadPromise = window.ReactomeDiagramEmbed.load(reactomeId, []);

            loadPromise.catch((err) => {
                // D-01 / D-03: render error into the SIBLING overlay; mount is preserved.
                $('#reactome-inline-embed-frame').hide();
                $('#reactome-inline-embed-error')
                    .html(window.ReactomeDiagramEmbed.buildErrorState(reactomeId))
                    .show();
                $('#reactome-inline-embed').show();
                if (window.console && console.warn) {
                    console.warn('[ReactomeDiagramEmbed] load failed:', err && err.message);
                }
            });

            // Phase 31 / D-14, D-15: race-tolerant gene-highlight application.
            // Capture the load token at the moment we kicked off load() so a later
            // pathway swap (which advances _loadToken) cannot trigger this branch.
            if (keId) {
                const expectedToken = window.ReactomeDiagramEmbed._loadToken;
                this.prefetchKeGenes(keId).then((genes) => {
                    if (!window.ReactomeDiagramEmbed) return;
                    if (window.ReactomeDiagramEmbed._loadToken !== expectedToken) return;   // newer load() superseded
                    if (window.ReactomeDiagramEmbed._lastLoadFailed) return;                // error card showing
                    window.ReactomeDiagramEmbed._pendingFlags = genes || [];
                    // If onDiagramLoaded has already fired (genes resolved AFTER mount),
                    // we explicitly call flagGenes to apply highlights. If it has NOT
                    // fired yet, the bind-once handler will pick up _pendingFlags
                    // when it does — both paths converge.
                    window.ReactomeDiagramEmbed.flagGenes();
                });
            }
        }
    }

    revealReactomeConfidenceStep() {
        const rp = this.selectedReactomePathway;
        if (!rp) return;

        const keInfo = this.selectedKEInfo || {};
        const keId = keInfo.keId || $('#ke_id').val() || '';
        const keTitle = keInfo.title || $('#ke_id option:selected').data('title') || '';
        const keBiolevel = keInfo.biolevel || this.selectedBiolevel || '';

        // Resolve gene overlap from the stored suggestion data (no DOM read needed)
        const geneOverlap = (rp.matchingGenes != null)
            ? { matchingGenes: rp.matchingGenes, genePercent: rp.genePercent || '0' }
            : null;

        // Build Reactome-specific pathway card HTML
        const pathwayCardHtml = `
                        <h4>Reactome Pathway</h4>
                        <p><strong>${this.escapeHtml(rp.reactomeId)} — ${this.escapeHtml(rp.pathwayName)}</strong></p>
                        <p class="text-muted" style="font-size: 12px;">Species: ${this.escapeHtml(rp.species || 'Homo sapiens')}</p>
                        <a href="https://reactome.org/PathwayBrowser/#/${encodeURIComponent(rp.reactomeId)}" target="_blank">View on Reactome &rarr;</a>`;

        const cardHtml = this.buildAssessmentCard({
            assessmentId: 'assessment-reactome',
            pathwayId: rp.reactomeId,
            pathwayIndex: rp.reactomeId,
            pathwayTitle: rp.pathwayName,
            keInfo: { keId, keTitle, keBiolevel },
            pathwayCardHtml,
            geneOverlap,
        });

        // Initialise pathwayAssessments slot for the Reactome pathway
        if (!this.pathwayAssessments) this.pathwayAssessments = {};
        if (!this.pathwayAssessments[rp.reactomeId]) {
            this.pathwayAssessments[rp.reactomeId] = {};
        }

        $('#reactome-assessment-container').html(cardHtml);
        $('#reactome-confidence-confirm').hide();
        $('#reactome-confidence-confirm-group .btn-option').removeClass('selected');
        $('#reactome-step-submit').hide();
        $('#reactome-assessment-guide').show();

        // Scroll to assessment section
        setTimeout(() => {
            const section = document.getElementById('reactome-assessment-guide');
            if (section && section.scrollIntoView) {
                section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }, 80);
    }

    /**
     * Called when all four Reactome assessment answers are collected.
     * Evaluates confidence via the shared rubric and reveals #reactome-confidence-confirm.
     */
    revealReactomeConfirmStep(reactomeId) {
        const result = this.pathwayResults && this.pathwayResults[reactomeId];
        if (!result) return;

        const recommended = result.confidence.toLowerCase();
        const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

        $('#reactome-confidence-recommendation').text(cap(recommended));
        $('#reactome-confidence-confirm-group .btn-option').removeClass('selected');
        $(`#reactome-confidence-confirm-group .btn-option[data-value="${recommended}"]`).addClass('selected');
        // Pre-select the recommended value so the submit button can enable immediately
        this.selectedReactomeConfidence = recommended;
        $('#reactome-confidence-confirm-error').hide();
        $('#reactome-confidence-confirm').show();
        $('#reactome-step-submit').show();
        this.enableReactomeSubmitIfReady();

        setTimeout(() => {
            const section = document.getElementById('reactome-confidence-confirm');
            if (section && section.scrollIntoView) {
                section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }, 80);
    }

    // -------------------------------------------------------------------------
    // Reactome search type-ahead rendering
    // -------------------------------------------------------------------------

    renderReactomeSearchResults(results) {
        const $dd = $('#reactome-search-results');
        $dd.empty();
        if (!results || results.length === 0) {
            $dd.hide();
            return;
        }
        const esc = (v) => this.escapeHtml(v == null ? '' : String(v));
        const items = results.map((r) => {
            const reactomeId = esc(r.reactome_id || '');
            const pathwayName = esc(r.pathway_name || '');
            const species = esc(r.species || 'Homo sapiens');
            const relevance = r.relevance_score != null ? Number(r.relevance_score) : null;
            const relText = relevance != null ? relevance.toFixed(3) : '';
            return `
                <div class="search-result-item reactome-search-result-item" style="padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--color-border-light);"
                     data-reactome-id="${reactomeId}"
                     data-pathway-name="${pathwayName}"
                     data-species="${species}"
                     data-relevance="${relevance != null ? relevance : ''}">
                    <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                        <strong>${pathwayName}</strong>
                        ${this.renderGeneSetSizeChip(r.reactome_pathway_gene_count)}
                    </div>
                    <div class="text-muted" style="font-size: 12px;">
                        <span style="font-family: monospace;">${reactomeId}</span> &middot; ${species}${relText ? ` &middot; score ${relText}` : ''}
                    </div>
                </div>
            `;
        }).join('');
        $dd.html(items).show();
    }

    // -------------------------------------------------------------------------
    // Reactome submit + reset
    // -------------------------------------------------------------------------

    handleReactomeFormSubmission(event) {
        if (event && event.preventDefault) event.preventDefault();

        if (!this.selectedReactomePathway) {
            $('#reactome-message').text('Please select a Reactome pathway first.')
                .css('color', 'var(--color-status-low)');
            return;
        }
        if (!this.selectedReactomeConfidence) {
            $('#reactome-confidence-confirm-error').show();
            return;
        }
        if (!this.isLoggedIn) {
            $('#reactome-message').text('Please log in to submit mappings.')
                .css('color', 'var(--color-status-low)');
            setTimeout(() => {
                this.saveFormState();
                window.location.href = '/auth/login';
            }, 2000);
            return;
        }

        const keId = $('#ke_id').val();
        const keTitle = $('#ke_id option:selected').data('title') || '';
        const csrfToken = this.csrfToken
            || $('meta[name="csrf-token"]').attr('content')
            || $('#reactome-mapping-form input[name="csrf_token"]').val();

        // Phase 37 ASMT-04: read the four step answers from pathwayResults keyed
        // by the Reactome pathway ID (stored by evaluatePathwayConfidence).
        // jQuery $.post skips undefined values, so absent answers don't send
        // empty strings that would fail Marshmallow's Optional semantics.
        const reactomeId = this.selectedReactomePathway.reactomeId;
        const reactomeResult = (this.pathwayResults && this.pathwayResults[reactomeId])
            ? this.pathwayResults[reactomeId] : {};
        const reactomeAnswers = reactomeResult.answers || {};
        // connection_type is step1's raw value (the relationship type); the
        // server also derives it from step1 as a fallback if absent.
        const reactomeConnectionType = reactomeAnswers.step1 || undefined;

        const payload = {
            ke_id: keId,
            ke_title: keTitle,
            reactome_id: reactomeId,
            pathway_name: this.selectedReactomePathway.pathwayName,
            species: this.selectedReactomePathway.species || 'Homo sapiens',
            confidence_level: this.selectedReactomeConfidence,
            suggestion_score: this.selectedReactomePathway.suggestionScore != null
                ? String(this.selectedReactomePathway.suggestionScore) : '',
            csrf_token: csrfToken,
            step1: reactomeAnswers.step1 || undefined,
            step2: reactomeAnswers.step2 || undefined,
            step3: reactomeAnswers.step3 || undefined,
            step4: reactomeAnswers.step4 || undefined,
            connection_type: reactomeConnectionType,
        };

        const $btn = $('#reactome-mapping-form button[type=submit]');
        $btn.prop('disabled', true).text('Submitting...');
        $('#reactome-message').empty();

        $.post('/submit_reactome_mapping', payload)
            .done(() => {
                // Populate + show the success modal from the captured payload
                // BEFORE resetReactomeTab() nulls selectedReactomePathway (#194).
                this.showReactomeSuccessMessage(payload);
                this.resetReactomeTab();
            })
            .fail((xhr) => {
                if (xhr && (xhr.status === 401 || xhr.status === 403)) {
                    const expired = xhr.responseJSON && xhr.responseJSON.error === 'session_expired';
                    $('#reactome-message').text(
                        expired
                            ? 'Your session has expired — please log in again, then resubmit.'
                            : 'Please log in to submit mappings.')
                        .css('color', 'var(--color-status-low)');
                    setTimeout(() => {
                        this.saveFormState();
                        window.location.href = '/auth/login';
                    }, 2000);
                } else {
                    const msg = (xhr && xhr.responseJSON && xhr.responseJSON.error)
                        || 'Failed to submit mapping. Please try again.';
                    $('#reactome-message').text(msg).css('color', 'var(--color-status-low)');
                }
                $btn.prop('disabled', false).text('Submit KE-Reactome Mapping');
            });
    }

    resetReactomeTab() {
        // Clear Reactome assessment answers keyed by reactomeId (if any)
        if (this.pathwayAssessments && this.selectedReactomePathway) {
            delete this.pathwayAssessments[this.selectedReactomePathway.reactomeId];
        }
        if (this.pathwayResults && this.selectedReactomePathway) {
            delete this.pathwayResults[this.selectedReactomePathway.reactomeId];
        }

        this.selectedReactomePathway = null;
        this.selectedReactomeConfidence = null;
        $('#reactome-assessment-guide').hide();
        $('#reactome-assessment-container').empty();
        $('#reactome-confidence-confirm').hide();
        $('#reactome-confidence-confirm-group .btn-option').removeClass('selected');
        $('#reactome-confidence-confirm-error').hide();
        $('#reactome-step-submit').hide();
        $('#duplicate-warning-reactome').hide().empty();
        // Phase 27 (RVIEW-01 / D-09): hide inline DiagramJS embed and clear stale flags.
        // Mirrors the WP analog at hidePathwaySuggestions where #wp-inline-embed is hidden
        // alongside the suggestion-banner reset. The embed utility's hide() also defensively
        // calls resetFlaggedItems() so a previous pathway's flags do not leak across selections.
        if (window.ReactomeDiagramEmbed) {
            window.ReactomeDiagramEmbed.hide();
        }
        $('#reactome-message').empty();
        $('#reactome-pathway-search').val('');
        $('#reactome-search-results').empty().hide();
        // Re-render default suggestions panel state
        const keId = $('#ke_id').val();
        const keTitle = $('#ke_id option:selected').data('title');
        if (keId && keTitle) {
            this.loadReactomeSuggestions(keId, keTitle);
        } else {
            $('#reactome-suggestions-container').html(
                '<p class="text-muted-italic">Select a Key Event above to see Reactome pathway suggestions.</p>'
            );
        }
        const $btn = $('#reactome-mapping-form button[type=submit]');
        $btn.prop('disabled', true).text('Complete Steps 2–3 First');
    }

    initV15Banner() {
        const STORAGE_KEY = 'kewp_v15_banner_dismissed';
        const $banner = $('#v15-pure-semantic-banner');
        if (!$banner.length) return;
        // Hide if already dismissed in a previous session
        try {
            if (localStorage.getItem(STORAGE_KEY) === '1') {
                $banner.addClass('is-dismissed');
                return;
            }
        } catch (e) { /* localStorage may be blocked; show banner anyway */ }
        // Bind dismiss button
        $('#v15-banner-dismiss').on('click', () => {
            $banner.addClass('is-dismissed');
            try { localStorage.setItem(STORAGE_KEY, '1'); } catch (e) {}
        });
    }

    initReactomeDevBanner() {
        const STORAGE_KEY = 'kewp_reactome_dev_notice_dismissed';
        const $banner = $('#reactome-dev-notice');
        if (!$banner.length) return;
        // Hide if already dismissed in a previous session
        try {
            if (localStorage.getItem(STORAGE_KEY) === '1') {
                $banner.addClass('is-dismissed');
                return;
            }
        } catch (e) { /* localStorage may be blocked; show banner anyway */ }
        // Bind dismiss button
        $('#reactome-dev-notice-dismiss').on('click', () => {
            $banner.addClass('is-dismissed');
            try { localStorage.setItem(STORAGE_KEY, '1'); } catch (e) {}
        });
    }

    saveFormState() {
        try {
            const formState = {
                // Basic selections
                keId: $("#ke_id").val(),
                keTitle: $("#ke_id option:selected").data("title"),
                keDescription: $("#ke_id option:selected").data("description"),
                keBiolevel: $("#ke_id option:selected").data("biolevel"),
                
                // Pathway selections
                pathwaySelections: [],
                
                // Assessment answers
                stepAnswers: this.stepAnswers || {},
                pathwayAssessments: this.pathwayAssessments || {},
                selectedBiolevel: this.selectedBiolevel || "",
                
                // Timestamp for cleanup
                timestamp: Date.now()
            };
            
            // Collect all pathway selections
            $(".pathway-selection-group").each(function(index) {
                const $select = $(this).find("select[name='wp_id']");
                const selectedValue = $select.val();
                if (selectedValue) {
                    const $option = $select.find("option:selected");
                    formState.pathwaySelections.push({
                        index: index,
                        pathwayId: selectedValue,
                        pathwayTitle: $option.data("title"),
                        pathwayDescription: $option.data("description"),
                        pathwaySvgUrl: $option.data("svg-url")
                    });
                }
            });
            
            // Saving form state to localStorage
            localStorage.setItem('kewp_form_state', JSON.stringify(formState));
            return true;
        } catch (error) {
            console.error("Failed to save form state:", error);
            return false;
        }
    }

    restoreFormState() {
        try {
            const savedState = localStorage.getItem('kewp_form_state');
            if (!savedState) {
                return false;
            }

            const formState = JSON.parse(savedState);

            // Check if state is too old (older than 1 hour)
            const oneHour = 60 * 60 * 1000;
            if (Date.now() - formState.timestamp > oneHour) {
                localStorage.removeItem('kewp_form_state');
                return false;
            }

            // Chained restoration phases to handle async DOM updates
            const restoreAfterLoad = () => {
                // Restore biological level early
                if (formState.selectedBiolevel) {
                    this.selectedBiolevel = formState.selectedBiolevel;
                }

                // Restore assessment data objects (not visual state yet)
                if (formState.stepAnswers) {
                    this.stepAnswers = formState.stepAnswers;
                }
                if (formState.pathwayAssessments) {
                    this.pathwayAssessments = formState.pathwayAssessments;
                }

                // Phase 1: Restore KE selection
                if (formState.keId) {
                    $("#ke_id").val(formState.keId).trigger('change');
                }

                // Phase 2: Poll until pathway option exists, then select it
                if (formState.pathwaySelections && formState.pathwaySelections.length > 0) {
                    const selection = formState.pathwaySelections[0];
                    this.pollForElement(
                        () => $(`select[name='wp_id'] option[value="${selection.pathwayId}"]`).length > 0,
                        () => {
                            const $select = $("select[name='wp_id']");
                            $select.val(selection.pathwayId).trigger('change');

                            // Phase 3: Update selection and trigger assessment generation
                            this.updateSelectedPathways();
                            this.toggleAssessmentSection();

                            // Phase 4: Wait for assessment DOM (300ms setTimeout in toggleAssessmentSection + buffer)
                            if (formState.pathwayAssessments && Object.keys(formState.pathwayAssessments).length > 0) {
                                this.pollForElement(
                                    () => $(`.pathway-assessment[data-pathway-id="${selection.pathwayId}"]`).length > 0,
                                    () => {
                                        this.restoreAssessmentState(formState);
                                        this.showMessage("Previous selections restored after login", "success");
                                        localStorage.removeItem('kewp_form_state');
                                    },
                                    50, 40 // 50ms intervals, up to 2s
                                );
                            } else {
                                this.showMessage("Previous selections restored after login", "success");
                                localStorage.removeItem('kewp_form_state');
                            }
                        },
                        50, 40 // 50ms intervals, up to 2s
                    );
                } else {
                    this.showMessage("Previous selections restored after login", "success");
                    localStorage.removeItem('kewp_form_state');
                }
            };

            // Wait for dropdown options to load, then restore
            if (this.pathwayOptions) {
                restoreAfterLoad.call(this);
            } else {
                this.pollForElement(
                    () => !!this.pathwayOptions,
                    () => restoreAfterLoad.call(this),
                    200, 25 // 200ms intervals, up to 5s
                );
            }

            return true;
        } catch (error) {
            console.error("Failed to restore form state:", error);
            localStorage.removeItem('kewp_form_state');
            return false;
        }
    }

    restoreAssessmentState(formState) {
        if (!formState.pathwayAssessments) return;

        Object.keys(formState.pathwayAssessments).forEach(pathwayId => {
            const answers = formState.pathwayAssessments[pathwayId];
            const $pathwayAssessment = $(`.pathway-assessment[data-pathway-id="${pathwayId}"]`);

            if ($pathwayAssessment.length === 0) return;

            // Restore button states for each step
            Object.keys(answers).forEach(stepId => {
                const value = answers[stepId];
                const $btn = $pathwayAssessment.find(`.btn-group[data-step="${stepId}"] .btn-option[data-value="${value}"]`);

                if ($btn.length > 0) {
                    $btn.addClass("selected");

                    // Show subsequent steps
                    const stepNum = parseInt(stepId.replace('step', ''));
                    for (let i = 2; i <= 4; i++) {
                        if (i <= stepNum + 1) {
                            $pathwayAssessment.find(`.assessment-step[data-step="step${i}"]`).show();
                        }
                    }
                }
            });

            // If assessment is complete, evaluate and show results
            if (answers.step1 && answers.step2 && answers.step3 && answers.step4) {
                setTimeout(() => {
                    this.evaluatePathwayConfidence(pathwayId);
                }, 200);
            }
        });

        // Show assessment sections
        $("#confidence-guide").show();
        $("#step-3-result").show();
    }

    pollForElement(conditionFn, callback, intervalMs = 50, maxAttempts = 40) {
        let attempts = 0;
        const check = () => {
            if (conditionFn()) {
                callback();
            } else if (attempts < maxAttempts) {
                attempts++;
                setTimeout(check, intervalMs);
            } else {
                console.warn("pollForElement: condition not met after max attempts");
                callback(); // Try anyway as fallback
            }
        };
        check();
    }
}

// Global function for confidence evaluation
function evaluateConfidence() {
    const app = window.KEWPApp;
    const config = app.scoringConfig;

    const s1 = app.stepAnswers["step1"]; // Relationship type (causative/responsive/bidirectional/unclear)
    const s2 = app.stepAnswers["step2"]; // Evidence quality (strong/moderate/computational/none)
    const s3 = app.stepAnswers["step3"]; // Pathway specificity (direct/partial/weak)
    const s4 = app.stepAnswers["step4"]; // Coverage comprehensiveness (complete/partial/limited)

    // Calculate base score using config
    let baseScore = 0;

    // Evidence quality scoring - use config
    baseScore += config.evidence_quality[s2] || 0;

    // Pathway specificity scoring - use config
    baseScore += config.pathway_specificity[s3] || 0;

    // Coverage comprehensiveness scoring - use config
    baseScore += config.ke_coverage[s4] || 0;

    // Add biological level modifier - use config
    const bioLevel = app.selectedBiolevel ? app.selectedBiolevel.toLowerCase() : '';
    const qualifyingLevels = config.biological_level.qualifying_levels;
    const isMolecularLevel = qualifyingLevels.some(level => bioLevel.includes(level));

    if (isMolecularLevel) {
        baseScore += config.biological_level.bonus;
    }

    // Determine confidence level based on total score - use config thresholds
    let confidence = "low";
    if (baseScore >= config.confidence_thresholds.high) {
        confidence = "high";
    } else if (baseScore >= config.confidence_thresholds.medium) {
        confidence = "medium";
    }

    // Update UI with results
    $("#auto-confidence").text(confidence.charAt(0).toUpperCase() + confidence.slice(1));
    $("#auto-connection").text(s1.charAt(0).toUpperCase() + s1.slice(1));
    $("#confidence_level").val(confidence);
    $("#connection_type").val(window.KEWPApp.mapConnectionTypeForServer(s1));
    $("#evaluateBtn").hide();

    // Show detailed result message - use config max scores
    const maxScore = isMolecularLevel ?
        config.max_scores.with_bio_bonus :
        config.max_scores.without_bio_bonus;
    const detailMessage = `Assessment completed: ${confidence} confidence (score: ${baseScore}/${maxScore})${isMolecularLevel ? ' with biological level bonus' : ''}`;
    $("#ca-result").text(detailMessage);

    app.showMessage("Confidence assessment completed successfully", "success");

    // Show confidence confirm section with the recommended level pre-selected
    $('#confidence-recommendation').text(confidence.charAt(0).toUpperCase() + confidence.slice(1));
    $('#confidence-select-group .btn-option').removeClass('selected');
    $(`#confidence-select-group .btn-option[data-value="${confidence}"]`).addClass('selected');
    $('#confidence-confirm').show();
    $('#confidence-select-error').hide();

    // Show Step 4 and enable submit for single pathway workflow
    $("#step-3-result").show();
    $("#step-3-result").find("button[type='submit']").prop('disabled', false).text('Review & Submit Mapping');

    // Scroll to Step 4
    $('html, body').animate({
        scrollTop: $('#step-3-result').offset().top - 20
    }, 500);
}

// Universal function to handle Ctrl+click for opening in new tabs
function handleCtrlClick(event, url) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) { // Ctrl (Windows/Linux) or Cmd (Mac)
        window.open(url, '_blank');
    } else {
        window.location.href = url;
    }
}

// Initialize app when document is ready
$(document).ready(() => {
    window.KEWPApp = new KEWPApp();
});