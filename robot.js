/* ============================================================
   robot.js — robot state controller
   Public API:
     setRobotState(state, statusText, progress)
       state     — one of: 'idle' | 'uploading' | 'parsing'
                          | 'categorizing' | 'done' | 'error'
       statusText — string shown under the robot (optional)
       progress   — number 0..100 for the progress bar (optional)

   Designed so the mock flow in app.js can later be swapped for
   real polling of `/api/jobs/{id}/status` — just call
   setRobotState() with the data the backend returns.
   ============================================================ */
(function () {
  'use strict';

  const VALID_STATES = new Set([
    'idle',
    'uploading',
    'parsing',
    'categorizing',
    'done',
    'error',
  ]);

  const robot       = document.getElementById('robot');
  const statusEl    = document.getElementById('statusText');
  const progressEl  = document.getElementById('progressBar');

  // Track current state so callers can read it back if useful
  let currentState = 'idle';

  function setRobotState(state, statusText, progress) {
    if (!VALID_STATES.has(state)) {
      console.warn('[robot] unknown state:', state);
      return;
    }

    // Swap state class on the robot root — CSS handles all animation.
    if (state !== currentState) {
      VALID_STATES.forEach((s) => robot.classList.remove('state-' + s));
      robot.classList.add('state-' + state);
      currentState = state;
    }

    // Status text
    if (typeof statusText === 'string') {
      statusEl.textContent = statusText;
      statusEl.classList.toggle('is-error', state === 'error');
      statusEl.classList.toggle('is-done',  state === 'done');
    }

    // Progress bar (clamped 0..100)
    if (typeof progress === 'number' && !Number.isNaN(progress)) {
      const clamped = Math.max(0, Math.min(100, progress));
      progressEl.style.width = clamped + '%';
      progressEl.classList.toggle('is-error', state === 'error');
    }
  }

  function getRobotState() {
    return currentState;
  }

  // Expose globally so app.js can drive the robot.
  window.setRobotState = setRobotState;
  window.getRobotState = getRobotState;
})();
