/**
 * AdminProposals IIFE
 *
 * Shared admin-queue JS for WP, GO, and Reactome proposal queues.
 * Provides: bulk-select, keyboard shortcuts with focus guard, auto-advancing
 * side panel, cheat-sheet modal, and the bulk-approve flow (Plan 38-03).
 *
 * Usage: AdminProposals.init(config) where config is:
 *   { resource, detailUrl, approveUrl, rejectUrl, bulkApproveUrl,
 *     tableId, csrfToken, stepLabels (optional override) }
 */
var AdminProposals = (function () {
    'use strict';

    // -------------------------------------------------------------------------
    // Private state
    // -------------------------------------------------------------------------
    var _config = null;
    var _currentProposalId = null;
    var _pendingQueue = [];   // [{id, data}, ...] — ordered list of pending rows

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    // Phase 32/37 XSS contract: escapeHtml at every interpolation into innerHTML.
    // Moved verbatim from templates/admin_proposals.html:154-162.
    function escapeHtml(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Phase 37 ASMT-05/06: key→label map for four-question assessment answers.
    // Moved verbatim from templates/admin_proposals.html:168-173.
    var stepLabels = {
        step1: { causative: 'Causative', responsive: 'Responsive', bidirectional: 'Bidirectional', unclear: 'Unclear' },
        step2: { known: 'Known connection', likely: 'Likely connection', possible: 'Possible connection', uncertain: 'Uncertain connection' },
        step3: { specific: 'KE-specific', includes: 'Includes KE', loose: 'Loosely related' },
        step4: { complete: 'Complete mechanism', keysteps: 'Key steps only', minor: 'Minor aspects' }
    };

    // Issue #213: GO proposals carry a different assessment schema. The WP and
    // Reactome queues store four categorical answers (proposed_relationship /
    // _basis / _specificity / _coverage on `proposals`), while the GO mapper
    // asks three High/Medium/Low questions stored as 3/2/1 integers
    // (proposed_connection_score / _specificity_score / _evidence_score on
    // `ke_go_proposals`). The panel used to test only the WP columns, so every
    // GO proposal — including freshly submitted ones — rendered as
    // "No assessment submitted (legacy proposal)" even though the answers were
    // stored correctly. Question wording matches the submitter's form in
    // static/js/main.js:4420-4460 so reviewer and submitter see the same labels.
    var goDimensionLabels = { 3: 'High', 2: 'Medium', 1: 'Low' };
    var goDimensions = [
        { key: 'proposed_connection_score', label: 'Connection (biological relevance)', dim: 'connection' },
        { key: 'proposed_specificity_score', label: 'Specificity (term precision)', dim: 'specificity' },
        { key: 'proposed_evidence_score', label: 'Evidence (literature support)', dim: 'evidence' }
    ];

    // #246: the four-question instrument as an editable description, mirroring
    // goDimensions above. WikiPathways and Reactome share it; GO has its own.
    // Both are now rendered by one editor parameterised on the resource rather
    // than a GO-only special case — three near-identical panels is how the GO
    // and WP paths drifted apart to begin with.
    var stepQuestions = [
        { key: 'proposed_relationship', field: 'step1', label: 'Relationship' },
        { key: 'proposed_basis', field: 'step2', label: 'Basis' },
        { key: 'proposed_specificity', field: 'step3', label: 'Specificity' },
        { key: 'proposed_coverage', field: 'step4', label: 'Coverage' }
    ];

    function _usesStepAssessment() {
        return _config && (_config.resource === 'wp' || _config.resource === 'reactome');
    }

    var GO_CONNECTION_TYPES = ['describes', 'involves', 'related', 'context'];
    var GO_CONNECTION_HINTS = {
        describes: 'the GO term directly describes the KE mechanism',
        involves: 'the KE involves this process',
        related: 'a related process',
        context: 'provides context'
    };

    // The reviewer's working copy of a GO assessment, seeded from the submitter's
    // stored values whenever a proposal is selected. Kept separate from the row data
    // so "reset to submitted" is possible, and so an untouched review still approves
    // exactly what the submitter recorded.
    var _edit = null;

    // Same weights and thresholds the submitter's form uses (static/js/main.js), so
    // the reviewer sees the confidence the mapper itself would compute. Replaced by
    // /api/go-scoring-config once that loads, which is the server's own source.
    var _goScoring = {
        weights: { connection: 0.33, specificity: 0.33, evidence: 0.34 },
        thresholds: { high: 2.5, medium: 1.5 }
    };

    function _loadGoScoringConfig() {
        if (!window.fetch) return;
        fetch('/api/go-scoring-config', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                var c = d && d.ke_go_assessment;
                if (!c) return;
                if (c.dimension_weights) _goScoring.weights = c.dimension_weights;
                if (c.dimension_thresholds) _goScoring.thresholds = c.dimension_thresholds;
                _refreshComputedConfidence();
            })
            .catch(function () { /* the defaults are the shipped values; a miss is not fatal */ });
    }

    function _computeGoConfidence(c, s, e) {
        if (!_hasValue(c) || !_hasValue(s) || !_hasValue(e)) return null;
        var w = _goScoring.weights;
        var avg = Number(c) * w.connection + Number(s) * w.specificity + Number(e) * w.evidence;
        var t = _goScoring.thresholds;
        return {
            level: avg >= t.high ? 'high' : (avg >= t.medium ? 'medium' : 'low'),
            score: avg
        };
    }

    function _hasValue(v) {
        return v !== null && v !== undefined && v !== '';
    }

    // -------------------------------------------------------------------------
    // DataTable init
    // -------------------------------------------------------------------------
    function _initDataTable() {
        var tableId = _config.tableId;
        if (!tableId || !window.DataTableConfig) return;

        var $table = $('#' + tableId);
        if (!$table.length) return;

        // Count existing columns to set targets correctly
        var colCount = $table.find('thead tr th').length;
        // Build column definitions — checkbox column at index 0, rest shifted by 1
        var colDefs = [
            { orderable: false, searchable: false, targets: 0 },
            { width: '3%', targets: 0 }
        ];

        // Shift existing column targets (7-8 column tables) by 1
        var existingTargets;
        if (colCount === 8) {
            // GO/Reactome: 8 columns before checkbox → 9 after
            existingTargets = [
                { width: '5%', targets: 1 },
                { width: '22%', targets: 2 },
                { width: '17%', targets: 3 },
                { width: '10%', targets: 4 },
                { width: '7%', targets: 5 },
                { width: '8%', targets: 6 },
                { width: '9%', targets: 7 },
                { width: '12%', targets: 8, orderable: false }
            ];
        } else {
            // WP: 7 columns before checkbox → 8 after
            existingTargets = [
                { width: '5%', targets: 1 },
                { width: '22%', targets: 2 },
                { width: '18%', targets: 3 },
                { width: '18%', targets: 4 },
                { width: '9%', targets: 5 },
                { width: '9%', targets: 6 },
                { width: '12%', targets: 7, orderable: false }
            ];
        }

        $table.DataTable(
            DataTableConfig.merge('base', {
                order: [[1, 'desc']],
                pageLength: 25,
                responsive: true,
                columnDefs: colDefs.concat(existingTargets)
            })
        );
    }

    // -------------------------------------------------------------------------
    // Pending queue management
    // -------------------------------------------------------------------------
    function _buildPendingQueue() {
        _pendingQueue = [];
        var $rows = $('tr[data-proposal-id][data-status="pending"]');
        $rows.each(function () {
            var $row = $(this);
            var id = parseInt($row.data('proposal-id'), 10);
            if (!isNaN(id)) {
                _pendingQueue.push({ id: id, $row: $row });
            }
        });
    }

    function _currentQueueIndex() {
        if (_currentProposalId === null) return -1;
        for (var i = 0; i < _pendingQueue.length; i++) {
            if (_pendingQueue[i].id === _currentProposalId) return i;
        }
        return -1;
    }

    // -------------------------------------------------------------------------
    // Checkbox / bulk-select
    // -------------------------------------------------------------------------
    function _initCheckboxes() {
        // Select-all checkbox
        $(document).on('change', '#selectAll', function () {
            var checked = this.checked;
            $('.proposal-select:not(:disabled)').prop('checked', checked);
            _updateBulkBar();
        });

        // Per-row checkbox
        $(document).on('change', '.proposal-select', function () {
            var total = $('.proposal-select:not(:disabled)').length;
            var selectedTotal = $('.proposal-select:not(:disabled):checked').length;
            $('#selectAll').prop('indeterminate', selectedTotal > 0 && selectedTotal < total);
            $('#selectAll').prop('checked', total > 0 && selectedTotal === total);
            _updateBulkBar();
        });
    }

    function _updateBulkBar() {
        var count = $('.proposal-select:checked').length;
        $('#selectedCount').text(count);
        var $btn = $('#bulkApproveBtn');
        if (count > 0) {
            $btn.prop('disabled', false);
            $('#bulkActionBar').show();
        } else {
            $btn.prop('disabled', true);
        }
    }

    // -------------------------------------------------------------------------
    // Side panel
    // -------------------------------------------------------------------------
    function _initSidePanel() {
        _buildPendingQueue();

        // Row click → set current and load panel
        $(document).on('click', 'tr[data-proposal-id]', function (e) {
            // Ignore clicks on checkboxes or action buttons
            if ($(e.target).is(':checkbox') || $(e.target).closest('.proposal-actions').length) return;
            var id = parseInt($(this).data('proposal-id'), 10);
            if (!isNaN(id)) {
                _setCurrentProposal(id);
            }
        });

        // Auto-load first pending proposal
        if (_pendingQueue.length > 0) {
            _setCurrentProposal(_pendingQueue[0].id);
        } else {
            _renderPanelEmpty('No pending proposals in queue.');
        }
    }

    function _setCurrentProposal(id) {
        _currentProposalId = id;

        // Highlight current row
        $('tr[data-proposal-id]').removeClass('proposal-row-active');
        $('tr[data-proposal-id="' + id + '"]').addClass('proposal-row-active');

        // Try to populate from data-* attrs first (synchronous, per Pitfall 6)
        var $row = $('tr[data-proposal-id="' + id + '"]');
        if ($row.length && $row.data('ke-id')) {
            var proposal = _rowToProposal($row);
            _renderPanel(proposal);
        } else {
            // Fall back to detail endpoint fetch
            _fetchAndRenderPanel(id);
        }
    }

    function _currentAssessmentFromRow($row) {
        var version = $row.data('current-version') || null;
        var confidence = $row.data('current-confidence') || null;
        var answers = {
            proposed_relationship: $row.data('current-relationship') || null,
            proposed_basis: $row.data('current-basis') || null,
            proposed_specificity: $row.data('current-specificity') || null,
            proposed_coverage: $row.data('current-coverage') || null
        };
        // A new-pair proposal has no target mapping, so every one of these is
        // absent. Distinguish that from a mapping that exists but predates the
        // assessment, which has a version and a tier but no answers.
        if (!version && !confidence &&
            !Object.keys(answers).some(function (k) { return _hasValue(answers[k]); })) {
            return null;
        }
        answers.confidence_level = confidence;
        answers.assessment_version = version;
        return answers;
    }

    function _rowToProposal($row) {
        return {
            id: parseInt($row.data('proposal-id'), 10),
            status: $row.data('status') || '',
            ke_id: $row.data('ke-id') || '',
            ke_title: $row.data('ke-title') || '',
            pathway_id: $row.data('pathway-id') || '',
            pathway_title: $row.data('pathway-title') || '',
            confidence: $row.data('confidence') || '',
            connection_type: $row.data('connection-type') || '',
            proposed_relationship: $row.data('proposed-relationship') || null,
            proposed_basis: $row.data('proposed-basis') || null,
            proposed_specificity: $row.data('proposed-specificity') || null,
            proposed_coverage: $row.data('proposed-coverage') || null,
            // #247: the assessment this revision replaces. Null throughout for a
            // new-pair proposal, which has no current state — the panel then
            // shows the proposed answers alone rather than an empty column
            // implying something was lost.
            current_assessment: _currentAssessmentFromRow($row),
            // Issue #213: GO's three-dimension assessment. Absent on the WP and
            // Reactome queues, where these stay null and the four-answer block
            // above is rendered instead.
            proposed_connection_score: $row.data('proposed-connection-score') || null,
            proposed_specificity_score: $row.data('proposed-specificity-score') || null,
            proposed_evidence_score: $row.data('proposed-evidence-score') || null,
            suggestion_score: $row.data('suggestion-score') || null,
            user_name: $row.data('user-name') || '',
            submitted_by: $row.data('submitted-by') || '',
            created_at: $row.data('created-at') || '',
            admin_notes: $row.data('admin-notes') || '',
            uuid: $row.data('uuid') || '',
            // Issue #197: change/deletion proposals target an existing mapping.
            mapping_id: $row.data('mapping-id') || null,
            proposed_delete: $row.data('proposed-delete') || 0,
            _from_row: true
        };
    }

    // A proposal is a deletion / change request (vs a new-pair proposal) when
    // it targets an existing mapping. proposed_delete arrives as 1/0, true/false
    // or "1"/"0" depending on the source (row data-* vs JSON detail endpoint).
    function _isDeletionProposal(p) {
        var d = p.proposed_delete;
        return d === true || d === 1 || d === '1';
    }
    function _isChangeProposal(p) {
        var m = p.mapping_id;
        return !_isDeletionProposal(p) && m != null && m !== '' && m !== 0 && m !== '0';
    }

    function _fetchAndRenderPanel(id) {
        var url = _config.detailUrl + '/' + id;
        $('#reviewPanelContent').html('<p style="color:var(--color-text-muted,#6c757d);">Loading&hellip;</p>');
        $.get(url)
            .done(function (proposal) {
                _renderPanel(proposal);
            })
            .fail(function (xhr) {
                $('#reviewPanelContent').html(
                    '<p style="color:var(--color-primary-pink,#E6007E);">Failed to load proposal: ' +
                    escapeHtml((xhr.responseJSON && xhr.responseJSON.error) || 'Unknown error') + '</p>'
                );
            });
    }

    // -------------------------------------------------------------------------
    // Editable GO assessment (reviewer refinement before approval)
    // -------------------------------------------------------------------------

    function _seedEdit(p) {
        _edit = {
            submitted: {
                connection: _hasValue(p.proposed_connection_score) ? Number(p.proposed_connection_score) : null,
                specificity: _hasValue(p.proposed_specificity_score) ? Number(p.proposed_specificity_score) : null,
                evidence: _hasValue(p.proposed_evidence_score) ? Number(p.proposed_evidence_score) : null,
                connection_type: p.connection_type || p.proposed_connection_type || p.new_pair_connection_type || '',
                confidence: p.confidence || p.proposed_confidence || p.new_pair_confidence_level || ''
            },
            confidenceOverride: ''   // '' means "use the computed tier"
        };
        _edit.current = {
            connection: _edit.submitted.connection,
            specificity: _edit.submitted.specificity,
            evidence: _edit.submitted.evidence,
            connection_type: _edit.submitted.connection_type
        };
    }

    function _editIsDirty() {
        if (!_edit) return false;
        var s = _edit.submitted, c = _edit.current;
        return c.connection !== s.connection || c.specificity !== s.specificity ||
               c.evidence !== s.evidence || c.connection_type !== s.connection_type ||
               (_edit.confidenceOverride && _edit.confidenceOverride !== s.confidence);
    }

    function _effectiveConfidence() {
        if (!_edit) return '';
        if (_edit.confidenceOverride) return _edit.confidenceOverride;
        var c = _computeGoConfidence(_edit.current.connection, _edit.current.specificity, _edit.current.evidence);
        return c ? c.level : (_edit.submitted.confidence || '');
    }

    function _renderGoAssessmentEditor(p) {
        _seedEdit(p);
        var rows = goDimensions.map(function (d) {
            var val = _edit.current[d.dim];
            var buttons = [3, 2, 1].map(function (score) {
                var on = (val === score);
                return '<button type="button" class="go-review-dim-btn' + (on ? ' is-selected' : '') + '"' +
                    ' data-dimension="' + d.dim + '" data-score="' + score + '"' +
                    ' aria-pressed="' + (on ? 'true' : 'false') + '"' +
                    ' style="padding:3px 10px;font-size:12px;cursor:pointer;border:1px solid ' +
                    (on ? 'var(--color-primary,#29235C);background:var(--color-primary,#29235C);color:#fff' :
                          'var(--color-border-light,#dee2e6);background:#fff;color:#333') +
                    ';border-radius:4px;">' + goDimensionLabels[score] + '</button>';
            }).join(' ');
            return '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px;">' +
                '<span style="font-size:13px;">' + escapeHtml(d.label) + '</span>' +
                '<span style="white-space:nowrap;">' + buttons + '</span></div>';
        }).join('');

        var ctOptions = GO_CONNECTION_TYPES.map(function (t) {
            return '<option value="' + t + '"' + (_edit.current.connection_type === t ? ' selected' : '') + '>' +
                   t + ' — ' + escapeHtml(GO_CONNECTION_HINTS[t]) + '</option>';
        }).join('');

        var confOptions = ['', 'high', 'medium', 'low'].map(function (v) {
            var label = v === '' ? 'Auto (from the scores above)' : v;
            return '<option value="' + v + '"' + (_edit.confidenceOverride === v ? ' selected' : '') + '>' +
                   escapeHtml(label) + '</option>';
        }).join('');

        return '<div id="goReviewEditor">' +
            '<div style="margin-bottom:8px;">' +
            '<label for="goReviewConnType" style="display:block;font-size:13px;font-weight:600;margin-bottom:3px;">Connection type</label>' +
            '<select id="goReviewConnType" style="width:100%;font-size:13px;padding:4px;border:1px solid var(--color-border-light,#dee2e6);border-radius:4px;">' +
            ctOptions + '</select></div>' +
            rows +
            '<div style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
            '<label for="goReviewConfidence" style="font-size:13px;font-weight:600;">Confidence</label>' +
            '<select id="goReviewConfidence" style="flex:1;min-width:150px;font-size:13px;padding:4px;border:1px solid var(--color-border-light,#dee2e6);border-radius:4px;">' +
            confOptions + '</select>' +
            '<span id="goReviewComputed" style="font-size:12px;color:var(--color-text-muted,#6c757d);"></span>' +
            '</div>' +
            '<div id="goReviewDirty" style="margin-top:8px;font-size:12px;display:none;">' +
            '<span style="color:#7a4b00;">Edited — approving records your values, not the submitter\'s.</span> ' +
            '<button type="button" id="goReviewReset" style="font-size:12px;padding:2px 8px;cursor:pointer;">Reset</button>' +
            '</div>' +
            '</div>';
    }

    // -------------------------------------------------------------------------
    // Editable four-question assessment (WikiPathways + Reactome)
    // -------------------------------------------------------------------------

    function _seedStepEdit(p) {
        var submitted = {};
        stepQuestions.forEach(function (q) { submitted[q.key] = p[q.key] || ''; });
        submitted.confidence = p.proposed_confidence || p.new_pair_confidence_level || p.confidence || '';
        _edit = {
            submitted: submitted,
            current: $.extend({}, submitted),
            confidenceOverride: ''
        };
        delete _edit.current.confidence;
    }

    function _stepEditIsDirty() {
        if (!_edit) return false;
        var dirty = stepQuestions.some(function (q) {
            return _edit.current[q.key] !== _edit.submitted[q.key];
        });
        return dirty || (!!_edit.confidenceOverride &&
                         _edit.confidenceOverride !== _edit.submitted.confidence);
    }

    function _renderStepAssessmentEditor(p) {
        _seedStepEdit(p);
        var current = (p.current_assessment || null);
        // A mapping that predates the assessment has a tier but no answers.
        // Four "was: —" lines read as "no answers given" when the truth is that
        // answers were never collectable, and a revision against one of those is
        // precisely where a reviewer most needs to be told (#247).
        var currentIsLegacy = !!current && !stepQuestions.some(function (q) {
            return _hasValue(current[q.key]);
        });
        if (currentIsLegacy) current = null;

        var rows = stepQuestions.map(function (q) {
            var labels = stepLabels[q.field];
            var options = Object.keys(labels).map(function (v) {
                return '<option value="' + v + '"' +
                    (_edit.current[q.key] === v ? ' selected' : '') + '>' +
                    escapeHtml(labels[v]) + '</option>';
            }).join('');
            // #247: what this answer replaces, and whether it actually moved.
            var was = current ? current[q.key] : null;
            var note = '';
            if (current) {
                if (!_hasValue(was)) {
                    note = '<span style="font-size:11px;color:var(--color-text-muted,#6c757d);">was: —</span>';
                } else if (was !== _edit.current[q.key]) {
                    note = '<span class="assessment-changed" style="font-size:11px;color:#7a4b00;font-weight:600;">' +
                           'changed from ' + escapeHtml(labels[was] || was) + '</span>';
                } else {
                    note = '<span style="font-size:11px;color:var(--color-text-muted,#6c757d);">unchanged</span>';
                }
            }
            return '<div style="margin-bottom:8px;">' +
                '<label for="stepReview_' + q.field + '" style="display:block;font-size:13px;font-weight:600;margin-bottom:2px;">' +
                escapeHtml(q.label) + '</label>' +
                '<select id="stepReview_' + q.field + '" class="step-review-select" data-key="' + q.key + '"' +
                ' style="width:100%;font-size:13px;padding:4px;border:1px solid var(--color-border-light,#dee2e6);border-radius:4px;">' +
                '<option value="">—</option>' + options + '</select>' +
                (note ? '<div style="margin-top:2px;">' + note + '</div>' : '') +
                '</div>';
        }).join('');

        var confOptions = ['', 'high', 'medium', 'low'].map(function (v) {
            var label = v === '' ? 'Auto (calculated on approval)' : v;
            return '<option value="' + v + '"' + (_edit.confidenceOverride === v ? ' selected' : '') + '>' +
                   escapeHtml(label) + '</option>';
        }).join('');

        // Deliberately no live tier here, unlike GO. The KE-WP score depends on
        // the Key Event's biological level, which this panel does not hold and
        // which the browser getting wrong is exactly what #237 was about. The
        // server recomputes on approval from its own metadata; a reviewer who
        // wants a specific tier pins it explicitly instead.
        var currentTier = current && current.confidence_level
            ? '<span style="font-size:12px;color:var(--color-text-muted,#6c757d);">currently ' +
              escapeHtml(current.confidence_level) + '</span>'
            : '';

        var legacyNote = currentIsLegacy
            ? '<div style="margin-bottom:8px;font-size:12px;color:var(--color-text-muted,#6c757d);">' +
              'The mapping being revised predates the assessment, so there are no ' +
              'current answers to compare against.</div>'
            : '';

        return '<div id="stepReviewEditor">' + legacyNote + rows +
            '<div style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
            '<label for="stepReviewConfidence" style="font-size:13px;font-weight:600;">Confidence</label>' +
            '<select id="stepReviewConfidence" style="flex:1;min-width:150px;font-size:13px;padding:4px;border:1px solid var(--color-border-light,#dee2e6);border-radius:4px;">' +
            confOptions + '</select>' + currentTier +
            '</div>' +
            '<div id="stepReviewDirty" style="margin-top:8px;font-size:12px;display:none;">' +
            '<span style="color:#7a4b00;">Edited — approving records your values, not the submitter\'s.</span> ' +
            '<button type="button" id="stepReviewReset" style="font-size:12px;padding:2px 8px;cursor:pointer;">Reset</button>' +
            '</div></div>';
    }

    function _refreshStepDirty() {
        var el = document.getElementById('stepReviewDirty');
        if (el) el.style.display = _stepEditIsDirty() ? 'block' : 'none';
    }

    function _initStepReviewEditor() {
        $(document).on('change', '#stepReviewEditor .step-review-select', function () {
            if (!_edit) return;
            _edit.current[this.getAttribute('data-key')] = this.value;
            _refreshStepDirty();
        });
        $(document).on('change', '#stepReviewConfidence', function () {
            if (_edit) { _edit.confidenceOverride = this.value; _refreshStepDirty(); }
        });
        $(document).on('click', '#stepReviewReset', function () {
            if (_currentProposalId !== null) _setCurrentProposal(_currentProposalId);
        });
    }

    function _refreshComputedConfidence() {
        if (!_edit) return;
        var el = document.getElementById('goReviewComputed');
        if (el) {
            var c = _computeGoConfidence(_edit.current.connection, _edit.current.specificity, _edit.current.evidence);
            el.textContent = c ? ('computed: ' + c.level + ' (' + c.score.toFixed(2) + ')') : 'computed: —';
        }
        var dirty = document.getElementById('goReviewDirty');
        if (dirty) dirty.style.display = _editIsDirty() ? 'block' : 'none';
    }

    function _initGoReviewEditor() {
        // Delegated once at init; the panel HTML is replaced on every selection.
        $(document).on('click', '#goReviewEditor .go-review-dim-btn', function () {
            if (!_edit) return;
            var dim = this.getAttribute('data-dimension');
            var score = Number(this.getAttribute('data-score'));
            _edit.current[dim] = score;
            $(this).siblings('.go-review-dim-btn').attr('aria-pressed', 'false')
                .css({ background: '#fff', color: '#333', borderColor: 'var(--color-border-light,#dee2e6)' })
                .removeClass('is-selected');
            $(this).attr('aria-pressed', 'true').addClass('is-selected')
                .css({ background: 'var(--color-primary,#29235C)', color: '#fff', borderColor: 'var(--color-primary,#29235C)' });
            _refreshComputedConfidence();
        });
        $(document).on('change', '#goReviewConnType', function () {
            if (_edit) { _edit.current.connection_type = this.value; _refreshComputedConfidence(); }
        });
        $(document).on('change', '#goReviewConfidence', function () {
            if (_edit) { _edit.confidenceOverride = this.value; _refreshComputedConfidence(); }
        });
        $(document).on('click', '#goReviewReset', function () {
            // Re-select through the normal path so the panel is rebuilt from the row
            // data, which reseeds _edit from the submitter's values.
            if (_currentProposalId !== null) _setCurrentProposal(_currentProposalId);
        });
    }

    function _renderPanel(proposal) {
        var isPending = (proposal.status === 'pending');

        // Determine pathway/GO/Reactome ID+title display
        var pathwayId = escapeHtml(proposal.wp_id || proposal.go_id || proposal.reactome_id || proposal.pathway_id || '');
        var pathwayTitle = escapeHtml(proposal.wp_title || proposal.go_name || proposal.pathway_name || proposal.pathway_title || '');
        var pathwayLabel = 'Pathway/Term';
        if (proposal.wp_id) pathwayLabel = 'WikiPathways';
        else if (proposal.go_id) pathwayLabel = 'GO Term';
        else if (proposal.reactome_id) pathwayLabel = 'Reactome Pathway';

        // Confidence field
        var confidence = escapeHtml(
            proposal.new_pair_confidence_level ||
            proposal.proposed_confidence ||
            proposal.confidence || ''
        );

        // Submitter
        var submittedBy = escapeHtml(
            proposal.provider_username ||
            proposal.github_username ||
            proposal.user_name ||
            proposal.submitted_by || ''
        );

        // Score
        var scoreText = '&mdash;';
        var rawScore = proposal.suggestion_score;
        if (rawScore !== null && rawScore !== undefined && rawScore !== '') {
            scoreText = Number(rawScore).toFixed(3);
        }

        // Admin notes area UUID
        var panelNotesId = 'panelAdminNotes';

        // Assessment block (verbatim from admin_proposals.html:280-296)
        var assessmentHtml = (function () {
            var p = proposal;
            var hasAssessment = _hasValue(p.proposed_relationship)
                || _hasValue(p.proposed_basis)
                || _hasValue(p.proposed_specificity)
                || _hasValue(p.proposed_coverage);
            // Issue #213: GO's three-dimension schema (see goDimensions above).
            var hasGoAssessment = goDimensions.some(function (d) {
                return _hasValue(p[d.key]);
            });
            var body;
            if (hasGoAssessment && isPending && _config && _config.resource === 'go') {
                // Editable for a pending GO proposal: a reviewer who disagrees with a
                // dimension, the connection type or the resulting tier can correct it
                // here instead of rejecting the proposal and asking for a resubmit.
                body = _renderGoAssessmentEditor(p);
            } else if (isPending && _usesStepAssessment() && !_isDeletionProposal(p)) {
                // #246: the same affordance for WikiPathways and Reactome, and
                // for new-pair proposals as much as revisions. Rendered even
                // when the proposal carries no answers — a legacy one is
                // precisely where a reviewer most needs to be able to supply
                // them, and #247's comparison says so rather than showing four
                // blanks that read as "no answers given".
                body = _renderStepAssessmentEditor(p);
            } else if (hasGoAssessment) {
                body = goDimensions.map(function (d) {
                    var raw = p[d.key];
                    var label = _hasValue(raw) ? (goDimensionLabels[raw] || raw) : '—';
                    return '<div><strong>' + escapeHtml(d.label) + ':</strong> ' + escapeHtml(label) + '</div>';
                }).join('');
            } else if (hasAssessment) {
                body = '<div><strong>Relationship:</strong> ' + escapeHtml(stepLabels.step1[p.proposed_relationship] || p.proposed_relationship || '—') + '</div>' +
                       '<div><strong>Basis:</strong> ' + escapeHtml(stepLabels.step2[p.proposed_basis] || p.proposed_basis || '—') + '</div>' +
                       '<div><strong>Specificity:</strong> ' + escapeHtml(stepLabels.step3[p.proposed_specificity] || p.proposed_specificity || '—') + '</div>' +
                       '<div><strong>Coverage:</strong> ' + escapeHtml(stepLabels.step4[p.proposed_coverage] || p.proposed_coverage || '—') + '</div>';
            } else {
                body = '<div style="font-size:13px;color:var(--color-text-muted,#6c757d);">No assessment submitted (legacy proposal)</div>';
            }
            return '<div style="margin-bottom:16px;border-top:1px solid var(--color-border-light,#dee2e6);padding-top:14px;">' +
                   '<h4 style="margin-bottom:8px;font-size:14px;">Assessment</h4>' + body + '</div>';
        }());

        // Issue #197: banner distinguishing deletion / change-to-existing
        // proposals from new-pair proposals so curators know the approval
        // effect before they act.
        var changeBannerHtml = '';
        if (_isDeletionProposal(proposal)) {
            changeBannerHtml = '<div style="margin-bottom:12px;padding:8px 10px;border-radius:4px;' +
                'background:#fdecea;color:#8a1c12;font-weight:600;">Deletion requested &mdash; ' +
                'approving removes this existing mapping.</div>';
        } else if (_isChangeProposal(proposal)) {
            changeBannerHtml = '<div style="margin-bottom:12px;padding:8px 10px;border-radius:4px;' +
                'background:#fff4e5;color:#7a4b00;font-weight:600;">Change to an existing mapping &mdash; ' +
                'approving updates it in place.</div>';
        }

        // Status-specific actions
        var actionsHtml = '';
        if (isPending) {
            actionsHtml = '<div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;">' +
                '<button class="btn-approve panel-approve-btn" onclick="AdminProposals.handleApprove()">Approve</button>' +
                '<button class="btn-reject panel-reject-btn" onclick="AdminProposals.handleReject()">Reject</button>' +
                '</div>';
        } else {
            var statusEsc = escapeHtml(proposal.status || '');
            actionsHtml = '<div style="margin-top:16px;"><span class="status-' + statusEsc + '">' + statusEsc + '</span></div>';
        }

        var html = '<div style="font-size:14px;line-height:1.5;">' +

            // Header
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">' +
            '<h4 style="margin:0;font-size:15px;color:var(--color-primary-dark,#29235C);">Proposal #' + escapeHtml(String(proposal.id || '')) + '</h4>' +
            '<span class="status-' + escapeHtml(proposal.status || '') + '">' + escapeHtml(proposal.status || '') + '</span>' +
            '</div>' +

            // Deletion / change banner (issue #197)
            changeBannerHtml +

            // Mapping info
            '<div style="margin-bottom:12px;">' +
            '<div><strong>KE:</strong> ' + escapeHtml(proposal.ke_id || '') + ' &mdash; ' + escapeHtml(proposal.ke_title || '') + '</div>' +
            '<div><strong>' + escapeHtml(pathwayLabel) + ':</strong> ' + pathwayId + ' &mdash; ' + pathwayTitle + '</div>' +
            '<div><strong>Confidence:</strong> ' + confidence + '</div>' +
            '<div><strong>Score:</strong> ' + scoreText + '</div>' +
            '<div><strong>Submitted by:</strong> ' + submittedBy + '</div>' +
            '<div><strong>Date:</strong> ' + escapeHtml(proposal.created_at_formatted || proposal.created_at || '') + '</div>' +
            '</div>' +

            // Assessment
            assessmentHtml +

            // Admin notes
            '<div style="margin-bottom:12px;">' +
            '<label for="' + escapeHtml(panelNotesId) + '" style="display:block;font-weight:600;font-size:13px;margin-bottom:4px;">Admin notes (optional):</label>' +
            '<textarea id="' + escapeHtml(panelNotesId) + '" style="width:100%;height:70px;font-size:13px;padding:6px;border:1px solid var(--color-border-light,#dee2e6);border-radius:4px;resize:vertical;box-sizing:border-box;" placeholder="Add notes&hellip;"></textarea>' +
            '</div>' +

            // Actions
            actionsHtml +

            '</div>';

        $('#reviewPanelContent').html(html);

        // The computed-confidence readout depends on DOM that only exists once the
        // panel is in the document.
        if (document.getElementById('goReviewEditor')) _refreshComputedConfidence();
    }

    function _renderPanelEmpty(msg) {
        $('#reviewPanelContent').html('<p style="color:var(--color-text-muted,#6c757d);">' + escapeHtml(msg) + '</p>');
    }

    // -------------------------------------------------------------------------
    // Keyboard handler
    // -------------------------------------------------------------------------
    function _initKeyboardHandler() {
        document.addEventListener('keydown', function (e) {
            // Focus guard: suppress keyboard actions when user is in an input field
            var tag = document.activeElement ? document.activeElement.tagName.toUpperCase() : '';
            if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;

            if (e.key === 'a') { e.preventDefault(); _handleApprove(); }
            else if (e.key === 'r') { e.preventDefault(); _handleReject(); }
            else if (e.key === '?') { e.preventDefault(); _openCheatSheet(); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); _navigatePanel(-1); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); _navigatePanel(1); }
        });
    }

    // -------------------------------------------------------------------------
    // Navigation
    // -------------------------------------------------------------------------
    function _navigatePanel(direction) {
        _buildPendingQueue();
        if (_pendingQueue.length === 0) return;
        var idx = _currentQueueIndex();
        var newIdx = idx + direction;
        if (newIdx < 0) newIdx = 0;
        if (newIdx >= _pendingQueue.length) newIdx = _pendingQueue.length - 1;
        if (newIdx !== idx) {
            _setCurrentProposal(_pendingQueue[newIdx].id);
        }
    }

    // -------------------------------------------------------------------------
    // Approve / Reject (single — keyboard a/r and panel buttons)
    // D-08: a/r are ALWAYS single-current-proposal; never branch on checked set
    // -------------------------------------------------------------------------
    function _handleApprove() {
        if (_currentProposalId === null) return;
        var adminNotes = _getPanelNotes();
        _singleApprove(_currentProposalId, adminNotes);
    }

    function _handleReject() {
        if (_currentProposalId === null) return;
        var adminNotes = _getPanelNotes();
        _singleReject(_currentProposalId, adminNotes);
    }

    function _getPanelNotes() {
        var el = document.getElementById('panelAdminNotes');
        return el ? el.value : '';
    }

    function _singleApprove(proposalId, adminNotes) {
        var formData = new FormData();
        formData.append('admin_notes', adminNotes || '');
        formData.append('csrf_token', _config.csrfToken);

        // Carry the GO assessment through explicitly. Sending it even when untouched
        // is deliberate: it makes the approved mapping a faithful record of what the
        // panel showed the reviewer, rather than depending on the server re-reading
        // the proposal row.
        if (_edit && _config.resource === 'go' && document.getElementById('goReviewEditor')) {
            var c = _edit.current;
            if (_hasValue(c.connection) && _hasValue(c.specificity) && _hasValue(c.evidence)) {
                formData.append('connection_score', c.connection);
                formData.append('specificity_score', c.specificity);
                formData.append('evidence_score', c.evidence);
            }
            if (c.connection_type) formData.append('connection_type', c.connection_type);
            // Only send a confidence when the reviewer pinned one; otherwise let the
            // server compute it from the scores, so the two never disagree.
            if (_edit.confidenceOverride) formData.append('confidence_level', _edit.confidenceOverride);
        }

        // #246: the same for the four-question resources. All four go together
        // or none do — the server refuses a partial assessment rather than
        // filling the gaps from the submitter's, which would attribute answers
        // to a reviewer who never gave them.
        if (_edit && _usesStepAssessment() && document.getElementById('stepReviewEditor')) {
            var answered = stepQuestions.every(function (q) {
                return _hasValue(_edit.current[q.key]);
            });
            if (answered) {
                stepQuestions.forEach(function (q) {
                    formData.append(q.field, _edit.current[q.key]);
                });
            }
            if (_edit.confidenceOverride) formData.append('confidence_level', _edit.confidenceOverride);
        }

        fetch(_config.approveUrl + '/' + proposalId + '/approve', {
            method: 'POST',
            body: formData
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.message) {
                _removeRowAndAdvance(proposalId);
            } else {
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(function (err) {
            alert('Error: ' + err);
        });
    }

    function _singleReject(proposalId, adminNotes) {
        var formData = new FormData();
        formData.append('admin_notes', adminNotes || 'No reason provided');
        formData.append('csrf_token', _config.csrfToken);

        fetch(_config.rejectUrl + '/' + proposalId + '/reject', {
            method: 'POST',
            body: formData
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.message) {
                _removeRowAndAdvance(proposalId);
            } else {
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(function (err) {
            alert('Error: ' + err);
        });
    }

    function _removeRowAndAdvance(proposalId) {
        // Find current index before rebuild
        _buildPendingQueue();
        var idx = _currentQueueIndex();

        // Remove row from table and uncheck its checkbox
        var $row = $('tr[data-proposal-id="' + proposalId + '"]');
        // Update DataTable if active
        var tableId = _config.tableId;
        if (tableId && $.fn.DataTable && $.fn.DataTable.isDataTable('#' + tableId)) {
            var dt = $('#' + tableId).DataTable();
            var dtRow = dt.row($row);
            if (dtRow.length) {
                dtRow.remove().draw(false);
            } else {
                $row.remove();
            }
        } else {
            $row.remove();
        }

        // Rebuild queue and advance
        _buildPendingQueue();
        _currentProposalId = null;

        if (_pendingQueue.length === 0) {
            _renderPanelEmpty('All proposals reviewed.');
            return;
        }

        // Advance: try the same index position, or last
        var nextIdx = Math.min(idx, _pendingQueue.length - 1);
        _setCurrentProposal(_pendingQueue[nextIdx].id);
    }

    // -------------------------------------------------------------------------
    // Bulk approve
    // -------------------------------------------------------------------------
    function _bulkApprove() {
        var selectedIds = [];
        $('.proposal-select:checked').each(function () {
            var id = parseInt($(this).data('id'), 10);
            if (!isNaN(id)) selectedIds.push(id);
        });

        if (selectedIds.length === 0) return;

        // D-12: confirm only when n > 1
        if (selectedIds.length > 1) {
            if (!confirm('Approve ' + selectedIds.length + ' proposals? This cannot be undone.')) return;
        }

        var adminNotes = _getPanelNotes() || '';

        fetch(_config.bulkApproveUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': _config.csrfToken
            },
            body: JSON.stringify({ ids: selectedIds, admin_notes: adminNotes })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            var approved = data.approved || [];
            var failed = data.failed || [];

            // Remove approved rows from table
            if (approved.length > 0) {
                // Remove by id — we need to find which proposal ids were approved.
                // approved contains UUIDs; remove all checked rows that are now gone.
                // Re-fetch the table to get fresh state — simplest reliable approach.
                // Also uncheck the selectAll
                $('.proposal-select:checked').each(function () {
                    var $cb = $(this);
                    var id = parseInt($cb.data('id'), 10);
                    var $row = $cb.closest('tr');
                    // Remove from DataTable
                    var tableId = _config.tableId;
                    if (tableId && $.fn.DataTable && $.fn.DataTable.isDataTable('#' + tableId)) {
                        var dt = $('#' + tableId).DataTable();
                        var dtRow = dt.row($row);
                        if (dtRow.length) dtRow.remove();
                    } else {
                        $row.remove();
                    }
                });

                // Redraw DataTable
                var tableId = _config.tableId;
                if (tableId && $.fn.DataTable && $.fn.DataTable.isDataTable('#' + tableId)) {
                    $('#' + tableId).DataTable().draw(false);
                }

                $('#selectAll').prop('checked', false).prop('indeterminate', false);
                _updateBulkBar();
            }

            if (failed.length > 0) {
                var reasons = failed.map(function (f) {
                    return 'ID ' + f.id + ': ' + f.reason;
                }).join('\n');
                alert('Some proposals could not be approved:\n' + reasons);
            } else if (approved.length > 0) {
                // Success summary
                var msg = 'Approved ' + approved.length + ' proposal(s).';
                if (approved.length === 1) {
                    // No confirm needed for single — just show brief status
                    console.log(msg);
                } else {
                    alert(msg);
                }
            }

            // Rebuild panel
            _buildPendingQueue();
            _currentProposalId = null;
            if (_pendingQueue.length > 0) {
                _setCurrentProposal(_pendingQueue[0].id);
            } else {
                _renderPanelEmpty('All proposals reviewed.');
            }
        })
        .catch(function (err) {
            alert('Bulk approve error: ' + err);
        });
    }

    // -------------------------------------------------------------------------
    // Cheat-sheet modal
    // -------------------------------------------------------------------------
    function _openCheatSheet() {
        var modal = document.getElementById('cheatSheetModal');
        var overlay = document.getElementById('cheatSheetOverlay');
        if (modal) modal.style.display = 'block';
        if (overlay) overlay.style.display = 'block';
    }

    function _closeCheatSheet() {
        var modal = document.getElementById('cheatSheetModal');
        var overlay = document.getElementById('cheatSheetOverlay');
        if (modal) modal.style.display = 'none';
        if (overlay) overlay.style.display = 'none';
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------
    return {
        init: function (config) {
            _config = config;
            // Merge stepLabels override if provided
            if (config.stepLabels) {
                stepLabels = config.stepLabels;
            }

            $(document).ready(function () {
                _initDataTable();
                _initCheckboxes();
                _initKeyboardHandler();
                _initSidePanel();
                if (config.resource === 'go') {
                    _initGoReviewEditor();
                    _loadGoScoringConfig();
                } else if (_usesStepAssessment()) {
                    _initStepReviewEditor();
                }

                // Wire bulk approve button
                $(document).on('click', '#bulkApproveBtn', function () {
                    _bulkApprove();
                });
            });
        },

        // Exposed for panel button onclick attributes and cheat-sheet close
        handleApprove: function () { _handleApprove(); },
        handleReject: function () { _handleReject(); },
        openCheatSheet: function () { _openCheatSheet(); },
        closeCheatSheet: function () { _closeCheatSheet(); }
    };
}());

window.AdminProposals = AdminProposals;
