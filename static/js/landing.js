/**
 * Landing page count-up animation.
 * Vanilla requestAnimationFrame — no CountUp.js dependency (locked by RESEARCH.md).
 * Selects all .stat-card__value[data-target] and animates each to its target value.
 *
 * The template server-renders the real number as the element's text; this script
 * resets it to 0 and animates back up. That order matters (#211): the markup used
 * to ship a literal "0", so anything that captured the page before the animation
 * finished — a screenshot, a background tab where requestAnimationFrame is
 * throttled, a print render, reader mode — reported a number far below the truth,
 * and with JavaScript disabled the cards read "0" permanently. The animation is
 * now purely decorative: remove it and the page still tells the truth.
 */
(function () {
  'use strict';

  var DURATION = 1200; // ms

  function finalize(el, target) {
    el.textContent = target.toLocaleString();
  }

  function animateCount(el, target) {
    var start = null;
    var from = 0;

    function step(timestamp) {
      if (!start) start = timestamp;
      var elapsed = timestamp - start;
      var progress = Math.min(elapsed / DURATION, 1);
      if (progress >= 1) {
        // Assign the target explicitly rather than trusting the eased value to
        // round exactly onto it.
        finalize(el, target);
        return;
      }
      // Ease-out cubic
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = Math.round(from + (target - from) * eased).toLocaleString();
      requestAnimationFrame(step);
    }

    requestAnimationFrame(step);
  }

  function init() {
    var cards = document.querySelectorAll('.stat-card__value[data-target]');
    var pending = [];

    for (var i = 0; i < cards.length; i++) {
      var el = cards[i];
      var target = parseInt(el.getAttribute('data-target'), 10);
      if (isNaN(target) || target < 0) {
        // Leave the server-rendered text alone rather than blanking it.
        continue;
      }
      pending.push({ el: el, target: target });
    }

    // prefers-reduced-motion cannot be honoured in CSS here: main.css only zeroes
    // animation/transition durations, which has no effect on a JS textContent
    // loop. Leave the server-rendered values in place and animate nothing.
    var reduceMotion = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) {
      return;
    }

    // Introduce the 0 start state synchronously, in the same frame as the first
    // requestAnimationFrame, so there is no visible flash of the final value.
    for (var j = 0; j < pending.length; j++) {
      pending[j].el.textContent = '0';
      animateCount(pending[j].el, pending[j].target);
    }

    // Backstops. A tab hidden at load never runs the rAF loop, so the cards would
    // sit at "0" until it is focused; and a loop interrupted part-way would leave
    // a partial number on screen. Snap to the truth in both cases.
    function snapAll() {
      for (var k = 0; k < pending.length; k++) {
        finalize(pending[k].el, pending[k].target);
      }
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) snapAll();
    });
    window.addEventListener('pagehide', snapAll);
    setTimeout(snapAll, DURATION + 100);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
