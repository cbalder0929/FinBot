/* ============================================================
   reports.js — Reports tab controller.

   - Tab switching between Upload (robot) and Reports.
   - Fetches /api/reports/{topline,trend,by-category,top-merchants}
     and renders the dashboard.
   - "AI summary" button triggers /api/reports/topline?narrate=1
     which calls Ollama on the server. Slow on CPU-only Ollama, so
     the button shows a clear loading state.
   ============================================================ */
(function () {
  'use strict';

  // ---------- DOM ----------
  const tabUpload  = document.getElementById('tabUpload');
  const tabReports = document.getElementById('tabReports');
  const viewUpload = document.getElementById('viewUpload');
  const viewReports = document.getElementById('viewReports');

  const reportsEmpty = document.getElementById('reportsEmpty');
  const reportsBody  = document.getElementById('reportsBody');
  const refreshBtn   = document.getElementById('reportsRefreshBtn');
  const clearBtn     = document.getElementById('reportsClearBtn');
  const narrateBtn   = document.getElementById('reportsNarrateBtn');

  const narrationBlock = document.getElementById('narrationBlock');
  const narrationText  = document.getElementById('narrationText');

  const elIncome   = document.getElementById('toplineIncome');
  const elSpending = document.getElementById('toplineSpending');
  const elNet      = document.getElementById('toplineNet');
  const elTopCat   = document.getElementById('toplineTopCat');
  const periodLine = document.getElementById('periodLine');

  const trendList    = document.getElementById('trendList');
  const categoryList = document.getElementById('categoryList');
  const merchantList = document.getElementById('merchantList');

  // ---------- State ----------
  let loaded = false;
  let lastTopline = null;   // remember for narration
  let narrating = false;
  const API_BASE = getApiBase();

  function getApiBase() {
    if (window.FINSCRAPE_API_BASE) {
      return String(window.FINSCRAPE_API_BASE).replace(/\/$/, '');
    }
    const staticDevPorts = new Set(['3000', '5173', '5500', '5501']);
    if (location.protocol === 'file:' || staticDevPorts.has(location.port)) {
      return 'http://127.0.0.1:8000';
    }
    return '';
  }

  function apiUrl(path) {
    return API_BASE + path;
  }

  // ---------- Utilities ----------
  const fmt = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0,
  });
  const fmtCents = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  function money(n) { return fmt.format(Number(n || 0)); }
  function moneyCents(n) { return fmtCents.format(Number(n || 0)); }
  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ---------- Tab switching ----------
  function showTab(which) {
    const isReports = which === 'reports';
    tabUpload.classList.toggle('is-active', !isReports);
    tabReports.classList.toggle('is-active', isReports);
    tabUpload.setAttribute('aria-selected', String(!isReports));
    tabReports.setAttribute('aria-selected', String(isReports));
    viewUpload.classList.toggle('is-active', !isReports);
    viewReports.classList.toggle('is-active', isReports);

    if (isReports && !loaded) loadReports();
  }
  tabUpload.addEventListener('click', () => showTab('upload'));
  tabReports.addEventListener('click', () => showTab('reports'));

  // Auto-flip to Reports tab after a successful Done! so the user lands
  // on something useful instead of an idle robot.
  window.addEventListener('finscrape:done', () => {
    // Pre-load in the background so the data is ready when they click.
    loadReports().catch(() => {});
  });

  refreshBtn.addEventListener('click', () => loadReports());
  clearBtn.addEventListener('click', () => clearProjectFiles());
  narrateBtn.addEventListener('click', () => narrateNow());

  // ---------- Q&A (Phase 3) ----------
  const qaForm        = document.getElementById('qaForm');
  const qaInput       = document.getElementById('qaInput');
  const qaBtn         = document.getElementById('qaBtn');
  const qaThinking    = document.getElementById('qaThinking');
  const qaThinkingText = document.getElementById('qaThinkingText');
  const qaAnswerEl    = document.getElementById('qaAnswer');
  const qaNarration   = document.getElementById('qaNarration');
  const qaResult      = document.getElementById('qaResult');
  const qaSpec        = document.getElementById('qaSpec');

  let qaInFlight = false;

  qaForm.addEventListener('submit', (e) => {
    e.preventDefault();
    askQuestion(qaInput.value);
  });

  document.querySelectorAll('.qa-example').forEach((b) => {
    b.addEventListener('click', () => {
      qaInput.value = b.dataset.q || b.textContent;
      askQuestion(qaInput.value);
    });
  });

  async function askQuestion(text) {
    text = (text || '').trim();
    if (!text || qaInFlight) return;
    qaInFlight = true;
    qaBtn.disabled = true;
    qaInput.disabled = true;
    qaAnswerEl.hidden = true;
    qaThinking.hidden = false;
    qaThinkingText.textContent = 'Asking Ollama… this can take a few minutes on a CPU-only system.';

    // Tick the message every ~20s so the user knows we're not frozen.
    let ticks = 0;
    const tickTimer = setInterval(() => {
      ticks += 1;
      const elapsed = ticks * 20;
      qaThinkingText.textContent =
        `Still working (${elapsed}s elapsed)… two LLM calls per question on CPU-only Ollama is slow.`;
    }, 20000);

    try {
      const r = await fetch(apiUrl('/api/qa'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: text }),
      });
      const body = await r.json();
      renderQAAnswer(body);
    } catch (err) {
      renderQAAnswer({ error: err.message || String(err) });
    } finally {
      clearInterval(tickTimer);
      qaThinking.hidden = true;
      qaAnswerEl.hidden = false;
      qaBtn.disabled = false;
      qaInput.disabled = false;
      qaInFlight = false;
    }
  }

  function renderQAAnswer(body) {
    qaResult.innerHTML = '';
    qaSpec.textContent = '';
    qaNarration.classList.remove('is-error');

    if (body.error) {
      qaNarration.textContent = body.error;
      qaNarration.classList.add('is-error');
      return;
    }

    qaNarration.textContent = body.narration
      || 'Ollama returned no narration — the numbers below are still correct.';

    // Render the structured result.
    const result = body.result;
    if (!result) {
      qaResult.textContent = '(no result)';
    } else if (result.kind === 'scalar') {
      const isMoney = result.currency === 'USD';
      const big = document.createElement('div');
      big.className = 'qa-result-scalar';
      big.textContent = isMoney ? moneyCents(result.value) : String(result.value);
      qaResult.appendChild(big);

      const meta = document.createElement('div');
      meta.className = 'qa-result-meta';
      const bits = [`${result.count} matching transaction${result.count === 1 ? '' : 's'}`];
      if (typeof result.months === 'number') bits.push(`across ${result.months} month${result.months === 1 ? '' : 's'}`);
      meta.textContent = bits.join(' · ');
      qaResult.appendChild(meta);
    } else if (result.kind === 'list') {
      if (!result.items || result.items.length === 0) {
        qaResult.textContent = 'No matching rows.';
      } else {
        const ul = document.createElement('ul');
        for (const it of result.items) {
          const li = document.createElement('li');
          const label = document.createElement('span');
          label.textContent = it.label || it.item || '(unlabeled)';
          const value = document.createElement('span');
          if (typeof it.value === 'number') {
            value.textContent = result.currency === 'USD' ? moneyCents(it.value) : String(it.value);
          } else if (typeof it.debits === 'number' || typeof it.credits === 'number') {
            const amt = (it.debits || 0) - (it.credits || 0);
            value.textContent = moneyCents(amt > 0 ? amt : (it.credits || 0));
          }
          li.appendChild(label);
          li.appendChild(value);
          ul.appendChild(li);
        }
        qaResult.appendChild(ul);
      }
    }

    qaSpec.textContent = JSON.stringify(body.spec, null, 2);
  }

  // ---------- Fetching ----------
  async function loadReports() {
    refreshBtn.disabled = true;
    refreshBtn.classList.add('processing');
    try {
      const [tl, trend, byCat, top] = await Promise.all([
        fetchJSON('/api/reports/topline'),
        fetchJSON('/api/reports/trend'),
        fetchJSON('/api/reports/by-category'),
        fetchJSON('/api/reports/top-merchants?limit=10'),
      ]);

      const tldata = (tl && tl.data) || {};
      if (!tldata.transaction_count) {
        showEmpty();
        return;
      }

      lastTopline = tldata;
      renderTopline(tldata);
      renderTrend(((trend && trend.data) || {}).months || []);
      renderCategories(((byCat && byCat.data) || {}).categories || []);
      renderMerchants(((top && top.data) || {}).merchants || []);
      showLoaded();
    } catch (err) {
      console.error('[reports] load failed', err);
      showEmpty(`Couldn't load reports: ${err.message || err}`);
    } finally {
      refreshBtn.disabled = false;
      refreshBtn.classList.remove('processing');
    }
  }

  async function clearProjectFiles() {
    if (narrating || qaInFlight) return;
    const ok = window.confirm(
      'Clear all processed files and report data? This starts a new project.'
    );
    if (!ok) return;

    clearBtn.disabled = true;
    refreshBtn.disabled = true;
    narrateBtn.disabled = true;
    clearBtn.classList.add('processing');

    try {
      const r = await fetch(apiUrl('/api/files'), { method: 'DELETE' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await r.json().catch(() => ({}));

      loaded = false;
      lastTopline = null;
      narrationBlock.hidden = true;
      narrationText.textContent = '';
      qaThinking.hidden = true;
      qaAnswerEl.hidden = true;
      qaResult.innerHTML = '';
      qaSpec.textContent = '';
      showEmpty('Project cleared. Process new statements on the Upload tab.');
      window.dispatchEvent(new CustomEvent('finscrape:project-cleared'));
    } catch (err) {
      showEmpty(`Couldn't clear project files: ${err.message || err}`);
    } finally {
      clearBtn.disabled = false;
      refreshBtn.disabled = false;
      narrateBtn.disabled = false;
      clearBtn.classList.remove('processing');
    }
  }

  async function fetchJSON(url) {
    const r = await fetch(apiUrl(url));
    if (!r.ok) throw new Error(`HTTP ${r.status} on ${url}`);
    return r.json();
  }

  function showEmpty(msg) {
    if (msg) {
      reportsEmpty.querySelector('p').textContent = msg;
    } else {
      reportsEmpty.querySelector('p').textContent =
        'No data yet. Process some statements on the Upload tab first.';
    }
    reportsEmpty.hidden = false;
    reportsBody.hidden = true;
    narrationBlock.hidden = true;
    loaded = false;
  }

  function showLoaded() {
    reportsEmpty.hidden = true;
    reportsBody.hidden = false;
    loaded = true;
  }

  // ---------- Renderers ----------
  function renderTopline(d) {
    elIncome.textContent = money(d.total_income);
    elSpending.textContent = money(d.total_spending);
    const net = Number(d.net || 0);
    elNet.textContent = money(net);
    elNet.classList.toggle('is-positive', net >= 0);
    elNet.classList.toggle('is-negative', net < 0);
    elTopCat.textContent = d.top_category || '—';

    if (d.period && d.period.from && d.period.to) {
      periodLine.textContent = `${d.transaction_count} transactions · ${d.period.from} → ${d.period.to}`;
    } else {
      periodLine.textContent = `${d.transaction_count} transactions`;
    }
  }

  function renderTrend(months) {
    trendList.innerHTML = '';
    for (const m of months) {
      const li = document.createElement('li');
      li.className = 'trend-row';
      const netCls = Number(m.net) >= 0 ? 'is-positive' : 'is-negative';
      li.innerHTML = `
        <span class="trend-month">${esc(m.month)}</span>
        <span class="trend-num">${moneyCents(m.income)}</span>
        <span class="trend-num">${moneyCents(m.spending)}</span>
        <span class="trend-num ${netCls}">${moneyCents(m.net)}</span>
      `;
      trendList.appendChild(li);
    }
  }

  function renderCategories(cats) {
    categoryList.innerHTML = '';
    if (cats.length === 0) return;
    const max = cats[0].spending || 1;
    for (const c of cats.slice(0, 12)) {
      const pct = max > 0 ? Math.max(2, (c.spending / max) * 100) : 0;
      const li = document.createElement('li');
      li.className = 'bar-row';
      li.style.setProperty('--bar-fill', pct + '%');
      li.innerHTML = `
        <span class="bar-label">
          <span>${esc(c.category)}</span>
          <span class="bar-count">${c.count} txn${c.count === 1 ? '' : 's'}</span>
        </span>
        <span class="bar-amount">${moneyCents(c.spending)}</span>
      `;
      categoryList.appendChild(li);
    }
  }

  function renderMerchants(merchants) {
    merchantList.innerHTML = '';
    for (const m of merchants) {
      const li = document.createElement('li');
      li.className = 'merchant-row';
      li.innerHTML = `
        <div>
          <div class="merchant-name" title="${esc(m.item)}">${esc(m.item)}</div>
          <div class="merchant-meta">${esc(m.category)} · ${m.count} txn${m.count === 1 ? '' : 's'}</div>
        </div>
        <span class="merchant-amount">${moneyCents(m.total)}</span>
      `;
      merchantList.appendChild(li);
    }
  }

  // ---------- Narration ----------
  async function narrateNow() {
    if (narrating) return;
    if (!loaded) {
      await loadReports();
      if (!loaded) return;
    }
    narrating = true;
    narrateBtn.disabled = true;
    narrateBtn.classList.add('processing');
    narrationBlock.hidden = false;
    narrationText.textContent = 'Asking Ollama for a summary… (this can take a minute on a CPU-only system)';
    try {
      const r = await fetch(apiUrl('/api/reports/topline?narrate=1'));
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      if (body.narration) {
        narrationText.textContent = body.narration;
      } else {
        // Prefer the specific reason returned by the server (timeout,
        // unreachable, empty response). Falling back to a generic
        // message only when the server didn't tell us why.
        const reason = body.narration_error || 'No narration returned.';
        narrationText.textContent = `${reason} The numbers above are still accurate.`;
      }
    } catch (err) {
      narrationText.textContent = `Couldn't generate narration: ${err.message || err}`;
    } finally {
      narrating = false;
      narrateBtn.disabled = false;
      narrateBtn.classList.remove('processing');
    }
  }
})();
