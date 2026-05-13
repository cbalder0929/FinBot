/* ============================================================
   app.js — file handling, state, and the real upload flow.

   On "Start Processing", staged files are POSTed to /api/parse
   (served by server.py, which runs them through parsers/ and
   categorize.py). The robot walks states driven by real events:
     • uploading — until xhr.upload reports the request body sent
     • parsing   — while the server is parsing (no streaming, so
                   we creep the bar gently from 50% to ~85%)
     • categorizing — brief beat before the response lands
     • done       — show the real transaction count
     • error      — show the first failure message
   ============================================================ */
(function () {
  'use strict';

  // ---------- State ----------
  /** @type {Array<{id: string, file: File, name: string, size: number}>} */
  const staged = [];
  let processing = false;

  // ---------- DOM ----------
  const dropZone        = document.getElementById('dropZone');
  const fileInput       = document.getElementById('fileInput');
  const folderInput     = document.getElementById('folderInput');
  const chooseFilesBtn  = document.getElementById('chooseFilesBtn');
  const chooseFolderBtn = document.getElementById('chooseFolderBtn');
  const fileListWrap    = document.getElementById('fileListWrap');
  const fileListEl      = document.getElementById('fileList');
  const fileListCount   = document.getElementById('fileListCount');
  const startBtn        = document.getElementById('startBtn');

  // ---------- Utilities ----------
  const ACCEPTED_EXTS = new Set(['pdf', 'csv']);
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

  function extOf(name) {
    const i = name.lastIndexOf('.');
    return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
  }

  function isAcceptedFile(file) {
    return ACCEPTED_EXTS.has(extOf(file.name));
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
  }

  function uid() {
    return 'f_' + Math.random().toString(36).slice(2, 10);
  }

  function sleep(ms) {
    return new Promise((res) => setTimeout(res, ms));
  }

  function plural(n, word) {
    return n === 1 ? `${n} ${word}` : `${n} ${word}s`;
  }

  // ---------- File staging ----------
  function addFiles(fileList) {
    if (!fileList || !fileList.length) return;
    let added = 0;
    let skipped = 0;
    for (const file of fileList) {
      if (!isAcceptedFile(file)) { skipped++; continue; }
      // de-duplicate by name + size
      const dup = staged.some((s) => s.name === file.name && s.size === file.size);
      if (dup) continue;
      staged.push({
        id: uid(),
        file,
        name: file.name,
        size: file.size,
      });
      added++;
    }
    if (added > 0) renderFileList();
    if (added === 0 && skipped > 0 && !processing) {
      window.setRobotState('idle', 'Only PDF or CSV files are supported.', 0);
    }
  }

  function removeFile(id) {
    const idx = staged.findIndex((s) => s.id === id);
    if (idx >= 0) {
      staged.splice(idx, 1);
      renderFileList();
    }
  }

  function clearFiles() {
    staged.length = 0;
    renderFileList();
  }

  function renderFileList() {
    fileListEl.innerHTML = '';
    if (staged.length === 0) {
      fileListWrap.hidden = true;
    } else {
      fileListWrap.hidden = false;
      fileListCount.textContent = String(staged.length);
      for (const f of staged) {
        fileListEl.appendChild(buildFileRow(f));
      }
    }
    if (!processing) {
      startBtn.disabled = staged.length === 0;
    }
  }

  function buildFileRow(entry) {
    const ext = extOf(entry.name);
    const li = document.createElement('li');
    li.className = 'file-row';
    li.dataset.id = entry.id;

    const iconClass = ['pdf', 'csv', 'xlsx', 'xls'].includes(ext) ? ext : 'other';
    const iconLabel = ext ? ext.toUpperCase().slice(0, 4) : 'FILE';

    li.innerHTML = `
      <span class="file-icon ${iconClass}">${iconLabel}</span>
      <span class="file-name" title="${escapeAttr(entry.name)}">${escapeHtml(entry.name)}</span>
      <span class="file-size">${formatSize(entry.size)}</span>
      <button class="file-remove" type="button" aria-label="Remove ${escapeAttr(entry.name)}">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/>
        </svg>
      </button>
    `;

    li.querySelector('.file-remove').addEventListener('click', () => {
      if (processing) return;
      removeFile(entry.id);
    });

    return li;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ---------- Drag & drop ----------
  function preventDefaults(e) { e.preventDefault(); e.stopPropagation(); }

  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach((evt) => {
    dropZone.addEventListener(evt, preventDefaults);
  });
  ['dragenter', 'dragover'].forEach((evt) => {
    dropZone.addEventListener(evt, () => dropZone.classList.add('dragover'));
  });
  ['dragleave', 'drop'].forEach((evt) => {
    dropZone.addEventListener(evt, () => dropZone.classList.remove('dragover'));
  });

  dropZone.addEventListener('drop', (e) => {
    if (processing) return;
    const dt = e.dataTransfer;
    if (!dt) return;
    addFiles(dt.files);
  });

  dropZone.addEventListener('click', () => {
    if (processing) return;
    fileInput.click();
  });
  dropZone.addEventListener('keydown', (e) => {
    if (processing) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });

  // ---------- File pickers ----------
  chooseFilesBtn.addEventListener('click', () => {
    if (processing) return;
    fileInput.click();
  });
  chooseFolderBtn.addEventListener('click', () => {
    if (processing) return;
    folderInput.click();
  });

  fileInput.addEventListener('change', (e) => {
    addFiles(e.target.files);
    e.target.value = '';
  });
  folderInput.addEventListener('change', (e) => {
    addFiles(e.target.files);
    e.target.value = '';
  });

  // ---------- Real processing ----------
  startBtn.addEventListener('click', () => {
    if (processing || staged.length === 0) return;
    runProcessing();
  });

  window.addEventListener('finscrape:project-cleared', () => {
    if (processing) return;
    clearFiles();
    window.setRobotState('idle', 'Ready for a new project.', 0);
  });

  async function runProcessing() {
    processing = true;
    startBtn.disabled = true;
    startBtn.classList.add('processing');

    const fileCount = staged.length;

    try {
      window.setRobotState('uploading', `Uploading ${plural(fileCount, 'file')}…`, 0);
      const result = await uploadFiles();

      // Brief categorizing beat so the state shift reads clearly even
      // when the server returns quickly.
      window.setRobotState('categorizing', 'Categorizing transactions…', 92);
      await sleep(700);

      const created = result.created || [];
      const errors  = result.errors  || [];
      const total   = typeof result.total_transactions === 'number'
        ? result.total_transactions
        : created.reduce((s, c) => s + (c.rows || 0), 0);

      if (created.length === 0 && errors.length > 0) {
        // Every file failed — surface the first error.
        const first = errors[0];
        window.setRobotState('error', truncate(`Failed: ${first.error || 'unknown error'}`, 80), 100);
        await sleep(3200);
      } else {
        const msg = errors.length
          ? `Done! ${plural(total, 'transaction')} · ${plural(errors.length, 'file')} failed`
          : `Done! ${plural(total, 'transaction')} processed`;
        window.setRobotState('done', msg, 100);
        // Tell the Reports tab fresh data is available — it preloads
        // in the background so switching tabs is instant.
        window.dispatchEvent(new CustomEvent('finscrape:done', { detail: { total } }));
        await sleep(2200);
        clearFiles();
      }
    } catch (err) {
      console.error(err);
      const msg = (err && err.message) || 'Something went wrong';
      window.setRobotState('error', truncate(msg, 80), 100);
      await sleep(3200);
    } finally {
      window.setRobotState('idle', 'Waiting for files…', 0);
      processing = false;
      startBtn.classList.remove('processing');
      startBtn.disabled = staged.length === 0;
    }
  }

  /**
   * POST staged files to /api/parse using XMLHttpRequest so we can
   * track upload progress. Returns the parsed JSON body on success.
   */
  function uploadFiles() {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      for (const entry of staged) {
        form.append('files', entry.file, entry.name);
      }

      const xhr = new XMLHttpRequest();
      xhr.open('POST', apiUrl('/api/parse'));

      let parseTimer = null;

      // 0..50% = upload, 50..92% = waiting on server, 92..100% = categorize beat.
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          const pct = (e.loaded / e.total) * 50;
          window.setRobotState(
            'uploading',
            `Uploading ${plural(staged.length, 'file')}…`,
            pct
          );
        }
      });

      xhr.upload.addEventListener('load', () => {
        // Body sent — server is now parsing. Creep the bar so the user
        // sees motion until the JSON response lands.
        window.setRobotState('parsing', 'Parsing transactions…', 50);
        let p = 50;
        parseTimer = setInterval(() => {
          p = Math.min(88, p + 1.2);
          window.setRobotState('parsing', 'Parsing transactions…', p);
        }, 220);
      });

      xhr.addEventListener('load', () => {
        if (parseTimer) clearInterval(parseTimer);
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch (e) {
            reject(new Error('Invalid response from server'));
          }
        } else {
          reject(new Error(httpErrorMessage(xhr.status)));
        }
      });

      xhr.addEventListener('error', () => {
        if (parseTimer) clearInterval(parseTimer);
        reject(new Error('Network error — is the server running?'));
      });

      xhr.addEventListener('abort', () => {
        if (parseTimer) clearInterval(parseTimer);
        reject(new Error('Upload aborted'));
      });

      xhr.send(form);
    });
  }

  function truncate(s, n) {
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function httpErrorMessage(status) {
    if (status === 405) {
      return 'Server returned HTTP 405. Open the app from FastAPI at http://127.0.0.1:8000, or keep FastAPI running there if using a static dev server.';
    }
    if (status === 0 && API_BASE) {
      return 'Cannot reach the API server at http://127.0.0.1:8000.';
    }
    return `Server returned HTTP ${status}`;
  }

  // ---------- Init ----------
  window.setRobotState('idle', 'Waiting for files…', 0);
  renderFileList();
})();
