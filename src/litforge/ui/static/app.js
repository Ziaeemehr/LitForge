/* LitForge web UI: input form, background jobs, results with stats, filters and a quick view. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var store = {
    get: function (k, d) { try { var v = localStorage.getItem('lf-' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem('lf-' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  };

  var state = {
    mode: 'similar', projects: [], project: store.get('project', null), runId: null, data: null,
    filter: '', oa: false, onlyNew: false, hide: false, channel: null, sort: store.get('sort', 'score'),
    screenFilter: '', criteria: null, criteriaFor: null, criteriaLoading: null,
    sel: -1, view: [], page: 'new', seedPdfs: [], list: null, pdfs: [], missing: [], fetchReasons: {}, job: null,
    anchor: -1, exportsRoot: '', dirEdited: false, consolePlan: null, consoleRunId: null,
    libraryAllVersions: store.get('library-all-versions', false)
  };

  var CHANNEL_LABEL = {
    references: 'Cited by the seed', cited_by: 'Cites the seed', related: 'Related (OpenAlex)',
    keywords: 'Key-phrase search', neighbours: 'Citations of close matches', query: 'Search query',
    'import': 'Imported list', seed: 'Seed', pdf: 'Your PDFs'
  };

  /* ---------- helpers ---------- */

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) {
          var error = new Error(body.error || ('HTTP ' + r.status));
          error.status = r.status; error.preview = body.preview || null;
          throw error;
        }
        return body;
      });
    });
  }

  function post(path, body) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  var toastTimer;
  function toast(msg) {
    var t = $('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2600);
  }

  var SCREEN_ORDER = { include: 0, uncertain: 1, exclude: 2 };
  function screenRank(p) { return p.screening ? SCREEN_ORDER[p.screening.decision] : 3; }
  function screenScore(p) { return p.screening ? p.screening.score : 0; }
  function screenBadge(p) {
    var s = p.screening;
    if (!s) return '';
    var tip = (s.stale ? 'Criteria changed since screening. ' : '') + (s.error ? 'Model error: ' + s.error : s.reason || '');
    return '<span class="badge scr ' + esc(s.decision) + (s.stale ? ' stale' : '') + '" title="' + esc(tip) + '">' +
      esc(s.decision) + ' ' + s.score.toFixed(2) + '</span>';
  }

  function projectName() { return ($('project-name').value || state.project || 'default').trim(); }

  function paperLinks(p) {
    var out = [];
    if (p.doi) out.push('<a target="_blank" rel="noopener" href="https://doi.org/' + esc(p.doi) + '">DOI</a>');
    if (p.arxiv_id) out.push('<a target="_blank" rel="noopener" href="https://arxiv.org/abs/' + esc(p.arxiv_id) + '">arXiv</a>');
    if (p.openalex_id) out.push('<a target="_blank" rel="noopener" href="https://openalex.org/' + esc(p.openalex_id) + '">OpenAlex</a>');
    if (p.oa_url) out.push('<a target="_blank" rel="noopener" href="' + esc(p.oa_url) + '">Open access</a>');
    return out.join('');
  }

  /* ---------- projects and runs ---------- */

  function loadProjects() {
    return api('/api/projects').then(function (projects) {
      state.projects = projects;
      if (state.project && !projects.some(function (p) { return p.name === state.project; })) state.project = null;
      if (!state.project && projects.length) state.project = projects[0].name;
      renderSideProjects();
      $('project-list').innerHTML = projects.map(function (p) { return '<option value="' + esc(p.name) + '">'; }).join('');
      $('recents-project').innerHTML = '<option value="">All projects</option>' +
        projects.map(function (p) { return '<option value="' + esc(p.name) + '">' + esc(p.name) + '</option>'; }).join('');
      $('recents-project').value = state.recentsProject || '';
      syncProjectFields();
      if (state.page === 'write' && !W.doc) loadWrite();
      return loadRuns();
    });
  }

  function renderSideProjects() {
    var el = $('side-projects');
    el.innerHTML = state.projects.length ? '' : '<div class="none">No projects yet</div>';
    state.projects.forEach(function (p) {
      var a = document.createElement('a');
      a.href = '#';
      a.className = p.name === state.project ? 'on' : '';
      a.title = p.name + ' · ' + p.papers + ' papers · ' + p.runs + ' searches';
      a.innerHTML = '<span class="count">' + p.papers + '</span>' + esc(p.name);
      a.onclick = function (e) { e.preventDefault(); selectProject(p.name); state.recentsProject = p.name; showView('recents'); };
      el.appendChild(a);
    });
    var cur = state.projects.find(function (p) { return p.name === state.project; });
    $('current-project').innerHTML = cur ? esc(cur.name) + ' <small>· ' + cur.papers + ' papers</small>'
      : '<small>No project yet: start with + New</small>';
    $('btn-delete-project').hidden = !cur;
    $('btn-delete-project').onclick = cur ? function () { deleteProject(cur); } : null;
    $('btn-open-folder').hidden = !cur;
    $('btn-open-folder').onclick = cur ? function () {
      post('/api/open-exports', { project: cur.name }).catch(function (err) { toast(err.message); });
    } : null;
  }

  // Exports and new searches use the selected project.
  function selectProject(name) {
    if (name === state.project) return;
    state.project = name;
    store.set('project', name);
    syncProjectFields();
    renderSideProjects();
    if (state.page === 'write') closeWriteDoc(false);
  }

  function syncProjectFields() {
    $('project-name').value = state.project || '';
    state.dirEdited = false;
    showPaths();
  }

  // The export folder field starts at the project's folder (or the folder saved for the project) and
  // follows the project name until edited; the value is saved with the next run or export.
  function defaultDir(name) { return state.exportsRoot ? state.exportsRoot + '/' + name : ''; }

  function showPaths() {
    var name = $('project-name').value.trim(), el = $('project-paths'), dir = $('export-dir');
    var p = state.projects.find(function (x) { return x.name === name; });
    if (!state.dirEdited) dir.value = p ? p.export_dir : (name ? defaultDir(name) : '');
    dir.placeholder = name ? defaultDir(name) : 'Give a project name first';
    $('btn-export').title = 'Write Markdown notes to ' + (dir.value || 'the project folder');
    var links = [];
    if (name && dir.value.trim() !== defaultDir(name)) links.push('<a href="#" id="reset-exports">use default folder</a>');
    if (p) links.push('<a href="#" id="open-exports">open folder</a>', '<a href="#" id="delete-project" class="danger">delete project</a>');
    else if (name) links.unshift('New project');
    el.innerHTML = links.join(' · ');
    if ($('reset-exports')) {
      $('reset-exports').onclick = function (e) { e.preventDefault(); state.dirEdited = true; dir.value = defaultDir(name); showPaths(); };
    }
    if (!p) return;
    $('open-exports').onclick = function (e) {
      e.preventDefault();
      post('/api/open-exports', { project: p.name }).catch(function (err) { toast(err.message); });
    };
    $('delete-project').onclick = function (e) { e.preventDefault(); deleteProject(p); };
  }

  // Papers without a PDF: their notes hold only the abstract, so list where to get each one by hand.
  // Unpaywall finds many more open-access copies, but needs the user's e-mail: ask for it here, where it helps.
  function showMail(mailto) {
    var el = $('missing-mail');
    el.replaceChildren();
    var text = document.createElement('span');
    text.textContent = mailto ? 'Contact e-mail for PDF discovery: ' + mailto + ' · ' :
      'Add your contact e-mail in Settings to find more open-access copies with Unpaywall. ';
    el.appendChild(text);
    el.appendChild(modelButton('Settings', function () { $('missing').close(); showView('settings'); showSettingsTab('general'); }));
  }

  function fetchMissing() {
    var ids = state.missing.map(function (p) { return p.id; });
    if (!ids.length) return;
    $('missing-fetch').disabled = true;
    post('/api/fetch', { project: state.project, paper_ids: ids }).then(function (job) {
      (function check() {
        api('/api/jobs/' + job.id).then(function (j) {
          $('missing-status').textContent = j.messages.length ? j.messages[j.messages.length - 1] : 'Searching…';
          if (j.status === 'running') { setTimeout(check, 1000); return; }
          $('missing-fetch').disabled = false;
          $('missing-status').textContent = j.status === 'done' ? '' : 'Failed: ' + j.error;
          if (j.report) showFetchReport(j.report);
          showMissing();
          if (state.runId && state.page === 'results') loadRun(state.runId);  // refresh the PDF badges
        }).catch(function (e) { $('missing-fetch').disabled = false; toast(e.message); });
      })();
    }).catch(function (e) { $('missing-fetch').disabled = false; toast(e.message); });
  }

  // What the search did, and where the downloaded files are; each paper's reason shows under its title.
  function showFetchReport(r) {
    state.fetchReasons = {};
    r.items.forEach(function (i) { state.fetchReasons[i.paper_id] = i; });
    var got = r.items.filter(function (i) { return i.outcome === 'downloaded'; });
    var left = r.candidates - got.length;
    var html = '<h3>Downloaded ' + got.length + ' of ' + r.candidates + ' papers</h3>';
    if (got.length) {
      html += '<p>' + got.map(function (i) {
        return '✓ ' + esc(i.title || '(no title)') + ' · <a target="_blank" rel="noopener" href="/api/papers/' +
          esc(i.paper_id) + '/pdf">open PDF</a>';
      }).join('<br>') + '</p><p>Saved in the library (<code>' + esc(r.library) + '</code>) with their full text. ' +
        'Export copies them to <code>' + esc(r.export_dir) + '/pdfs</code> and rewrites their notes in <code>' +
        esc(r.export_dir) + '/markdown</code>. <button class="btn small" id="report-export">Export now</button> ' +
        '<button class="btn small" data-open-exports>Open export folder</button></p>';
    }
    html += left ? '<p>The ' + left + ' paper' + (left === 1 ? '' : 's') + ' below still need' + (left === 1 ? 's' : '') +
      ' a PDF; under each title is why it could not be downloaded. Get them in your browser, then add them with ' +
      '+ New → My PDFs.</p>' : '<p>Every listed paper now has its PDF.</p>';
    $('missing-report').innerHTML = html;
    $('missing-report').hidden = false;
    if ($('report-export')) $('report-export').onclick = function () { $('missing').close(); $('btn-export').click(); };
    wireOpenExports($('missing-report'));
  }

  // After adding PDFs: which paper each file went to, whether its text was extracted, and where files are kept.
  function showAddReport(r) {
    var LABEL = {
      attached: ['ok', '✓', 'attached to'], new_paper: ['warn', '+', 'not matched to a paper of the project; added as a new paper:'],
      already: ['ok', '=', 'already in the library, for'], failed: ['no', '✗', 'not added:']
    };
    var count = function (o) { return r.items.filter(function (i) { return i.outcome === o; }).length; };
    var parts = [['attached', 'attached'], ['new_paper', 'new'], ['already', 'already there'], ['failed', 'failed']]
      .filter(function (o) { return count(o[0]); }).map(function (o) { return count(o[0]) + ' ' + o[1]; });
    $('added-title').textContent = r.items.length + ' PDF' + (r.items.length === 1 ? '' : 's') +
      (parts.length ? ': ' + parts.join(', ') : '');
    var html = '<ul>' + r.items.map(function (i) {
      var l = LABEL[i.outcome];
      var what = i.outcome === 'failed' ? esc(i.reason) : '<b>' + esc(i.title || '(no title)') + '</b>' +
        (i.full_text ? ' · full text extracted' : ' · <span class="no">text not extracted</span>') +
        ' · <a target="_blank" rel="noopener" href="/api/papers/' + esc(i.paper_id) + '/pdf">open PDF</a>';
      return '<li><span class="' + l[0] + '">' + l[1] + '</span> <code>' + esc(i.file) + '</code> ' + l[2] + ' ' + what + '</li>';
    }).join('') + '</ul>';
    if (count('new_paper')) {
      html += '<p class="hint">A PDF is matched by the DOI or title printed in it. One added as a new paper may be a ' +
        'paper you were missing whose PDF does not show these clearly; it is still in the project and gets its own note.</p>';
    }
    var head = r.exported
      ? '<div class="done"><span class="ok">✓</span> Exported ' + r.exported.papers + ' note' +
        (r.exported.papers === 1 ? '' : 's') + ' (' + esc(state.addScope || SCOPE_WORDS.all) + '), with full text where ' +
        'there is a PDF, to <code>' + esc(r.export_dir) + '</code> <button class="btn small" data-open-exports>Open export ' +
        'folder</button></div>'
      : '<div class="done">' + (r.export_error ? '<span class="no">✗</span> Export failed: ' + esc(r.export_error)
        : 'Not exported yet.') + ' <button class="btn small accent" id="added-export">Export now</button> ' +
        '<button class="btn small" data-open-exports>Open export folder</button></div>';
    html += '<p class="hint">The library keeps its own copy of each file in <code>' + esc(r.library) + '</code>; your ' +
      'originals are not moved. An export writes the notes to <code>markdown</code> and the PDFs to <code>pdfs</code> ' +
      'in the export folder.</p>';
    $('added-report').innerHTML = head + html;
    $('added').showModal();
    if ($('added-export')) $('added-export').onclick = function () { $('added').close(); $('btn-export').click(); };
    wireOpenExports($('added-report'));
  }

  // Better math (high) without Ollama or its model: what is missing, what this computer has, and an offer to
  // install. On success the export the user asked for runs (see poll).
  function showMathSetup(st, fromSettings) {
    state.settingsVisionSetup = !!fromSettings;
    var req = st.requirements, has = function (ok, text) {
      return '<li class="' + (ok ? 'ok' : 'no') + '">' + (ok ? '✓ ' : '✗ ') + text + '</li>';
    };
    var html = '<h3>What is missing</h3><ul>' +
      has(st.installed, 'Ollama, the free program that runs the model' + (st.local ? '' : ' (at ' + esc(st.url) + ')')) +
      has(st.running, 'Ollama running') +
      has(st.model_ready, 'The model <code>' + esc(st.model) + '</code> (about ' + req.model_gb + ' GB download)') + '</ul>';
    if (st.local) {
      html += '<h3>Needed at least</h3><ul><li>' + req.memory_gb + ' GB of memory and ' + req.disk_gb +
        ' GB of free disk</li><li>A GPU is optional: NVIDIA or Apple Silicon makes it fast (about 12 s per page)</li></ul>' +
        '<h3>This computer</h3><ul>' +
        has(!!st.gpu, st.gpu ? 'GPU: ' + esc(st.gpu) : 'No GPU found') +
        (st.memory_gb == null ? '' : has(st.memory_gb >= req.memory_gb, Math.round(st.memory_gb) + ' GB of memory')) +
        (st.disk_free_gb == null ? '' : has(st.disk_free_gb >= req.disk_gb, Math.round(st.disk_free_gb) + ' GB of free disk')) +
        '</ul>';
    }
    st.warnings.forEach(function (w) { html += '<p class="warn">' + esc(w) + '</p>'; });
    st.blockers.forEach(function (b) { html += '<p class="no">' + esc(b) + '</p>'; });
    var canInstall = !st.blockers.length && (st.installed || !!st.install);
    if (canInstall) {
      html += '<p>LitForge can do this for you' + (st.installed ? '' : ' (Ollama from ollama.com' +
        (st.install === 'brew' ? ', with Homebrew' : '') + ')') + '. The download takes a while; progress shows in the log.</p>';
    } else if (!st.blockers.length && st.manual.length) {
      html += '<p>LitForge cannot install Ollama here. Run in a terminal, then export again:</p><ul>' +
        st.manual.map(function (m) { return '<li><code>' + esc(m) + '</code></li>'; }).join('') + '</ul>';
    }
    $('mathsetup-report').innerHTML = html;
    $('mathsetup-install').hidden = !canInstall;
    $('mathsetup-install').textContent = fromSettings ?
      (st.installed ? 'Download the image model' : 'Install the image model') :
      (st.installed ? 'Download the model and export' : 'Install and export');
    var medium = $('export-math').querySelector('option[value="medium"]');
    $('mathsetup-medium').hidden = !!fromSettings || medium.disabled;
    $('mathsetup').showModal();
  }

  // After "better math": how many pages of each paper the vision model rewrote, and where the notes are.
  function showVisionReport(r, done) {
    var v = r.vision;
    $('added-title').textContent = 'Better math (' + (v.level || 'high') + '): ' + v.pages_improved + ' pages rewritten in ' +
      v.papers + ' papers';
    var html = '<ul>' + v.items.map(function (i) {
      var what = !i.pages ? 'no page needed it' : i.improved + ' of ' + i.pages + ' pages rewritten' +
        (i.rejected ? ' · <span class="warn">' + i.rejected + ' kept as extracted (the transcription did not match the page)</span>' : '');
      return '<li><span class="' + (i.rejected ? 'warn' : 'ok') + '">' + (i.rejected ? '~' : '✓') + '</span> <b>' +
        esc(i.title) + '</b> — ' + what + '</li>';
    }).join('') + '</ul>';
    html += '<p>' + esc(done.replace(/^done: /, '')) + '. ' + (v.level === 'medium'
      ? 'On the rewritten pages every formula Pix2Text found is LaTeX ($…$ inline, $$…$$ displayed); the text is as extracted. '
      : 'Pages the model rewrote have equations in LaTeX ($$…$$) and tables as Markdown tables. ') +
      'Each note says in its "math" field (raw, medium or high) how far to trust its equations. The result is kept, so ' +
      'the next export is fast. <button class="btn small" data-open-exports>Open export folder</button></p>';
    $('added-report').innerHTML = html;
    $('added').showModal();
    wireOpenExports($('added-report'));
  }

  function wireOpenExports(el) {
    el.querySelectorAll('[data-open-exports]').forEach(function (b) {
      b.onclick = function () {
        post('/api/open-exports', { project: state.project }).catch(function (err) { toast(err.message); });
      };
    });
  }

  function showMissing() {
    if (!state.project) { toast('No project yet'); return; }
    var scope = $('missing-scope').value, el = $('missing-list');
    el.innerHTML = '<div class="empty">Loading…</div>';
    if (!$('missing').open) $('missing').showModal();
    api('/api/missing-pdfs?project=' + encodeURIComponent(state.project) + '&only=' + scope).then(function (list) {
      $('missing').querySelector('h2').textContent = list.length + ' paper' + (list.length === 1 ? '' : 's') +
        ' without a PDF in ' + state.project;
      state.missing = list;
      $('missing-fetch').hidden = !list.length;
      if (!list.length) { el.innerHTML = '<div class="empty">Every paper here has its PDF.</div>'; return; }
      el.innerHTML = list.map(function (p) {
        var meta = [p.year, p.venue, p.authors.join(', ')].filter(Boolean).map(esc).join(' · ');
        var links = [];
        if (p.oa_url) links.push('<a target="_blank" rel="noopener" href="' + esc(p.oa_url) + '">Open-access copy</a>');
        if (p.arxiv_id) links.push('<a target="_blank" rel="noopener" href="https://arxiv.org/abs/' + esc(p.arxiv_id) + '">arXiv</a>');
        if (p.doi) links.push('<a target="_blank" rel="noopener" href="https://doi.org/' + esc(p.doi) + '">Publisher (DOI)</a>');
        links.push('<a target="_blank" rel="noopener" href="https://scholar.google.com/scholar?q=' +
          encodeURIComponent(p.title || p.doi || '') + '">Google Scholar</a>');
        var why = state.fetchReasons[p.id];
        return '<div class="row"><div><div class="title">' + (p.mark === 'relevant' ? '✓ ' : '') + esc(p.title || '(no title)') +
          '</div><div class="meta">' + meta + '</div>' + (why ? '<div class="why">Not downloaded: ' + esc(why.reason) + '</div>' : '') +
          '<div class="links">' + links.join('') + '</div></div></div>';
      }).join('');
    }).catch(function (e) { el.innerHTML = ''; toast(e.message); });
  }

  function deleteProject(p) {
    var typed = window.prompt('Delete project "' + p.name + '" with its ' + p.papers + ' papers list, runs and marks?\n' +
      'Papers and PDFs stay in the library.\nType the project name to confirm:');
    if (typed === null) return;
    if (typed.trim() !== p.name) { toast('Name did not match; nothing deleted'); return; }
    var dropExports = window.confirm('Also delete its exports (the markdown and pdfs folders in ' + p.export_dir + ')?\n\n' +
      'OK = delete them too · Cancel = keep them');
    post('/api/delete-project', { project: p.name, delete_exports: dropExports }).then(function (r) {
      toast('Deleted ' + p.name + (r.kept_exports ? '; exports kept in ' + r.kept_exports : ''));
      state.project = null;
      store.set('project', null);
      state.runId = null;
      state.data = null;
      state.dirEdited = false;
      render();
      return loadProjects();
    }).catch(function (err) { toast(err.message); });
  }

  // Sidebar: the latest searches of all projects, like a chat app's history.
  function loadRuns() {
    return api('/api/runs?limit=15').then(function (runs) {
      var el = $('side-recent');
      el.innerHTML = runs.length ? '' : '<div class="none">No searches yet</div>';
      runs.forEach(function (r) { el.appendChild(runLink(r)); });
      if (state.page === 'recents') loadRecents();
    });
  }

  function runLink(r) {
    var a = document.createElement('a');
    a.href = '#';
    a.className = r.id === state.runId ? 'on' : '';
    a.title = r.label;
    a.innerHTML = esc(r.label) + '<small>' + esc(r.project) + ' · ' + r.papers + ' papers · ' +
      esc((r.started_at || '').slice(5, 16).replace('T', ' ')) + (r.status !== 'done' ? ' · ' + esc(r.status) : '') + '</small>';
    a.onclick = function (e) { e.preventDefault(); loadRun(r.id); };
    return a;
  }

  function loadRecents() {
    var q = state.recentsProject ? '&project=' + encodeURIComponent(state.recentsProject) : '';
    return api('/api/runs?limit=500' + q).then(function (runs) {
      var el = $('recents-list');
      el.innerHTML = runs.length ? '' : '<div class="empty">No searches yet. Start one with <b>+ New</b>.</div>';
      runs.forEach(function (r) {
        var row = document.createElement('div');
        row.className = 'row';
        row.innerHTML = '<div><div class="title">' + esc(r.label) + '</div><div class="meta">' +
          [r.project, r.kind, r.papers + ' papers', (r.started_at || '').slice(0, 16).replace('T', ' '),
           r.status !== 'done' ? r.status : null].filter(Boolean).map(esc).join(' · ') + '</div></div>';
        row.onclick = function () { loadRun(r.id); };
        el.appendChild(row);
      });
    }).catch(function (e) { toast(e.message); });
  }

  function loadLibrary() {
    return api('/api/library' + (state.libraryAllVersions ? '?all_versions=1' : ''))
      .then(function (papers) { state.library = papers; renderLibrary(); })
      .catch(function (e) { toast(e.message); });
  }

  function renderLibrary() {
    var f = $('library-filter').value.trim().toLowerCase(), pdfOnly = state.libraryPdf;
    var list = (state.library || []).filter(function (p) {
      if (pdfOnly && !p.has_pdf) return false;
      if (state.libraryUnused && !p.unused) return false;
      if (!f) return true;
      return [p.title, p.venue, p.doi, p.authors.join(' '), p.projects.join(' ')].join(' ').toLowerCase().indexOf(f) >= 0;
    });
    var all = state.library || [];
    $('library-count').textContent = all.length + ' papers · ' + all.filter(function (p) { return p.has_pdf; }).length + ' with PDF';
    var el = $('library-list');
    if (!list.length) { el.innerHTML = '<div class="empty">' + (all.length ? 'No papers match.' : 'The library is empty.') + '</div>'; return; }
    el.innerHTML = list.slice(0, 1000).map(function (p, i) {
      var meta = [p.year, p.venue, p.authors.join(', ')].filter(Boolean).map(esc).join(' · ');
      var version = p.version ? '<span class="badge version">' + esc(p.version) + '</span>' : '';
      if (p.pdf_warning) version += '<span class="badge warn" title="' + esc(p.pdf_warning) + '">PDF is another version</span>';
      var chips = p.projects.map(function (n) { return '<span class="chip">' + esc(n) + '</span>'; }).join('');
      if (p.unused) chips += '<span class="chip">Not in a project</span>';
      return '<div class="row" data-i="' + i + '"><div><div class="title">' + esc(p.title || '(no title)') + '</div>' +
        '<div class="meta">' + meta + '</div>' + version + (chips ? '<div class="chips">' + chips + '</div>' : '') + '</div>' +
        '<div class="badges">' + (p.has_pdf ? '<a class="btn small" target="_blank" rel="noopener" href="/api/papers/' +
          esc(p.id) + '/pdf">PDF</a>' : (p.oa_url ? '<span class="badge oa">open</span>' : '')) + '</div></div>';
    }).join('');
    el.querySelectorAll('.row').forEach(function (row) {
      row.onclick = function (e) { if (!e.target.closest('a')) openDrawer(list[+row.dataset.i]); };
    });
  }

  function removeLibraryPapers(ids) {
    var query = ids ? '?paper_ids=' + encodeURIComponent(ids.join(',')) : '';
    return api('/api/library/removal-preview' + query).then(function (preview) {
      var refusals = preview.refused.map(function (r) { return r.id + ': ' + r.reason; }).join('; ');
      if (!preview.paper_count) { toast('No papers can be removed.' + (refusals ? ' ' + refusals : '')); return; }
      var message = 'Remove ' + preview.paper_count + ' papers, ' + preview.pdf_count + ' PDFs and ' +
        preview.parsed_folder_count + ' parsed folders (' + (preview.bytes_freed / 1000000).toFixed(2) + ' MB)?' +
        '\nThis deletion is irreversible. PDFs and extracted text will be permanently removed.' +
        (refusals ? '\nKept: ' + refusals : '') +
        (preview.file_errors.length ? '\nSome files cannot be removed and will be reported.' : '');
      if (!window.confirm(message)) return;
      return post('/api/library/remove', { paper_ids: preview.papers.map(function (p) { return p.id; }) }).then(function (report) {
        closeDrawer();
        return loadLibrary().then(function () {
          var issues = report.refused.map(function (r) { return r.id + ': ' + r.reason; })
            .concat(report.file_errors.map(function (r) { return r.path + ': ' + r.reason; }));
          toast('Removed ' + report.removed.length + ' papers; freed ' + (report.bytes_freed / 1000000).toFixed(2) +
            ' MB.' + (issues.length ? ' Kept: ' + issues.join('; ') : '') + (refusals ? ' Kept: ' + refusals : ''));
        });
      });
    }).catch(function (e) { toast(e.message); });
  }

  // "+ New": an empty search form for the selected project; the open run stays in Recents.
  function newSearch() {
    state.runId = null;
    state.data = null;
    showView('new');
    $('seed').focus();
  }

  function showView(view) {
    if (view === 'models') view = 'settings';
    state.page = view;
    if (history.replaceState) history.replaceState(null, '', /^(recents|library|ask|matrix|claims|gaps|console|write|settings)$/.test(view) ? '#' + view : location.pathname);
    ['new', 'recents', 'library', 'ask', 'matrix', 'claims', 'gaps', 'console', 'write', 'settings'].forEach(function (v) { $(('view-' + v)).hidden = v !== view; });
    $('results').hidden = view !== 'results';
    document.querySelectorAll('.sidebar [data-view]').forEach(function (b) { b.classList.toggle('on', b.dataset.view === view); });
    document.querySelectorAll('#side-recent a').forEach(function (a) { a.classList.remove('on'); });
    if (view !== 'results') closeDrawer();
    if (view === 'recents') { $('recents-project').value = state.recentsProject || ''; loadRecents(); }
    if (view === 'library') loadLibrary();
    if (view === 'ask' || view === 'matrix' || view === 'claims') enterLens(view);
    if (view === 'gaps') loadGaps();
    if (view === 'console') loadConsole();
    if (view === 'write') loadWrite(); else leaveWrite();
    if (view === 'settings') { loadSettings(); loadUpdates(); }
  }

  function deleteRun() {
    if (!window.confirm('Delete this run? It leaves History, and its ' + state.data.papers.length + ' papers leave ' +
        'the project, except those you marked, have a PDF for, or that another run also found.')) return;
    post('/api/delete-run', { run_id: state.runId }).then(function (r) {
      toast('Run deleted: ' + r.removed + ' papers removed from the project' + (r.kept ? ', ' + r.kept + ' kept' : ''));
      state.runId = null;
      state.data = null;
      render();
      return loadProjects();
    }).catch(function (e) { toast(e.message); });
  }

  function loadRun(id) {
    state.runId = id;
    return api('/api/runs/' + id + (state.libraryAllVersions ? '?all_versions=1' : '')).then(function (data) {
      state.data = data;
      state.channel = null;
      state.anchor = -1;
      state.sel = -1;
      selectProject(data.run.project);  // marks and exports belong to the run's project
      render();
      loadRuns();
    }).catch(function (e) { toast(e.message); });
  }

  /* ---------- results ---------- */

  function render() {
    var d = state.data;
    if (!d) { if (state.page === 'results') showView('new'); return; }
    if (state.page !== 'results') showView('results');
    var run = d.run, s = d.stats, params = run.params || {};
    $('run-title').textContent = String(run.label || run.kind).slice(0, 120);
    var cost = run.stats && run.stats.cost_usd ? ' · API cost $' + run.stats.cost_usd.toFixed(4) : '';
    $('run-meta').innerHTML = esc(run.kind) + ' · ' + esc((run.started_at || '').replace('T', ' ').slice(0, 16)) +
      ' · ' + esc(run.status) + cost + (s.retracted ? ' · <b style="color:var(--bad)">' + s.retracted + ' retracted</b>' : '') +
      ' <button class="btn small" id="btn-expand" title="Run a similar-paper search seeded with the top papers of this list">Find similar to these</button>' +
      ' <button class="btn small ghost danger" id="btn-delete-run" title="Undo this run: remove it from History and take its new papers out of the project">Delete run</button>';
    $('btn-expand').onclick = function () { startSimilar({ seed_run: state.runId }); };
    $('btn-delete-run').onclick = deleteRun;

    var years = s.year_min ? (s.year_min === s.year_max ? s.year_min : s.year_min + '–' + s.year_max) : '–';
    var cards = [
      [s.papers, 'papers'], [s.new, 'new to project'], [s.high_impact, 'top-10% cited in field'],
      [s.open_access, 'open access'], [years, 'years'], [s.relevant + ' / ' + s.irrelevant, 'marked ✓ / ✗']
    ];
    if (s.screened) cards.push([s.screen_include + ' / ' + s.screen_uncertain + ' / ' + s.screen_exclude, 'screened in / ? / out']);
    var STAT_TIP = {
      'new to project': 'Papers this project did not have before this search',
      'top-10% cited in field': 'Among the 10% most cited papers of their field and year (OpenAlex)',
      'open access': 'Free copy online or PDF already in the library',
      'marked ✓ / ✗': 'Papers you marked relevant / not relevant in this list',
      'screened in / ? / out': 'AI screening suggestions: include / uncertain / exclude'
    };
    $('stats').innerHTML = cards.map(function (c) {
      return '<div class="card stat"' + (STAT_TIP[c[1]] ? ' title="' + esc(STAT_TIP[c[1]]) + '"' : '') + '><b>' + esc(c[0]) +
        '</b><span>' + esc(c[1]) + '</span></div>';
    }).join('');
    showScreeningControls(!!s.screened);
    renderYears(s.years);
    renderBars($('chart-channels'), s.channels.map(function (c) { return [c[0], CHANNEL_LABEL[c[0]] || c[0], c[1]]; }), true);
    renderBars($('chart-venues'), s.venues.map(function (v) { return [v[0], v[0], v[1]]; }), false);
    if (state.criteria && state.criteriaFor === state.project) showCriteria(state.criteria); else loadCriteria();
    renderList();
  }

  /* ---------- screening ---------- */

  // The form belongs to state.criteriaFor; until the current project's criteria arrive, Save and Screen are
  // disabled so the previous project's text is never saved under the new project.
  function criteriaReady(ready) {
    $('btn-crit-save').disabled = !ready;
    $('btn-screen').disabled = !ready;
  }

  function loadCriteria() {
    if (!state.project || state.criteriaLoading === state.project) return;
    var project = state.project;
    state.criteriaLoading = project;
    criteriaReady(false);
    api('/api/criteria?project=' + encodeURIComponent(project)).then(function (c) {
      if (state.project !== project) return;
      state.criteriaFor = project;
      state.criteria = null;  // a different project: replace the form's text
      showCriteria(c);
      criteriaReady(true);
    }).catch(function (e) { toast(e.message); }).then(function () {
      if (state.criteriaLoading === project) state.criteriaLoading = null;
    });
  }

  function showCriteria(c) {
    var fresh = state.criteria !== c;
    state.criteria = c;
    if (fresh) {  // keep what the user is typing when only the summary needs refreshing
      $('crit-question').value = c.question || '';
      $('crit-include').value = (c.include || []).join('\n');
      $('crit-exclude').value = (c.exclude || []).join('\n');
    }
    if (!$('screen-n').dataset.touched) $('screen-n').value = c.default_top;
    $('screen-model').textContent = 'model: ' + c.model;
    var s = state.data && state.data.stats;
    $('screen-summary').textContent = !c.question ? 'pre-sort papers with a local model · not set up' :
      (s && s.screened ? s.screened + ' screened: ' + s.screen_include + ' include, ' + s.screen_uncertain +
        ' uncertain, ' + s.screen_exclude + ' exclude' : 'criteria set · not screened yet');
  }

  // Screening sorts and filters only mean something once a paper of this list was screened.
  function showScreeningControls(on) {
    $('screen-filter').hidden = !on;
    $('sort').querySelector('[data-screen]').hidden = !on;
    if (!on && state.screenFilter) { state.screenFilter = ''; $('screen-filter').value = ''; }
    if (!on && state.sort === 'screening') { state.sort = 'score'; $('sort').value = 'score'; }
  }

  function saveCriteria() {
    var project = state.project;
    return post('/api/criteria', { project: project, question: $('crit-question').value,
      include: $('crit-include').value, exclude: $('crit-exclude').value }).then(function (c) {
      state.criteriaFor = project;
      showCriteria(c);
      return c;
    });
  }

  function renderYears(pairs) {
    var el = $('chart-years');
    if (!pairs.length) { el.innerHTML = '<small class="hint">No years.</small>'; return; }
    var counts = {}, lo = pairs[0][0], hi = pairs[pairs.length - 1][0];
    pairs.forEach(function (p) { counts[p[0]] = p[1]; });
    var step = hi - lo > 40 ? 5 : 1, bins = [];
    for (var y = lo - (lo % step); y <= hi; y += step) {
      var n = 0;
      for (var k = y; k < y + step; k++) n += counts[k] || 0;
      bins.push([y, n]);
    }
    var max = Math.max.apply(null, bins.map(function (b) { return b[1]; })), w = 100 / bins.length;
    var svg = '<svg viewBox="0 0 100 60" preserveAspectRatio="none">';
    bins.forEach(function (b, i) {
      var h = max ? (b[1] / max) * 48 : 0;
      svg += '<rect x="' + (i * w + w * 0.1) + '" y="' + (50 - h) + '" width="' + (w * 0.8) + '" height="' + h + '">' +
        '<title>' + b[0] + (step > 1 ? '–' + (b[0] + step - 1) : '') + ': ' + b[1] + '</title></rect>';
    });
    // Year labels sit outside the stretched SVG so they keep their shape.
    el.innerHTML = svg + '</svg><div class="axis"><span>' + bins[0][0] + '</span><span>' + bins[bins.length - 1][0] + '</span></div>';
  }

  function renderBars(el, rows, filterable) {
    if (!rows.length) { el.innerHTML = '<small class="hint">–</small>'; return; }
    var max = Math.max.apply(null, rows.map(function (r) { return r[2]; }));
    el.innerHTML = rows.map(function (r) {
      return '<div class="hbar' + (filterable && state.channel === r[0] ? ' on' : '') + '" data-key="' + esc(r[0]) + '">' +
        '<span class="label" title="' + esc(r[1]) + '">' + esc(r[1]) + '</span>' +
        '<span class="track"><i style="width:' + (100 * r[2] / max) + '%"></i></span><span class="n">' + r[2] + '</span></div>';
    }).join('');
    if (filterable) {
      el.querySelectorAll('.hbar').forEach(function (b) {
        b.onclick = function () {
          state.channel = state.channel === b.dataset.key ? null : b.dataset.key;
          state.anchor = -1;
          render();
        };
      });
    } else {
      el.querySelectorAll('.hbar').forEach(function (b) {
        b.onclick = function () { $('filter').value = b.dataset.key; state.filter = b.dataset.key.toLowerCase(); renderList(); };
      });
    }
  }

  function visiblePapers() {
    var f = state.filter;
    var list = state.data.papers.filter(function (p) {
      if (state.oa && !p.oa_url && !p.has_pdf) return false;
      if (state.onlyNew && !p.new_to_project) return false;
      if (state.hide && p.mark === 'irrelevant') return false;
      if (state.channel && p.channels.indexOf(state.channel) < 0) return false;
      if (state.screenFilter === 'none' && p.screening) return false;
      if (state.screenFilter && state.screenFilter !== 'none' &&
          (!p.screening || p.screening.decision !== state.screenFilter)) return false;
      if (f) {
        var hay = [p.title, p.venue, (p.authors || []).join(' '), p.doi, (p.reasons || []).join(' ')].join(' ').toLowerCase();
        if (hay.indexOf(f) < 0) return false;
      }
      return true;
    });
    var key = state.sort;
    list.sort(function (a, b) {
      if (key === 'queue') return ((b.review_queue || {}).info == null ? -1 : b.review_queue.info) -
        ((a.review_queue || {}).info == null ? -1 : a.review_queue.info) || a.id.localeCompare(b.id);
      if (key === 'screening') {
        return screenRank(a) - screenRank(b) || screenScore(b) - screenScore(a) || b.priority - a.priority;
      }
      if (key === 'year') return (b.year || 0) - (a.year || 0) || b.priority - a.priority;
      if (key === 'cited') return (b.cited_by_count || 0) - (a.cited_by_count || 0) || b.priority - a.priority;
      if (key === 'relevance') return b.score - a.score;
      if (key === 'importance') return b.importance - a.importance || b.score - a.score;
      return b.priority - a.priority || b.score - a.score;
    });
    return list;
  }

  function renderList() {
    var view = visiblePapers();
    state.view = view;
    $('count').textContent = view.length + ' of ' + state.data.papers.length;
    var el = $('list');
    if (!view.length) {
      el.innerHTML = '<div class="empty">No papers match the filters. <button class="btn small" id="clear-filters">Clear filters</button></div>';
      $('clear-filters').onclick = clearFilters;
      return;
    }
    el.innerHTML = view.map(function (p, i) {
      var isSeed = (p.reasons || [])[0] === 'seed paper';
      var meta = [p.year, p.venue, p.cited_by_count != null ? p.cited_by_count + ' citations' : null,
        (p.authors || []).join(', ')].filter(Boolean).map(esc).join(' · ');
      var chips = (p.reasons || []).filter(function (r) { return r !== 'seed paper'; })
        .map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('') +
        (p.impact_notes || []).filter(function (n) { return /^top|in this set|RETRACTED/.test(n); })
          .map(function (n) { return '<span class="chip impact' + (n === 'RETRACTED' ? ' bad' : '') + '">' + esc(n) + '</span>'; }).join('');
      var badges = screenBadge(p) + (isSeed ? '<span class="badge seed" title="The paper this search started from">seed</span>' : '') +
        (p.new_to_project && !isSeed ? '<span class="badge new" title="Not in this project before this search">new</span>' : '') +
        (p.has_pdf ? '<span class="badge pdf" title="Its PDF is in the library; notes get the full text">pdf</span>' : '') +
        (p.oa_url && !p.has_pdf ? '<span class="badge oa" title="A free copy exists online; Export with PDFs downloads it">open</span>' : '');
      return '<div class="item' + (i === state.sel ? ' sel' : '') + (p.mark === 'irrelevant' ? ' irrelevant' : '') + '" data-i="' + i + '">' +
        '<div class="rank">' + (i + 1) + '</div>' +
        '<div class="score" title="Priority ' + p.priority.toFixed(2) + ' = relevance ' + p.score.toFixed(2) +
          ' (blue) and importance ' + p.importance.toFixed(2) + ' (amber)">' + p.priority.toFixed(2) +
          '<div class="track"><i style="width:' + Math.round(100 * p.score) + '%"></i></div>' +
          '<div class="track imp"><i style="width:' + Math.round(100 * p.importance) + '%"></i></div></div>' +
        '<div><div class="title">' + esc(p.title || '(no title)') + '</div><div class="meta">' + meta + '</div>' +
        (chips ? '<div class="chips">' + chips + '</div>' : '') + '</div>' +
        '<div class="side"><div class="badges">' + badges + '</div>' +
        '<div class="marks"><button class="rel' + (p.mark === 'relevant' ? ' on' : '') + '" title="Relevant (r)" data-mark="relevant">✓</button>' +
        '<button class="irr' + (p.mark === 'irrelevant' ? ' on' : '') + '" title="Not relevant (x)" data-mark="irrelevant">✗</button></div></div></div>';
    }).join('');
    el.querySelectorAll('.item').forEach(function (row) {
      var paper = view[+row.dataset.i];
      lensResultBadges(row.querySelector('.badges'), paper);
      if (state.sort === 'queue' && paper.review_queue) {
        var score = row.querySelector('.score');
        score.replaceChildren(lensNode('span', paper.review_queue.info.toFixed(3)));
        score.title = paper.review_queue.reason;
      }
      row.onclick = function (e) {
        var i = +row.dataset.i, btn = e.target.closest('button[data-mark]');
        if (btn) {
          e.stopPropagation();
          if (e.shiftKey && state.anchor >= 0 && state.anchor !== i) markRange(state.anchor, i, btn.dataset.mark);
          else toggleMark(view[i], btn.dataset.mark);
          state.anchor = i;
          return;
        }
        select(i, true);
      };
    });
  }

  function clearFilters() {
    state.filter = ''; state.oa = state.onlyNew = state.hide = false; state.channel = null; state.screenFilter = '';
    state.anchor = -1;
    $('filter').value = '';
    $('screen-filter').value = '';
    ['t-oa', 't-new', 't-hide'].forEach(function (id) { $(id).classList.remove('on'); });
    render();
  }

  function select(i, open) {
    if (!state.view.length) return;
    state.sel = Math.max(0, Math.min(i, state.view.length - 1));
    document.querySelectorAll('.item').forEach(function (r) { r.classList.toggle('sel', +r.dataset.i === state.sel); });
    var row = document.querySelector('.item[data-i="' + state.sel + '"]');
    if (row) row.scrollIntoView({ block: 'nearest' });
    if (open) openDrawer(state.view[state.sel]);
  }

  function setMarks(papers, mark) {
    if (!papers.length) { toast('No papers to mark'); return Promise.resolve(); }
    var ids = papers.map(function (p) { return p.id; }), project = state.project, data = state.data;
    return post('/api/marks', { project: state.project, paper_ids: ids, mark: mark }).then(function () {
      papers.forEach(function (p) { p.mark = mark; });
      if (state.hide && mark === 'irrelevant') state.anchor = -1;  // those rows just left the list
      state.data.stats.relevant = state.data.papers.filter(function (x) { return x.mark === 'relevant'; }).length;
      state.data.stats.irrelevant = state.data.papers.filter(function (x) { return x.mark === 'irrelevant'; }).length;
      render();
      var open = state.view[state.sel];
      if ($('drawer').classList.contains('open') && open && papers.indexOf(open) >= 0) openDrawer(open);
      return api('/api/queue?project=' + encodeURIComponent(project) + '&limit=2147483647').then(function (queue) {
        if (project !== state.project || data !== state.data) return;
        var byId = {}; queue.forEach(function (q) { byId[q.paper_id] = q; });
        data.papers.forEach(function (p) { p.review_queue = byId[p.id] || null; });
        renderList();
      });
    }).catch(function (e) { toast(e.message); });
  }

  function toggleMark(p, mark) {
    return setMarks([p], p.mark === mark ? null : mark);
  }

  // Shift-click: give every paper between the last clicked row and this one the clicked mark.
  function markRange(from, to, mark) {
    var lo = Math.min(from, to), hi = Math.max(from, to);
    return setMarks(state.view.slice(lo, hi + 1), mark).then(function () {
      toast((hi - lo + 1) + ' papers marked ' + (mark === 'relevant' ? '✓' : '✗'));
    });
  }

  function bulkMark(papers, mark, what) {
    var verb = mark === 'relevant' ? 'Mark ✓' : mark === 'irrelevant' ? 'Mark ✗' : 'Clear marks of';
    if (papers.length > 25 || !mark) {
      if (!window.confirm(verb + ' ' + papers.length + ' ' + what + '?')) return;
    }
    setMarks(papers, mark);
  }

  /* ---------- quick view ---------- */

  var drawerRequest = 0;

  function openDrawer(p) {
    var request = ++drawerRequest;
    var drawer = $('drawer');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    $('dhead').innerHTML = '<h2>' + esc(p.title || '(no title)') + '</h2><div class="meta">loading…</div>';
    $('dbody').innerHTML = '';
    api('/api/papers/' + p.id + '?project=' + encodeURIComponent(state.project || '')).then(function (d) {
      if (request !== drawerRequest) return;
      var meta = [d.year, d.venue, d.cited_by_count != null ? d.cited_by_count + ' citations' : null]
        .filter(Boolean).map(esc).join(' · ');
      var authors = d.authors.length ? esc(d.authors.slice(0, 12).join(', ') + (d.authors.length > 12 ? ', et al.' : '')) : '';
      $('dhead').innerHTML = '<h2>' + esc(d.title || '(no title)') + '</h2>' +
        (d.version ? '<div class="badge version">' + esc(d.version) + '</div>' : '') +
        (d.pdf_warning ? '<div class="badge warn">' + esc(d.pdf_warning) + '</div>' : '') +
        '<div class="meta">' + authors + '</div><div class="meta">' + meta + '</div>' +
        '<div class="links">' + paperLinks(d) + '</div>' +
        '<div class="dactions">' + (state.page === 'results' ? '<span class="marks">' +
        '<button class="rel' + (p.mark === 'relevant' ? ' on' : '') + '" data-mark="relevant" title="Relevant (r)">✓</button>' +
        '<button class="irr' + (p.mark === 'irrelevant' ? ' on' : '') + '" data-mark="irrelevant" title="Not relevant (x)">✗</button></span>' : '') +
        '<button class="btn small" id="btn-like" title="Start a new similar-paper search from this paper">Find similar to this</button>' +
        (d.unused ? '<button class="btn small ghost danger" id="btn-remove-paper">Remove from library</button>' : '') +
        (d.markdown ? '<button class="btn small" id="btn-full" title="Show or hide the text extracted from its PDF">Full text</button>' : '') +
        (d.pdf_url ? '<a class="btn small" target="_blank" rel="noopener" href="' + esc(d.pdf_url) + '" title="Open the PDF in a new tab">Open PDF</a>' : '') + '</div>';
      $('dhead').querySelectorAll('button[data-mark]').forEach(function (b) {
        b.onclick = function () { toggleMark(p, b.dataset.mark); };
      });
      $('btn-like').onclick = function () { startSimilar({ seeds: [d.id] }); };
      if (d.unused) $('btn-remove-paper').onclick = function () { removeLibraryPapers([d.id]); };

      var comp = p.components || {}, html = '';
      if (comp.text !== undefined) {
        var parts = [['Text similarity', comp.text], ['Shared references', comp.coupling],
          ['Direct citation', comp.direct], ['Found by channels', comp.channels]];
        html += '<h3>Relevance (' + p.score.toFixed(2) + ')</h3><div class="breakdown">' + parts.map(function (x) {
          return '<span>' + x[0] + '</span><span class="track"><i style="width:' + Math.round(100 * (x[1] || 0)) +
            '%"></i></span><span>' + (x[1] || 0).toFixed(2) + '</span>';
        }).join('') + '</div>';
      }
      var imp = p.impact || {};
      if (Object.keys(imp).length) {
        var iparts = [['Citations (field-normalized)', imp.citations], ['Centrality in this set', imp.centrality],
          ['Recent attention', imp.momentum], ['Venue impact', imp.venue]];
        html += '<h3>Importance (' + p.importance.toFixed(2) + ') · priority ' + p.priority.toFixed(2) + '</h3><div class="breakdown imp">' +
          iparts.map(function (x) {
            var v = x[1];
            return '<span>' + x[0] + '</span><span class="track"><i style="width:' + Math.round(100 * (v || 0)) +
              '%"></i></span><span>' + (v == null ? '–' : v.toFixed(2)) + '</span>';
          }).join('') + '</div>';
        if ((p.impact_notes || []).length) {
          html += '<ul class="reasons">' + p.impact_notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
        }
      }
      var sc = d.screening;
      if (sc) {
        var crit = function (r) {
          return '<li><b class="scr-' + esc(r.status) + '">' + esc(r.status.replace('_', ' ')) + '</b> ' + esc(r.criterion) +
            (r.evidence ? '<br><q>' + esc(r.evidence) + '</q>' +
              (r.verified === false ? ' <small class="bad">not found in the abstract</small>' : '') : '') + '</li>';
        };
        html += '<h3>Screening: ' + esc(sc.decision) + ' (' + sc.score.toFixed(2) + ')' +
          (sc.stale ? ' <small class="bad">criteria changed since</small>' : '') + '</h3>' +
          (sc.error ? '<p class="bad">Model error: ' + esc(sc.error) + '</p>' : '<p>' + esc(sc.reason) + '</p>') +
          '<ul class="reasons">' + sc.inclusion.map(crit).join('') + sc.exclusion.map(crit).join('') + '</ul>' +
          '<small class="hint">relevance ' + esc(sc.relevance) + '/5 · ' + esc(sc.model) + ' · ' +
          esc((sc.screened_at || '').replace('T', ' ').slice(0, 16)) + '</small>';
      }
      if ((p.reasons || []).length) {
        html += '<h3>Found because</h3><ul class="reasons">' + p.reasons.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>';
      }
      html += '<h3>Versions</h3><div id="paper-versions"></div>';
      html += '<h3>Abstract</h3><div class="md" id="abstract-md"></div>';
      if (d.markdown) html += '<div id="full" hidden><h3>Full text</h3><div class="md" id="full-md"></div></div>';
      $('dbody').innerHTML = html;
      renderPaperVersions($('paper-versions'), d, p, request);
      renderPaperAssets($('dbody'), d.assets || [], request);
      lensScreeningDetail($('dbody'), d);
      window.renderMarkdown($('abstract-md'), d.abstract || '_No abstract available._');
      if (d.markdown) {
        $('btn-full').onclick = function () {
          var full = $('full');
          if (full.hidden && !$('full-md').innerHTML) window.renderMarkdown($('full-md'), d.markdown);
          full.hidden = !full.hidden;
          if (!full.hidden) full.scrollIntoView({ behavior: 'smooth' });
        };
      }
      if (d.pdf_url) {
        var reread = document.createElement('button');
        reread.className = 'btn small'; reread.textContent = 'Re-read accurately (MinerU)';
        reread.onclick = function () { rereadMineru(d); };
        var parseStatus = document.createElement('p'); parseStatus.id = 'mineru-paper-status';
        parseStatus.setAttribute('role', 'status');
        parseStatus.textContent = [d.parse_status_label, d.parse_status_hint, mineruEstimate(d)].filter(Boolean).join(' · ');
        $('dhead').appendChild(reread); $('dhead').appendChild(parseStatus);
      }
      attachReproTabs(d, request);
    }).catch(function (e) { if (request === drawerRequest) $('dbody').textContent = e.message; });
  }

  function renderPaperVersions(parent, d, paper, request) {
    parent.replaceChildren();
    var peers = d.versions || [];
    if (!peers.length) parent.appendChild(lensNode('p', d.version || 'No linked versions.', 'hint'));
    var linked = peers.some(function (v) { return v.confirmed && v.paper_id !== d.id; });
    if (linked && !d.primary) {
      parent.appendChild(lensButton('Set this version as primary', function () {
        post('/api/versions/set-primary', { paper_id: d.id }).then(function () {
          return loadLibrary().then(function () { openDrawer(paper); });
        }).catch(function (e) { toast(e.message); });
      }, 'btn small'));
    }
    if (linked) {
      parent.appendChild(lensButton('Unlink: a different work', function () {
        post('/api/versions/unlink', { paper_id: d.id }).then(function () {
          return loadLibrary().then(function () { openDrawer(paper); });
        }).catch(function (e) { toast(e.message); });
      }, 'btn small'));
    }
    peers.forEach(function (v) {
      if (v.paper_id === d.id) return;
      var row = lensNode('div', null, 'version-row');
      row.appendChild(lensNode('span', (v.role || 'unknown') + ' · ' + (v.title || v.paper_id) +
        (v.version ? ' · PDF: ' + v.version : ' · no PDF'), 'meta'));
      if (v.confirmed && !v.is_primary) row.appendChild(lensButton('Set primary', function () {
        post('/api/versions/set-primary', { paper_id: v.paper_id }).then(function () {
          return loadLibrary().then(function () { openDrawer(paper); });
        }).catch(function (e) { toast(e.message); });
      }, 'btn small'));
      if (!v.confirmed) {
        row.appendChild(lensButton('Confirm link', function () {
          post('/api/versions/confirm', { a: d.id, b: v.paper_id }).then(function () {
            return loadLibrary().then(function () { openDrawer(paper); });
          }).catch(function (e) { toast(e.message); });
        }, 'btn small'));
        row.appendChild(lensButton('Reject', function () {
          post('/api/versions/reject', { a: d.id, b: v.paper_id }).then(function () {
            return loadLibrary().then(function () { openDrawer(paper); });
          }).catch(function (e) { toast(e.message); });
        }, 'btn small'));
      }
      parent.appendChild(row);
    });
    var discover = lensButton('Find version links', function () {
      discover.disabled = true;
      // A background job: registry lookups can take a while.
      post('/api/versions/link', { project: state.project || '' }).then(function (job) {
        return new Promise(function (resolve, reject) {
          (function poll() {
            api('/api/jobs/' + encodeURIComponent(job.id)).then(function (j) {
              if (j.status === 'running') { setTimeout(poll, 700); return; }
              if (j.status === 'failed') { reject(new Error(j.error || 'Version linking failed')); return; }
              resolve(j);
            }).catch(reject);
          })();
        });
      }).then(function () {
        return api('/api/papers/' + encodeURIComponent(d.id) + '?project=' +
          encodeURIComponent(state.project || '')).then(function (fresh) {
          if (request === drawerRequest) renderPaperVersions(parent, fresh, paper, request);
        });
      }).catch(function (e) { toast(e.message); }).finally(function () { discover.disabled = false; });
    }, 'btn small');
    parent.appendChild(discover);
  }

  function closeDrawer() {
    drawerRequest++;
    $('drawer').classList.remove('open');
    $('drawer').setAttribute('aria-hidden', 'true');
  }

  function renderPaperAssets(parent, assets, request) {
    if (!assets.length) return;
    var section = lensNode('section', null, 'paper-assets');
    section.id = 'paper-assets';
    section.appendChild(lensNode('h3', 'Figure, table and equation analysis'));
    assets.forEach(function (asset) {
      var row = lensNode('article', null, 'asset-analysis-row');
      var heading = asset.label || (asset.kind + ' · p.' + asset.page);
      row.appendChild(lensNode('h4', heading));
      if (asset.caption) row.appendChild(lensNode('p', asset.caption, 'meta'));
      var layout = lensNode('div', null, 'asset-analysis-layout');
      if (asset.crop_url) {
        var cropWrap = lensNode('div', null, 'asset-crop-wrap');
        var crop = lensNode('img', null, 'asset-crop');
        crop.src = asset.crop_url; crop.alt = (asset.kind + ' crop, page ' + asset.page);
        crop.onerror = function () {
          crop.hidden = true;
          cropWrap.appendChild(lensNode('p', 'Crop image could not be loaded.', 'hint'));
          layout.classList.add('asset-no-crop');
        };
        cropWrap.appendChild(crop);
        layout.appendChild(cropWrap);
      } else {
        layout.classList.add('asset-no-crop');
      }
      var analyses = lensNode('div', null, 'asset-analysis-results');
      if (!asset.crop_url) analyses.appendChild(lensNode('p', 'Asset crop is unavailable.', 'hint'));
      var results = lensNode('div', null, 'asset-analysis-result-list');
      var mode = asset.kind === 'figure' ? 'chart' : asset.kind;
      var button = lensButton('Analyze crop', function () {
        button.disabled = true;
        results.replaceChildren(lensNode('p', 'Analyzing the selected crop…', 'hint'));
        function start(approved) {
          return post('/api/assets/' + encodeURIComponent(asset.id) + '/analyze',
            { mode: mode, approved: approved }).catch(function (error) {
              if (error.status !== 403) throw error;
              var plan = error.preview || {};
              var cost = plan.max_cost_usd == null ? 'unknown cost' : '$' + Number(plan.max_cost_usd).toFixed(4);
              var message = 'Send this asset crop to ' + (plan.provider || 'the configured provider') + ' (' +
                (plan.model || 'model') + ')? Estimated maximum cost: ' + cost + '.';
              if (!window.confirm(message)) throw new Error('Cloud vision analysis was not approved.');
              return post('/api/assets/' + encodeURIComponent(asset.id) + '/analyze',
                { mode: mode, approved: true });
            });
        }
        start(false).then(function (job) {
          (function poll() {
            api('/api/jobs/' + encodeURIComponent(job.id)).then(function (state) {
              if (request !== drawerRequest) return;
              if (state.status === 'running') {
                if (state.messages && state.messages.length) results.replaceChildren(lensNode('p',
                  state.messages[state.messages.length - 1], 'hint'));
                setTimeout(poll, 500); return;
              }
              button.disabled = false;
              results.replaceChildren();
              if (state.status !== 'done') {
                results.appendChild(lensNode('p', state.error || 'Analysis failed.', 'hint'));
                return;
              }
              var refreshed = assets.find(function (candidate) { return candidate.id === asset.id; });
              if (refreshed) refreshed.analyses.push(state.report);
              renderAssetAnalyses(results, refreshed ? refreshed.analyses : [state.report]);
            }).catch(function (error) { button.disabled = false; analyses.textContent = error.message; });
          })();
        }).catch(function (error) {
          button.disabled = false; analyses.replaceChildren(lensNode('p', error.message, 'hint'));
        });
      });
      button.disabled = !asset.crop_url;
      analyses.appendChild(button);
      renderAssetAnalyses(results, asset.analyses || []);
      analyses.appendChild(results);
      layout.appendChild(analyses);
      row.appendChild(layout);
      section.appendChild(row);
    });
    parent.appendChild(section);
  }

  function renderAssetAnalyses(parent, analyses) {
    analyses.forEach(function (analysis) {
      var block = lensNode('div', null, 'asset-analysis-result');
      block.appendChild(lensNode('strong', 'Read from the image — check against the crop'));
      block.appendChild(lensNode('p', analysis.mode + ' · ' + analysis.model, 'meta'));
      var result = analysis.result || {};
      var rendered = lensNode('div', null, 'asset-read-result');
      if (analysis.mode === 'table') {
        var table = lensNode('table', null, 'asset-result-table');
        var head = lensNode('thead'), headerRow = lensNode('tr');
        (result.columns || []).forEach(function (column) {
          var unit = result.units && result.units[column];
          headerRow.appendChild(lensNode('th', unit ? column + ' (' + unit + ')' : column));
        });
        head.appendChild(headerRow); table.appendChild(head);
        var body = lensNode('tbody');
        (result.rows || []).forEach(function (row) {
          var tr = lensNode('tr');
          row.forEach(function (cell) { tr.appendChild(lensNode('td', cell)); });
          body.appendChild(tr);
        });
        table.appendChild(body);
        var tableWrap = lensNode('div', null, 'asset-result-table-wrap');
        tableWrap.appendChild(table); rendered.appendChild(tableWrap);
        if ((result.footnotes || []).length) {
          var footnotes = lensNode('ul', null, 'asset-footnotes');
          result.footnotes.forEach(function (note) { footnotes.appendChild(lensNode('li', note)); });
          rendered.appendChild(footnotes);
        }
      } else if (analysis.mode === 'chart') {
        var axes = result.axes || {};
        var axisLabels = Object.keys(axes).map(function (axis) { return axis + ': ' + axes[axis]; });
        if (axisLabels.length) rendered.appendChild(lensNode('p', axisLabels.join(' · '), 'asset-chart-axes'));
        if ((result.legend || []).length) {
          var legend = lensNode('div', null, 'asset-chart-legend');
          legend.appendChild(lensNode('strong', 'Legend:'));
          result.legend.forEach(function (entry) { legend.appendChild(lensNode('span', entry, 'chip')); });
          rendered.appendChild(legend);
        }
        (result.series || []).forEach(function (series) {
          rendered.appendChild(lensNode('h5', series.name || 'Series'));
          var points = lensNode('table', null, 'asset-result-table asset-chart-points');
          var pointHead = lensNode('thead'), pointHeader = lensNode('tr');
          pointHeader.appendChild(lensNode('th', axes.x || 'x'));
          pointHeader.appendChild(lensNode('th', axes.y || 'y'));
          pointHead.appendChild(pointHeader); points.appendChild(pointHead);
          var pointBody = lensNode('tbody');
          (series.points || []).forEach(function (point) {
            var tr = lensNode('tr');
            point.forEach(function (value) {
              var cell = lensNode('td');
              cell.appendChild(lensNode('span', value));
              cell.appendChild(lensNode('span', 'Digitized estimate', 'badge neutral'));
              tr.appendChild(cell);
            });
            pointBody.appendChild(tr);
          });
          points.appendChild(pointBody);
          var pointWrap = lensNode('div', null, 'asset-result-table-wrap');
          pointWrap.appendChild(points); rendered.appendChild(pointWrap);
          if (series.error_bars) rendered.appendChild(lensNode('p', 'Error bars: ' + series.error_bars, 'meta'));
        });
        if (result.error_bars) rendered.appendChild(lensNode('p', 'Error bars: ' + result.error_bars, 'meta'));
      } else if (analysis.mode === 'equation') {
        var math = lensNode('div', null, 'asset-equation-latex');
        if (window.renderMarkdown) window.renderMarkdown(math, '$$\n' + (result.latex || '') + '\n$$');
        else math.textContent = result.latex || '';
        rendered.appendChild(math);
        if ((result.symbols || []).length) {
          var symbols = lensNode('ul', null, 'asset-symbols');
          result.symbols.forEach(function (symbol) {
            var item = lensNode('li');
            item.appendChild(lensNode('span', symbol.symbol + ' — ' + symbol.meaning));
            var source = symbol.evidence === 'text' ? 'from text' :
              symbol.evidence === 'caption' ? 'from caption' : 'unknown';
            item.appendChild(lensNode('span', source, 'badge ' + (source === 'unknown' ? 'warning' : 'neutral')));
            symbols.appendChild(item);
          });
          rendered.appendChild(symbols);
        }
      }
      block.appendChild(rendered);
      var fidelity = lensFidelityLabels(analysis.mode, analysis.fidelity || {}, result);
      if (fidelity.length) {
        var badges = lensNode('div', null, 'lens-badges asset-fidelity');
        fidelity.forEach(function (label) { badges.appendChild(lensNode('span', label, 'badge neutral')); });
        block.appendChild(badges);
      }
      var json = lensNode('details', null, 'asset-result-details');
      json.appendChild(lensNode('summary', 'Show JSON'));
      json.appendChild(lensNode('pre', JSON.stringify(result, null, 2), 'asset-result-json'));
      block.appendChild(json);
      if (analysis.evidence && analysis.evidence.length) lensEvidence(block, analysis.evidence);
      parent.appendChild(block);
    });
  }

  function loadGaps() {
    var list = $('gaps-list'), project = state.project;
    list.replaceChildren();
    if (!project) {
      list.appendChild(lensNode('p', 'Choose a project to detect research gaps.', 'hint'));
      return Promise.resolve();
    }
    $('gaps-status').textContent = 'Checking claims and study matrices…';
    return post('/api/gaps/detect', { project: project }).then(function (items) {
      if (project !== state.project || state.page !== 'gaps') return;
      $('gaps-status').textContent = items.length + ' deterministic candidate' + (items.length === 1 ? '' : 's');
      list.replaceChildren();
      if (!items.length) {
        list.appendChild(lensNode('p', 'No candidates yet. Extract claims or add matrix data to find evidence gaps.', 'hint'));
      }
      items.forEach(function (item) { renderGapCandidate(list, item); });
    }).catch(function (error) {
      if (project === state.project) $('gaps-status').textContent = error.message;
    });
  }

  function renderGapCandidate(parent, item) {
    var row = lensNode('article', null, 'lens-row');
    row.appendChild(lensNode('h3', item.title));
    var kindLabels = {
      single_source_claim: 'Single-source claim', contradicted_cluster: 'Contradicted claim',
      underrepresented_category: 'Underrepresented category', missing_baseline: 'Missing baseline',
      parameter_without_sensitivity: 'No sensitivity analysis'
    };
    lensBadges(row, [kindLabels[item.kind] || lensLabel(item.kind)], []);
    var statusBadges = lensNode('div', null, 'lens-badges');
    var cardStatus = item.card ? item.card.review_status : item.review_status;
    statusBadges.appendChild(lensNode('span', lensLabel(cardStatus) || 'Open', 'badge neutral'));
    row.appendChild(statusBadges);
    if (item.detail && item.detail.trim().toLowerCase() !== (item.title || '').trim().toLowerCase()) {
      row.appendChild(lensNode('p', item.detail));
    }
    row.appendChild(lensNode('p', (item.evidence || []).length + ' evidence span' +
      ((item.evidence || []).length === 1 ? '' : 's'), 'meta'));
    if (item.evidence && item.evidence.length) lensEvidence(row, item.evidence);
    var actions = lensNode('div', null, 'lens-actions');
    if (!item.card) {
      actions.appendChild(lensButton('Design proposal', function () { designGap(item.id); }, 'btn accent small'));
    } else {
      renderGapCard(row, item.card, item.evidence || []);
      if (item.card.review_status === 'open') {
        actions.appendChild(lensButton('Accept proposal', function () { reviewGap(item.card.id, 'accept'); }, 'btn small'));
        actions.appendChild(lensButton('Reject proposal', function () { reviewGap(item.card.id, 'reject'); }, 'btn small'));
      } else {
        if (item.card.review_status === 'rejected') {
          actions.appendChild(lensButton('Redesign proposal', function () { designGap(item.id); }, 'btn small'));
        }
      }
      var exportButton = lensButton('Export proposal', function () { exportGap(item.card.id); }, 'btn accent small');
      exportButton.disabled = item.card.review_status !== 'accepted';
      exportButton.title = exportButton.disabled ? 'Accept the proposal before exporting.' : 'Export accepted proposal';
      actions.appendChild(exportButton);
    }
    row.appendChild(actions);
    parent.appendChild(row);
  }

  function renderGapCard(parent, card, evidence) {
    var block = lensNode('section', null, 'gap-card');
    block.appendChild(lensNode('h4', 'Research proposal — not a literature finding', 'gap-card-title'));
    var statusBadges = lensNode('div', null, 'lens-badges');
    statusBadges.appendChild(lensNode('span', lensLabel(card.review_status) || 'Open', 'badge neutral'));
    block.appendChild(statusBadges);
    var headings = {
      why_it_matters: 'Why it matters', minimal_experiment: 'Minimal experiment or analysis',
      data: 'Data', controls: 'Controls', outcome: 'Outcome', failure_modes: 'Failure modes',
      what_model_does_not_know: 'What the model does not know'
    };
    Object.keys(headings).forEach(function (key) {
      var sentences = card.content[key] || [];
      var section = lensNode('section');
      section.appendChild(lensNode('h5', headings[key]));
      sentences.forEach(function (sentence) {
        var line = lensNode('p', sentence.text, 'gap-sentence');
        var spans = evidence.filter(function (span) { return sentence.evidence_ids.indexOf(span.id) >= 0; });
        if (spans.length) {
          spans.forEach(function (span) {
            var chip = lensNode('a', (span.title || span.paper_id) + ' · p.' + span.page, 'chip gap-citation');
            chip.href = '/api/papers/' + encodeURIComponent(span.paper_id) + '/pdf#page=' +
              encodeURIComponent(span.page);
            chip.target = '_blank'; chip.rel = 'noopener'; line.appendChild(chip);
          });
        } else {
          var label = sentence.label === 'unknown' ? 'Unknown' : 'Proposal';
          line.appendChild(lensNode('span', label, 'badge neutral'));
        }
        section.appendChild(line);
        if (spans.length) lensEvidence(section, spans);
      });
      block.appendChild(section);
    });
    parent.appendChild(block);
  }

  function loadConsole() {
    $('console-status').textContent = state.project ? 'Planning uses the current project.' :
      'Choose a project for project-scoped actions.';
    $('console-run-detail').replaceChildren();
    return loadConsoleRuns();
  }

  function loadConsoleRuns() {
    var query = state.project ? '?project=' + encodeURIComponent(state.project) : '';
    return api('/api/console/runs' + query).then(function (runs) {
      var list = $('console-runs');
      list.replaceChildren();
      if (!runs.length) list.appendChild(lensNode('p', 'No console runs yet.', 'hint'));
      runs.forEach(function (run) {
        var item = lensNode('article', null, 'lens-row');
        item.appendChild(lensNode('strong', run.request || 'Research workflow'));
        item.appendChild(lensNode('p', run.status + ' · step ' + run.current_step + ' · ' +
          new Date(run.updated_at).toLocaleString(), 'meta'));
        item.appendChild(lensButton('Show run', function () { pollConsoleRun(run.id, false); }));
        list.appendChild(item);
      });
    }).catch(function (error) { $('console-status').textContent = error.message; });
  }

  var CONSOLE_ACTIONS = {
    filter_papers: 'Filter papers', search_library: 'Search the library', ask: 'Ask the library',
    create_matrix: 'Create a study matrix', add_column: 'Add a matrix column', extract_matrix: 'Extract matrix cells',
    extract_claims: 'Extract claims', cluster_claims: 'Cluster claims', relate_claims: 'Assess claim relations',
    screen_fulltext: 'Screen full text', export_matrix: 'Export a matrix', write_brief: 'Write a research brief',
    snapshot: 'Take a snapshot', refresh: 'Refresh the living review'
  };

  // Readable "key: value" lines for a step's arguments; unset values are left out.
  function consoleArgs(args) {
    var list = lensNode('ul', null, 'console-args');
    Object.keys(args || {}).forEach(function (key) {
      var value = args[key];
      if (value == null || value === '' || (Array.isArray(value) && !value.length)) return;
      var text = typeof value === 'boolean' ? (value ? 'yes' : 'no') :
        /^\$step_\d+$/.test(String(value)) ? 'result of step ' + String(value).slice(6) :
        typeof value === 'object' ? JSON.stringify(value) : String(value);
      list.appendChild(lensNode('li', key.replace(/_/g, ' ') + ': ' + text));
    });
    if (!list.children.length) list.appendChild(lensNode('li', 'no arguments'));
    return list;
  }

  function renderConsolePreview(data) {
    var target = $('console-preview');
    target.replaceChildren();
    var preview = data.preview;
    target.appendChild(lensNode('p', preview.model_call_count + ' model calls · maximum cost ' +
      (preview.max_cost_usd == null ? 'unknown' : '$' + Number(preview.max_cost_usd).toFixed(4)) +
      ' · about ' + Math.ceil(preview.time_estimate_seconds) + ' seconds · writes ' +
      (preview.writes ? 'yes' : 'no'), 'meta'));
    preview.blocked_reasons.forEach(function (reason) {
      target.appendChild(lensNode('p', 'Blocked: ' + reason, 'no'));
    });
    preview.steps.forEach(function (step) {
      var row = lensNode('article', null, 'lens-row');
      row.appendChild(lensNode('h3', step.step + '. ' + (CONSOLE_ACTIONS[step.action] || step.action)));
      row.appendChild(consoleArgs(step.args));
      row.appendChild(lensNode('p', 'Papers in scope: ' +
        (step.papers_in_scope == null ? 'not determined' : step.papers_in_scope) +
        ' · model calls: ' + (step.estimate_known === false ? 'known after earlier steps run' :
          step.model_call_count) + ' · max cost: ' +
        (step.max_cost_usd == null ? 'unknown' : '$' + Number(step.max_cost_usd).toFixed(4)) +
        ' · time: ' + Math.ceil(step.time_estimate_seconds) + 's · writes: ' + (step.writes ? 'yes' : 'no'), 'meta'));
      target.appendChild(row);
    });
    state.consolePlan = data.plan;
    $('console-run').hidden = !data.plan || !!preview.blocked_reasons.length;
    $('console-status').textContent = 'Preview only. No actions have run.';
  }

  function showConsoleRun(run) {
    state.consoleRunId = run.id;
    var target = $('console-run-detail');
    target.replaceChildren();
    target.appendChild(lensNode('h3', 'Run ' + run.id + ' · ' + run.status + ' · step ' + run.current_step));
    (run.results || []).forEach(function (result) {
      var row = lensNode('article', null, 'lens-row');
      row.appendChild(lensNode('strong', 'Step ' + result.step + ': ' + (CONSOLE_ACTIONS[result.action] || result.action)));
      row.appendChild(lensNode('pre', JSON.stringify(result.output, null, 2), 'console-json'));
      var output = result.output || {};
      if (output.matrix_id) {
        row.appendChild(lensButton('Open matrix', function () {
          showView('matrix');
          loadLensMatrices(output.matrix_id).catch(function (error) { toast(error.message); });
        }));
      }
      if (output.run_id && result.action === 'ask') {
        row.appendChild(lensButton('Open answer', function () {
          showView('ask');
          loadLensAnswer(output.run_id).catch(function (error) { toast(error.message); });
        }));
      }
      if (output.path) {
        var filename = String(output.path).split('/').pop();
        var brief = lensNode('a', 'Open exported file: ' + filename, 'btn small');
        brief.href = '/api/console/runs/' + encodeURIComponent(run.id) + '/exports/' + encodeURIComponent(filename);
        brief.download = filename;
        row.appendChild(brief);
      }
      target.appendChild(row);
    });
    $('console-run').hidden = true;
    $('console-cancel').hidden = run.status !== 'running';
    if (run.status === 'failed' || run.status === 'cancelled') {
      target.appendChild(lensButton('Resume from step ' + run.current_step, function () {
        resumeConsoleRun(run.id);
      }, 'btn accent'));
    }
    $('console-status').textContent = run.status === 'running' ? 'Execution checkpointed after each step.' :
      'Run status: ' + run.status + (run.status === 'failed' && run.error ? ' — step ' + run.current_step +
        ': ' + run.error : '');
  }

  function pollConsoleRun(runId, repeat) {
    return api('/api/console/runs/' + encodeURIComponent(runId)).then(function (run) {
      showConsoleRun(run);
      loadConsoleRuns();
      if (repeat && (run.status === 'pending' || run.status === 'running')) {
        setTimeout(function () { pollConsoleRun(runId, true); }, 900);
      }
      return run;
    }).catch(function (error) { $('console-status').textContent = error.message; });
  }

  function planConsole(request, approvedCloud) {
    return post('/api/console/plan', { project: state.project, request: request, profile: $('console-profile').value || null,
      approved_cloud: !!approvedCloud }).catch(function (error) {
      if (error.status !== 403 || approvedCloud) throw error;
      var plan = error.preview || {};
      var message = 'Separately approve the cloud planner route ' + (plan.provider || '') + '/' +
        (plan.model || '') + '? It receives only your request and the action registry.';
      if (!window.confirm(message)) throw new Error('Cloud planner route was not approved.');
      return planConsole(request, true);
    });
  }

  function runConsolePlan(approvedCloud) {
    var body = { plan: state.consolePlan, project: state.project, approved: true,
      approved_cloud: !!approvedCloud };
    return post('/api/console/execute', body).catch(function (error) {
      if (error.status !== 403 || approvedCloud) throw error;
      if (!window.confirm('Separately approve the cloud model routes for these steps?')) {
        throw new Error('Cloud routes were not approved.');
      }
      body.approved_cloud = true;
      return post('/api/console/execute', body);
    });
  }

  function resumeConsoleRun(runId) {
    if (!window.confirm('Resume this run from its failed or cancelled step?')) return;
    post('/api/console/resume', { run_id: runId, approved: true, approved_cloud: false }).catch(function (error) {
      if (error.status !== 403) throw error;
      if (!window.confirm('Separately approve the cloud model routes for the remaining steps?')) {
        throw new Error('Cloud routes were not approved.');
      }
      return post('/api/console/resume', { run_id: runId, approved: true, approved_cloud: true });
    }).then(function () { pollConsoleRun(runId, true); })
      .catch(function (error) { $('console-status').textContent = error.message; });
  }

  function cancelConsoleRun() {
    if (!state.consoleRunId) return;
    post('/api/console/cancel', { run_id: state.consoleRunId }).then(function () {
      $('console-status').textContent = 'Cancellation requested; the current step will finish first.';
    }).catch(function (error) { $('console-status').textContent = error.message; });
  }

  function designGap(gapId, approved) {
    var status = $('gaps-status');
    status.textContent = 'Planning proposal…';
    function start(isApproved) {
      return post('/api/gaps/design', { gap_id: gapId, profile: $('gaps-profile').value || null, approved: isApproved }).catch(function (error) {
        if (error.status !== 403) throw error;
        var plan = error.preview || {};
        var cost = plan.max_cost_usd == null ? 'unknown cost' : '$' + Number(plan.max_cost_usd).toFixed(4);
        if (!window.confirm('Send project evidence to ' + (plan.provider || 'the configured provider') + ' (' +
            (plan.model || 'model') + ')? Estimated maximum cost: ' + cost + '.')) throw error;
        return post('/api/gaps/design', { gap_id: gapId, profile: $('gaps-profile').value || null, approved: true });
      });
    }
    start(approved === true).then(function (job) {
      (function poll() {
        api('/api/jobs/' + encodeURIComponent(job.id)).then(function (result) {
          if (result.status === 'running') { status.textContent = result.messages && result.messages.length ?
            result.messages[result.messages.length - 1] : 'Designing a cited proposal…'; setTimeout(poll, 500); return; }
          if (result.status !== 'done') { status.textContent = result.error || 'Proposal design failed.'; return; }
          loadGaps();
        }).catch(function (error) { status.textContent = error.message; });
      })();
    }).catch(function (error) { status.textContent = error.message; });
  }

  function reviewGap(cardId, action) {
    var reason = window.prompt('Optional reason for this decision:', '');
    if (reason === null) return;
    post('/api/gaps/review', { card_id: cardId, action: action, reason: reason }).then(loadGaps)
      .catch(function (error) { $('gaps-status').textContent = error.message; });
  }

  function exportGap(cardId) {
    api('/api/gaps/cards/' + encodeURIComponent(cardId) + '/export').then(function (result) {
      var url = URL.createObjectURL(new Blob([result.markdown], { type: 'text/markdown;charset=utf-8' }));
      var link = lensNode('a'); link.href = url; link.download = 'research-proposal-' + cardId + '.md';
      link.click(); URL.revokeObjectURL(url);
    }).catch(function (error) { $('gaps-status').textContent = error.message; });
  }

  /* ---------- running searches ---------- */

  function setBusy(busy) {
    $('btn-run').disabled = busy;
    $('btn-more').disabled = busy;
    $('btn-export').disabled = busy;
    $('btn-screen').disabled = busy || state.criteriaFor !== state.project;
  }

  function logLine(text, cls) {
    var div = document.createElement('div');
    div.textContent = text;
    if (cls) div.className = cls;
    $('progress').appendChild(div);
    $('progress').scrollTop = 1e9;
  }

  function poll(job) {
    state.job = job.id;
    showView('new');  // the progress log lives with the search form
    var shown = 0;
    $('progress').innerHTML = '';
    var running = document.createElement('div');
    running.className = 'run';
    running.textContent = 'working… ';
    var stop = document.createElement('button');
    stop.className = 'btn small';
    stop.textContent = 'Stop';
    stop.onclick = function () {
      stop.disabled = true;
      stop.textContent = 'Stopping…';
      post('/api/jobs/' + job.id + '/stop', {}).catch(function (e) { stop.disabled = false; stop.textContent = 'Stop'; toast(e.message); });
    };
    running.appendChild(stop);
    (function tick() {
      api('/api/jobs/' + job.id).then(function (j) {
        for (; shown < j.messages.length; shown++) logLine(j.messages[shown], /^warning/.test(j.messages[shown]) ? 'err' : '');
        if (j.status === 'running') {
          $('progress').appendChild(running);
          setTimeout(tick, 900);
          return;
        }
        running.remove();
        setBusy(false);
        if (j.status === 'failed' && j.error === 'stopped') {
          logLine('Stopped; what was finished before stays.', 'err');
          toast('Stopped');
          return;
        }
        if (j.status === 'failed') { logLine(j.error, 'err'); toast({ export: 'Export failed', pdfs: 'Adding PDFs failed', links: 'Adding links failed', screen: 'Screening failed', setup: 'Setting up the vision model failed' }[j.kind] || 'Search failed'); return; }
        if (j.kind === 'setup') { toast('Better math (high) is ready; exporting'); $('btn-export').click(); return; }
        if (j.kind === 'export') toast(j.messages[j.messages.length - 1].replace(/^done: /, ''));
        if (j.kind === 'pdfs' && j.report) showAddReport(j.report);
        if (j.kind === 'export' && j.report && j.report.vision) showVisionReport(j.report, j.messages[j.messages.length - 1]);
        state.project = j.project;
        store.set('project', j.project);
        state.runId = null;
        loadProjects().then(function () { if (j.run_id) loadRun(j.run_id); });
      }).catch(function (e) { setBusy(false); logLine(e.message, 'err'); });
    })();
  }

  // The export folder in the field is saved with the project (created if new) when a run starts.
  function common() {
    return { project: projectName(), export_dir: $('export-dir').value.trim() };
  }

  function startSimilar(extra) {
    setBusy(true);
    var body = Object.assign(common(), { embeddings: $('embeddings').checked }, extra);
    post('/api/similar', body).then(poll).catch(function (e) { setBusy(false); toast(e.message); });
    closeDrawer();
  }

  /* ---------- Seed PDFs ---------- */
  // The PDFs of a "Find similar" search; the same file picked twice is kept once.
  function addSeedPdfs(list, picked) {
    var out = list.slice();
    picked.forEach(function (f) {
      if (!out.some(function (g) { return g.name === f.name && g.size === f.size; })) out.push(f);
    });
    return out;
  }

  // One upload at a time, in order; resolves to the saved paths.
  function uploadSeedPdfs(files) {
    return files.reduce(function (done, f, i) {
      return done.then(function (paths) {
        logLine('uploading ' + (files.length > 1 ? (i + 1) + '/' + files.length + ' ' : '') + f.name);
        return api('/api/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f })
          .then(function (r) { return paths.concat([r.path]); });
      });
    }, Promise.resolve([]));
  }
  /* ---------- End Seed PDFs ---------- */

  function run() {
    if (state.mode === 'similar') {
      var seed = $('seed').value.trim(), title = $('title').value.trim(), abstract = $('abstract').value.trim();
      if (!seed && !title && !abstract && !state.seedPdfs.length) { toast('Give a DOI, a PDF, a title or an abstract'); return; }
      setBusy(true);
      $('progress').innerHTML = '';
      uploadSeedPdfs(state.seedPdfs).then(function (paths) {
        var seeds = (seed ? seed.split(/[\s,]+/).filter(Boolean) : []).concat(paths);
        setBusy(false);
        startSimilar({ seeds: seeds, title: title, abstract: abstract });
      }).catch(function (e) { setBusy(false); toast(e.message); });
    } else if (state.mode === 'search') {
      var queries = $('queries').value.split('\n').map(function (q) { return q.trim(); }).filter(Boolean);
      var sources = Array.prototype.map.call(document.querySelectorAll('input[name=src]:checked'), function (c) { return c.value; });
      if (!queries.length) { toast('Give at least one query'); return; }
      setBusy(true);
      post('/api/search', Object.assign(common(), { queries: queries, sources: sources }))
        .then(poll).catch(function (e) { setBusy(false); toast(e.message); });
    } else if (state.mode === 'pdfs') {
      addPdfs();
    } else {
      if (!state.list) { toast('Choose a .csv, .bib or .ris file'); return; }
      setBusy(true);
      var c = common();
      api('/api/import?name=' + encodeURIComponent(state.list.name) + '&project=' + encodeURIComponent(c.project) +
          '&export_dir=' + encodeURIComponent(c.export_dir), { method: 'POST', body: state.list })
        .then(function (r) {
          setBusy(false);
          toast('Imported ' + r.papers + ' papers (' + r.created + ' new to the library)');
          state.project = c.project;
          store.set('project', c.project);
          state.runId = null;
          return loadProjects().then(function () { loadRun(r.run_id); });
        }).catch(function (e) { setBusy(false); toast(e.message); });
    }
  }

  /* ---------- my PDFs ---------- */

  // What the Export menu in the header covers, said as the My PDFs form and its report say it.
  var SCOPE_WORDS = { all: 'all papers of the project', relevant: 'the papers marked ✓',
    'not-irrelevant': 'all papers except ✗', visible: 'the visible list and the added papers' };
  function scopeWords() { return SCOPE_WORDS[$('export-scope').value] || SCOPE_WORDS.all; }

  // Which papers an export covers, from the Export menu in the header.
  function exportSelection() {
    var scope = $('export-scope').value;
    if (scope === 'visible' && state.data) return { paper_ids: state.view.map(function (p) { return p.id; }) };
    return { only: scope === 'visible' ? 'all' : scope };
  }

  // Upload the chosen files one by one (the server keeps them only until they are in the library),
  // then add them together with the typed folder as one background job.
  // Links to articles: each paper joins the project and its open-access PDF is downloaded and read.
  function addLinks(links) {
    setBusy(true);
    $('progress').innerHTML = '';
    return post('/api/links', Object.assign(common(), { links: links }))
      .then(function (job) { $('pdf-links').value = ''; poll(job); })
      .catch(function (e) { setBusy(false); toast(e.message); });
  }

  function addPdfs() {
    var folder = $('pdf-folder').value.trim(), files = state.pdfs.slice();
    var links = $('pdf-links').value.split('\n').map(function (l) { return l.trim(); }).filter(Boolean);
    if (links.length && (folder || files.length)) { toast('Add the links and the PDFs one at a time'); return; }
    if (links.length) return addLinks(links);
    if (!folder && !files.length) { toast('Give a folder, drop PDF files or paste links'); return; }
    setBusy(true);
    $('progress').innerHTML = '';
    var uploaded = [];
    files.reduce(function (chain, f, i) {
      return chain.then(function () {
        logLine('uploading ' + (i + 1) + '/' + files.length + ' ' + f.name);
        return api('/api/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f })
          .then(function (r) { uploaded.push(r.path); })
          .catch(function (e) { logLine(f.name + ': ' + e.message, 'err'); });
      });
    }, Promise.resolve()).then(function () {
      if (!folder && !uploaded.length) throw new Error('No PDF could be uploaded');
      var body = Object.assign(common(), { folder: folder, files: uploaded });
      if ($('pdfs-export').checked) { body.export = exportSelection(); state.addScope = scopeWords(); }
      return post('/api/add', body);
    }).then(function (job) { setPdfs([]); poll(job); })
      .catch(function (e) { setBusy(false); toast(e.message); });
  }

  function setPdfs(files) {
    state.pdfs = files;
    var zone = $('drop-pdfs');
    zone.querySelector('.file') && zone.querySelector('.file').remove();
    if (!files.length) return;
    var size = files.reduce(function (n, f) { return n + f.size; }, 0);
    var span = document.createElement('div');
    span.className = 'file';
    span.innerHTML = files.length + ' PDF' + (files.length > 1 ? 's' : '') + ' (' + (size / 1048576).toFixed(1) +
      ' MB) · <a href="#">clear</a>';
    span.querySelector('a').onclick = function (e) { e.preventDefault(); setPdfs([]); };
    zone.appendChild(span);
  }

  // PDFs and the documents MarkItDown reads (Word, PowerPoint, Excel, HTML, EPUB, Markdown, text).
  function isPdf(f) { return /\.(pdf|docx|pptx|xlsx|html?|epub|md|markdown|txt)$/i.test(f.name); }

  // A dropped folder arrives as a directory entry: walk it (readEntries returns batches).
  function readEntry(entry) {
    if (entry.isFile) return new Promise(function (ok) { entry.file(ok, function () { ok(null); }); });
    if (!entry.isDirectory) return Promise.resolve(null);
    var reader = entry.createReader(), all = [];
    return new Promise(function (ok) {
      (function next() {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            Promise.all(all.map(readEntry)).then(function (lists) { ok([].concat.apply([], lists)); });
            return;
          }
          all = all.concat(Array.prototype.slice.call(batch));
          next();
        }, function () { ok([]); });
      })();
    });
  }

  function wirePdfs() {
    var zone = $('drop-pdfs');
    function add(list) {
      var picked = list.filter(function (f) { return f && isPdf(f); });
      if (!picked.length) { toast('No PDF or document files found'); return; }
      setPdfs(state.pdfs.concat(picked));
    }
    $('pick-pdfs').onclick = function (e) { e.preventDefault(); $('files-pdfs').click(); };
    $('pick-dir').onclick = function (e) { e.preventDefault(); $('dir-pdfs').click(); };
    ['files-pdfs', 'dir-pdfs'].forEach(function (id) {
      $(id).onchange = function () { add(Array.prototype.slice.call(this.files)); this.value = ''; };
    });
    zone.ondragover = function (e) { e.preventDefault(); zone.classList.add('over'); };
    zone.ondragleave = function () { zone.classList.remove('over'); };
    zone.ondrop = function (e) {
      e.preventDefault();
      zone.classList.remove('over');
      var items = e.dataTransfer.items, entries = [];
      for (var i = 0; items && i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (entry) entries.push(entry);
      }
      if (!entries.length) { add(Array.prototype.slice.call(e.dataTransfer.files)); return; }
      Promise.all(entries.map(readEntry)).then(function (lists) { add([].concat.apply([], lists)); });
    };
  }

  /* ---------- quit ---------- */

  // LitForge usually runs without a terminal, so the page is where it is stopped.
  function quit(force) {
    post('/api/quit', { force: !!force }).then(function (r) {
      if (!r.stopped) {
        if (window.confirm(r.running + ' task' + (r.running === 1 ? ' is' : 's are') + ' still running and will be ' +
            'cancelled. Quit anyway?')) quit(true);
        return;
      }
      showStopped();
    }).catch(function (e) { toast(e.message); });
  }

  function showStopped() {
    document.body.innerHTML = '<div class="stopped"><h2>LitForge has stopped</h2><p>You can close this tab. ' +
      'To open LitForge again, use its icon in your apps.</p></div>';
  }

  /* ---------- help ---------- */

  function openGuide() {
    store.set('guide-seen', true);
    if (!$('guide').open) $('guide').showModal();
  }

  // Native title tooltips appear late and small; show the same text at once in a readable bubble.
  // The title moves to data-tip while hovered so the browser does not show its own copy too.
  function wireTooltips() {
    var tip = $('tip'), timer, cur = null;
    function hide() {
      clearTimeout(timer);
      tip.classList.remove('show');
      if (cur && cur.dataset.tip && !cur.getAttribute('title')) cur.setAttribute('title', cur.dataset.tip);
      cur = null;
    }
    document.addEventListener('mouseover', function (e) {
      var el = e.target.closest && e.target.closest('[title], [data-tip]');
      if (el === cur) return;
      hide();
      if (!el || el.tagName === 'OPTION' || el.closest('svg')) return;
      var text = el.getAttribute('title') || el.dataset.tip;
      if (!text) return;
      cur = el;
      el.dataset.tip = text;
      el.removeAttribute('title');
      timer = setTimeout(function () {
        tip.textContent = text;
        var r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
        var x = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8));
        var y = r.bottom + 8 + h > innerHeight ? r.top - h - 8 : r.bottom + 8;
        tip.style.left = x + 'px';
        tip.style.top = y + 'px';
        tip.classList.add('show');
      }, 350);
    });
    ['mousedown', 'scroll', 'keydown'].forEach(function (ev) { document.addEventListener(ev, hide, true); });
    document.addEventListener('mouseleave', hide);
  }

  /* ---------- wiring ---------- */

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll('[data-mode]').forEach(function (b) { b.classList.toggle('on', b.dataset.mode === mode); });
    $('form-similar').hidden = mode !== 'similar';
    $('form-search').hidden = mode !== 'search';
    $('form-import').hidden = mode !== 'import';
    $('form-pdfs').hidden = mode !== 'pdfs';
    $('emb-wrap').hidden = mode !== 'similar';
    $('pdfs-export-wrap').hidden = mode !== 'pdfs';
    $('btn-more').hidden = mode !== 'similar';
    $('btn-run').textContent = { similar: 'Find similar papers', search: 'Search', 'import': 'Import list',
      pdfs: $('pdfs-export').checked ? 'Add PDFs & export' : 'Add PDFs' }[mode];
    store.set('mode', mode);
  }

  // `multi`: onFile gets the list of every accepted file instead of the first file.
  function dropZone(zone, input, pick, accept, onFile, multi) {
    pick.onclick = function (e) { e.preventDefault(); input.click(); };
    input.onchange = function () {
      if (multi && input.files.length) onFile(Array.prototype.slice.call(input.files));
      else if (input.files[0]) onFile(input.files[0]);
    };
    zone.ondragover = function (e) { e.preventDefault(); zone.classList.add('over'); };
    zone.ondragleave = function () { zone.classList.remove('over'); };
    zone.ondrop = function (e) {
      e.preventDefault();
      zone.classList.remove('over');
      if (multi) {
        var ok = Array.prototype.filter.call(e.dataTransfer.files, function (f) { return accept.test(f.name); });
        if (ok.length) onFile(ok); else toast('Unsupported file');
        return;
      }
      var f = e.dataTransfer.files[0];
      if (f && accept.test(f.name)) onFile(f); else toast('Unsupported file');
    };
  }

  function showFile(zone, file, clear) {
    zone.innerHTML = '<span class="file">' + esc(file.name) + '</span> · <a href="#">remove</a>';
    zone.querySelector('a').onclick = function (e) { e.preventDefault(); clear(); };
  }

  /* ---------- MinerU ---------- */
  function loadMineru() {
    return api('/api/mineru').then(function (info) {
      $('mineru-status').textContent = (info.installed ? 'Installed · ' + info.version : 'Not installed') +
        ' · Models: ' + (info.models_bytes / 1000000000).toFixed(2) + ' GB' + (info.error ? ' · ' + info.error : '');
      $('mineru-install').hidden = info.installed;
      $('mineru-install').onclick = installMineru;
    }).catch(function (e) { $('mineru-status').textContent = e.message; });
  }

  function watchMineru(job, target, done) {
    return api('/api/jobs/' + encodeURIComponent(job.id)).then(function (report) {
      target.textContent = (report.messages || []).join('\n') + (report.error ? '\n' + report.error : '');
      if (report.status === 'running') {
        setTimeout(function () { watchMineru(job, target, done); }, 1500);
      } else if (report.status === 'done') {
        done();
      }
    }).catch(function (e) { target.textContent = e.message; });
  }

  function installMineru() {
    return api('/api/mineru').then(function (info) {
      if (!window.confirm(info.notice + '\nDownload and install MinerU?')) return;
      $('mineru-progress').textContent = 'Installing MinerU…';
      return post('/api/mineru/install', { confirmed: true }).then(function (job) {
        return watchMineru(job, $('mineru-progress'), loadMineru);
      });
    }).catch(function (e) { $('mineru-progress').textContent = e.message; });
  }

  function rereadMineru(paper) {
    return api('/api/mineru').then(function (info) {
      if (!info.installed) { showView('settings'); showSettingsTab('tools'); return; }
      var estimate = mineruEstimate(paper);
      if (!window.confirm('Re-read accurately with MinerU? ' + estimate + '. Models download on first use (~2 GB).')) return;
      var target = $('mineru-paper-status');
      target.textContent = 'MinerU is running · ' + estimate;
      return post('/api/papers/' + encodeURIComponent(paper.id) + '/parse-mineru', {}).then(function (job) {
        return watchMineru(job, target, function () { openDrawer(paper); });
      });
    }).catch(function (e) { $('mineru-paper-status').textContent = e.message; });
  }
  function mineruEstimate(paper) {
    var seconds = paper.mineru_seconds_per_page || 20;
    return 'about ' + Math.max(1, Math.ceil((paper.page_count || 1) * seconds / 60)) + ' min';
  }
  /* ---------- End MinerU ---------- */

  /* ---------- Write ---------- */
  var W = { doc: null, file: 'main.tex', hash: null, editor: null, timer: null, saving: null, dirty: false,
    conflictHash: null, job: null, SAVE_DELAY: 2000 };

  function makeEditor(parent, text, handlers) {
    var CM = window.LitForgeCM;
    parent.replaceChildren();
    if (!CM) {  // the bundle failed to load: a plain text area still edits and saves
      var area = document.createElement('textarea');
      area.className = 'write-fallback'; area.value = text; area.dir = 'auto'; area.spellcheck = false;
      area.addEventListener('input', handlers.change); area.addEventListener('blur', handlers.blur);
      parent.appendChild(area);
      return {
        getText: function () { return area.value; },
        setText: function (t) { area.value = t; },
        gotoLine: function (n) {
          var lines = area.value.split('\n'), pos = 0;
          for (var i = 0; i < Math.min(n - 1, lines.length); i++) pos += lines[i].length + 1;
          area.focus(); area.setSelectionRange(pos, pos);
        },
        selection: function () { return { from: area.selectionStart, to: area.selectionEnd,
          text: area.value.slice(area.selectionStart, area.selectionEnd) }; },
        replace: function (from, to, t) { area.setRangeText(t, from, to, 'end'); handlers.change(); },
        select: function (from, to) { area.focus(); area.setSelectionRange(from, to); },
        lineRange: function (n) { return lineRange(area.value, n); },
        mark: function () {}, coords: function () { return null; },
        destroy: function () { parent.replaceChildren(); }
      };
    }
    var setMark = CM.StateEffect.define();
    var markField = CM.StateField.define({
      create: function () { return CM.Decoration.none; },
      update: function (marks, tr) {
        marks = marks.map(tr.changes);
        tr.effects.forEach(function (e) {
          if (!e.is(setMark)) return;
          marks = e.value && e.value.to > e.value.from ?
            CM.Decoration.set([CM.Decoration.mark({ class: 'cm-ai-range' }).range(e.value.from, e.value.to)]) :
            CM.Decoration.none;
        });
        return marks;
      },
      provide: function (f) { return CM.EditorView.decorations.from(f); }
    });
    var setSpellMarks = CM.StateEffect.define();
    var spellField = CM.StateField.define({
      create: function () { return CM.Decoration.none; },
      update: function (marks, tr) {
        marks = marks.map(tr.changes);
        tr.effects.forEach(function (e) {
          if (!e.is(setSpellMarks)) return;
          var len = tr.state.doc.length;
          marks = CM.Decoration.set(e.value.filter(function (s) { return s.to <= len && s.from < s.to; }).map(function (s) {
            return CM.Decoration.mark({ class: 'cm-misspelled' }).range(s.from, s.to);
          }), true);
        });
        return marks;
      },
      provide: function (f) { return CM.EditorView.decorations.from(f); }
    });
    // Notes: a dot in a gutter left of the line numbers; the marks move with the text.
    function NoteMarker(id, body) { this.id = id; this.body = body; }
    NoteMarker.prototype = Object.create(CM.GutterMarker.prototype);
    NoteMarker.prototype.constructor = NoteMarker;
    // The mark sits at the line's start and must stay with the line's text: text inserted at that spot (a git
    // conflict block, Enter at the line start) goes before it (real run: a note stayed on the conflict block).
    NoteMarker.prototype.startSide = NoteMarker.prototype.endSide = 1;
    NoteMarker.prototype.mapMode = 0;  // MapMode.Simple: a deleted line moves its note to where the text was, never drops it
    NoteMarker.prototype.eq = function (other) { return other.id === this.id && other.body === this.body; };
    NoteMarker.prototype.toDOM = function () {
      var dot = document.createElement('span');
      dot.className = 'cm-note-dot'; dot.textContent = '●'; dot.title = this.body;
      return dot;
    };
    var setNotes = CM.StateEffect.define();
    var notesField = CM.StateField.define({
      create: function () { return CM.RangeSet.empty; },
      update: function (notes, tr) {
        notes = notes.map(tr.changes);
        tr.effects.forEach(function (e) { if (e.is(setNotes)) notes = e.value; });
        return notes;
      }
    });
    var notesGutter = CM.gutter({ class: 'cm-notes-gutter', markers: function (v) { return v.state.field(notesField); },
      initialSpacer: function () { return new NoteMarker('', ''); },
      domEventHandlers: { mousedown: function (v, block) {
        if (handlers.note) handlers.note(v.state.doc.lineAt(block.from).number);
        return true;
      } } });
    var view = new CM.EditorView({
      parent: parent,
      state: CM.EditorState.create({ doc: text, extensions: [markField, spellField, notesField, notesGutter,
        CM.hoverTooltip(function (v, pos) { return handlers.hover ? handlers.hover(v, pos) : null; }, { hoverTime: 450 }),
        CM.lineNumbers(), CM.highlightActiveLineGutter(), CM.history(), CM.foldGutter(), CM.drawSelection(),
        CM.indentOnInput(), CM.bracketMatching(), CM.closeBrackets(), CM.highlightActiveLine(),
        CM.highlightSelectionMatches(), CM.StreamLanguage.define(CM.stex),
        CM.autocompletion({ override: [latexCompletionSource], icons: false }),
        // Stable tok-* classes instead of the built-in palette, so app.css colours them for light and dark.
        CM.classHighlighter ? CM.syntaxHighlighting(CM.classHighlighter) :
          CM.syntaxHighlighting(CM.defaultHighlightStyle, { fallback: true }),
        CM.EditorView.lineWrapping, CM.EditorView.perLineTextDirection.of(true), autoDirection(CM),
        CM.keymap.of([{ key: 'Mod-s', run: function () { handlers.blur(); return true; } },
          { key: 'Mod-Enter', run: function () { handlers.compile(); return true; } },
          { key: 'Tab', run: CM.acceptCompletion },
          { key: 'Mod-Alt-j', run: function () { syncToPdf(); return true; } }]  // only when the list is open; otherwise Tab indents
          .concat(CM.searchKeymap, CM.historyKeymap, [CM.indentWithTab], CM.defaultKeymap)),
        CM.EditorView.updateListener.of(function (u) {
          if (u.docChanged || u.viewportChanged) spellRefresh(u.view);
          if (u.docChanged) handlers.change();
          if (u.docChanged || u.selectionSet) handlers.cursor && handlers.cursor(u.view);
        }),
        CM.EditorView.domEventHandlers({
          blur: function () { handlers.blur(); },
          contextmenu: function (e, v) {
            var pos = v.posAtCoords({ x: e.clientX, y: e.clientY }), span = null;
            if (pos != null) {  // the underline as it is now (decorations follow edits at once)
              v.state.field(spellField).between(pos, pos, function (from, to) {
                var word = v.state.sliceDoc(from, to);
                span = { from: from, to: to, word: word, lang: /[\u0600-\u06ff]/.test(word) ? 'fa' : 'en' };
                return false;
              });
            }
            if (!span) return false;  // the browser's own menu elsewhere
            e.preventDefault();
            openSpellMenu(span, e.clientX, e.clientY);
            return true;
          }
        })
      ] })
    });
    view.scrollDOM.addEventListener('scroll', function () { if (AI.pending) placeWriteSuggestion(AI.pending.range); });
    view.litforgeSpell = function (spans) { view.dispatch({ effects: setSpellMarks.of(spans) }); };
    SPELL.spans = [];
    spellRefresh(view);
    return {
      view: view,
      getText: function () { return view.state.doc.toString(); },
      setText: function (t) { view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: t } }); },
      gotoLine: function (n) {
        var line = view.state.doc.line(Math.max(1, Math.min(n, view.state.doc.lines)));
        view.dispatch({ selection: { anchor: line.from }, scrollIntoView: true }); view.focus();
      },
      selection: function () {
        var r = view.state.selection.main;
        return { from: r.from, to: r.to, text: view.state.sliceDoc(r.from, r.to) };
      },
      replace: function (from, to, t) {
        view.dispatch({ changes: { from: from, to: to, insert: t }, selection: { anchor: from + t.length } });
        view.focus();
      },
      select: function (from, to) { view.dispatch({ selection: { anchor: from, head: to } }); view.focus(); },
      setNotes: function (notes) {
        var doc = view.state.doc;
        var ranges = notes.filter(function (n) { return n.line >= 1 && n.line <= doc.lines; }).map(function (n) {
          return new NoteMarker(n.id, n.body).range(doc.line(n.line).from);
        }).sort(function (a, b) { return a.from - b.from; });
        view.dispatch({ effects: setNotes.of(CM.RangeSet.of(ranges)) });
      },
      noteLines: function () {
        var out = [];
        view.state.field(notesField).between(0, view.state.doc.length, function (from, to, marker) {
          out.push({ id: marker.id, line: view.state.doc.lineAt(from).number });
        });
        return out;
      },
      lineRange: function (n) { return lineRange(view.state.doc.toString(), n); },
      mark: function (range) {  // highlight the text a suggestion would change; null clears it
        var len = view.state.doc.length;
        view.dispatch({ effects: setMark.of(range ? { from: Math.min(range.from, len), to: Math.min(range.to, len) } : null) });
      },
      coords: function (pos) { return view.coordsAtPos(Math.min(pos, view.state.doc.length)); },
      destroy: function () { view.destroy(); }
    };
  }

  // Each line takes its direction from its first strong character, so Persian lines run right to left and
  // cursor movement follows them (perLineTextDirection reads the direction from the line element).
  function autoDirection(CM) {
    var mark = CM.Decoration.line({ attributes: { dir: 'auto' } });
    return CM.EditorView.decorations.of(function (view) {
      var marks = [];
      view.visibleRanges.forEach(function (range) {
        for (var pos = range.from; pos <= range.to;) {
          var line = view.state.doc.lineAt(pos);
          marks.push(mark.range(line.from));
          pos = line.to + 1;
        }
      });
      return CM.Decoration.set(marks, true);
    });
  }

  function lineRange(text, n) {
    var lines = text.split('\n'), from = 0;
    n = Math.max(1, Math.min(n, lines.length));
    for (var i = 0; i < n - 1; i++) from += lines[i].length + 1;
    return { from: from, to: from + lines[n - 1].length };
  }

  function writeSaveState(text) { $('write-save-state').textContent = text; }

  // Layout: editor only, split or PDF only; the split position is a percentage of the width. Both are remembered.
  var WRITE_LAYOUTS = ['editor', 'split', 'pdf'];
  function setWriteLayout(layout) {
    if (WRITE_LAYOUTS.indexOf(layout) < 0) layout = 'split';
    $('write-split').dataset.layout = layout;
    document.querySelectorAll('.seg [data-layout]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.layout === layout));
    });
    store.set('write-layout', layout);
    return layout;
  }

  function setWriteSplit(pct) {
    pct = Math.round(Math.max(20, Math.min(80, Number(pct) || 50)));
    $('write-split').style.setProperty('--write-left', pct + '%');
    $('write-gutter').setAttribute('aria-valuenow', String(pct));
    store.set('write-split', pct);
    return pct;
  }

  function wireWriteGutter() {
    var gutter = $('write-gutter'), split = $('write-split');
    gutter.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      gutter.setPointerCapture(e.pointerId);
      gutter.classList.add('dragging'); split.classList.add('resizing');
      var box = split.getBoundingClientRect();
      var move = function (ev) { setWriteSplit((ev.clientX - box.left) / box.width * 100); };
      var up = function () {
        gutter.classList.remove('dragging'); split.classList.remove('resizing');
        gutter.removeEventListener('pointermove', move); gutter.removeEventListener('pointerup', up);
        gutter.removeEventListener('pointercancel', up);
      };
      gutter.addEventListener('pointermove', move); gutter.addEventListener('pointerup', up);
      gutter.addEventListener('pointercancel', up);
    });
    gutter.addEventListener('dblclick', function () { setWriteSplit(50); });
    gutter.addEventListener('keydown', function (e) {
      var now = Number(gutter.getAttribute('aria-valuenow')) || 50;
      var step = { ArrowLeft: -2, ArrowRight: 2, PageUp: -10, PageDown: 10 }[e.key];
      if (step) { e.preventDefault(); setWriteSplit(now + step); }
      else if (e.key === 'Home') { e.preventDefault(); setWriteSplit(20); }
      else if (e.key === 'End') { e.preventDefault(); setWriteSplit(80); }
    });
  }

  function loadWrite() {
    var project = projectName();
    $('write-project').textContent = project;
    if (W.doc) return Promise.resolve();
    if (!state.projects.some(function (p) { return p.name === project; })) {  // not loaded yet, or no project
      var hint = document.createElement('li');
      hint.className = 'muted'; hint.textContent = 'Choose or create a project first (sidebar → Projects).';
      $('write-docs').replaceChildren(hint);
      return Promise.resolve();
    }
    return api('/api/write/docs?project=' + encodeURIComponent(project)).then(function (data) {
      var select = $('write-new-template');
      select.replaceChildren();
      Object.keys(data.templates || {}).forEach(function (key) {
        var opt = document.createElement('option');
        opt.value = key; opt.textContent = data.templates[key];
        select.appendChild(opt);
      });
      var eng = data.engines || {}, tec = eng.tectonic || {};
      $('write-engine').textContent = tec.installed ? 'Engine: Tectonic ' + (tec.version || '') +
        (eng.sandbox === 'scan' ? ' · partial sandbox: on this system a document could still read other files'
          : ' · sandboxed') : (eng.xelatex || {}).installed ? 'Engine: system XeLaTeX (latexmk)' :
        'No LaTeX engine yet: install Tectonic in Settings → Optional tools.';
      var list = $('write-docs');
      list.replaceChildren();
      (data.documents || []).forEach(function (d) {
        var li = document.createElement('li');
        var open = document.createElement('button');
        open.className = 'link'; open.textContent = d.name; open.dir = 'auto';
        open.onclick = function () { openWriteDoc(d.id); };
        var meta = document.createElement('span');
        meta.className = 'muted';
        meta.textContent = (data.templates[d.template] || d.template) + ' · edited ' + String(d.updated_at).slice(0, 16).replace('T', ' ');
        li.appendChild(open); li.appendChild(meta);
        list.appendChild(li);
      });
      if (!(data.documents || []).length) {
        var empty = document.createElement('li');
        empty.className = 'muted'; empty.textContent = 'No documents yet.';
        list.appendChild(empty);
      }
    }).catch(function (e) {
      var list = $('write-docs');
      list.replaceChildren();
      var li = document.createElement('li');
      li.className = 'muted';
      li.textContent = e.status === 404 ? 'Choose or create a project first (sidebar → Projects).' : e.message;
      list.appendChild(li);
    });
  }

  function leaveWrite() {
    if (W.doc && W.dirty) saveWriteNow();
  }

  function createWriteDoc() {
    var name = $('write-new-name').value.trim();
    if (!name) { toast('Give the document a name'); return Promise.resolve(); }
    return post('/api/write/create', { project: projectName(), name: name, template: $('write-new-template').value })
      .then(function (doc) { $('write-new-name').value = ''; return openWriteDoc(doc.id); })
      .catch(function (e) { toast(e.message); });
  }

  function fillWriteFileList(doc) {
    var select = $('write-file');
    select.replaceChildren();
    doc.files.filter(function (f) { return f.kind === 'text'; }).forEach(function (f) {
      var opt = document.createElement('option');
      opt.value = f.name; opt.textContent = f.name;
      select.appendChild(opt);
    });
    select.value = W.file;
  }

  function openWriteDoc(id) {
    return api('/api/write/doc?doc=' + encodeURIComponent(id)).then(function (doc) {
      W.doc = doc;
      resetChat();  // one conversation per open document
      $('write-home').hidden = true; $('write-editor').hidden = false;
      $('write-title').textContent = doc.name; $('write-title').dir = 'auto';
      fillWriteFileList(doc);
      $('write-errors').replaceChildren();
      showWritePdf(doc.pdf);
      fillWriteSettings(doc);
      return openWriteFile(doc.main_file || 'main.tex');
    }).catch(function (e) { toast(e.message); });
  }

  // Unsaved text is never dropped: the switch waits for the save and is refused while a conflict or a failed
  // save leaves text that is only in the editor. `discard` is for "Reload theirs" and restore, which mean it.
  // Resolves to true when the file was opened.
  // `unchanged` (a reload): the editor text it may replace; if the user typed meanwhile, the editor is kept.
  function openWriteFile(name, discard, unchanged) {
    var doc = W.doc;
    var load = function () {
      if (!discard && (W.dirty || W.conflictHash)) {
        $('write-file').value = W.file;
        toast(W.conflictHash ? 'Resolve the conflict first (reload or keep mine).' : 'Not saved yet; try again in a moment.');
        return false;
      }
      var fileReq = api('/api/write/file?doc=' + encodeURIComponent(W.doc.id) + '&name=' + encodeURIComponent(name));
      // The notes come with the file, before any edit could move their lines.
      var notesReq = api('/api/write/notes?doc=' + encodeURIComponent(W.doc.id) + '&file=' + encodeURIComponent(name))
        .catch(function () { return { notes: [] }; });
      return Promise.all([fileReq, notesReq])
        .then(function (both) {
          if (unchanged != null && (W.doc !== doc || W.file !== name || !W.editor || W.editor.getText() !== unchanged)) {
            return false;  // typed (or moved on) while it loaded: a later save shows the usual reload/keep choice
          }
          var file = both[0];
          dropWriteSuggestion();
          $('write-versions').hidden = true; VERS.selected = null;  // versions belong to the file that was open
          if (W.editor) W.editor.destroy();
          W.file = file.name; W.hash = file.hash; W.dirty = false; W.conflictHash = null;
          $('write-file').value = file.name;
          $('write-conflict').hidden = true;
          W.editor = makeEditor($('write-cm'), file.text, {
            change: writeChanged, blur: function () { if (W.dirty) saveWriteNow(); }, compile: compileWrite,
            cursor: function () { if (!$('write-outline').hidden) scheduleWriteStats(150); },
            note: openWriteNote, hover: writeHover
          });
          closeWriteNote(); closeWriteSymbols();
          NOTES.token = null; NOTES.list = (both[1] && both[1].notes) || [];
          showWriteNotes();
          writeSaveState('Saved');
          loadOtherTexts();
          return true;
        });
    };
    return (W.dirty && !discard ? saveWriteNow() : Promise.resolve()).then(load)
      .catch(function (e) { toast(e.message); return false; });
  }

  // The other .tex files of the document, read once per file switch, so \ref{ also offers their labels.
  function loadOtherTexts() {
    var doc = W.doc, names = (doc.files || []).filter(function (f) {
      return f.kind === 'text' && /\.tex$/i.test(f.name) && f.name !== W.file;
    }).map(function (f) { return f.name; });
    var token = {};  // only the newest load may fill the cache (switching files quickly starts several)
    W.otherTexts = []; W.otherTextsToken = token;
    W.otherTextsLoading = Promise.all(names.map(function (name) {
      return api('/api/write/file?doc=' + encodeURIComponent(doc.id) + '&name=' + encodeURIComponent(name))
        .then(function (f) { return f.text; }).catch(function () { return ''; });
    })).then(function (texts) { if (W.doc === doc && W.otherTextsToken === token) W.otherTexts = texts; });
    return W.otherTextsLoading;
  }

  function writeChanged() {
    W.dirty = true;
    writeSaveState('Edited');
    scheduleWriteStats(400);  // before the save timer, which must stay the last one set
    clearTimeout(W.timer);
    W.timer = setTimeout(saveWriteNow, W.SAVE_DELAY);
  }

  function saveWriteNow(force) {
    clearTimeout(W.timer);
    if (!W.doc || !W.editor) return Promise.resolve();
    if (W.conflictHash && !force) return Promise.resolve();  // wait for the user's choice
    if (W.saving) return W.saving.then(function () { return W.dirty ? saveWriteNow(force) : null; });
    var text = W.editor.getText();
    var doc = W.doc;
    writeSaveState('Saving…');
    W.saving = post('/api/write/save', { doc: doc.id, name: W.file, text: text, hash: W.hash })
      .then(function (r) {
        if (W.doc !== doc) return;
        W.hash = r.hash; W.conflictHash = null;
        W.dirty = W.editor.getText() !== text;
        $('write-conflict').hidden = true;
        writeSaveState(W.dirty ? 'Edited' : 'Saved');
        if (W.dirty) W.timer = setTimeout(saveWriteNow, W.SAVE_DELAY);
        else syncWriteNotes();  // the saved text is what the notes' new lines refer to
      })
      .catch(function (e) {
        if (e.status === 409) {
          return api('/api/write/file?doc=' + encodeURIComponent(doc.id) + '&name=' + encodeURIComponent(W.file))
            .then(function (current) {
              W.conflictHash = current.hash;
              $('write-conflict').hidden = false;
              writeSaveState('Conflict — reload or keep mine');
            });
        }
        writeSaveState('Not saved: ' + e.message);
      })
      .then(function () { W.saving = null; });
    return W.saving;
  }

  function keepMine() {
    W.hash = W.conflictHash;
    return saveWriteNow(true);
  }

  function reloadTheirs() {
    return openWriteFile(W.file, true);
  }

  function showWritePdf(ready) {
    var frame = $('write-pdf'), url = '/api/write/pdf?doc=' + encodeURIComponent(W.doc.id) + '&t=' + Date.now();
    $('write-pdf-empty').hidden = !!ready;
    if (window.pdfjsLib && ready) {  // PDF.js: pages we can click on and draw highlights over
      frame.hidden = true; frame.removeAttribute && frame.removeAttribute('src');
      $('write-pdfview').hidden = false;
      $('write-pdf-open').href = url;
      return loadPdf(url);
    }
    $('write-pdfview').hidden = true;
    frame.hidden = !ready;
    if (ready) frame.src = url;
    return Promise.resolve();
  }

  /* Spell check: words from latex_words.js, checked in a worker (vendor/spell.worker.js) against the English and
     Persian dictionaries; misspelled words are underlined, right-click offers suggestions. */
  var SPELL = { on: true, cache: new Map(), worker: null, timer: null, seq: 0, waiting: {}, spans: [], view: null };

  // What to underline and which words to ask the worker about. The whole text is read so a formula or comment
  // that starts above the screen is still known; only words inside `ranges` ([from, to] pairs) are reported.
  function spellQuery(text, ranges, cache) {
    var spans = [], unknown = [], asked = new Set();
    window.LitForgeWords.latexWords(text, 0).forEach(function (w) {
      if (!ranges.some(function (r) { return w.from < r[1] && w.to > r[0]; })) return;
      var key = w.lang + ':' + w.word, known = cache.get(key);
      if (known === false) spans.push({ from: w.from, to: w.to, word: w.word, lang: w.lang });
      else if (known === undefined && !asked.has(key)) { asked.add(key); unknown.push({ word: w.word, lang: w.lang }); }
    });
    return { spans: spans, unknown: unknown };
  }

  function spellMenu(word, suggestions) {
    var items = suggestions.length ? suggestions.map(function (s) { return { label: s, action: 'replace', text: s }; }) :
      [{ label: 'No suggestions', action: 'none' }];
    return items.concat([{ label: 'Add to my dictionary', action: 'add' }, { label: 'Ignore', action: 'ignore' }]);
  }

  function applySpellChoice(item, from, to, word, lang, editor) {
    if (item.action === 'replace') {
      editor = editor || W.editor;
      if (W.editor !== editor || editor.getText().slice(from, to) !== word) {
        toast('The text changed; right-click the word again.');
        return;
      }
      editor.replace(from, to, item.text);
      return;
    }
    if (item.action === 'add') {
      var words = store.get('spell-words', []);
      if (words.indexOf(word) < 0) words = words.concat([word]);
      store.set('spell-words', words);
      if (SPELL.worker) SPELL.worker.postMessage({ type: 'personal', words: words });
    }
    if (item.action === 'add' || item.action === 'ignore') {
      SPELL.cache.set(lang + ':' + word, true);  // "ignore" lasts until the page is reloaded
      if (SPELL.view) spellRefresh(SPELL.view);
    }
  }

  function setSpell(on) {
    SPELL.on = !!on;
    store.set('spell', SPELL.on);
    $('write-spell').setAttribute('aria-pressed', String(SPELL.on));
    if (SPELL.view) { if (SPELL.on) spellRefresh(SPELL.view); else spellShow(SPELL.view, []); }
  }

  function spellWorker() {
    if (SPELL.worker || !window.Worker) return SPELL.worker;
    SPELL.worker = new Worker('/static/vendor/spell.worker.js');
    SPELL.worker.postMessage({ type: 'personal', words: store.get('spell-words', []) });
    SPELL.worker.onmessage = function (e) {
      var m = e.data || {};
      if (m.type === 'checked') {
        if (SPELL.cache.size > 20000) SPELL.cache.clear();  // bounded, like the worker's
        m.results.forEach(function (r) { SPELL.cache.set(r[0], r[1]); });
        if (SPELL.view) spellRefresh(SPELL.view, true);
      } else if (m.type === 'suggested' && SPELL.waiting[m.id]) {
        SPELL.waiting[m.id](m.list); delete SPELL.waiting[m.id];
      }
    };
    return SPELL.worker;
  }

  function spellShow(view, spans) {
    SPELL.spans = spans;
    if (view && view.litforgeSpell) view.litforgeSpell(spans);
  }

  // Check the visible part of the document (after a short pause in typing).
  function spellRefresh(view, now) {
    SPELL.view = view;
    clearTimeout(SPELL.timer);
    var run = function () {
      if (!SPELL.on) { spellShow(view, []); return; }
      var q = spellQuery(view.state.doc.toString(), view.visibleRanges.map(function (r) { return [r.from, r.to]; }),
        SPELL.cache);
      spellShow(view, q.spans);
      if (q.unknown.length && spellWorker()) SPELL.worker.postMessage({ type: 'check', words: q.unknown });
    };
    if (now) run(); else SPELL.timer = setTimeout(run, 400);
  }

  function spellSuggest(word, lang) {
    if (!spellWorker()) return Promise.resolve([]);
    var id = ++SPELL.seq;
    return new Promise(function (resolve) {
      SPELL.waiting[id] = resolve;
      SPELL.worker.postMessage({ type: 'suggest', id: id, word: word, lang: lang });
      setTimeout(function () { if (SPELL.waiting[id]) { delete SPELL.waiting[id]; resolve([]); } }, 5000);
    });
  }

  // Where a menu of size w×h opens for a click at (x, y): below it, or above when there is no room, inside the pane.
  function menuPosition(pane, x, y, w, h) {
    var left = Math.max(6, Math.min(x - pane.left, pane.width - w - 6));
    var top = y - pane.top + 4;
    if (top + h > pane.height - 6) top = Math.max(6, y - pane.top - h - 4);
    return { left: Math.round(left), top: Math.round(top) };
  }

  // Right-click on an underlined word: suggestions, add to the dictionary, ignore.
  function openSpellMenu(span, x, y) {
    var menu = $('write-spell-menu'), editor = W.editor;
    return spellSuggest(span.word, span.lang).then(function (list) {
      if (W.editor !== editor) return;  // the file changed while the suggestions were prepared
      var pane = menu.parentNode.getBoundingClientRect();
      menu.replaceChildren();
      spellMenu(span.word, list).forEach(function (item) {
        var b = document.createElement('button');
        b.type = 'button'; b.setAttribute('role', 'menuitem'); b.textContent = item.label; b.dir = 'auto';
        b.disabled = item.action === 'none';
        if (item.action === 'add' || item.action === 'ignore') b.className = 'spell-extra' + (item.action === 'add' ? ' spell-sep' : '');
        b.onclick = function () {
          menu.hidden = true;
          applySpellChoice(item, span.from, span.to, span.word, span.lang, editor);
        };
        menu.appendChild(b);
      });
      menu.style.left = '0px'; menu.style.top = '0px';
      menu.hidden = false;  // shown first so its size is known
      var at = menuPosition(pane, x, y, menu.offsetWidth, menu.offsetHeight);
      menu.style.left = at.left + 'px'; menu.style.top = at.top + 'px';
      var first = menu.querySelector('button:not([disabled])');
      if (first) first.focus();
    });
  }

  /* Assistant: questions about the text and the project's papers; answers carry verified quotes; an edit of the
     selection comes back as an ordinary suggestion (accept or reject). One conversation per open document. */
  var CHAT = { turns: [], busy: false };

  function chatContext(text, pos) {  // a few lines on each side of the cursor when nothing is selected
    var lines = text.split('\n'), at = text.slice(0, pos).split('\n').length - 1;
    return lines.slice(Math.max(0, at - 8), at + 9).join('\n').slice(0, 6000);
  }

  function resetChat() {
    CHAT.turns = []; CHAT.busy = false;
    $('write-chat-log').replaceChildren();
    $('write-chat-status').textContent = '';
  }

  function toggleChat(open) {
    var chat = $('write-chat');
    chat.hidden = open === undefined ? !chat.hidden : !open;
    $('write-chat-btn').setAttribute('aria-pressed', String(!chat.hidden));
    if (!chat.hidden && $('write-chat-input').focus) $('write-chat-input').focus();
  }

  function addChatMessage(role, text, citations, flags) {
    var msg = document.createElement('div');
    msg.className = 'chat-msg ' + role;
    var body = document.createElement('p');
    body.className = 'chat-text'; body.dir = 'auto'; body.textContent = text;
    msg.appendChild(body);
    if (citations && citations.length) {
      var sources = document.createElement('div');
      sources.className = 'chat-sources';
      citations.forEach(function (c) {
        var src = document.createElement('div');
        src.className = 'src'; src.dir = 'auto';
        src.textContent = c.key + ' · p.' + c.page + ' · “' + c.quote + '”';
        var insert = document.createElement('button');
        insert.type = 'button'; insert.className = 'btn small'; insert.textContent = 'Insert \\cite';
        insert.title = 'Cite ' + c.key + ' at the cursor';
        insert.onclick = function () { return insertChatCitation(c); };
        sources.appendChild(src); sources.appendChild(insert);
      });
      msg.appendChild(sources);
    }
    (flags || []).forEach(function (f) {
      var p = document.createElement('p'); p.className = 'flag'; p.textContent = '⚠ ' + f; msg.appendChild(p);
    });
    $('write-chat-log').appendChild(msg);
    if (msg.scrollIntoView) msg.scrollIntoView({ block: 'end' });
    return msg;
  }

  function insertChatCitation(c) {
    if (!W.doc || !W.editor) return Promise.resolve();
    var editor = W.editor;
    return post('/api/write/cite', { doc: W.doc.id, paper_id: c.paper_id, evidence_id: c.evidence_id || null })
      .then(function (res) {
        if (W.editor !== editor) { toast('Not inserted: ' + res.key + ' (the file changed)'); return; }
        var edit = insertCiteKey(editor.getText(), editor.selection().to, res.key);
        if (edit) editor.replace(edit.from, edit.to, edit.insert);
      }).catch(function (e) { toast(e.message); });
  }

  function pollChat(job) {
    return api('/api/jobs/' + encodeURIComponent(job.id)).then(function (j) {
      if (j.status === 'running') {
        return new Promise(function (resolve) { setTimeout(resolve, 800); }).then(function () { return pollChat(job); });
      }
      if (j.status === 'failed') throw new Error(j.error || 'The assistant could not answer');
      return j.report;
    });
  }

  // The request is built once: an approval covers exactly what was shown, even if the selection, the document or
  // the input change while the dialog is open.
  function sendChat() {
    var input = $('write-chat-input'), question = input.value.trim();
    if (!question || !W.doc || !W.editor || CHAT.busy) return Promise.resolve();
    var doc = W.doc, file = W.file, sel = W.editor.selection(), text = W.editor.getText();
    // With nothing selected, text the assistant offers to add goes in at the cursor, on its own lines; the text
    // around it is checked again before applying.
    var at = sel.from, range = sel.text ? { from: sel.from, to: sel.to, text: sel.text, doc: doc.id, file: file } :
      { from: at, to: at, text: '', doc: doc.id, file: file, before: text.slice(Math.max(0, at - 40), at),
        after: text.slice(at, at + 40) };
    var lineStart = at === 0 || text.charAt(at - 1) === '\n', lineEnd = at === text.length || text.charAt(at) === '\n';
    var body = { doc: doc.id, question: question, selection: sel.text || '', context: sel.text ? '' : chatContext(text, sel.from),
      history: CHAT.turns.slice(-8), file: file, approved: false, pad_before: !sel.text && !lineStart,
      pad_after: !sel.text && !lineEnd };
    CHAT.busy = true;
    $('write-chat-status').textContent = 'Preparing…';
    return postChat(body, { question: question, doc: doc, file: file, range: range })
      .catch(function (e) { $('write-chat-status').textContent = e.message; })
      .then(function (r) { CHAT.busy = false; return r; });
  }

  function postChat(body, ask) {
    var input = $('write-chat-input'), question = ask.question, doc = ask.doc, file = ask.file, range = ask.range;
    return post('/api/write/chat', body).then(function (res) {
      if (res.needs_approval) {
        var p = res.preview;
        var ok = window.confirm('Send your question, ' + res.sent.characters + ' characters of your text' +
          (res.sent.evidence ? ' and ' + res.sent.evidence + ' passages from the papers' : '') + ' to ' + p.model + ' (' +
          p.provider + ', cloud' + (p.max_cost_usd != null ? ', at most $' + p.max_cost_usd.toFixed(4) : '') + ')?');
        if (!ok) { $('write-chat-status').textContent = 'Not sent.'; return null; }
        return postChat(Object.assign({}, body, { approved: true }), ask);
      }
      if (W.doc !== doc) return null;  // another document is open now
      if (input.value.trim() === question) input.value = '';  // keep anything typed meanwhile
      addChatMessage('user', question);
      CHAT.turns.push({ role: 'user', text: question });
      $('write-chat-status').textContent = 'Asking ' + (res.preview.model || res.preview.profile) +
        (res.preview.remote ? ' (cloud)' : ' (local)') + '…';
      return pollChat(res).then(function (report) {
        if (W.doc !== doc) return;  // another document is open now
        addChatMessage('assistant', report.answer || '(no answer)', report.citations, report.flags);
        CHAT.turns.push({ role: 'assistant', text: report.answer || '' });
        $('write-chat-status').textContent = '';
        if (report.suggestion && range && W.file === file) showWriteSuggestion(report.suggestion, range);
      });
    });
  }

  /* PDF viewer (PDF.js) and SyncTeX jumps. PDF positions are points from the page's top-left corner. */
  // loadId counts loaded files (a newer compile wins); request counts renders (zoom re-renders the same file).
  var PDFV = { doc: null, pages: [], scale: 0, fit: true, request: 0, loadId: 0 };

  function loadPdf(url) {
    var load = ++PDFV.loadId, box = $('write-pdf-pages');
    var keep = box.scrollHeight ? box.scrollTop / box.scrollHeight : 0;  // stay at the same place after a recompile
    return window.pdfjsLib.getDocument({ url: url, isEvalSupported: false }).promise.then(function (pdf) {
      if (load !== PDFV.loadId) { pdf.destroy(); return null; }  // a newer compile's PDF is on its way
      if (PDFV.doc) PDFV.doc.destroy();
      PDFV.doc = pdf;
      PDFV.request++;
      return renderPdfPages().then(function () { box.scrollTop = keep * box.scrollHeight; });
    }).catch(function (e) { toast('The PDF could not be shown: ' + e.message); });
  }

  function renderPdfPages() {
    var pdf = PDFV.doc, box = $('write-pdf-pages');
    if (!pdf) return Promise.resolve();
    var request = PDFV.request, ratio = window.devicePixelRatio || 1, pages = [];
    return pdf.getPage(1).then(function (first) {
      var base = first.getViewport({ scale: 1 });
      if (PDFV.fit || !PDFV.scale) PDFV.scale = Math.max(0.3, (box.clientWidth - 24) / base.width);
      var chain = Promise.resolve();
      for (var n = 1; n <= pdf.numPages; n++) {
        (function (number) {
          chain = chain.then(function () { return pdf.getPage(number); }).then(function (page) {
            if (request !== PDFV.request) return;
            var view = page.getViewport({ scale: PDFV.scale });
            var wrap = document.createElement('div'), canvas = document.createElement('canvas');
            wrap.className = 'pdf-page'; wrap.dataset.page = String(number);
            wrap.style.width = Math.floor(view.width) + 'px'; wrap.style.height = Math.floor(view.height) + 'px';
            canvas.width = Math.floor(view.width * ratio); canvas.height = Math.floor(view.height * ratio);
            canvas.style.width = Math.floor(view.width) + 'px'; canvas.style.height = Math.floor(view.height) + 'px';
            wrap.appendChild(canvas);
            pages.push({ wrap: wrap, number: number });
            return page.render({ canvasContext: canvas.getContext('2d'), viewport: view,
              transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null }).promise;
          });
        })(n);
      }
      return chain.then(function () {
        if (request !== PDFV.request) return;
        box.replaceChildren.apply(box, pages.map(function (p) { return p.wrap; }));
        PDFV.pages = pages;
        showPdfPageNumber();
      });
    });
  }

  function zoomPdf(factor) {
    if (!PDFV.doc) return Promise.resolve();
    PDFV.fit = factor === 0;
    if (factor) PDFV.scale = Math.min(4, Math.max(0.3, PDFV.scale * factor));
    PDFV.request++;
    return renderPdfPages();
  }

  function showPdfPageNumber() {
    var box = $('write-pdf-pages'), mid = box.scrollTop + box.clientHeight / 2, current = 1;
    PDFV.pages.forEach(function (p) { if (p.wrap.offsetTop <= mid) current = p.number; });
    $('write-pdf-page').textContent = PDFV.pages.length ? current + ' / ' + PDFV.pages.length : '';
  }

  // Fit mode follows the pane: dragging the divider or switching layout changes its width.
  function needsRefit(oldWidth, newWidth, fit) {
    return fit && oldWidth > 0 && newWidth > 0 && Math.abs(newWidth - oldWidth) > 24;
  }

  function pdfPoint(rect, clientX, clientY, scale) {
    return { x: Math.round((clientX - rect.left) / scale * 100) / 100,
      y: Math.round((clientY - rect.top) / scale * 100) / 100 };
  }

  function cursorLine(text, pos) { return text.slice(0, pos).split('\n').length; }

  // Editor → PDF: scroll to the cursor's line and highlight it for a moment.
  function syncToPdf() {
    if (!W.doc || !W.editor) return Promise.resolve();
    if (!PDFV.pages.length) { toast('Compile first: the PDF has no position data yet.'); return Promise.resolve(); }
    var line = cursorLine(W.editor.getText(), W.editor.selection().from);
    return api('/api/write/synctex?doc=' + encodeURIComponent(W.doc.id) + '&file=' + encodeURIComponent(W.file) +
      '&line=' + line).then(function (hit) {
      var page = hit && PDFV.pages[hit.page - 1];
      if (!page) { toast('This line is not in the PDF (recompile after changes).'); return; }
      (page.wrap.querySelectorAll ? Array.from(page.wrap.querySelectorAll('.pdf-mark')) : []).forEach(function (m) { m.remove(); });
      var first = null;
      hit.boxes.forEach(function (b) {
        var mark = document.createElement('div');
        mark.className = 'pdf-mark';
        mark.style.left = Math.round(b.x * PDFV.scale) + 'px'; mark.style.top = Math.round(b.y * PDFV.scale) + 'px';
        mark.style.width = Math.round(b.w * PDFV.scale) + 'px'; mark.style.height = Math.round(b.h * PDFV.scale) + 'px';
        page.wrap.appendChild(mark);
        first = first || mark;
        setTimeout(function () { mark.remove && mark.remove(); }, 2500);
      });
      (first && first.scrollIntoView ? first : page.wrap).scrollIntoView({ block: 'center', behavior: 'smooth' });
    }).catch(function (e) { toast(e.message); });
  }

  // PDF → editor: the source line of a place in the PDF.
  function syncFromPdf(page, x, y) {
    if (!W.doc) return Promise.resolve();
    var doc = W.doc;
    return api('/api/write/synctex?doc=' + encodeURIComponent(doc.id) + '&page=' + page + '&x=' + x + '&y=' + y)
      .then(function (hit) {
        if (!hit || W.doc !== doc) return;  // the user moved to another document meanwhile
        var open = hit.file !== W.file ? openWriteFile(hit.file) : Promise.resolve(true);
        return open.then(function (ok) { if (ok && W.editor) W.editor.gotoLine(hit.line); });
      }).catch(function (e) { toast(e.message); });
  }

  function wirePdfView() {
    var box = $('write-pdf-pages');
    box.addEventListener('dblclick', function (e) {
      var wrap = e.target.closest && e.target.closest('.pdf-page');
      if (!wrap) return;
      var p = pdfPoint(wrap.getBoundingClientRect(), e.clientX, e.clientY, PDFV.scale);
      syncFromPdf(Number(wrap.dataset.page), p.x, p.y);
    });
    box.addEventListener('scroll', showPdfPageNumber);
    if (window.ResizeObserver) {
      var width = 0, timer = null;
      new ResizeObserver(function () {
        var now = box.clientWidth;
        if (!needsRefit(width, now, PDFV.fit)) { if (!width) width = now; return; }
        clearTimeout(timer);
        timer = setTimeout(function () { width = box.clientWidth; zoomPdf(0); }, 200);
      }).observe(box);
    }
    $('write-pdf-in').onclick = function () { zoomPdf(1.2); };
    $('write-pdf-out').onclick = function () { zoomPdf(1 / 1.2); };
    $('write-pdf-fit').onclick = function () { zoomPdf(0); };
    $('write-sync').onclick = syncToPdf;
    window.addEventListener('pdfjs-ready', function () { if (W.doc && W.doc.pdf) showWritePdf(true); });
  }

  function renderWriteMessages(report) {
    var list = $('write-errors');
    list.replaceChildren();
    [['error', report.errors || []], ['warning', report.warnings || []]].forEach(function (group) {
      group[1].forEach(function (item) {
        var li = document.createElement('li');
        li.className = group[0];
        li.textContent = (item.line ? (item.file || 'main.tex') + ':' + item.line + ' · ' : '') + item.message;
        if (item.line) li.onclick = function () { jumpToWriteLine(item); };
        li.dir = 'auto';
        list.appendChild(li);
      });
    });
  }

  function jumpToWriteLine(item) {
    W.lastError = item;
    var target = item.file && item.file !== W.file ? openWriteFile(item.file) : Promise.resolve(true);
    return target.then(function (opened) { if (opened && W.editor) W.editor.gotoLine(item.line); });
  }

  function compileWrite() {
    if (!W.doc || W.job) return Promise.resolve();
    var doc = W.doc;
    $('write-compile-state').textContent = 'Saving…';
    return saveWriteNow().then(function () {
      if (W.conflictHash) { $('write-compile-state').textContent = 'Resolve the conflict first.'; return; }
      $('write-compile-state').textContent = 'Compiling…';
      return post('/api/write/compile', { doc: doc.id }).then(function (job) {
        W.job = job;
        return pollWriteJob(job, doc);
      });
    }).catch(function (e) { W.job = null; $('write-compile-state').textContent = e.message; });
  }

  function pollWriteJob(job, doc) {
    return api('/api/jobs/' + encodeURIComponent(job.id)).then(function (j) {
      if (j.status === 'running') {
        var said = j.messages || [];
        if (W.doc === doc && said.length) $('write-compile-state').textContent = said[said.length - 1];
        return new Promise(function (resolve) { setTimeout(resolve, 700); }).then(function () { return pollWriteJob(job, doc); });
      }
      W.job = null;
      if (W.doc !== doc) return;
      var report = j.report || {};
      if (j.status === 'failed') { $('write-compile-state').textContent = j.error || 'Compile failed'; return; }
      renderWriteMessages(report);
      $('write-compile-state').textContent = report.ok ?
        'Compiled in ' + (report.elapsed_ms / 1000).toFixed(1) + ' s · ' + report.engine +
        (report.warnings && report.warnings.length ? ' · ' + report.warnings.length + ' warning(s)' : '') :
        (report.errors || []).length + ' error(s) — click one to jump to its line';
      if (report.ok) showWritePdf(true);
    });
  }

  /* Versions: the automatic versions kept while saving, named versions, and what changed. */
  var VERS = { selected: null, list: [], request: 0 };

  function versionLabel(s) {
    return String(s.created_at || s.id).replace('T', ' ').slice(0, 19) + ((s.name || s.note) ? ' · ' + (s.name || s.note) : '');
  }

  function changeCounts(s) {
    if (s.added == null && s.removed == null) return '';
    return (s.added || s.removed) ? '+' + (s.added || 0) + ' −' + (s.removed || 0) : 'no change';
  }

  function showWriteVersions() {
    var box = $('write-versions');
    if (!box.hidden) { box.hidden = true; return Promise.resolve(); }
    return saveWriteNow().then(loadVersions).then(function () {
      box.hidden = false;
      var changed = VERS.list.filter(function (v) { return v.added || v.removed; })[0] || VERS.list[0];
      if (changed) return selectVersion(changed.id);
    }).catch(function (e) { toast(e.message); });
  }

  function loadVersions() {
    return api('/api/write/snapshots?doc=' + encodeURIComponent(W.doc.id)).then(function (data) {
      VERS.list = data.snapshots || [];
      var list = $('write-version-list');
      list.replaceChildren();
      VERS.list.forEach(function (s) {
        var li = document.createElement('li');
        li.className = 'ver-row' + (s.name ? ' named' : '') + (s.id === VERS.selected ? ' on' : '');
        var label = document.createElement('span');
        label.className = 'ver-label'; label.dir = 'auto';
        label.textContent = versionLabel(s);
        var chip = document.createElement('small');
        chip.className = 'ver-chip'; chip.textContent = changeCounts(s);
        label.appendChild(chip);
        label.onclick = function () { return selectVersion(s.id); };
        var restore = document.createElement('button');
        restore.className = 'btn small'; restore.textContent = 'Restore';
        restore.onclick = function () { return restoreWriteVersion(s.id); };
        var name = document.createElement('button');
        name.className = 'btn small ghost'; name.textContent = s.name ? 'Rename' : 'Name';
        name.onclick = function () { return nameVersion(s); };
        li.appendChild(label); li.appendChild(restore); li.appendChild(name);
        list.appendChild(li);
      });
      if (!VERS.list.length) {
        var none = document.createElement('li'); none.textContent = 'No versions yet; they are kept as you save.';
        list.appendChild(none);
      }
      var against = $('write-version-against'), keep = against.value || 'previous';
      var opts = [['previous', 'The version before it'], ['current', 'Current text']].concat(VERS.list.map(function (s) { return [s.id, versionLabel(s)]; }));
      against.replaceChildren.apply(against, opts.map(function (o) {
        var opt = document.createElement('option'); opt.value = o[0]; opt.textContent = o[1]; return opt;
      }));
      var still = opts.some(function (o) { return o[0] === keep; });
      against.value = still ? keep : 'previous';
      // the version compared with was removed (rotation): show the differences for what is chosen now
      if (!still && VERS.selected) return selectVersion(VERS.selected);
    });
  }

  // The differences between version `id` and the "Compare with" choice (the current text by default).
  function selectVersion(id) {
    VERS.selected = id;
    var against = $('write-version-against').value || 'current', a = id, b = against;
    if (against === 'previous') {  // what changed in this version: the one before it → this one
      var at = VERS.list.map(function (s) { return s.id; }).indexOf(id), before = VERS.list[at + 1];
      if (at >= 0 && before) { a = before.id; b = id; } else { b = 'current'; }
    }
    (document.querySelectorAll('#write-version-list .ver-row') || []).forEach(function (row, i) {
      row.classList.toggle('on', VERS.list[i] && VERS.list[i].id === id);
    });
    var request = ++VERS.request, doc = W.doc, file = W.file;
    return api('/api/write/diff?doc=' + encodeURIComponent(W.doc.id) + '&a=' + encodeURIComponent(a) + '&b=' +
      encodeURIComponent(b) + '&file=' + encodeURIComponent(W.file)).then(function (d) {
      if (request !== VERS.request || W.doc !== doc || W.file !== file) return;  // a newer choice was made
      var target = $('write-version-diff'), box = renderDiff(d);
      target.replaceChildren.apply(target, box.children);
    }).catch(function (e) { toast(e.message); });
  }

  function renderDiff(d) {
    var box = document.createElement('div');
    var add = function (cls, text) {
      var el = document.createElement('div'); el.className = cls; el.textContent = text; box.appendChild(el); return el;
    };
    if (!d.hunks || !d.hunks.length) { add('diff-none', 'No differences in ' + (d.file || 'this file') + '.'); return box; }
    d.hunks.forEach(function (h) {
      add('diff-hunk', 'line ' + h.b_start + (h.b_lines > 1 ? '–' + (h.b_start + h.b_lines - 1) : ''));
      h.lines.forEach(function (l) {
        add('diff-line ' + (l.op === '+' ? 'add' : l.op === '-' ? 'del' : 'ctx'), l.text).dir = 'auto';
      });
    });
    if (d.truncated) add('diff-more', 'Showing only the first part of the differences.');
    return box;
  }

  function saveNamedVersion() {
    var input = $('write-version-name'), name = input.value.trim();
    return saveWriteNow().then(function () {
      if (W.dirty || W.conflictHash) { toast('Save or resolve the conflict first.'); return null; }
      return post('/api/write/snapshot', { doc: W.doc.id, note: 'named', name: name }).then(function (r) {
        input.value = '';
        toast(name ? 'Version saved: ' + name : 'Version saved');
        VERS.selected = r && r.id;
        return loadVersions().then(function () { if (VERS.selected) return selectVersion(VERS.selected); });
      });
    }).catch(function (e) { toast(e.message); });
  }

  function nameVersion(s) {
    var name = window.prompt('Name for this version (leave empty to remove the name)', s.name || '');
    if (name === null) return Promise.resolve();
    return post('/api/write/snapshot/name', { doc: W.doc.id, snapshot: s.id, name: name.trim() })
      .then(loadVersions).catch(function (e) { toast(e.message); });
  }

  function restoreWriteVersion(id) {
    if (!window.confirm('Restore this version? The current text is kept as a version first.')) return Promise.resolve();
    // Save what is in the editor first, so the version kept before restoring holds the latest text.
    return saveWriteNow().then(function () {
      if (W.dirty || W.conflictHash) { toast('Save or resolve the conflict before restoring.'); return null; }
      return post('/api/write/restore', { doc: W.doc.id, snapshot: id }).then(function () {
        $('write-versions').hidden = true;
        return openWriteFile(W.file, true);
      }).then(function () { toast('Version restored'); });
    }).catch(function (e) { toast(e.message); });
  }

  /* ---- git sync: connect a remote, sync both ways, resolve conflicting blocks ---- */

  var GIT = { status: null };

  // Blocks git left in a file it could not merge: <<<<<<< mine ======= theirs >>>>>>>.
  function conflictBlocks(text) {
    var out = [], m, re = /^<<<<<<< [^\n]*\n([\s\S]*?)^=======\n([\s\S]*?)^>>>>>>> [^\n]*(?:\n|$)/gm;
    while ((m = re.exec(text))) {
      out.push({ from: m.index, to: m.index + m[0].length, mine: m[1], theirs: m[2],
        line: text.slice(0, m.index).split('\n').length });
    }
    return out;
  }

  function keptText(block, choice) {
    return choice === 'mine' ? block.mine : choice === 'theirs' ? block.theirs : block.mine + block.theirs;
  }

  function resolveConflict(text, block, choice) {
    return text.slice(0, block.from) + keptText(block, choice) + text.slice(block.to);
  }

  function showWriteConflicts() {
    if (!W.editor) return;
    var blocks = conflictBlocks(W.editor.getText());
    $('write-conflicts').hidden = !blocks.length;
    if (!blocks.length) return;
    $('write-conflicts-msg').textContent = blocks.length + ' conflicting block' + (blocks.length === 1 ? '' : 's') +
      ' from the git sync in this file: keep your version, theirs, or both, then Sync again.';
    var rows = blocks.map(function (block, i) {
      var li = document.createElement('li'), label = document.createElement('span');
      label.textContent = 'Line ' + block.line + ': ' + block.mine.trim().slice(0, 60);
      label.title = 'Yours:\n' + block.mine + '\nTheirs:\n' + block.theirs; label.dir = 'auto';
      label.onclick = function () { W.editor.gotoLine(block.line); };
      li.appendChild(label);
      [['mine', 'Mine'], ['theirs', 'Theirs'], ['both', 'Both']].forEach(function (c) {
        var b = document.createElement('button');
        b.type = 'button'; b.textContent = c[1];
        b.onclick = function () {
          var now = conflictBlocks(W.editor.getText())[i];  // positions as they are now
          if (!now) return;
          W.editor.replace(now.from, now.to, keptText(now, c[0]));
          showWriteConflicts();
        };
        li.appendChild(b);
      });
      return li;
    });
    $('write-conflicts-list').replaceChildren.apply($('write-conflicts-list'), rows);
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function renderWriteGit(st) {
    GIT.status = st;
    var connected = !!st.remote;
    $('write-git-connect-row').hidden = connected;
    $('write-git-sync').hidden = !connected; $('write-git-disconnect').hidden = !connected;
    $('write-git-abort').hidden = !st.merging;
    $('write-git-state').textContent = connected ?
      st.remote + ' · ' + (st.branch || '?') + ' · ' + plural((st.changed || []).length, 'changed file') +
      (st.ahead ? ' · ' + st.ahead + ' to push' : '') + (st.behind ? ' · ' + st.behind + ' to pull' : '') +
      (st.last_sync ? ' · last sync ' + new Date(st.last_sync).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '') :
      'Not connected to a git remote.';
  }

  function showWriteGit() {
    var doc = W.doc;
    $('write-git').hidden = false; $('write-git-msg').textContent = '';
    return api('/api/write/git?doc=' + encodeURIComponent(doc.id)).then(function (st) {
      if (W.doc === doc) renderWriteGit(st);
    }).catch(function (e) { $('write-git-msg').textContent = e.message; });
  }

  function connectWriteGit() {
    var doc = W.doc, url = $('write-git-url').value.trim();
    if (!url) { $('write-git-msg').textContent = 'Paste the remote\'s URL first.'; return Promise.resolve(); }
    return post('/api/write/git/connect', { doc: doc.id, url: url }).then(function (st) {
      if (W.doc !== doc) return;
      renderWriteGit(st); $('write-git-msg').textContent = 'Connected. Sync now to send the document there.';
    }).catch(function (e) { $('write-git-msg').textContent = e.message; });
  }

  function waitJob(id) {
    return api('/api/jobs/' + encodeURIComponent(id)).then(function (j) {
      if (j.status !== 'running') return j;
      return new Promise(function (resolve) { setTimeout(resolve, 800); }).then(function () { return waitJob(id); });
    });
  }

  // The files may have changed on disk (pulled): the file list and the open file are read again.
  function reloadWriteFiles() {
    var doc = W.doc;
    var text = W.editor ? W.editor.getText() : null, file = W.file;
    return api('/api/write/doc?doc=' + encodeURIComponent(doc.id)).then(function (info) {
      if (W.doc !== doc) return;
      doc.files = info.files; fillWriteFileList(doc);
      return W.dirty || W.file !== file ? null : openWriteFile(file, true, text);
    }).then(showWriteConflicts);
  }

  function syncWriteGit() {
    var doc = W.doc, started = false;
    $('write-git-msg').textContent = 'Saving…';
    return saveWriteNow().then(function () {
      if (W.dirty || W.conflictHash) throw new Error('Save the file first (see the banner above).');
      $('write-git-msg').textContent = 'Syncing with the remote…';
      return post('/api/write/git/sync', { doc: doc.id });
    }).then(function (job) { started = true; return waitJob(job.id); }).then(function (j) {
      if (W.doc !== doc) return;
      if (j.status === 'failed') throw new Error(j.error || 'git sync failed');
      var r = j.report;
      renderWriteGit(r.status);
      $('write-git-msg').textContent = r.conflicts.length ?
        'Conflicts in ' + r.conflicts.join(', ') + ': choose a version for each marked block, then Sync again (or Abort merge).' :
        'Synced: ' + plural(r.pulled, 'commit') + ' pulled, ' + r.pushed + ' pushed.';
      return reloadWriteFiles();
    }).catch(function (e) {
      $('write-git-msg').textContent = e.message;
      // A failed push may follow a merge that already changed the files: show what is on disk now.
      if (started && W.doc === doc) {
        return api('/api/write/git?doc=' + encodeURIComponent(doc.id)).then(function (st) {
          if (W.doc === doc) renderWriteGit(st);
        }).catch(function () {}).then(reloadWriteFiles);
      }
    });
  }

  function abortWriteGit() {
    var doc = W.doc;
    return post('/api/write/git/abort', { doc: doc.id }).then(function (st) {
      renderWriteGit(st); $('write-git-msg').textContent = 'The merge was undone.';
      return reloadWriteFiles();
    }).catch(function (e) { $('write-git-msg').textContent = e.message; });
  }

  function disconnectWriteGit() {
    if (!window.confirm('Forget this document\'s git remote? Its history stays in the folder.')) return Promise.resolve();
    return post('/api/write/git/disconnect', { doc: W.doc.id }).then(renderWriteGit)
      .catch(function (e) { $('write-git-msg').textContent = e.message; });
  }

  function cloneWriteGit() {
    var url = (window.prompt('Git URL to clone (Overleaf\'s git link, GitHub, GitLab…). Your own git sign-in is used.') || '').trim();
    if (!url) return Promise.resolve();
    var name = url.replace(/\/+$/, '').split(/[/:]/).pop().replace(/\.git$/i, '') || 'Cloned';
    toast('Cloning ' + name + '…');
    return post('/api/write/git/clone', { project: projectName(), name: name, url: url })
      .then(function (job) { return waitJob(job.id); })
      .then(function (j) {
        if (j.status === 'failed') throw new Error(j.error || 'clone failed');
        return openWriteDoc(j.report.id);
      }).catch(function (e) { toast(e.message); });
  }

  /* ---- writing aids: math symbols, previews on hover, notes on lines ---- */

  var MATH_ENVS = 'equation|align|gather|multline|eqnarray|displaymath|math|alignat|flalign';
  var PREVIEW_MAX = 4000;  // characters of math sent for a preview (as on the server)

  // The math span (delimiters included) that `pos` is in, as LaTeX reads the text: escaped dollars, \\[ line
  // breaks and comments do not count.
  function mathSpanAt(text, pos) {
    var plain = maskComments(text), open = null, m;
    var re = new RegExp('\\$\\$|\\$|\\\\\\(|\\\\\\)|\\\\\\[|\\\\\\]|\\\\(begin|end)\\{(' + MATH_ENVS + ')(\\*?)\\}', 'g');
    while ((m = re.exec(plain))) {
      if (escapedAt(plain, m.index)) continue;
      var tok = m[0];
      if (!open) {
        if (m.index > pos) return null;
        if (tok === '$$' || tok === '$' || tok === '\\(' || tok === '\\[' || m[1] === 'begin') {
          open = { tok: tok, from: m.index, env: m[1] === 'begin' ? m[2] + m[3] : null };
        }
        continue;
      }
      var closes = open.env ? (m[1] === 'end' && m[2] + m[3] === open.env) :
        (open.tok === '$$' && tok === '$$') || (open.tok === '$' && tok === '$') ||
        (open.tok === '\\(' && tok === '\\)') || (open.tok === '\\[' && tok === '\\]');
      if (!closes) continue;
      var to = m.index + tok.length;
      if (open.from <= pos && pos < to) return { from: open.from, to: to };
      open = null;
    }
    return null;
  }

  function figureAt(text, pos) {
    var plain = maskComments(text), m, re = /\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}/g;
    while ((m = re.exec(plain))) {
      if (escapedAt(plain, m.index)) continue;
      if (m.index > pos) return null;
      if (pos < m.index + m[0].length) return { from: m.index, to: m.index + m[0].length, name: m[1].trim() };
    }
    return null;
  }

  function hoverPreview(text, pos) {
    var math = mathSpanAt(text, pos);
    if (math) return math.to - math.from > PREVIEW_MAX ? null : { kind: 'math', from: math.from, to: math.to, tex: text.slice(math.from, math.to) };
    var fig = figureAt(text, pos);
    return fig ? { kind: 'figure', from: fig.from, to: fig.to, name: fig.name } : null;
  }

  // What to insert for a palette symbol: $...$ around it outside math; the cursor goes into its first {}. A command
  // ending in a letter gets a space before a following letter (\alpha N, not the unknown \alphaN).
  function symbolInsert(command, inMath, next) {
    var text = inMath ? command : '$' + command + '$', brace = command.indexOf('{}');
    if (inMath && brace < 0 && /[a-zA-Z]$/.test(command) && /^[a-zA-Z]/.test(next || '')) text += ' ';
    return { text: text, cursor: brace >= 0 ? (inMath ? 0 : 1) + brace + 1 : text.length };
  }

  function openWriteSymbols() {
    if (!W.editor) return;
    var sel = W.editor.selection();
    $('write-symbols').hidden = false;
    placeWriteSuggestion({ to: sel.to }, $('write-symbols'));
    $('write-symbols-q').value = '';
    renderWriteSymbols();
    if ($('write-symbols-q').focus) $('write-symbols-q').focus();
  }

  function closeWriteSymbols() { $('write-symbols').hidden = true; }

  function renderWriteSymbols() {
    var found = window.LitForgeSymbols ? window.LitForgeSymbols.search($('write-symbols-q').value).slice(0, 160) : [];
    var buttons = found.map(function (sym) {
      var b = document.createElement('button');
      b.type = 'button'; b.textContent = sym.glyph; b.title = sym.command;
      b.setAttribute('aria-label', sym.command + ' (' + sym.words + ')');
      b.onclick = function () { insertWriteSymbol(sym.command); };
      return b;
    });
    $('write-symbols-grid').replaceChildren.apply($('write-symbols-grid'), buttons);
  }

  function insertWriteSymbol(command) {
    var text = W.editor.getText(), sel = W.editor.selection(), span = mathSpanAt(text, sel.from);
    var ins = symbolInsert(command, !!span && span.from < sel.from && sel.from < span.to, text.charAt(sel.to));
    W.editor.replace(sel.from, sel.to, ins.text);
    if (W.editor.select) W.editor.select(sel.from + ins.cursor, sel.from + ins.cursor);
    closeWriteSymbols();
  }

  /* Previews: math through KaTeX (already on the page); what KaTeX cannot draw, and PDF figures, through the
     server and PDF.js. */
  var PREVIEW = { cache: new Map() };

  // \newcommand, \renewcommand and \DeclareMathOperator lines of the document, as KaTeX macros.
  function katexMacros(texts) {
    var macros = {};
    texts.forEach(function (t) {
      stripComments(t || '').replace(/\\(?:re|provide)?newcommand\*?\s*\{?(\\[a-zA-Z]+)\}?\s*(?:\[\d\])?\s*\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}/g,
        function (all, name, body) { macros[name] = body; return all; })
        .replace(/\\DeclareMathOperator(\*?)\s*\{(\\[a-zA-Z]+)\}\s*\{([^{}]*)\}/g, function (all, star, name, body) {
          macros[name] = '\\operatorname' + star + '{' + body + '}'; return all;
        });
    });
    return macros;
  }

  function katexSource(tex) {
    var t = tex.replace(/\\label\{[^{}]*\}/g, '').replace(/\\(?:eq)?ref\{[^{}]*\}/g, '??').replace(/\\nonumber|\\notag/g, '');
    var m = /^\$\$([\s\S]*)\$\$$/.exec(t) || /^\\\[([\s\S]*)\\\]$/.exec(t);
    if (m) return { tex: m[1], display: true };
    if ((m = /^\$([\s\S]*)\$$/.exec(t) || /^\\\(([\s\S]*)\\\)$/.exec(t))) return { tex: m[1], display: false };
    return { tex: t.replace(/\\(begin|end)\{(?:equation|displaymath|math)\*?\}/g, ''), display: true };
  }

  // A formula's preview depends on the macros too; a figure file may be replaced, so its preview is kept briefly.
  function previewKey(docId, hit, macros) {
    return [docId, hit.kind, hit.tex || hit.name, hit.kind === 'math' ? macros : ''].join('\n');
  }

  function previewFresh(entry, now) { return entry.kind !== 'figure' || now - entry.at < 30000; }

  function renderWritePreview(hit, dom) {
    var doc = W.doc, macros = hit.kind === 'math' ? katexMacros([W.editor.getText()].concat(W.otherTexts || [])) : {};
    var key = previewKey(doc.id, hit, JSON.stringify(macros)), cached = PREVIEW.cache.get(key);
    if (cached && previewFresh(cached, Date.now())) { dom.replaceChildren(cached.node.cloneNode(true)); return Promise.resolve(); }
    var keep = function (node) {
      PREVIEW.cache.delete(key);
      if (PREVIEW.cache.size >= 60) PREVIEW.cache.delete(PREVIEW.cache.keys().next().value);
      PREVIEW.cache.set(key, { node: node.cloneNode(true), kind: hit.kind, at: Date.now() });
      dom.replaceChildren(node);
    };
    var fail = function (e) { dom.textContent = e.message || String(e); };
    if (hit.kind === 'math' && window.katex) {
      var src = katexSource(hit.tex), box = document.createElement('div'), drawn = true;
      try {
        window.katex.render(src.tex, box, { displayMode: src.display, throwOnError: true, macros: macros });
      } catch (e) { drawn = false; }  // not something KaTeX draws: the engine renders it below
      if (drawn) { keep(box); return Promise.resolve(); }
    }
    var request = hit.kind === 'math' ?
      fetch('/api/write/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc: doc.id, tex: hit.tex }) }) :
      fetch('/api/write/figure?doc=' + encodeURIComponent(doc.id) + '&name=' + encodeURIComponent(hit.name));
    return request.then(function (r) {
      if (!r.ok) return r.json().then(function (b) { throw new Error(b.error || 'no preview'); });
      var type = r.headers.get('Content-Type') || '';
      if (/pdf/.test(type)) return r.arrayBuffer().then(function (data) { return pdfPreviewImage(data, hit.kind === 'math'); });
      return r.blob().then(function (blob) {
        var img = document.createElement('img'); img.src = URL.createObjectURL(blob); img.alt = hit.name || '';
        return img;
      });
    }).then(keep).catch(fail);
  }

  // A formula is shown about one and a half times text size; a figure fits 340 pixels. Drawn at twice that for
  // sharp text on high-density screens.
  function pdfPreviewImage(data, formula) {
    if (!window.pdfjsLib) throw new Error('The PDF viewer is not loaded.');
    return window.pdfjsLib.getDocument({ data: new Uint8Array(data), isEvalSupported: false }).promise.then(function (pdf) {
      return pdf.getPage(1);
    }).then(function (page) {
      var base = page.getViewport({ scale: 1 }), shown = Math.min(340, base.width * (formula ? 1.6 : 340 / base.width));
      var viewport = page.getViewport({ scale: 2 * shown / base.width }), canvas = document.createElement('canvas');
      canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height);
      return page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise.then(function () {
        var img = document.createElement('img');
        img.src = canvas.toDataURL('image/png'); img.style.width = Math.round(viewport.width / 2) + 'px';
        return img;
      });
    });
  }

  // The tooltip CodeMirror shows while the pointer rests on math or an \includegraphics.
  function writeHover(view, pos) {
    var hit = hoverPreview(view.state.doc.toString(), pos);
    if (!hit || !W.doc) return null;
    return { pos: hit.from, end: hit.to, above: true, create: function () {
      var dom = document.createElement('div');
      dom.className = 'write-preview'; dom.textContent = 'Rendering…';
      renderWritePreview(hit, dom);
      return { dom: dom };
    } };
  }

  /* Notes: the user's own remarks on lines, stored by LitForge (never in the .tex file). A note keeps the text of
     its line, so it finds the line again after edits made elsewhere. */
  var NOTES = { list: [], editing: null, token: null, pending: null };  // pending: the mark of a note being written

  function writeNotes() { return NOTES.list; }

  function notesUrl() {
    return '/api/write/notes?doc=' + encodeURIComponent(W.doc.id) + '&file=' + encodeURIComponent(W.file);
  }

  // Reloads the notes; a reply for text that changed meanwhile would put them on old lines, so it is dropped.
  function loadWriteNotes() {
    var doc = W.doc, file = W.file, token = {}, text = W.editor ? W.editor.getText() : null;
    NOTES.token = token;
    return api(notesUrl()).then(function (res) {
      if (NOTES.token !== token || W.doc !== doc || W.file !== file || !W.editor || W.editor.getText() !== text) return;
      NOTES.list = res.notes || [];
      showWriteNotes();
    }).catch(function () {});
  }

  // The lines the editor's marks are on now (they move with the text); the plain text area keeps the stored ones.
  function markLines() {
    var lines = {};
    if (W.editor && W.editor.noteLines) W.editor.noteLines().forEach(function (n) { lines[n.id] = n.line; });
    return lines;
  }

  function showWriteNotes() {
    if (W.editor && W.editor.setNotes) {
      var lines = markLines(), marks = NOTES.list.map(function (n) { return { id: n.id, body: n.body, line: lines[n.id] || n.line }; });
      if (NOTES.pending) marks.push({ id: '__new', body: '', line: lines.__new || NOTES.pending.line });
      W.editor.setNotes(marks);
    }
    refreshWriteStats();
  }

  function currentNoteLines() {
    var lines = markLines();
    return NOTES.list.map(function (n) { return { note: n, line: lines[n.id] || n.line }; });
  }

  function lineText(line) {
    var r = W.editor.lineRange(line);
    return W.editor.getText().slice(r.from, r.to).trim().slice(0, 200);
  }

  function openWriteNote(line) {
    if (!W.editor) return;
    var found = currentNoteLines().filter(function (n) { return n.line === line; })[0];
    NOTES.editing = { line: line, id: found ? found.note.id : null };
    NOTES.pending = found ? null : { line: line };
    if (!found) showWriteNotes();
    $('write-note-title').textContent = 'Note · line ' + line;
    $('write-note-text').value = found ? found.note.body : '';
    $('write-note-delete').hidden = !found;
    $('write-note').hidden = false;
    placeWriteSuggestion({ to: W.editor.lineRange(line).from }, $('write-note'));
    if ($('write-note-text').focus) $('write-note-text').focus();
  }

  function closeWriteNote() {
    var hadPending = !!NOTES.pending;
    NOTES.editing = null; NOTES.pending = null; $('write-note').hidden = true;
    if (hadPending && W.editor) showWriteNotes();
  }

  function saveWriteNote() {
    var editing = NOTES.editing, doc = W.doc, file = W.file;
    if (!editing) return Promise.resolve();
    var body = $('write-note-text').value.trim();
    if (!body) { toast('Write a note, or delete it.'); return Promise.resolve(); }
    var line = markLines()[editing.id || '__new'] || editing.line;  // where its line is now, after any edits
    var req = { doc: doc.id, file: file, line: line, anchor: lineText(line), body: body };
    if (editing.id) req.id = editing.id;
    return post('/api/write/note', req).then(function (note) {
      if (W.doc !== doc || W.file !== file) return;
      NOTES.list = NOTES.list.filter(function (n) { return n.id !== note.id; }).concat([note]);
      closeWriteNote();
      showWriteNotes();
    }).catch(function (e) { toast(e.message); });
  }

  function deleteWriteNote() {
    var editing = NOTES.editing, doc = W.doc;
    if (!editing || !editing.id) { closeWriteNote(); return Promise.resolve(); }
    return post('/api/write/note/delete', { doc: doc.id, id: editing.id }).then(function () {
      NOTES.list = NOTES.list.filter(function (n) { return n.id !== editing.id; });
      closeWriteNote();
      showWriteNotes();
    }).catch(function (e) { toast(e.message); });
  }

  // After a save: store where notes are now and the text of their lines (it may have changed in place), so they
  // open there next time. Only an acknowledged change counts: a failed one is sent again after the next save.
  function syncWriteNotes() {
    if (!W.doc || !W.editor || !NOTES.list.length) return Promise.resolve();
    var changed = currentNoteLines().map(function (n) {
      return { note: n.note, line: n.line, anchor: lineText(n.line) };
    }).filter(function (c) { return c.line !== c.note.line || c.anchor !== c.note.anchor; });
    if (!changed.length) return Promise.resolve();
    return post('/api/write/notes/move', { doc: W.doc.id, file: W.file, notes: changed.map(function (c) {
      return { id: c.note.id, line: c.line, anchor: c.anchor };
    }) }).then(function () {
      changed.forEach(function (c) { c.note.line = c.line; c.note.anchor = c.anchor; });
    }).catch(function () {});
  }

  /* ---- outline, word count, compile settings ---- */

  var OUTLINE_LEVELS = { part: 0, chapter: 1, section: 2, subsection: 3, subsubsection: 4, paragraph: 5 };

  // A heading as plain text: citations and labels dropped, other commands unwrapped (\textbf{x} -> x).
  function headingText(tex) {
    return tex.replace(/\\(?:[a-zA-Z]*cite[a-zA-Z]*|label|ref|eqref|footnote)\*?(?:\[[^\]]*\])*\{[^{}]*\}/g, '')
      .replace(/\\([%&$#_{}])/g, '$1').replace(/\\[a-zA-Z]+\*?/g, '').replace(/[{}]/g, '')
      .replace(/\s+/g, ' ').trim();
  }

  // Sections of a file (as LaTeX reads it: commented ones do not count) and the files it includes.
  function outlineOf(text) {
    var plain = maskComments(text), items = [], m;
    var re = /\\(part|chapter|section|subsection|subsubsection|paragraph|input|include)\b\*?\s*(\[[^\]]*\])?\s*\{/g;
    var lineAt = function (pos) { return text.slice(0, pos).split('\n').length; };
    while ((m = re.exec(plain))) {
      if (escapedAt(plain, m.index)) continue;
      var open = m.index + m[0].length - 1, close = closingBrace(plain, open);
      if (close < 0) continue;
      var arg = plain.slice(open + 1, close);  // comments inside the heading are blanked out
      if (m[1] === 'input' || m[1] === 'include') {
        var name = arg.trim();
        if (name) items.push({ kind: 'file', name: /\.[a-z]+$/i.test(name) ? name : name + '.tex', line: lineAt(m.index) });
      } else {
        items.push({ kind: 'section', level: OUTLINE_LEVELS[m[1]], title: headingText(arg), line: lineAt(m.index) });
      }
      re.lastIndex = close + 1;
    }
    return items;
  }

  // The section the cursor's line is in (the last heading at or above it), or -1.
  function outlineAt(items, line) {
    var at = -1;
    items.forEach(function (item, i) { if (item.kind === 'section' && item.line <= line) at = i; });
    return at;
  }

  // Words of the text a reader sees: the document body without commands, math, citations, labels and comments.
  function countWords(text) {
    var t = stripComments(text).replace(/\\[$%&#_{}]/g, ' '), begin = t.indexOf('\\begin{document}'), end = t.indexOf('\\end{document}');
    if (end >= 0) t = t.slice(0, end);
    if (begin >= 0) t = t.slice(begin + '\\begin{document}'.length);
    t = t.replace(/\\begin\{(equation|align|gather|multline|eqnarray|math|displaymath)(\*?)\}[\s\S]*?\\end\{\1\2\}/g, ' ')
      .replace(/\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\\])*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]/g, ' ')
      .replace(/\\begin\{(?:tabular|array|tabularx)\*?\}(?:\[[^\]]*\])?\{(?:[^{}]|\{[^{}]*\})*\}/g, ' ')
      .replace(/\\(?:[a-zA-Z]*cite[a-zA-Z]*|[a-zA-Z]*ref|label|usepackage|documentclass|begin|end|includegraphics|input|include|bibliographystyle|bibliography|url|hspace|vspace|setlength|newcommand|renewcommand)(?![a-zA-Z])\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})?(?:\[[^\]]*\])*/g, ' ')
      .replace(/\\[a-zA-Z]+\*?/g, ' ').replace(/\\./g, ' ');
    var words = t.match(/[\p{L}\p{N}][\p{L}\p{N}\p{M}'\u2019\u200c-]*/gu);
    return words ? words.length : 0;
  }

  function setWriteOutline(on) {
    $('write-outline').hidden = !on;
    $('write-outline-btn').setAttribute('aria-pressed', String(!!on));
    store.set('write-outline', !!on);
    if (on && W.editor) refreshWriteStats();
  }

  function refreshWriteStats() {
    if (!W.editor) return;
    if (!$('write-conflicts').hidden || W.editor.getText().indexOf('<<<<<<< ') >= 0) showWriteConflicts();
    var text = W.editor.getText(), words = countWords(text);
    $('write-words').textContent = words.toLocaleString('en-US') + ' words';
    if ($('write-outline').hidden) return;
    var items = outlineOf(text), on = outlineAt(items, cursorLine(text, W.editor.selection().from));
    var rows = items.map(function (item, i) {
      var li = document.createElement('li');
      if (item.kind === 'file') {
        li.className = 'file'; li.textContent = '↳ ' + item.name; li.title = 'Open ' + item.name;
        li.onclick = function () { return openWriteFile(item.name); };
      } else {
        li.textContent = item.title || '(untitled)'; li.dataset.level = String(item.level); li.title = 'Line ' + item.line;
        if (i === on) li.className = 'on';
        li.onclick = function () { W.editor.gotoLine(item.line); };
      }
      return li;
    });
    var notes = currentNoteLines().sort(function (a, b) { return a.line - b.line; });
    if (notes.length) {
      var head = document.createElement('li');
      head.className = 'ol-head'; head.textContent = 'Notes';
      rows.push(head);
      notes.forEach(function (n) {
        var li = document.createElement('li');
        li.className = 'note'; li.title = n.note.body; li.dir = 'auto';
        li.textContent = n.line + ' · ' + (n.note.body.length > 60 ? n.note.body.slice(0, 59) + '…' : n.note.body);
        li.onclick = function () { W.editor.gotoLine(n.line); openWriteNote(n.line); };
        rows.push(li);
      });
    }
    $('write-outline-list').replaceChildren.apply($('write-outline-list'), rows);
  }

  function scheduleWriteStats(delay) {
    clearTimeout(W.statsTimer);
    W.statsTimer = setTimeout(refreshWriteStats, delay);
  }

  function fillWriteSettings(doc) {
    var select = $('write-main'), names = (doc.main_candidates || []).slice();
    if (names.indexOf(doc.main_file) < 0) names.unshift(doc.main_file);
    select.replaceChildren.apply(select, names.map(function (name) {
      var opt = document.createElement('option'); opt.value = name; opt.textContent = name; return opt;
    }));
    select.value = doc.main_file;
    $('write-engine-pick').value = doc.engine;
    $('write-draft').checked = !!doc.draft;
    $('write-export').href = '/api/write/export.zip?doc=' + encodeURIComponent(doc.id);
  }

  // One request at a time: replies arriving out of order would leave the controls showing an older setting.
  function changeWriteSettings(patch) {
    var doc = W.doc;
    W.settingsChain = (W.settingsChain || Promise.resolve()).then(function () {
      return post('/api/write/settings', Object.assign({ doc: doc.id }, patch));
    }).then(function (res) {
      if (W.doc !== doc) return;
      ['engine', 'main_file', 'draft'].forEach(function (k) { doc[k] = res[k]; });
      fillWriteSettings(doc);
      if (patch.main_file) {  // the PDF shown is the new main file's, if it was compiled before
        return api('/api/write/doc?doc=' + encodeURIComponent(doc.id)).then(function (info) {
          if (W.doc === doc) showWritePdf(info.pdf);
        });
      }
    }).catch(function (e) { toast(e.message); if (W.doc === doc) fillWriteSettings(doc); });
    return W.settingsChain;
  }

  var IMPORT_MAX = 100 * 1024 * 1024;  // as on the server

  function importWriteZip(file) {
    if (!file) return Promise.resolve();
    if (file.size > IMPORT_MAX) { toast('The ZIP is too large: at most 100 MB.'); return Promise.resolve(); }
    var name = file.name.replace(/\.zip$/i, '') || 'Imported';
    return fetch('/api/write/import?project=' + encodeURIComponent(projectName()) + '&name=' + encodeURIComponent(name),
      { method: 'POST', body: file }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body.error || 'import failed');
        toast('Imported "' + body.name + '"');
        return openWriteDoc(body.id);
      });
    }).catch(function (e) { toast(e.message); });
  }

  function uploadWriteFigure(file) {
    if (!file) return Promise.resolve();
    return fetch('/api/write/upload?doc=' + encodeURIComponent(W.doc.id) + '&name=' + encodeURIComponent(file.name),
      { method: 'POST', body: file }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body.error || 'upload failed');
        var sel = W.editor.selection();
        W.editor.replace(sel.from, sel.to, '\\includegraphics[width=\\linewidth]{' + body.name + '}');
        toast('Figure added: ' + body.name);
      });
    }).catch(function (e) { toast(e.message); });
  }

  function renameWriteDoc() {
    var name = window.prompt('Document name', W.doc.name);
    if (!name || !name.trim()) return Promise.resolve();
    return post('/api/write/rename', { doc: W.doc.id, name: name.trim() }).then(function (doc) {
      W.doc.name = doc.name; $('write-title').textContent = doc.name;
    }).catch(function (e) { toast(e.message); });
  }

  function deleteWriteDoc() {
    if (!window.confirm('Move "' + W.doc.name + '" to the project\'s trash folder?')) return Promise.resolve();
    return post('/api/write/delete', { doc: W.doc.id }).then(function () { return closeWriteDoc(true); })
      .catch(function (e) { toast(e.message); });
  }

  function closeWriteDoc(skipSave) {
    return (skipSave || !W.doc ? Promise.resolve() : saveWriteNow()).then(function () {
      if (!skipSave && (W.conflictHash || W.dirty)) {
        toast(W.conflictHash ? 'Resolve the conflict first (reload or keep mine).' : 'Not saved yet; the document stays open.');
        return;
      }
      dropWriteSuggestion();
      if (W.editor) W.editor.destroy();
      W.doc = null; W.editor = null; W.dirty = false;
      $('write-editor').hidden = true; $('write-home').hidden = false;
      return loadWrite();
    });
  }

  // Where to put a key: inside the \cite{...} the cursor is in (added to its list), else a new \cite{key}.
  function citeGroupAt(text, pos) {
    var open = -1;
    for (var i = pos - 1; i >= 0 && i >= pos - 400; i--) {
      var ch = text[i];
      if (ch === '}' || ch === '\n') return null;
      if (ch === '{') { open = i; break; }
    }
    if (open < 0 || !/\\[a-zA-Z]*cite[a-zA-Z]*\*?\s*(?:\[[^\]\n]*\]\s*){0,2}$/.test(text.slice(Math.max(0, open - 200), open))) return null;
    var close = -1;
    for (var j = pos; j < text.length && j < pos + 400; j++) {
      if (text[j] === '}') { close = j; break; }
      if (text[j] === '\n' || text[j] === '{') break;
    }
    var inner = text.slice(open + 1, close < 0 ? pos : close);
    return { open: open, close: close, keys: inner.split(',').map(function (k) { return k.trim(); }).filter(Boolean) };
  }

  function insertCiteKey(text, pos, key) {
    var group = citeGroupAt(text, pos);
    if (!group) return { from: pos, to: pos, insert: '\\cite{' + key + '}' };
    if (group.keys.indexOf(key) >= 0) return null;
    var keys = group.keys.concat([key]).join(',');
    if (group.close < 0) return { from: group.open + 1, to: pos, insert: keys + '}' };
    return { from: group.open + 1, to: group.close, insert: keys };
  }

  var CITE = { tab: 'library', timer: null, request: 0 };

  function openWriteCite() {
    var panel = $('write-cite-panel');
    panel.hidden = !panel.hidden;
    if (panel.hidden) return Promise.resolve();
    $('write-cite-q').value = '';
    $('write-cite-q').focus();
    return searchWriteCite();
  }

  function setWriteCiteTab(tab) {
    CITE.tab = tab;
    $('write-cite-tab-lib').classList.toggle('on', tab === 'library');
    $('write-cite-tab-ev').classList.toggle('on', tab === 'evidence');
    return searchWriteCite();
  }

  function searchWriteCite() {
    var request = ++CITE.request;
    var q = $('write-cite-q').value.trim();
    var list = $('write-cite-results');
    var context = W.editor ? citeGroupAt(W.editor.getText(), W.editor.selection().from) : null;
    var quoteButtons = (context ? context.keys : []).map(function (key) {
      var li = document.createElement('li');
      var btn = document.createElement('button');
      btn.className = 'btn small'; btn.textContent = 'Show quote: ' + key;
      btn.onclick = function () { showWriteQuote(key); };
      li.appendChild(btn);
      return li;
    });
    if (CITE.tab === 'evidence' && !q) {
      list.replaceChildren.apply(list, quoteButtons.concat([citeNote('Type words to search quotes in the project\'s papers.')]));
      return Promise.resolve();
    }
    return api('/api/write/cite/search?doc=' + encodeURIComponent(W.doc.id) + '&tab=' + CITE.tab +
      '&q=' + encodeURIComponent(q)).then(function (data) {
      if (request !== CITE.request) return;
      var rows = (data.results || []).map(citeRow);
      if (!rows.length) rows.push(citeNote('No matches in this project.'));
      list.replaceChildren.apply(list, quoteButtons.concat(rows));
    }).catch(function (e) { list.replaceChildren(citeNote(e.message)); });
  }

  function citeNote(text) {
    var li = document.createElement('li');
    li.className = 'muted'; li.textContent = text;
    return li;
  }

  function citeRow(r) {
    var li = document.createElement('li');
    var add = document.createElement('button');
    add.className = 'btn small'; add.textContent = 'Cite';
    add.onclick = function () { citeWritePaper(r); };
    var label = document.createElement('span');
    label.dir = 'auto';
    var who = (r.authors || []).join(', ');
    label.textContent = (r.title || r.paper_id) + (r.year ? ' (' + r.year + ')' : '') + (who ? ' · ' + who : '') +
      (r.page ? ' · p.' + r.page : '') + (r.key ? ' · ' + r.key : '');
    li.appendChild(add); li.appendChild(label);
    if (r.version) {
      var badge = document.createElement('span');
      badge.className = 'chip'; badge.textContent = r.version;
      li.appendChild(badge);
    }
    if (r.quote) {
      var quote = document.createElement('div');
      quote.className = 'quote'; quote.dir = 'auto'; quote.textContent = r.quote;
      li.appendChild(quote);
    }
    return li;
  }

  function citeWritePaper(r) {
    return post('/api/write/cite', { doc: W.doc.id, paper_id: r.paper_id, evidence_id: r.evidence_id || null })
      .then(function (res) {
        var sel = W.editor.selection();
        var edit = insertCiteKey(W.editor.getText(), sel.to, res.key);
        if (edit) W.editor.replace(edit.from, edit.to, edit.insert);
        $('write-cite-panel').hidden = true;
        toast('Cited ' + res.key + '; its entry is in references.bib');
      }).catch(function (e) { toast(e.message); });
  }

  function showWriteQuote(key) {
    return api('/api/write/quote?doc=' + encodeURIComponent(W.doc.id) + '&key=' + encodeURIComponent(key))
      .then(function (data) {
        var list = $('write-cite-results');
        list.replaceChildren();
        var head = document.createElement('li');
        head.dir = 'auto';
        head.textContent = key + ' · ' + (data.title || '') + (data.year ? ' (' + data.year + ')' : '');
        list.appendChild(head);
        if (!data.quotes.length) list.appendChild(citeNote('Cited from the library list, not from a quote.'));
        data.quotes.forEach(function (q) {
          var li = document.createElement('li');
          var quote = document.createElement('div');
          quote.className = 'quote'; quote.dir = 'auto'; quote.textContent = q.text;
          var link = document.createElement('a');
          link.href = '/api/papers/' + encodeURIComponent(data.paper_id) + '/pdf#page=' + q.page;
          link.target = '_blank'; link.rel = 'noopener';
          link.textContent = 'p.' + q.page + (q.version ? ' · ' + q.version : '');
          li.appendChild(link); li.appendChild(quote);
          list.appendChild(li);
        });
      }).catch(function (e) { toast(e.message); });
  }

  /* Autocomplete: a small list under the cursor after \, inside \ref{, \begin{ and \cite{. Offline, no model. */
  var LATEX_COMMANDS = [
    ['section', 1], ['subsection', 1], ['subsubsection', 1], ['paragraph', 1],  // starred forms come from LATEX_FORMS
    ['cite', 1], ['citep', 1], ['citet', 1], ['ref', 1], ['eqref', 1], ['autoref', 1], ['label', 1],
    ['textbf', 1], ['textit', 1], ['emph', 1], ['texttt', 1], ['underline', 1], ['footnote', 1], ['url', 1],
    ['href', 2], ['includegraphics', 1], ['caption', 1], ['centering', 0], ['item', 0], ['frac', 2], ['sqrt', 1],
    ['mathbf', 1], ['mathrm', 1], ['mathcal', 1], ['left', 0], ['right', 0], ['alpha', 0], ['beta', 0],
    ['gamma', 0], ['delta', 0], ['epsilon', 0], ['lambda', 0], ['mu', 0], ['sigma', 0], ['tau', 0], ['omega', 0],
    ['sum', 0], ['int', 0], ['partial', 0], ['dot', 1], ['hat', 1], ['bar', 1], ['vec', 1], ['cdot', 0],
    ['times', 0], ['leq', 0], ['geq', 0], ['approx', 0], ['infty', 0], ['title', 1], ['author', 1], ['date', 1],
    ['maketitle', 0], ['tableofcontents', 0], ['bibliography', 1], ['bibliographystyle', 1], ['usepackage', 1],
    ['newcommand', 2], ['input', 1], ['include', 1], ['hline', 0], ['newline', 0], ['noindent', 0],
    ['vspace', 1], ['hspace', 1], ['begin', 1], ['end', 1], ['chapter', 1], ['documentclass', 1],
    ['renewcommand', 2], ['newenvironment', 3], ['textcolor', 2], ['colorbox', 2], ['resizebox', 3],
    ['scalebox', 2], ['rotatebox', 2], ['multicolumn', 3], ['multirow', 3], ['hyperref', 1], ['dfrac', 2],
    ['tfrac', 2], ['overset', 2], ['underset', 2], ['textcite', 1], ['parencite', 1], ['autocite', 1],
    ['subcaption', 1], ['mathbb', 1], ['text', 1], ['operatorname', 1], ['toprule', 0], ['midrule', 0],
    ['bottomrule', 0], ['clearpage', 0], ['newpage', 0], ['appendix', 0],
    // xepersian
    ['lr', 1], ['rl', 1], ['latin', 1], ['persian', 1], ['settextfont', 1], ['setlatintextfont', 1]];
  var LATEX_ENVS = ['figure', 'figure*', 'table', 'table*', 'tabular', 'equation', 'equation*', 'align', 'align*',
    'itemize', 'enumerate', 'description', 'abstract', 'center', 'quote', 'minipage', 'thebibliography',
    'document', 'latin', 'persian', 'subfigure', 'tabularx', 'multicols', 'cases', 'gather', 'gather*',
    'verbatim', 'proof', 'theorem', 'lemma'];

  // Other common forms, each its own entry in the list: [signature after the name, what it is for].
  var LATEX_FORMS = {
    section: [['*{}', 'unnumbered'], ['[]{}', 'short title for the contents']],
    subsection: [['*{}', 'unnumbered'], ['[]{}', 'short title for the contents']],
    subsubsection: [['*{}', 'unnumbered']], chapter: [['*{}', 'unnumbered'], ['[]{}', 'short title for the contents']],
    paragraph: [['*{}', 'unnumbered']],
    includegraphics: [['[width=\\linewidth]{}', 'full line width'], ['[width=0.5\\textwidth]{}', 'half the text width'],
      ['[height=5cm]{}', 'fixed height'], ['[scale=0.5]{}', 'scaled'], ['[angle=90]{}', 'rotated'],
      ['[width=\\linewidth,height=6cm,keepaspectratio]{}', 'fit in a box'], ['[page=1]{}', 'one page of a PDF'],
      ['[trim=0 0 0 0,clip]{}', 'cropped: left bottom right top']],
    cite: [['[]{}', 'with a page or note: \\cite[p.~3]{key}']],
    citep: [['[]{}', 'with a note (natbib)'], ['[][]{}', 'before and after notes (natbib)']],
    citet: [['[]{}', 'with a note (natbib)']],
    caption: [['[]{}', 'short caption for the list of figures']],
    footnote: [['[]{}', 'with its own number']],
    sqrt: [['[]{}', 'n-th root']],
    item: [['[]', 'custom label']],
    vspace: [['*{}', 'kept at a page break']], hspace: [['*{}', 'kept at a line break']],
    usepackage: [['[]{}', 'with options']],
    documentclass: [['{}', ''], ['[11pt,a4paper]{}', 'with options']],
    newcommand: [['{}[]{}', 'with arguments: \\newcommand{\\name}[1]{… #1 …}'],
      ['{}[][]{}', 'with an optional first argument']],
    renewcommand: [['{}{}', ''], ['{}[]{}', 'with arguments']],
    newenvironment: [['{}{}{}', ''], ['{}[]{}{}', 'with arguments']],
    label: [['{fig:}', 'figure'], ['{tab:}', 'table'], ['{eq:}', 'equation'], ['{sec:}', 'section']],
    textcolor: [['{}{}', 'colour, text (xcolor)']], colorbox: [['{}{}', 'colour, text (xcolor)']],
    resizebox: [['{\\linewidth}{!}{}', 'scale to the line width (graphicx)']],
    scalebox: [['{}{}', 'factor, content (graphicx)']], rotatebox: [['{}{}', 'angle, content'], ['[origin=c]{}{}', 'about the centre']],
    multicolumn: [['{}{}{}', 'columns, alignment, text']], multirow: [['{}{}{}', 'rows, width (* = natural), text']],
    hyperref: [['[]{}', 'link to a label']], input: [], include: [],
    frac: [], dfrac: [['{}{}', 'display-size fraction (amsmath)']], tfrac: [['{}{}', 'text-size fraction (amsmath)']],
    overset: [['{}{}', 'over, base']], underset: [['{}{}', 'under, base']],
    textcite: [['{}', 'Author (year) — biblatex'], ['[]{}', 'with a note — biblatex']],
    parencite: [['{}', '(Author, year) — biblatex'], ['[]{}', 'with a note — biblatex']],
    autocite: [['{}', 'biblatex'], ['[]{}', 'with a note — biblatex']],
    subcaption: [['{}', 'caption of a subfigure (subcaption)']],
    mathbb: [['{}', 'ℝ, ℕ (amssymb)']], text: [['{}', 'text inside math (amsmath)']],
    operatorname: [['{}', 'named operator (amsmath)']]
  };
  // Common forms of \begin{...}: what follows the name, and what it is for.
  var LATEX_ENV_FORMS = {
    figure: [['[htbp]', 'let LaTeX place it'], ['[H]', 'exactly here (float package)'], ['[t]', 'top of a page']],
    'figure*': [['[t]', 'across both columns']],
    table: [['[htbp]', 'let LaTeX place it'], ['[H]', 'exactly here (float package)']],
    tabular: [['{lll}', 'three left columns'], ['{|l|c|r|}', 'ruled columns'], ['{p{3cm}l}', 'a wrapped column']],
    tabularx: [['{\\linewidth}{lX}', 'full width, X stretches (tabularx)']],
    minipage: [['{0.5\\textwidth}', 'half width'], ['[t]{0.48\\textwidth}', 'aligned at the top']],
    enumerate: [['[label=(\\alph*)]', '(a), (b), … (enumitem)']], itemize: [['[noitemsep]', 'compact (enumitem)']],
    subfigure: [['{0.48\\textwidth}', 'subfigure (subcaption)']],
    multicols: [['{2}', 'two columns (multicol)']], thebibliography: [['{99}', 'widest label']]
  };

  // What is being completed just before the cursor (`before` is the line up to the cursor), or null.
  function latexContext(before) {
    var m = /\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^{}]*)$/.exec(before);
    if (m) {
      var prefix = m[1].split(',').pop().replace(/^\s+/, '');
      return { kind: 'cite', prefix: prefix, start: before.length - prefix.length };
    }
    if ((m = /\\(?:ref|eqref|autoref|cref|Cref|pageref|nameref)\{([^{}]*)$/.exec(before))) {
      return { kind: 'ref', prefix: m[1], start: before.length - m[1].length };
    }
    if ((m = /\\(begin|end)\{([a-zA-Z*]*)$/.exec(before))) {
      return { kind: 'env', prefix: m[2], start: before.length - m[2].length, begin: m[1] === 'begin' };
    }
    if ((m = /(^|[^\\])\\([a-zA-Z]*)$/.exec(before))) {  // not after another backslash (\\ is a line break)
      return { kind: 'command', prefix: m[2], start: before.length - m[2].length - 1 };
    }
    return null;
  }

  function uniqueMatches(texts, re) {
    var seen = [];
    texts.forEach(function (t) {
      var m; re.lastIndex = 0;
      while ((m = re.exec(t))) { if (seen.indexOf(m[1]) < 0) seen.push(m[1]); }
    });
    return seen;
  }
  function docLabels(texts) { return uniqueMatches(texts, /\\label\{([^{}]+)\}/g); }
  function docCommands(text) {
    return uniqueMatches([text], /\\(?:re)?newcommand\*?\s*\{?\s*\\([a-zA-Z]+)/g);
  }

  // The cursor goes into the first empty [] or {} (or the first unfinished value such as {fig:}).
  function formCursor(apply, nameLength) {
    var rest = apply.slice(nameLength), m = /\[\]|\{\}|:\}/.exec(rest);
    return m ? nameLength + m.index + 1 : apply.length;
  }

  function commandOptions(prefix, own) {
    var out = [];
    var add = function (name, sig, detail) {
      var apply = '\\' + name + sig;
      out.push({ label: sig && detail !== '' ? apply : '\\' + name, apply: apply, detail: detail || undefined,
        back: apply.length - formCursor(apply, name.length + 1) });
    };
    LATEX_COMMANDS.forEach(function (c) {
      if (c[0].indexOf(prefix) !== 0) return;
      add(c[0], new Array(c[1] + 1).join('{}'), '');
      (LATEX_FORMS[c[0]] || []).forEach(function (f) {
        if (f[0] !== new Array(c[1] + 1).join('{}')) add(c[0], f[0], f[1]);
      });
    });
    own.forEach(function (name) {
      if (name.indexOf(prefix) !== 0 || out.some(function (o) { return o.label === '\\' + name; })) return;
      out.push({ label: '\\' + name, apply: '\\' + name, back: 0, detail: 'this document' });
    });
    return out;
  }

  // A completed argument (label, \\end name): when its closing brace follows right away, step past it.
  function closedReplacement(text, after) {
    if (after.charAt(0) === '}') {
      return { text: text + '}', cursor: text.length + 1, consume: 1 };
    }
    return { text: text, cursor: text.length, consume: 0 };
  }

  // After \\begin{ an environment's common forms are offered too (figure[htbp], tabular{lll}).
  function envOptions(prefix, withForms) {
    var out = [];
    LATEX_ENVS.filter(function (e) { return e.indexOf(prefix) === 0; }).forEach(function (e) {
      out.push({ label: e, name: e, form: '' });
      if (withForms) (LATEX_ENV_FORMS[e] || []).forEach(function (f) {
        out.push({ label: e + f[0], name: e, form: f[0], detail: f[1] });
      });
    });
    return out;
  }

  // What replaces the typed environment name (`after` is the rest of the line). After \begin{ the matching \end
  // is added and the cursor goes inside, also when the editor's own auto-closed } is all that follows (it is
  // replaced: `consume`). When more follows, or after \end{, only the name is completed.
  function envReplacement(name, isBegin, after, form) {
    form = form || '';
    if (!isBegin) return closedReplacement(name, after);
    if (after !== '' && after !== '}') return { text: name, cursor: name.length, consume: 0 };
    return { text: name + '}' + form + '\n  \n\\end{' + name + '}', cursor: name.length + form.length + 4,
      consume: after.length };
  }

  function citeOptions(prefix, docKeys, rows) {
    var p = prefix.toLowerCase(), out = [];
    docKeys.forEach(function (k) {
      if (k.toLowerCase().indexOf(p) >= 0) out.push({ label: k, detail: 'in this document', source: 'document' });
    });
    var rank = function (r) {  // key, then first author's surname, starting with the typed text
      var who = (r.authors && r.authors[0] || '').toLowerCase();
      return (r.key || '').toLowerCase().indexOf(p) === 0 ? 0 : who.indexOf(p) === 0 ? 1 : 2;
    };
    rows = rows.map(function (r, i) { return { r: r, i: i }; })
      .sort(function (a, b) { return rank(a.r) - rank(b.r) || a.i - b.i; }).map(function (x) { return x.r; });
    rows.forEach(function (r) {
      if (r.key && docKeys.indexOf(r.key) >= 0) return;
      var who = (r.authors && r.authors[0] || '').split(',')[0];
      out.push({ label: r.key || (who.toLowerCase().replace(/[^a-z]/g, '') + (r.year || '')), source: 'library',
        paper_id: r.paper_id, detail: who + (r.year ? ' ' + r.year : '') + ' · ' + (r.title || '') });
    });
    out.push({ label: 'Search the library and evidence…', source: 'search' });
    return out;
  }

  // Apply a chosen \cite{ entry to the text between from and to (the typed part of the key).
  function applyCiteCompletion(opt, from, to) {
    if (opt.source === 'search') {
      if (W.editor) W.editor.replace(from, to, '');
      return $('write-cite-panel').hidden ? openWriteCite() : Promise.resolve();
    }
    if (opt.source === 'document') { W.editor.replace(from, to, opt.label); return Promise.resolve(); }
    // The key comes from the server: by then the user may have typed on, moved, or left the file. The old
    // offsets are used only when the text before them is unchanged; otherwise the key goes into the \cite{...}
    // the cursor is in, and nowhere when the cursor has left it.
    var editor = W.editor, start = Math.max(0, from - 40), before = editor.getText().slice(start, to);
    var typed = editor.getText().slice(from, to);
    return post('/api/write/cite', { doc: W.doc.id, paper_id: opt.paper_id, evidence_id: null })
      .then(function (res) {
        var skip = function (why) { toast('Not inserted: ' + res.key + ' (' + why + '); its entry is in references.bib'); };
        if (W.editor !== editor) return skip('the file was closed');
        var text = editor.getText(), cursor = editor.selection().to;
        if (text.slice(start, to) === before) return editor.replace(from, to, res.key);
        if (!citeGroupAt(text, cursor)) return skip('the cursor left the citation');
        if (typed && text.slice(cursor - typed.length, cursor) === typed) {
          return editor.replace(cursor - typed.length, cursor, res.key);
        }
        var edit = insertCiteKey(text, cursor, res.key);
        if (edit) editor.replace(edit.from, edit.to, edit.insert);
      })
      .catch(function (e) { toast(e.message); });
  }

  // After these commands the next list (keys, labels, environments) is wanted at once.
  function chainsCompletion(insert) {
    return /^\\(?:[a-zA-Z]*cite[a-zA-Z]*|ref|eqref|autoref|cref|Cref|pageref|nameref|begin|end)\{\}$/.test(insert);
  }

  // CodeMirror completion source built on the functions above.
  function latexCompletionSource(context) {
    var line = context.state.doc.lineAt(context.pos);
    var c = latexContext(line.text.slice(0, context.pos - line.from));
    if (!c) return null;
    var from = line.from + c.start, text = context.state.doc.toString();
    var put = function (insert, cursor) {
      return function (view, completion, f, t) {
        view.dispatch({ changes: { from: f, to: t, insert: insert }, selection: { anchor: f + cursor } });
      };
    };
    if (c.kind === 'command') {
      // The whole list: CodeMirror filters it as you type, and Backspace can widen it again.
      return { from: from, validFor: /^\\[a-zA-Z*]*$/, options: commandOptions('', docCommands(text)).map(function (o) {
        var apply = put(o.apply, o.apply.length - o.back);
        return { label: o.label, detail: o.detail, type: 'keyword', apply: !chainsCompletion(o.apply) ? apply :
          function (view, completion, f, t) {
            apply(view, completion, f, t);
            setTimeout(function () { window.LitForgeCM.startCompletion(view); });
          } };
      }) };
    }
    if (c.kind === 'ref') {
      return { from: from, validFor: /^[^{}]*$/, options: docLabels([text].concat(W.otherTexts || [])).map(function (l) {
        return { label: l, type: 'variable', apply: function (view, completion, f, t) {
          var ins = closedReplacement(l, view.state.sliceDoc(t, view.state.doc.lineAt(t).to));
          put(ins.text, ins.cursor)(view, completion, f, t + ins.consume);
        } };
      }) };
    }
    if (c.kind === 'env') {
      return { from: from, validFor: /^[a-zA-Z*]*$/, options: envOptions('', c.begin).map(function (o) {
        return { label: o.label, detail: o.detail, type: 'type', apply: function (view, completion, f, t) {
          var ins = envReplacement(o.name, c.begin, view.state.sliceDoc(t, view.state.doc.lineAt(t).to).trimEnd(), o.form);
          put(ins.text, ins.cursor)(view, completion, f, t + ins.consume);
        } };
      }) };
    }
    var doc = W.doc;
    return api('/api/write/cite/search?doc=' + encodeURIComponent(doc.id) + '&tab=library&q=' + encodeURIComponent(c.prefix))
      .catch(function () { return { results: [] }; })
      .then(function (data) {
        var keys = docCiteKeys(text, from, context.pos);
        return { from: from, filter: false, options: citeOptions(c.prefix, keys, data.results || []).map(function (o) {
          return { label: o.label, detail: o.detail, type: o.source === 'search' ? 'text' : 'constant',
            apply: function (view, completion, f, t) { applyCiteCompletion(o, f, t); } };
        }) };
      });
  }

  // Keys cited in the document, leaving out the one being typed between from and to.
  function docCiteKeys(text, from, to) { return citedKeysIn(text.slice(0, from) + text.slice(to)); }

  function citedKeysIn(text) {
    var keys = [];
    text.replace(/\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^{}]*)\}/g, function (all, list) {
      list.split(',').forEach(function (k) { k = k.trim(); if (k && keys.indexOf(k) < 0) keys.push(k); });
      return all;
    });
    return keys;
  }

  var AI = { pending: null };
  var FIX_CONTEXT = 5;
  var IMAGE_ACTIONS = { 'equation-image': 'Equation from image', 'table-image': 'Table from image' };
  var IMAGE_MAX = 8 * 1024 * 1024;  // as on the server
  var IMG = { action: null, range: null };

  // [start, end) of each comment: % to the end of its line, unless escaped. A backslash always takes the next
  // character with it, so in \\% the % starts a comment and in \% it does not.
  function commentRanges(text) {
    var out = [];
    for (var i = 0; i < text.length; i++) {
      if (text[i] === '\\') { i++; continue; }
      if (text[i] === '%') {
        var end = text.indexOf('\n', i);
        if (end < 0) end = text.length;
        out.push([i, end]);
        i = end;
      }
    }
    return out;
  }

  // The text with comments blanked out (same length, so offsets still match the original).
  function maskComments(text) {
    var out = '', pos = 0;
    commentRanges(text).forEach(function (r) { out += text.slice(pos, r[0]) + ' '.repeat(r[1] - r[0]); pos = r[1]; });
    return out + text.slice(pos);
  }

  // The index of the } closing the { at `open`, or -1; escaped braces do not count.
  function closingBrace(text, open) {
    for (var i = open, depth = 0; i < text.length; i++) {
      var ch = text[i];
      if (ch === '\\') { i++; continue; }
      if (ch === '{') depth++;
      else if (ch === '}' && --depth === 0) return i;
    }
    return -1;
  }

  // The text inside \title{...} (after an optional [short title]), or null.
  // A backslash right after an odd number of backslashes is the second half of \\ (a line break), not a command.
  function escapedAt(text, pos) {
    var n = 0;
    while (pos - n - 1 >= 0 && text[pos - n - 1] === '\\') n++;
    return n % 2 === 1;
  }

  // The first match of `re` (a /g pattern) that is a real command: not escaped, not in a comment.
  function commandMatch(plain, re, from) {
    re.lastIndex = from || 0;
    for (var m = re.exec(plain); m; m = re.exec(plain)) if (!escapedAt(plain, m.index)) return m;
    return null;
  }

  // Both read the text with its comments blanked out: a commented \title or } does not count.
  function titleRange(text) {
    var plain = maskComments(text), m = commandMatch(plain, /\\title\s*(?:\[[^\]]*\])?\s*\{/g);
    if (!m) return null;
    var open = m.index + m[0].length - 1, close = closingBrace(plain, open);
    return close < 0 ? null : { from: open + 1, to: close };
  }

  function abstractRange(text) {
    var plain = maskComments(text), m = commandMatch(plain, /\\begin\{abstract\}/g);
    if (!m) return null;
    var from = m.index + m[0].length, end = commandMatch(plain, /\\end\{abstract\}/g, from);
    return end ? { from: from, to: end.index } : null;
  }

  // Where a new abstract goes: on the line after \maketitle, else after the \begin{document} line; -1 if neither.
  function abstractInsertAt(text) {
    var plain = maskComments(text), names = [/\\maketitle(?![a-zA-Z])/g, /\\begin\{document\}/g];
    for (var n = 0; n < names.length; n++) {
      var m = commandMatch(plain, names[n]);
      if (!m) continue;
      var end = text.indexOf('\n', m.index);
      return end < 0 ? text.length : end + 1;
    }
    return -1;
  }

  function stripComments(text) {
    var out = '', pos = 0;
    commentRanges(text).forEach(function (r) { out += text.slice(pos, r[0]); pos = r[1]; });
    return out + text.slice(pos);
  }

  // What a title or an abstract is written from: the document's body, without comments.
  function documentBody(text) {
    var begin = text.indexOf('\\begin{document}'), end = text.lastIndexOf('\\end{document}');
    var body = text.slice(begin < 0 ? 0 : begin + '\\begin{document}'.length, end < 0 ? text.length : end);
    return stripComments(body);
  }

  // A block inserted at `pos` starts and ends on its own line.
  function imagePads(text, pos) {
    return { pad_before: pos > 0 && text[pos - 1] !== '\n', pad_after: pos < text.length && text[pos] !== '\n' };
  }

  function pastedImage(e) {
    var items = (e && e.clipboardData && e.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === 'file' && /^image\//.test(items[i].type)) return items[i].getAsFile();
    }
    return null;
  }

  function openWriteImage(action) {
    var text = W.editor.getText(), pos = W.editor.selection().to, pads = imagePads(text, pos);
    IMG = { action: action, range: { from: pos, to: pos, text: '', pad_before: pads.pad_before, pad_after: pads.pad_after } };
    $('write-image-title').textContent = IMAGE_ACTIONS[action];
    $('write-image').hidden = false;
    placeWriteSuggestion(IMG.range, $('write-image'));
    if ($('write-image-drop').focus) $('write-image-drop').focus();
  }

  function closeWriteImage() {
    IMG = { action: null, range: null };
    $('write-image').hidden = true;
  }

  function writeImageFile(file) {
    var panel = IMG;  // a read finishing after the panel closed (or another opened) is dropped
    if (!file) return Promise.resolve();
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type || '')) { toast('Use a PNG, JPEG, GIF or WebP image.'); return Promise.resolve(); }
    if (file.size > IMAGE_MAX) { toast('The image is too large: at most 8 MB.'); return Promise.resolve(); }
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { reject(new Error('The image could not be read.')); };
      reader.readAsDataURL(file);
    }).then(function (url) { return sendWriteImage(url, panel); }).catch(function (e) { toast(e.message); });
  }

  function sendWriteImage(dataUrl, panel) {
    if (!IMG.action || (panel && panel !== IMG)) return Promise.resolve();
    var action = IMG.action, range = IMG.range;
    range.image = dataUrl;
    closeWriteImage();
    return runWriteAi(action, false, range);
  }

  function writeAiRange(action) {
    if (action === 'fix') {
      var err = W.lastError;
      if (!err || !err.line) return { problem: 'Click a compile error first, then choose "Fix this error".' };
      if (err.file && err.file !== W.file) return { problem: 'Open ' + err.file + ' to fix this error.' };
      var lines = W.editor.getText().split('\n').length;
      // ±5 lines: with ±15 both local models (gemma4, qwen3:8b) looped and cut the reply off (real run, 2026-10-19).
      var first = Math.max(1, err.line - FIX_CONTEXT), last = Math.min(lines, err.line + FIX_CONTEXT);
      var from = W.editor.lineRange(first).from, to = W.editor.lineRange(last).to;
      return { from: from, to: to, text: W.editor.getText().slice(from, to), error: err.message, first_line: first,
        error_line: err.line };
    }
    if (action === 'title' || action === 'abstract') {
      var full = W.editor.getText(), found = action === 'title' ? titleRange(full) : abstractRange(full);
      // Sections in other files (\input) count too; they are read for completion already.
      var context = [documentBody(full)].concat((W.otherTexts || []).map(stripComments)).join('\n');
      if (!found && action === 'abstract' && abstractInsertAt(full) >= 0) {  // the templates have no abstract
        var pos = abstractInsertAt(full), pads = imagePads(full, pos);
        return { from: pos, to: pos, text: '', context: context, new_env: true, pad_before: pads.pad_before,
          pad_after: pads.pad_after };
      }
      if (!found) {
        return { problem: action === 'title' ? 'Add \\title{} to the document first.' :
          'Add \\begin{document} to the document first.' };
      }
      return { from: found.from, to: found.to, text: full.slice(found.from, found.to), context: context };
    }
    var sel = W.editor.selection();
    if (!sel.text.trim()) return { problem: 'Select the text first.' };
    return { from: sel.from, to: sel.to, text: sel.text };
  }

  // `given` is a prepared range (an image action, after the image was given).
  function runWriteAi(action, approved, given) {
    if (!W.doc || !W.editor || !action) return Promise.resolve();
    if (AI.pending) { toast('Apply or reject the open suggestion first.'); return Promise.resolve(); }
    if (IMAGE_ACTIONS[action] && !given && !approved) { openWriteImage(action); return Promise.resolve(); }
    var range = given || writeAiRange(action);
    if (range.problem) { toast(range.problem); return Promise.resolve(); }
    if (range.before == null) {  // the text around it: an empty range (a title, an insertion point) has no text to check
      var all = W.editor.getText();
      range.before = all.slice(Math.max(0, range.from - 40), range.from); range.after = all.slice(range.to, range.to + 40);
    }
    range.doc = range.doc || W.doc.id; range.file = range.file || W.file;
    AI.range = range;
    var body = { doc: W.doc.id, action: action, text: range.text, error: range.error || '', first_line: range.first_line || 1, error_line: range.error_line || 0,
      context: range.context || '', image: range.image || '', pad_before: !!range.pad_before, pad_after: !!range.pad_after,
      new_env: !!range.new_env,
      file: W.file, approved: !!approved };
    $('write-compile-state').textContent = 'Preparing the request…';
    return post('/api/write/assist', body).then(function (res) {
      if (res.needs_approval) {
        var p = res.preview;
        var ok = window.confirm('Send ' + (res.sent.images ? 'your image' + (res.sent.characters ? ' and ' : '') : '') +
          (res.sent.characters || !res.sent.images ? res.sent.characters + ' characters of your text' : '') +
          (res.sent.evidence ? ' and ' + res.sent.evidence + ' evidence quotes' : '') + ' to ' + p.model + ' (' +
          p.provider + ', cloud' + (p.max_cost_usd != null ? ', at most $' + p.max_cost_usd.toFixed(4) : '') + ')?');
        if (!ok) { $('write-compile-state').textContent = 'Not sent.'; return; }
        if (!W.doc || W.doc.id !== range.doc || W.file !== range.file) {  // approved for text no longer open
          $('write-compile-state').textContent = 'Not sent: another file is open now.'; return;
        }
        return runWriteAi(action, true, range);  // exactly the request shown, not a new one
      }
      $('write-compile-state').textContent = 'Asking ' + (res.preview.model || res.preview.profile) +
        (res.preview.remote ? ' (cloud)' : ' (local)') + ' · sending ' + (res.sent.images ? 'an image' : res.sent.characters + ' characters') +
        (res.sent.evidence ? ' and ' + res.sent.evidence + ' evidence quotes' : '') + '…';
      return pollWriteAi(res, range);
    }).catch(function (e) { $('write-compile-state').textContent = e.message; });
  }

  function pollWriteAi(job, range) {
    return api('/api/jobs/' + encodeURIComponent(job.id)).then(function (j) {
      if (j.status === 'running') {
        return new Promise(function (resolve) { setTimeout(resolve, 800); }).then(function () { return pollWriteAi(job, range); });
      }
      if (j.status === 'failed') { $('write-compile-state').textContent = j.error || 'The model call failed'; return; }
      $('write-compile-state').textContent = '';
      if (!W.doc || W.doc.id !== range.doc || W.file !== range.file) {  // the user moved on; record it as rejected
        post('/api/write/assist/decide', { doc: range.doc, suggestion: j.report.id, accepted: [] }).catch(function () {});
        return;
      }
      showWriteSuggestion(j.report, range);
    });
  }

  function showWriteSuggestion(s, range) {
    AI.pending = { suggestion: s, range: range, accepted: {} };
    s.hunks.forEach(function (h) { AI.pending.accepted[h.index] = !h.flags.length; });
    var box = $('write-suggestion');
    $('write-suggestion-title').textContent = { write_fix: 'Fix', write_rewrite: 'Rewrite', write_cite: 'Citations',
      write_summary: s.action === 'abstract' ? 'Abstract' : 'Title', write_image: 'From image' }[s.task] || 'Suggestion';
    $('write-suggestion-meta').textContent = s.provider + ':' + s.model + (s.note ? ' · ' + s.note : '');
    $('write-suggestion-meta').title = 'Nothing changes until you apply';
    var parts = [];
    s.flags.forEach(function (f) {
      var p = document.createElement('p'); p.className = 'flag'; p.textContent = '⚠ ' + f; parts.push(p);
    });
    s.hunks.forEach(function (h) { parts.push(suggestionHunk(h)); });
    if (!s.hunks.length) {  // seen in the real run: a model may return the text unchanged
      var same = document.createElement('p');
      same.className = 'sg-same'; same.textContent = 'The model returned the text unchanged.';
      parts.push(same);
    }
    s.citations.forEach(function (c) {
      var q = document.createElement('p');
      q.className = 'quote'; q.dir = 'auto';
      q.textContent = c.key + ' · p.' + c.page + ' · “' + c.quote + '”';
      parts.push(q);
    });
    var row = document.createElement('div');
    row.className = 'sg-actions';
    var reject = document.createElement('button');
    reject.type = 'button'; reject.textContent = 'Reject all';
    reject.onclick = function () {
      Object.keys(AI.pending.accepted).forEach(function (k) { AI.pending.accepted[k] = false; });
      return applyWriteSuggestion(true);
    };
    var apply = document.createElement('button');
    apply.type = 'button'; apply.className = 'sg-apply'; apply.textContent = 'Apply accepted';
    apply.onclick = function () { return applyWriteSuggestion(false); };
    row.appendChild(reject); row.appendChild(apply);
    parts.push(row);
    $('write-suggestion-hunks').replaceChildren.apply($('write-suggestion-hunks'), parts);
    box.hidden = false;
    if (W.editor) W.editor.mark(range);
    placeWriteSuggestion(range);
  }

  // Float the bar just under the changed text; above it when there is no room below. Without
  // coordinates (plain text area) the stylesheet keeps it at the bottom right of the editor.
  function placeWriteSuggestion(range, panel) {
    var box = panel || $('write-suggestion'), at = W.editor && W.editor.coords && W.editor.coords(range.to);
    box.style.top = ''; box.style.bottom = '';
    if (!at || !box.parentNode || !box.parentNode.getBoundingClientRect) return;
    var pane = box.parentNode.getBoundingClientRect(), height = box.offsetHeight || 120;
    var below = at.bottom - pane.top + 6, above = at.top - pane.top - height - 6;
    var top = below + height <= pane.height ? below : above;
    top = Math.max(6, Math.min(top, pane.height - height - 6));
    box.style.top = Math.round(top) + 'px'; box.style.bottom = 'auto';
  }

  // Leaving the file or document drops an open suggestion (recorded as rejected) so it can never be applied to
  // other text.
  function dropWriteSuggestion() {
    var pending = AI.pending;
    closeWriteImage();
    AI.pending = null; AI.range = null;
    hideWriteSuggestion();
    if (pending) {
      post('/api/write/assist/decide', { doc: pending.range.doc || (W.doc && W.doc.id), suggestion: pending.suggestion.id,
        accepted: [] }).catch(function () {});
    }
  }

  function hideWriteSuggestion() {
    $('write-suggestion').hidden = true;
    if (W.editor) W.editor.mark(null);
  }

  function suggestionHunk(h) {
    var div = document.createElement('div');
    div.className = 'hunk';
    var del = document.createElement('del'); del.textContent = h.old; del.dir = 'auto';
    var ins = document.createElement('ins'); ins.textContent = h.new; ins.dir = 'auto';
    if (h.old) div.appendChild(del);
    if (h.new) div.appendChild(ins);
    h.flags.forEach(function (f) {
      var p = document.createElement('p'); p.className = 'flag'; p.textContent = '⚠ ' + f; div.appendChild(p);
    });
    var toggle = document.createElement('button');
    toggle.type = 'button';
    var label = function () {
      var on = AI.pending.accepted[h.index];
      toggle.textContent = on ? 'Accepted ✓' : 'Rejected';
      toggle.className = 'hunk-toggle' + (on ? ' on' : '');
      toggle.title = on ? 'Click to reject this change' : 'Click to accept this change';
      toggle.setAttribute && toggle.setAttribute('aria-pressed', String(!!on));
    };
    label();
    toggle.onclick = function () { AI.pending.accepted[h.index] = !AI.pending.accepted[h.index]; label(); };
    div.appendChild(toggle);
    return div;
  }

  // Hunk offsets come from Python and count code points; JS strings count UTF-16 units (an emoji is two).
  function appliedSuggestion(original, hunks, accepted) {
    var chars = Array.from(original), out = '', pos = 0;
    hunks.slice().sort(function (a, b) { return a.start - b.start; }).forEach(function (h) {
      out += chars.slice(pos, h.start).join('') + (accepted[h.index] ? h.new : h.old);
      pos = h.end;
    });
    return out + chars.slice(pos).join('');
  }

  // rejectAll: the Reject all button. "Apply accepted" with nothing accepted keeps the suggestion open: changes
  // with a warning start as Rejected, and applying them silently recorded a rejection (real run).
  function applyWriteSuggestion(rejectAll) {
    var pending = AI.pending;
    if (!pending) return Promise.resolve();
    var s = pending.suggestion, range = pending.range;
    if (range.doc && (!W.doc || W.doc.id !== range.doc || W.file !== range.file)) { dropWriteSuggestion(); return Promise.resolve(); }
    var accepted = s.hunks.filter(function (h) { return pending.accepted[h.index]; }).map(function (h) { return h.index; });
    if (!accepted.length && s.hunks.length && rejectAll === false) {
      toast('Nothing is accepted yet: a change marked ⚠ starts as Rejected; click it to accept it, or use Reject all.');
      return Promise.resolve();
    }
    var now = W.editor.getText(), moved = range.before != null &&
      (now.slice(range.from - range.before.length, range.from) !== range.before ||
       now.slice(range.to, range.to + range.after.length) !== range.after);
    if (accepted.length && (moved || now.slice(range.from, range.to) !== s.original)) {
      toast('The text changed since the request; ask again.');
      return Promise.resolve();
    }
    var text = appliedSuggestion(s.original, s.hunks, pending.accepted);
    if (accepted.length) W.editor.replace(range.from, range.to, text);
    AI.pending = null; AI.range = null;
    hideWriteSuggestion();
    var used = s.citations.filter(function (c) { return text.indexOf(c.key) >= 0; });
    return saveWriteNow().then(function () {
      return used.reduce(function (chain, c) {
        return chain.then(function () {
          return post('/api/write/cite', { doc: W.doc.id, paper_id: c.paper_id, evidence_id: c.evidence_id });
        });
      }, Promise.resolve());
    }).then(function () {
      return post('/api/write/assist/decide', { doc: W.doc.id, suggestion: s.id, accepted: accepted });
    }).then(function (r) { toast(r.status === 'rejected' ? 'Suggestion rejected' : 'Applied ' + accepted.length + ' change(s)'); })
      .catch(function (e) { toast(e.message); });
  }

  function wireWrite() {
    $('write-create').onclick = createWriteDoc;
    $('write-new-name').addEventListener('keydown', function (e) { if (e.key === 'Enter') createWriteDoc(); });
    $('write-back').onclick = function () { closeWriteDoc(false); };
    $('write-file').onchange = function () { openWriteFile($('write-file').value); };
    $('write-compile').onclick = compileWrite;
    $('write-versions-btn').onclick = showWriteVersions;
    $('write-rename').onclick = function () { $('write-more').open = false; return renameWriteDoc(); };
    $('write-delete').onclick = function () { $('write-more').open = false; return deleteWriteDoc(); };
    $('write-reload').onclick = reloadTheirs;
    $('write-keep').onclick = keepMine;
    $('write-cite').onclick = openWriteCite;
    $('write-ai').onchange = function () { var action = $('write-ai').value; $('write-ai').value = ''; runWriteAi(action, false); };
    $('write-image-close').onclick = closeWriteImage;
    $('write-image-file').onchange = function () { var f = $('write-image-file').files[0]; $('write-image-file').value = ''; writeImageFile(f); };
    document.addEventListener('paste', function (e) {  // only while the image panel is open
      var file = !$('write-image').hidden && pastedImage(e);
      if (file) { e.preventDefault(); writeImageFile(file); }
    });
    $('write-image-drop').addEventListener('keydown', function (e) { if (e.key === 'Escape') closeWriteImage(); });
    $('write-suggestion-close').onclick = function () {  // closing rejects, so the record is complete
      if (!AI.pending) { hideWriteSuggestion(); return; }
      Object.keys(AI.pending.accepted).forEach(function (k) { AI.pending.accepted[k] = false; });
      applyWriteSuggestion();
    };
    $('write-cite-tab-lib').onclick = function () { setWriteCiteTab('library'); };
    $('write-cite-tab-ev').onclick = function () { setWriteCiteTab('evidence'); };
    $('write-cite-q').addEventListener('input', function () {
      clearTimeout(CITE.timer); CITE.timer = setTimeout(searchWriteCite, 250);
    });
    $('write-upload').onchange = function () { uploadWriteFigure($('write-upload').files[0]); $('write-upload').value = ''; };
    document.querySelectorAll('.seg [data-layout]').forEach(function (b) {
      b.onclick = function () { setWriteLayout(b.dataset.layout); };
    });
    setWriteLayout(store.get('write-layout', 'split'));
    setWriteSplit(store.get('write-split', 50));
    setWriteOutline(store.get('write-outline', false));
    $('write-outline-btn').onclick = function () { setWriteOutline($('write-outline').hidden); };
    $('write-git-btn').onclick = function () { $('write-more').open = false; return showWriteGit(); };
    $('write-git-close').onclick = function () { $('write-git').hidden = true; };
    $('write-git-connect').onclick = connectWriteGit;
    $('write-git-sync').onclick = syncWriteGit;
    $('write-git-abort').onclick = abortWriteGit;
    $('write-git-disconnect').onclick = disconnectWriteGit;
    $('write-clone').onclick = cloneWriteGit;
    $('write-symbols-btn').onclick = function () { if ($('write-symbols').hidden) openWriteSymbols(); else closeWriteSymbols(); };
    $('write-symbols-close').onclick = closeWriteSymbols;
    $('write-symbols-q').addEventListener('input', renderWriteSymbols);
    $('write-symbols-q').addEventListener('keydown', function (e) {
      var first = $('write-symbols-grid').children[0];
      if (e.key === 'Enter' && first) { e.preventDefault(); first.onclick(); }
      if (e.key === 'Escape') { closeWriteSymbols(); if (W.editor && W.editor.view) W.editor.view.focus(); }
    });
    $('write-note-close').onclick = closeWriteNote;
    $('write-note-save').onclick = saveWriteNote;
    $('write-note-delete').onclick = deleteWriteNote;
    $('write-note-text').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveWriteNote(); }
      if (e.key === 'Escape') closeWriteNote();
    });
    $('write-main').onchange = function () { changeWriteSettings({ main_file: $('write-main').value }); };
    $('write-engine-pick').onchange = function () { changeWriteSettings({ engine: $('write-engine-pick').value }); };
    $('write-draft').onchange = function () { changeWriteSettings({ draft: $('write-draft').checked }); };
    $('write-import').onchange = function () { var f = $('write-import').files[0]; $('write-import').value = ''; importWriteZip(f); };
    wireWriteGutter();
    wirePdfView();
    $('write-spell').onclick = function () { setSpell(!SPELL.on); };
    $('write-chat-btn').onclick = function () { toggleChat(); };
    $('write-chat-close').onclick = function () { toggleChat(false); };
    $('write-chat-send').onclick = function () { sendChat(); };
    $('write-chat-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendChat(); }
    });
    $('write-version-save').onclick = saveNamedVersion;
    $('write-version-name').addEventListener('keydown', function (e) { if (e.key === 'Enter') saveNamedVersion(); });
    $('write-version-against').onchange = function () { if (VERS.selected) selectVersion(VERS.selected); };
    $('write-versions-close').onclick = function () { $('write-versions').hidden = true; };
    setSpell(store.get('spell', true));
    document.addEventListener('mousedown', function (e) {
      var menu = $('write-spell-menu');
      if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true;
    });
    $('write-spell-menu').addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { $('write-spell-menu').hidden = true; if (W.editor && W.editor.view) W.editor.view.focus(); }
    });
    window.addEventListener('resize', function () { if (AI.pending) placeWriteSuggestion(AI.pending.range); });
    window.addEventListener('beforeunload', function (e) {
      if (W.doc && W.dirty && !W.conflictHash) {  // keepalive lets the save finish while the page closes
        fetch('/api/write/save', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ doc: W.doc.id, name: W.file, text: W.editor.getText(), hash: W.hash }) })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (r) { if (r && r.hash) { W.hash = r.hash; W.dirty = false; writeSaveState('Saved'); } })
          .catch(function () { /* the page is closing */ });
        e.preventDefault(); e.returnValue = '';
      }
    });
  }
  /* ---------- End Write ---------- */

  /* ---------- Optional tools ---------- */
  function watchOptionalToolJob(job, target, done) {
    return api('/api/jobs/' + encodeURIComponent(job.id)).then(function (report) {
      target.textContent = (report.messages || []).join('\n') + (report.error ? '\n' + report.error : '');
      if (report.status === 'running') {
        setTimeout(function () { watchOptionalToolJob(job, target, done); }, 1500);
      } else if (report.status === 'done') {
        done();
      }
    }).catch(function (e) { target.textContent = e.message; });
  }

  function loadOptionalTools() {
    return api('/api/tools').then(function (tools) {
      var mineru = tools.mineru || {};
      $('mineru-status').textContent = (mineru.installed ? 'Installed · ' + mineru.version : 'Not installed') +
        ' · Size: ' + (mineru.size || '~4 GB') + (mineru.error ? ' · ' + mineru.error : '');
      $('mineru-install').hidden = !!mineru.installed;
      $('mineru-install').onclick = installMineru;

      var pix = tools.pix2text || {};
      $('tool-pix2text-status').textContent = pix.installed ?
        'Installed · ' + pix.version + ' · ' + (pix.size || '~2 GB') :
        pix.offerable ? 'Not installed · ' + (pix.size || '~2 GB') :
          'Not available on this install. Run: ' + (pix.command || 'uv sync --extra formulas --inexact');
      $('tool-pix2text-action').hidden = !!pix.installed || !pix.offerable;
      $('tool-pix2text-action').textContent = 'Install Pix2Text…';
      $('tool-pix2text-action').onclick = installPix2Text;

      var tec = (tools.tectonic || {}).tectonic || {};
      $('tool-tectonic-status').textContent = tec.installed ? 'Installed · ' + (tec.version || '') :
        tec.offerable ? 'Not installed · ' + tec.size : 'No Tectonic build for this platform; install TeX Live or MacTeX';
      $('tool-tectonic-action').hidden = !!tec.installed || !tec.offerable;
      $('tool-tectonic-action').onclick = function () { installTectonic(tec); };

      var vision = tools.vision || {};
      var visionDetail = vision.message || (vision.model ? 'Model: ' + vision.model : 'No image model configured');
      $('tool-vision-status').textContent = (vision.ready ? 'Ready · ' : 'Not ready · ') + visionDetail;
      $('tool-vision-action').textContent = vision.legacy ? 'Set up image model…' : 'Open Models';
      $('tool-vision-action').onclick = vision.legacy ? setupVisionTool : openVisionModels;
    }).catch(function (e) {
      $('mineru-status').textContent = e.message;
      $('tool-pix2text-status').textContent = e.message;
      $('tool-vision-status').textContent = e.message;
    });
  }

  function installPix2Text() {
    return api('/api/tools').then(function (tools) {
      var pix = tools.pix2text || {};
      if (!pix.offerable) {
        $('tool-pix2text-status').textContent = 'Not available on this install. Run: ' +
          (pix.command || 'uv sync --extra formulas --inexact');
        return;
      }
      if (!window.confirm('Install Pix2Text for Math: medium (' + (pix.size || '~2 GB') + ')?')) return;
      $('tool-pix2text-progress').textContent = 'Installing Pix2Text…';
      return post('/api/tools/pix2text/install', { confirmed: true }).then(function (job) {
        return watchOptionalToolJob(job, $('tool-pix2text-progress'), loadOptionalTools);
      });
    }).catch(function (e) { $('tool-pix2text-progress').textContent = e.message; });
  }

  function installTectonic(tec) {
    if (!window.confirm('Download Tectonic ' + (tec.release || '') + ' (' + (tec.size || '~22 MB') + ')?')) return;
    $('tool-tectonic-progress').textContent = 'Installing Tectonic…';
    return post('/api/tools/tectonic/install', { confirmed: true }).then(function (job) {
      return watchOptionalToolJob(job, $('tool-tectonic-progress'), loadOptionalTools);
    }).catch(function (e) { $('tool-tectonic-progress').textContent = e.message; });
  }

  function openVisionModels() {
    showView('settings');
    showSettingsTab('models');
    $('models-settings-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function setupVisionTool() {
    return api('/api/math-setup').then(function (status) {
      showMathSetup(status, true);
    }).catch(function (e) { $('tool-vision-progress').textContent = e.message; });
  }
  /* ---------- End Optional tools ---------- */

  /* ---------- Models settings ---------- */
  var modelsInfo = null;
  var modelProviders = {
    openrouter: ['OpenRouter — one key for GPT, Claude, Gemini, DeepSeek, Qwen, Kimi', 'https://openrouter.ai/models'],
    openai: ['OpenAI (GPT)', 'https://platform.openai.com/docs/models'],
    anthropic: ['Anthropic (Claude)', 'https://docs.anthropic.com/en/docs/about-claude/models/overview'],
    google: ['Google (Gemini)', 'https://ai.google.dev/gemini-api/docs/models'],
    deepseek: ['DeepSeek', 'https://api-docs.deepseek.com/quick_start/pricing'],
    qwen: ['Qwen (Alibaba Cloud)', 'https://www.alibabacloud.com/help/en/model-studio/models'],
    kimi: ['Kimi (Moonshot)', 'https://platform.moonshot.ai/docs/models']
  };
  var modelTaskLabels = {
    ask: 'Ask questions', audit: 'Audit answers', matrix_extract: 'Extract comparison matrix',
    claim_extract: 'Extract claims', claim_relation: 'Compare claim relationships',
    screen: 'Title and abstract screening', screen_fulltext: 'Full-text screening',
    asset_analyze: 'Analyze figures and tables', equation_compare: 'Compare equations',
    gap_design: 'Design research gaps', console_plan: 'Plan console workflows', page_rewrite: 'Rewrite pages',
    write_fix: 'Write: fix errors', write_rewrite: 'Write: rewrite and translate', write_cite: 'Write: suggest citations',
    write_chat: 'Write: assistant', write_summary: 'Write: title and abstract', write_image: 'Write: LaTeX from an image'
  };
  function taskLabel(task) { return modelTaskLabels[task] || task.replace(/_/g, ' '); }

  // The cloud model choices matter only for the Cloud-light tier (chosen, or recommended and nothing chosen).
  function showCloudPickers() {
    var tier = $('models-tier').value || (modelsInfo && modelsInfo.proposal && modelsInfo.proposal.recommended_tier);
    $('cloud-pickers').hidden = tier !== 'cloud';
  }

  function updateProviderModelsLink() {
    var provider = modelProviders[$('models-provider').value] || modelProviders.openrouter;
    $('models-provider-models').href = provider[1];
    $('models-provider-models').textContent = 'Browse models ↗';
  }


  function modelButton(label, action) {
    var button = document.createElement('button');
    button.className = 'btn small'; button.textContent = label; button.onclick = action;
    return button;
  }

  function loadModels() {
    return api('/api/models').then(function (info) {
      modelsInfo = info;
      var hw = info.hardware, proposal = info.proposal;
      var build = (hw.llama_version || '').match(/\bbuild[:\s]+b?(\d+)/i) ||
        (hw.llama_version || '').match(/\bversion[:\s]+b?(\d+)(?![\d.])/i);
      $('models-hardware').textContent = hw.os + ' · ' + (hw.arch || '') + ' · RAM ' + hw.ram_gb + ' GB' +
        (hw.gpu_name ? ' · ' + hw.gpu_name + ' (' + hw.gpu_vram_gb + ' GB VRAM)' : '') +
        (build ? ' · llama.cpp build ' + build[1] : '') + ' · Free disk ' + (hw.free_disk_gb || 0).toFixed(1) + ' GB';
      $('models-current').textContent = 'Current setup: ' + (info.tier || 'original defaults') +
        ' · Cloud ' + (info.cloud_enabled ? 'enabled (approval required)' : 'off');
      $('models-recommendation').textContent = 'Recommended: ' + proposal.tier + ' · About ' + proposal.download_gb +
        ' GB download / ' + proposal.memory_gb + ' GB memory. ' + proposal.notes;
      $('models-vision').textContent = info.vision_enabled ? 'Image features enabled.' :
        'Image features are off for this setup. Choose a vision profile or apply the medium/large tier to enable them.';
      $('models-apply').disabled = false;
      $('models-server').textContent = info.server.foreign ? 'Model server: the port is used by another program.' :
        info.server.running ? 'Model server running' + (info.server.owned ? '' : ' (started outside LitForge)') +
          ' · ' + (info.server.preset_models || []).map(function (name) {
            var m = info.server.models.find(function (entry) { return entry.id === name; }) || { id: name };
            var st = typeof m.status === 'object' ? m.status.value : m.status || 'unknown';
            return m.id + ' ' + (st === 'loaded' ? 'loaded' : st === 'unloaded' ? 'not loaded' : st);
          }).join(' · ') : 'Model server stopped · it starts when a model is needed';
      $('models-tasks').replaceChildren();
      Object.keys(info.tasks || {}).forEach(function (task) {
        var row = document.createElement('div'), what = document.createElement('span'), who = document.createElement('b');
        what.textContent = taskLabel(task);
        var t = info.tasks[task];
        who.textContent = (t && (t.profile || (t.profiles || []).join(', '))) || '—';
        row.appendChild(what); row.appendChild(who); $('models-tasks').appendChild(row);
      });
      showCloudPickers();
      $('models-profiles').replaceChildren();
      info.profiles.forEach(function (p) {
        var row = document.createElement('div'), label = document.createElement('span');
        row.className = 'models-profile';
        var name = document.createElement('b'); name.textContent = p.name;
        var meta = document.createElement('small');
        meta.textContent = (p.model !== p.name ? p.model + ' · ' : '') + (p.remote ? 'cloud' : 'local') + (p.vision ? ' · images' : '');
        label.appendChild(name); label.appendChild(meta);
        row.appendChild(label);
        var result = document.createElement('span');
        result.className = 'models-test'; result.textContent = testResults[p.name] || '';
        row.appendChild(result);
        row.appendChild(modelButton('Test', function () { testSettingsModel(p, result); }));
        row.appendChild(modelButton('Assign tasks…', function () { assignSettingsTasks(p.name); }));
        if (p.name !== 'local' && p.name !== 'local_vision') row.appendChild(modelButton('Remove', function () {
          reviewModelsChange({ action: 'remove', name: p.name, delete_files: !p.remote && window.confirm('Also delete downloaded files owned by this profile?') });
        }));
        else row.appendChild(document.createElement('span'));  // keeps the buttons in their columns
        $('models-profiles').appendChild(row);
      });
      var provider = $('models-provider'), previous = provider.value;
      provider.replaceChildren();
      info.providers.forEach(function (name) {
        var option = document.createElement('option'); option.value = name; option.textContent = (modelProviders[name] || [name])[0];
        provider.appendChild(option);
      });
      provider.value = previous || info.providers[0];
      provider.onchange = updateProviderModelsLink;
      updateProviderModelsLink();
      // Keep selections while status refreshes.
      var selected = Array.from($('models-add-tasks').children).filter(function (label) {
        return label.children[0] && label.children[0].checked;
      }).map(function (label) { return label.children[0].value; });
      $('models-add-tasks').replaceChildren();
      (info.task_names || []).forEach(function (task) {
        var label = document.createElement('label'), input = document.createElement('input');
        var text = document.createElement('span');
        label.className = 'check'; input.type = 'checkbox'; input.value = task;
        input.checked = selected.indexOf(task) !== -1;
        text.textContent = taskLabel(task);
        label.appendChild(input); label.appendChild(text); $('models-add-tasks').appendChild(label);
      });
      $('models-cloud').textContent = info.cloud_enabled ? 'Turn cloud off' : 'Enable cloud…';
      if (info.budget) {
        ['call', 'run', 'month'].forEach(function (key) { $('budget-' + key).value = info.budget['max_per_' + key + '_usd']; });
        $('settings-policy').value = info.policy;
        $('settings-mailto').value = info.mailto || '';
        $('settings-exports').value = info.exports_root || '';
        renderTaskChecks('local-tasks', info.task_names || []);
      }
    }).catch(function (e) { $('models-status').textContent = e.message; });
  }

  function reviewModelsChange(body) {
    return post('/api/models/preview', body).then(function (preview) {
      $('models-diff').textContent = preview.notice + '\n\n' + preview.diff;
      if (!window.confirm(preview.notice + '\n\n' + preview.diff + '\nApply these exact file changes?')) return;
      return post('/api/models/apply', { token: preview.token, confirmed: true }).then(function () {
        $('models-status').textContent = 'Saved model settings.';
        refreshModelChoices();
        return loadModels();
      });
    }).catch(function (e) { $('models-status').textContent = e.message; });
  }

  // A one-line answer next to the model: does it answer, correctly, and how fast.
  function testResult(j) {
    if (j.status === 'failed') return '✗ ' + String(j.error || 'failed').split('\n')[0].slice(0, 120);
    var r = j.report || {};
    if (!r.reachable) return '✗ No answer';
    return (r.valid ? '✓ Works' : '⚠ Answers, but not in the requested format') +
      (r.latency_ms != null ? ' · ' + (r.latency_ms / 1000).toFixed(1) + ' s' : '');
  }

  var testResults = {};  // the last test of each model, kept while the list reloads

  function testSettingsModel(profile, out) {
    out = out || $('models-status');
    var approved = false;
    if (profile.remote) {
      approved = window.confirm('Send a fixed diagnostic prompt' + (profile.vision ? ' and a generated test image' : '') +
        ' to ' + profile.name + '? This uses your API account and budget. No library evidence is included.');
      if (!approved) return;
    }
    out.textContent = 'Testing…';
    post('/api/models/test', { profile: profile.name, vision: !!profile.vision, approved: approved }).then(function (job) {
      (function tick() {
        api('/api/jobs/' + job.id).then(function (j) {
          if (j.status === 'running') { setTimeout(tick, 1000); return; }
          testResults[profile.name] = out.textContent = testResult(j);
          loadModels();  // the test may have loaded the model: refresh the server line
        }).catch(function (e) { out.textContent = '✗ ' + e.message; });
      })();
    }).catch(function (e) { out.textContent = '✗ ' + e.message; });
  }

  function initModels() {
    $('models-refresh').onclick = loadModels;
    $('models-tier').onchange = function () {
      $('models-apply').disabled = false;
      showCloudPickers();
      if (this.value === 'cloud' && typeof searchCatalogue === 'function') searchCatalogue();
    };
    $('models-apply').onclick = function () { reviewModelsChange({ action: 'setup', tier: $('models-tier').value || null,
      cheap: $('cloud-cheap').value || null, strong: $('cloud-strong').value || null }); };
    $('models-cloud').onclick = function () { reviewModelsChange({ action: 'cloud', enabled: !modelsInfo.cloud_enabled }); };
    $('models-add').onsubmit = function (e) {
      e.preventDefault();
      function price(id) { return $(id).value.trim() ? Number($(id).value) : null; }
      reviewModelsChange({ action: 'add', provider: $('models-provider').value,
        model: $('models-id').value.trim(), name: $('models-name').value.trim(),
        price_in: price('models-price-in'), price_out: price('models-price-out'),
        tasks: Array.from($('models-add-tasks').querySelectorAll('input:checked')).map(function (input) { return input.value; }),
        vision: $('models-add-vision').checked, reasoning: $('models-reasoning').value });
    };
  }
  /* ---------- End Models settings ---------- */

  /* ---------- Updates ---------- */
  var updateTimer = null;

  function renderUpdate(info) {
    var status = info.status, latest = info.latest || {};
    if (typeof info.update_check === 'boolean') $('update-auto').checked = info.update_check;
    $('update-auto-note').textContent = info.update_check && info.update_check_enabled === false ?
      'Automatic checks are disabled by LITFORGE_NO_UPDATE_CHECK=1.' : '';
    $('update-notice').hidden = status === 'disabled';
    $('update-badge').hidden = status !== 'available';
    var labels = {
      up_to_date: 'LitForge is up to date',
      available: 'An update is available: ' + info.behind + ' new changes — ' + (latest.subject || '') +
        (latest.date ? ' (' + String(latest.date).slice(0, 10) + ')' : ''),
      diverged: 'Local and remote history have diverged (' + info.behind + ' behind, ' + info.ahead + ' ahead).',
      not_git: 'This copy was installed without git and cannot update itself.',
      error: 'Could not check for updates', checking: 'Checking for updates…', disabled: ''
    };
    $('update-status').textContent = labels[status] || labels.error;
    var message = info.message || '';
    $('update-message').textContent = message.replace(/\.$/, '') === labels[status] ? '' : message;
    $('update-message').hidden = !$('update-message').textContent;
    $('update-command').textContent = info.command || '';
    $('update-instructions').hidden = !info.command || status === 'disabled';
    $('update-check').disabled = status === 'checking';
  }

  function loadUpdates() {
    clearTimeout(updateTimer);
    return api('/api/update').then(function (info) {
      renderUpdate(info);
      if (info.status === 'checking') updateTimer = setTimeout(loadUpdates, 1000);
    }).catch(function () { renderUpdate({ status: 'error' }); });
  }

  function initUpdates() {
    $('update-check').onclick = function () {
      clearTimeout(updateTimer);
      renderUpdate({ status: 'checking' });
      return post('/api/update/check', {}).then(renderUpdate)
        .catch(function () { renderUpdate({ status: 'error' }); });
    };
    $('update-copy').onclick = function () {
      if (!navigator.clipboard) { toast('Select the command and copy it.'); return; }
      return navigator.clipboard.writeText($('update-command').textContent)
        .then(function () { toast('Copied'); }).catch(function () { toast('Select the command and copy it.'); });
    };
    $('update-auto').onchange = function () {
      return post('/api/settings', { update_check: $('update-auto').checked }).then(loadUpdates)
        .catch(function () { toast('Could not save update setting.'); });
    };
    loadUpdates();
  }
  /* ---------- End Updates ---------- */

  /* ---------- Settings ---------- */
  var localRepo = null, activeDownload = null, catalogueGeneration = 0, localBackend = 'hf', settingsAssignment = null;

  function settingsError(e) { $('models-status').textContent = e.message; }

  function renderTaskChecks(id, tasks) {
    var container = $(id), chosen = Array.from(container.querySelectorAll('input:checked')).map(function (i) { return i.value; });
    container.replaceChildren();
    tasks.forEach(function (task) {
      var label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      label.className = 'check'; input.type = 'checkbox'; input.value = task; input.checked = chosen.indexOf(task) >= 0;
      text.textContent = taskLabel(task);
      label.appendChild(input); label.appendChild(text); container.appendChild(label);
    });
  }
  function selectedLocalTasks() {
    return Array.from($('local-tasks').querySelectorAll('input:checked')).map(function (input) { return input.value; });
  }
  var TASK_GROUPS = [
    ['Library and analysis', ['ask', 'audit', 'matrix_extract', 'claim_extract', 'claim_relation', 'asset_analyze',
      'equation_compare', 'gap_design', 'console_plan', 'page_rewrite']],
    ['Screening', ['screen', 'screen_fulltext']],
    ['Writing', ['write_fix', 'write_rewrite', 'write_cite', 'write_chat', 'write_summary', 'write_image']]];

  // Tasks in groups; the ones this model already does are ticked and fixed ("uses it now"). Saving moves the
  // newly ticked tasks to this model (after the usual review of the file change).
  function assignSettingsTasks(name) {
    if (settingsAssignment) settingsAssignment.remove();
    var panel = document.createElement('div');
    panel.className = 'models-assign';
    settingsAssignment = panel;
    var names = modelsInfo.task_names || [], tasks = modelsInfo.tasks || {};
    var known = [].concat.apply([], TASK_GROUPS.map(function (g) { return g[1]; }));
    var groups = TASK_GROUPS.concat([['Other', names.filter(function (t) { return known.indexOf(t) < 0; })]]);
    var head = document.createElement('p');
    head.textContent = 'Tick the tasks ' + name + ' should do:';
    panel.appendChild(head);
    groups.forEach(function (g) {
      var list = g[1].filter(function (t) { return names.indexOf(t) >= 0; });
      if (!list.length) return;
      var box = document.createElement('fieldset'), legend = document.createElement('legend');
      legend.textContent = g[0]; box.appendChild(legend);
      list.forEach(function (task) {
        var label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
        var now = tasks[task] && tasks[task].profile === name;
        label.className = 'check'; input.type = 'checkbox'; input.value = task; input.checked = now; input.disabled = now;
        text.textContent = taskLabel(task) + (now ? ' (uses it now)' : '');
        label.appendChild(input); label.appendChild(text); box.appendChild(label);
      });
      panel.appendChild(box);
    });
    var row = document.createElement('div');
    row.className = 'row2';
    row.appendChild(modelButton('Save', function () {
      var chosen = Array.from(panel.querySelectorAll('input:checked:not(:disabled)')).map(function (i) { return i.value; });
      if (!chosen.length) { toast('Tick at least one task'); return; }
      reviewModelsChange({ action: 'assign', name: name, tasks: chosen });  // a save reloads the list
    }));
    row.appendChild(modelButton('Cancel', function () { panel.remove(); settingsAssignment = null; }));
    panel.appendChild(row);
    $('models-profiles').appendChild(panel);
  }

  function loadKeys() {
    return api('/api/keys').then(function (info) {
      $('keys-list').replaceChildren(); $('keys-name').replaceChildren();
      (info.keys || []).forEach(function (key) {
        var row = document.createElement('div'), text = document.createElement('span'), option = document.createElement('option');
        row.className = 'models-profile'; text.textContent = key.name + ' · ' + key.source; row.appendChild(text);
        option.value = key.name; option.textContent = key.name; $('keys-name').appendChild(option);
        if (info.available && key.source === 'keychain') row.appendChild(modelButton('Remove keychain entry', function () {
          if (!window.confirm('Remove the system keychain entry for ' + key.name + '? Environment variables stay set.')) return;
          api('/api/keys', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: key.name }) })
            .then(loadKeys).catch(settingsError);
        }));
        $('keys-list').appendChild(row);
      });
      $('keys-save').disabled = !info.available;
      $('keys-value').disabled = !info.available;
      $('keys-status').textContent = info.available ? 'Environment values take precedence over the system keychain. Values are never shown.' :
        'System keychain unavailable. Set keys as environment variables (on Linux, secret-tool enables keychain storage).';
    }).catch(settingsError);
  }

  function renderCatalogue(id, entries) {
    var select = $(id), previous = select.value;
    select.replaceChildren();
    var empty = document.createElement('option'); empty.value = '';
    empty.textContent = id === 'cloud-cheap' ? 'Choose an image-capable model' : 'Use the cheap model for analysis too';
    select.appendChild(empty);
    entries.forEach(function (item) {
      var option = document.createElement('option'); option.value = item.id;
      var input = Number(item.input_per_million_usd), output = Number(item.output_per_million_usd), context = Number(item.context_length);
      if (!Number.isFinite(input) || !Number.isFinite(output) || !Number.isFinite(context) || input < 0 || output < 0 || context <= 0) return;
      option.textContent = item.name + ' · ' + item.id + ' · $' + input + ' in / $' + output + ' out per million · ' + context + ' context' +
        (Array.isArray(item.input_modalities) && item.input_modalities.indexOf('image') >= 0 ? ' · accepts images' : '') + (item.label ? ' · ' + item.label : '');
      select.appendChild(option);
    });
    if (entries.some(function (item) { return item.id === previous; })) select.value = previous;
  }
  function searchCatalogue() {
    var generation = ++catalogueGeneration;
    var query = '?q=' + encodeURIComponent($('catalogue-query').value.trim());
    if ($('catalogue-max').value.trim()) query += '&max_input_price=' + encodeURIComponent($('catalogue-max').value);
    return Promise.all([api('/api/models/catalogue' + query + '&vision=1'), api('/api/models/catalogue' + query)])
      .then(function (results) {
        if (generation !== catalogueGeneration) return;
        renderCatalogue('cloud-cheap', results[0]); renderCatalogue('cloud-strong', results[1]);
        if (!results[0].length) $('models-status').textContent = 'No image-capable catalogue models matched. Try another search.';
      }).catch(settingsError);
  }
  function loadSettings() {
    return Promise.all([loadModels(), loadKeys(), loadOptionalTools()]).then(function () {
      if (modelsInfo && modelsInfo.proposal && modelsInfo.proposal.recommended_tier === 'cloud' && !$('cloud-cheap').value) return searchCatalogue();
    });
  }

  function renderLocalRepos(repos) {
    $('local-results').replaceChildren(); $('local-files').replaceChildren();
    $('local-options').hidden = true;
    repos.forEach(function (repo) {
      var row = document.createElement('div'), text = document.createElement('span'), link = document.createElement('a');
      row.className = 'models-profile'; text.textContent = repo.id + ' · ' + Number(repo.downloads || 0) + ' downloads · ' + repo.badge + (repo.label ? ' · ' + repo.label : '');
      row.appendChild(text);
      // Build a trusted origin from a validated identifier; catalogue-supplied card URLs are not used.
      if (/^[A-Za-z0-9_-][A-Za-z0-9._-]*\/[A-Za-z0-9_-][A-Za-z0-9._-]*$/.test(repo.id) && repo.id.indexOf('..') < 0) {
        link.href = 'https://huggingface.co/' + repo.id; link.target = '_blank'; link.rel = 'noopener noreferrer';
      }
      link.textContent = 'Model card ↗'; row.appendChild(link);
      row.appendChild(modelButton('Choose files', function () { loadLocalFiles(repo.id); }));
      $('local-results').appendChild(row);
    });
  }
  function loadLocalFiles(repo) {
    localRepo = repo;
    $('local-options').hidden = false;  // name, context and tasks are set before choosing a file to download
    $('local-files').textContent = 'Loading quantizations…';
    return api('/api/models/local-files?repo=' + encodeURIComponent(repo)).then(function (tree) {
      if (localRepo !== repo) return;
      $('local-files').replaceChildren(); $('local-mmproj').replaceChildren();
      var empty = document.createElement('option'); empty.value = ''; empty.textContent = 'Text only'; $('local-mmproj').appendChild(empty);
      (tree.projectors || []).forEach(function (file) {
        var option = document.createElement('option'); option.value = file.path;
        option.textContent = file.path + ' · ' + (Number(file.size) / 1e9).toFixed(2) + ' GB'; $('local-mmproj').appendChild(option);
      });
      (tree.models || []).forEach(function (file) {
        var row = document.createElement('div'), text = document.createElement('span'); row.className = 'models-profile';
        text.textContent = file.path + ' · ' + (Number(file.size) / 1e9).toFixed(2) + ' GB · ' + file.fit.badge +
          (file.fit.note ? ' · ' + file.fit.note : '') + (tree.vision ? ' · image projector available' : '') +
          (!file.fit.disk_ok ? ' · insufficient disk space' : '');
        row.appendChild(text); row.appendChild(modelButton('Download…', function () { previewDownload(repo, file.path); }));
        $('local-files').appendChild(row);
      });
      if (!(tree.models || []).length) $('local-files').textContent = 'No verifiable main GGUF files found.';
    }).catch(settingsError);
  }
  function previewDownload(repo, file) {
    post('/api/models/download-preview', { repo: repo, file: file, mmproj: $('local-mmproj').value || null,
      name: $('local-name').value.trim() || null, context_tokens: Number($('local-context').value), tasks: selectedLocalTasks() })
      .then(function (preview) {
        $('models-diff').textContent = preview.diff;
        var fit = preview.fit, tooLarge = fit.badge === 'too large';
        if (tooLarge && !window.confirm('This model is too large for this machine. Download anyway?')) return;
        if (!window.confirm(fit.badge + ' · Needs about ' + fit.needed_gb.toFixed(2) + ' GB; budget ' + fit.budget_gb.toFixed(2) +
            ' GB. ' + fit.note + '\n' + preview.notice + '\n\n' + preview.diff)) return;
        return post('/api/models/download', { token: preview.token, confirmed: true, too_large_confirmed: tooLarge }).then(watchModelDownload);
      }).catch(settingsError);
  }
  function watchModelDownload(job) {
    activeDownload = job.id;
    (function tick() {
      api('/api/jobs/' + job.id).then(function (report) {
        var info = report.report || {};
        $('local-progress').textContent = report.status === 'failed' ? report.error :
          (info.file || '') + ' · ' + (info.state || info.status || report.status) +
          (info.total ? ' · ' + (info.bytes || info.completed || 0) + '/' + info.total + ' bytes' : '') +
          (info.speed ? ' · ' + (info.speed / 1e6).toFixed(1) + ' MB/s · ETA ' + Math.ceil(info.eta_s || 0) + ' s' : '');
        $('download-controls').hidden = report.status !== 'running';
        if (report.status === 'running') setTimeout(tick, 750);
        else if (info.state === 'pulled' && window.confirm(info.diff + '\nRegister this Ollama profile?')) {
          post('/api/models/apply', { token: info.token, confirmed: true }).then(loadSettings).catch(settingsError);
        } else if (info.state === 'registered') { loadSettings(); refreshModelChoices(); }
      }).catch(settingsError);
    })();
  }
  // One tab of cards at a time; the review output (file changes, status) shows under every tab.
  function showSettingsTab(name) {
    state.settingsTab = name;
    document.querySelectorAll('#view-settings [data-stab]').forEach(function (el) {
      if (el.tagName === 'BUTTON') el.classList.toggle('on', el.dataset.stab === name);
      else el.hidden = el.dataset.stab !== name;
    });
  }

  function initSettings() {
    document.querySelectorAll('.settings-tabs button').forEach(function (b) {
      b.onclick = function () { showSettingsTab(b.dataset.stab); };
    });
    showSettingsTab(state.settingsTab || 'general');
    $('keys-form').onsubmit = function (event) {
      event.preventDefault();
      var request = post('/api/keys', { name: $('keys-name').value, value: $('keys-value').value });
      $('keys-value').value = '';
      return request.then(function () { $('models-status').textContent = 'Key saved in system keychain.'; return loadKeys(); }).catch(settingsError);
    };
    $('catalogue-search').onsubmit = function (event) { event.preventDefault(); searchCatalogue(); };
    $('settings-budget').onsubmit = function (event) {
      event.preventDefault();
      reviewModelsChange({ action: 'budget', policy: $('settings-policy').value, budget: {
        max_per_call_usd: Number($('budget-call').value), max_per_run_usd: Number($('budget-run').value),
        max_per_month_usd: Number($('budget-month').value) } });
    };
    $('settings-library').onsubmit = function (event) {
      event.preventDefault();
      reviewModelsChange({ action: 'library', mailto: $('settings-mailto').value, exports_root: $('settings-exports').value })
        .then(function () { return api('/api/info'); }).then(function (info) { state.exportsRoot = info.exports_root; showPaths(); });
    };
    ['hf', 'ollama'].forEach(function (source) {
      $('local-' + source).onclick = function () {
        localBackend = source;
        ['hf', 'ollama'].forEach(function (name) {
          $('local-' + name).classList.toggle('on', name === source);
          $('local-' + name).setAttribute('aria-pressed', String(name === source));
        });
        $('local-query').placeholder = source === 'hf' ? 'Search GGUF models' : 'Exact Ollama model name';
        $('local-options').hidden = source === 'hf' && !localRepo;  // an Ollama pull takes its tasks from here
      };
    });
    $('local-search').onsubmit = function (event) {
      event.preventDefault();
      var query = $('local-query').value.trim();
      if (localBackend === 'ollama') {
        if (window.confirm('Pull ' + query + ' through your local Ollama server?')) {
          post('/api/models/pull-ollama', { name: query, tasks: selectedLocalTasks(), confirmed: true }).then(watchModelDownload).catch(settingsError);
        }
        return;
      }
      $('local-results').textContent = 'Searching public Hugging Face models…';
      api('/api/models/local-search?q=' + encodeURIComponent(query) + '&show_all=' + ($('local-show-all').checked ? '1' : '0')).then(renderLocalRepos).catch(settingsError);
    };
    ['pause', 'resume', 'cancel'].forEach(function (action) {
      $('download-' + action).onclick = function () {
        if (!activeDownload) return;
        post('/api/models/download-control', { id: activeDownload, action: action }).then(watchModelDownload).catch(settingsError);
      };
    });
  }
  /* ---------- End Settings ---------- */


  /* ---------- Evidence Lens ---------- */

  var lens = { profiles: [], project: null, answer: null, matrix: null, tab: 'answer', audit: false, busy: false, answerRequest: 0, matrixRequest: 0, claimRequest: 0 };

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function lensNode(tag, text, cls) {
    var el = document.createElement(tag);
    if (text != null) el.textContent = String(text);
    if (cls) el.className = cls;
    return el;
  }

  function lensButton(text, action, cls) {
    var b = lensNode('button', text, cls || 'btn small');
    b.type = 'button'; b.onclick = action; return b;
  }

  var LENS_LABEL = {
    reported_fact: 'Reported', interpretation: 'Interpretation', unsupported: 'Unsupported',
    reported: 'Reported', not_reported: 'Not reported', not_found: 'Not found',
    not_applicable: 'Not applicable', error: 'Error',
    quote_not_in_evidence: 'Quote not in evidence', unquoted_fact: 'No exact quote',
    citation_without_quote: 'Not every citation quoted',
    number_not_in_evidence: 'Number not in evidence', uncited_fact: 'No citation',
    uncited_interpretation: 'No citation', uncited_value: 'No citation',
    option_mismatch: 'Option not allowed', type_mismatch: 'Type mismatch',
    evidence_changed_since_review: 'Evidence changed since review', invalid_kind: 'Invalid sentence kind',
    truncated: 'Answer shortened', invalid_citations: 'Invalid citations removed',
    supported: 'Supported', partial: 'Partly supported', contradicted: 'Contradicted',
    not_audited: 'Not audited', open: '', accepted: 'Accepted', rejected: 'Rejected', edited: 'Edited',
    sentence: 'Sentence', cell: 'Cell', single_source: 'Single-source', proposed: 'Proposed',
    unsupported_hypothesis: 'Unsupported hypothesis', replicates: 'Replicated', contradicts: 'Contradicted'
  };

  function lensLabel(value) {
    var key = String(value).toLowerCase();
    if (key === 'needs_docling' || key === 'parse_status:needs_docling') return 'needs accurate parser';
    return Object.prototype.hasOwnProperty.call(LENS_LABEL, key) ? LENS_LABEL[key] :
      key.replace(/_/g, ' ').replace(/^./, function (c) { return c.toUpperCase(); });
  }

  function lensEvidenceQuote(text, limit) {
    var full = String(text || ''), cap = limit || 300;
    if (full.length <= cap) return { text: full, truncated: false };
    var end = cap - 1, prefix = full.slice(0, end + 1);
    var boundary = prefix.search(/\s+\S*$/);
    if (boundary > 0) end = boundary;
    return { text: full.slice(0, end).trimEnd() + '…', truncated: true };
  }

  function lensFidelityLabels(mode, fidelity, result) {
    fidelity = fidelity || {}; result = result || {};
    var flags = fidelity.flags || [];
    if (mode === 'table') {
      var fraction = Number(fidelity.table_number_fraction);
      var labels = ['' + (Number.isFinite(fraction) ? Math.round(fraction * 100) : 0) +
        ' % of numbers found in page text'];
      labels.push('Units recognised: ' + ((fidelity.units_recognized || []).join(', ') || 'none'));
      return labels.concat(flags);
    }
    if (mode === 'chart') {
      var found = fidelity.legend_entries_found || [];
      return ['Legend entries in caption: ' + found.length + ' of ' + (result.legend || []).length].concat(flags);
    }
    if (mode === 'equation') {
      return ['Symbols found in text: ' + ((fidelity.symbols_found || []).join(', ') || 'none')].concat(flags);
    }
    return [];
  }

  function lensBadges(parent, labels, flags) {
    var badges = lensNode('div', null, 'lens-badges');
    function add(value, flag) {
      if (!value) return;
      var label = lensLabel(value), key = String(value).toLowerCase();
      if (!label) return;
      var severity = (flag && key !== 'citation_without_quote') || ['partial', 'unsupported', 'contradicted', 'contradicts', 'uncertain', 'exclude', 'single_source', 'unsupported_hypothesis', 'stale'].indexOf(key) >= 0 ? 'warning' :
        ['supported', 'supports', 'replicates', 'include'].indexOf(key) >= 0 ? 'ok' : 'neutral';
      badges.appendChild(lensNode('span', label, 'badge ' + severity));
    }
    labels.forEach(function (value) { add(value, false); });
    (flags || []).forEach(function (value) { add(value, true); });
    parent.appendChild(badges);
  }

  function lensQuoteRanges(text, ranges) {
    var length = Array.from(text).length, merged = [];
    (ranges || []).filter(function (r) {
      return Number.isInteger(r.start) && Number.isInteger(r.end) && r.start >= 0 && r.end > r.start && r.end <= length;
    }).sort(function (a, b) { return a.start - b.start; }).forEach(function (r) {
      var last = merged[merged.length - 1];
      if (last && r.start <= last.end) last.end = Math.max(last.end, r.end);
      else merged.push({ start: r.start, end: r.end });
    });
    return merged;
  }

  function lensEvidence(parent, spans, highlights) {
    if (!spans.length) parent.appendChild(lensNode('p', 'No cited evidence.', 'hint'));
    spans.forEach(function (s) {
      var card = lensNode('article', null, 'lens-evidence');
      card.appendChild(lensNode('h3', s.title || s.paper_id));
      if (s.version_label && s.version === 'submitted') {
        card.appendChild(lensNode('span', s.version_label, 'badge version'));
      }
      card.appendChild(lensNode('p', [s.section || s.kind, 'p.' + s.page].join(' · '), 'meta'));
      var fullText = String(s.text || ''), quote = lensEvidenceQuote(fullText);
      var ranges = lensQuoteRanges(fullText, (highlights || []).filter(function (r) { return r.evidence_id === s.id; }));
      var excerpt = lensNode('blockquote', ranges.length ? null : quote.text);
      if (ranges.length) {
        // Python offsets count Unicode code points; JS string offsets count UTF-16 units.
        var chars = Array.from(fullText), cursor = 0;
        ranges.forEach(function (r) {
          excerpt.appendChild(document.createTextNode(chars.slice(cursor, r.start).join('')));
          excerpt.appendChild(lensNode('mark', chars.slice(r.start, r.end).join('')));
          cursor = r.end;
        });
        excerpt.appendChild(document.createTextNode(chars.slice(cursor).join('')));
      }
      card.appendChild(excerpt);
      if (quote.truncated && !ranges.length) {
        var toggle = lensButton('Show more', function () {
          var expanded = toggle.textContent === 'Show less';
          excerpt.textContent = expanded ? lensEvidenceQuote(fullText).text : fullText;
          toggle.textContent = expanded ? 'Show more' : 'Show less';
        }, 'btn small evidence-toggle');
        card.appendChild(toggle);
      }
      card.appendChild(lensNode('p', s.id + ' · ' + s.parser + ' ' + s.parser_version, 'hint'));
      var link = lensNode('a', 'Open PDF at page ' + s.page, 'btn small');
      link.href = '/api/papers/' + encodeURIComponent(s.paper_id) + '/pdf#page=' + encodeURIComponent(s.page);
      link.target = '_blank'; link.rel = 'noopener'; card.appendChild(link); parent.appendChild(card);
    });
  }

  function lensDrawer(title, spans, target, highlights) {
    drawerRequest++;
    $('dhead').replaceChildren(lensNode('h2', title));
    $('dbody').replaceChildren();
    lensEvidence($('dbody'), spans, highlights);
    if (target) lensReviewEditor($('dbody'), target);
    $('drawer').classList.add('open'); $('drawer').setAttribute('aria-hidden', 'false');
    $('drawer-close').focus();
  }

  function lensReviewEditor(parent, target) {
    var box = lensNode('div', null, 'lens-review-editor');
    var valueLabel = lensNode('label', 'Edited value');
    var value = lensNode('textarea'); value.rows = 3;
    value.value = target.value == null ? '' : String(target.value);
    valueLabel.appendChild(value); box.appendChild(valueLabel);
    var reasonLabel = lensNode('label', 'Reason (optional)');
    var reason = lensNode('textarea'); reason.rows = 2;
    reasonLabel.appendChild(reason); box.appendChild(reasonLabel);
    var actions = lensNode('div', null, 'lens-actions');
    ['accept', 'reject', 'edit'].forEach(function (action) {
      actions.appendChild(lensButton(action.charAt(0).toUpperCase() + action.slice(1), function () {
        var edited = value.value;
        if (target.kind === 'cell' && target.type !== 'text') {
          try { edited = JSON.parse(edited); } catch (e) { /* server validates types */ }
        }
        actions.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        post('/api/review', { target_kind: target.kind, target_id: target.id, action: action,
          new_value: action === 'edit' ? edited : null, reason: reason.value }).then(function () {
          closeDrawer(); toast('Review saved');
          if (lens.answer) loadLensAnswer(lens.answer.run_id);
          if (lens.matrix) loadLensMatrix(lens.matrix.id);
          if (lens.tab === 'review') renderLensReviews();
        }).catch(function (e) { toast(e.message); actions.querySelectorAll('button').forEach(function (b) { b.disabled = false; }); });
      }));
    });
    box.appendChild(actions); parent.appendChild(box);
  }

  function lensSentence(sentence, evidence) {
    var row = lensNode('article', null, 'lens-row');
    row.appendChild(lensNode('p', sentence.text));
    lensBadges(row, [sentence.kind, sentence.audit_verdict || 'not_audited',
      sentence.review_status], sentence.flags);
    if (sentence.audit_note) row.appendChild(lensNode('p', sentence.audit_note, 'hint'));
    var cited = evidence.filter(function (e) { return sentence.evidence_ids.indexOf(e.id) >= 0; });
    (sentence.quotes || []).slice(0, 3).forEach(function (quote) {
      var span = cited.find(function (s) { return s.id === quote.evidence_id; });
      var text = String(quote.text || '').slice(0, 400).trim();
      var words = text.split(/\s+/).filter(function (word) { return /[\p{L}\p{N}]/u.test(word); });
      if (!span || words.length < 2 || !lensQuoteRanges(span.text, [quote]).length) return;
      var block = lensNode('blockquote', text, 'lens-quote');
      block.appendChild(lensButton((span.title || span.paper_id) + ' · p.' + span.page, function () {
        lensDrawer('Evidence', [span], null, [quote]);
      }, 'btn small quote-chip'));
      row.appendChild(block);
    });
    var chips = lensNode('div', null, 'lens-actions');
    cited.forEach(function (s) {
      chips.appendChild(lensButton((s.title || s.paper_id) + ' · p.' + s.page, function () {
        lensDrawer('Evidence', [s]);
      }));
    });
    chips.appendChild(lensButton('Review', function () {
      lensDrawer(sentence.text, cited, { kind: 'sentence', id: sentence.id, value: sentence.text });
    }));
    row.appendChild(chips); return row;
  }

  function renderLensAnswer() {
    var el = $('lens-answer'); el.replaceChildren();
    document.querySelectorAll('[data-lens-tab]').forEach(function (b) {
      var selected = b.dataset.lensTab === lens.tab;
      b.classList.toggle('on', selected); b.setAttribute('aria-selected', String(selected));
      b.tabIndex = selected ? 0 : -1;
    });
    el.setAttribute('aria-labelledby', 'lens-tab-' + lens.tab);
    $('lens-audit-filter').hidden = lens.tab !== 'answer';
    if (lens.tab === 'review') { renderLensReviews(); return; }
    if (!lens.answer) { el.appendChild(lensNode('p', 'Ask a question to see its answer and evidence.', 'hint')); return; }
    if (lens.tab === 'evidence') { lensEvidence(el, lens.answer.evidence); return; }
    el.appendChild(lensNode('h3', lens.answer.question));
    if (lens.answer.insufficient) el.appendChild(lensNode('p', 'Evidence is insufficient for a complete answer.', 'hint'));
    lensBadges(el, [], (lens.answer.flags || []).concat(lens.answer.invalid_citations.length ? ['invalid_citations'] : []));
    var sentences = lens.answer.sentences.filter(function (s) { return !lens.audit || s.audit_focus; });
    sentences.forEach(function (s) { el.appendChild(lensSentence(s, lens.answer.evidence)); });
    if (!sentences.length) el.appendChild(lensNode('p', 'No sentences match this filter.', 'hint'));
  }

  function renderLensReviews() {
    var project = state.project;
    if (!project) { $('lens-answer').replaceChildren(lensNode('p', 'Choose a project first.', 'hint')); return; }
    api('/api/review?project=' + encodeURIComponent(project || '')).then(function (items) {
      if (project !== state.project || lens.tab !== 'review') return;
      var el = $('lens-answer'); el.replaceChildren();
      if (!items.length) el.appendChild(lensNode('p', 'No open reviews in this project.', 'hint'));
      items.forEach(function (item) {
        var row = lensNode('article', null, 'lens-row');
        var context = item.target_kind === 'sentence' ? item.question :
          [item.matrix_name, item.paper_title, item.column_label].filter(Boolean).join(' · ');
        row.appendChild(lensNode('h3', context));
        row.appendChild(lensNode('p', item.text));
        lensBadges(row, [item.target_kind, item.audit_verdict], item.flags);
        row.appendChild(lensButton('Inspect and review', function () {
          if (item.target_kind === 'cell') {
            loadLensMatrix(item.matrix_id).then(function (m) {
              if (project !== state.project) return;
              var cell = m.cells.find(function (c) { return c.id === item.target_id; });
              if (cell) openLensCell(cell, m);
            }).catch(function (e) { toast(e.message); });
          } else lensDrawer(item.text, item.evidence, { kind: 'sentence', id: item.target_id, value: item.text });
        }));
        el.appendChild(row);
      });
    }).catch(function (e) { toast(e.message); });
  }

  function loadLensAnswer(id) {
    var project = state.project, request = ++lens.answerRequest;
    return api('/api/answers/' + encodeURIComponent(id)).then(function (answer) {
      if (project !== state.project || request !== lens.answerRequest) return;
      lens.answer = answer; store.set('answer-' + project, answer.run_id);
      renderLensAnswer();
      $('lens-ask-status').textContent = lensAnswerSummary(answer);
      $('lens-run-audit').disabled = lens.busy;
      return answer;
    }).catch(function (e) { toast(e.message); });
  }

  function enterLens(view) {
    if (lens.project !== state.project) {
      lens.project = state.project; lens.answer = null; lens.matrix = null;
      $('lens-matrix-detail').hidden = true; $('lens-answer').replaceChildren();
      $('lens-question').value = ''; $('lens-ask-status').textContent = '';
      $('lens-claims-clusters').replaceChildren(); $('lens-claims-detail').replaceChildren();
      $('lens-claims-status').textContent = '';
      $('living-snapshots').replaceChildren(); $('living-signals').replaceChildren();
      $('living-report').textContent = ''; $('living-status').textContent = '';
      var saved = store.get('answer-' + state.project, null);
      if (saved) loadLensAnswer(saved);
    }
    if (!state.project) { toast('Choose a project first'); return; }
    if (view === 'claims') { loadLensClaims(); loadLivingPanel(); }
    else if (view === 'ask') renderLensAnswer(); else loadLensMatrices();
  }

  function lensVersionScope(allVersions) {
    return allVersions ? 'all versions' : 'primary versions only';
  }

  function confirmLensPreview(plan, allVersions) {
    return new Promise(function (resolve) {
      var dialog = $('lens-preview');
      $('lens-preview-title').textContent = plan.remote ? 'Send evidence to a remote model?' : 'Run this action?';
      $('lens-preview-hint').textContent = plan.remote ?
        'The listed evidence will leave this computer. Confirm to start this action.' :
        'Local models only; nothing leaves this computer. Confirm to start this action.';
      $('lens-preview-confirm').textContent = plan.remote ? 'Confirm and send' : 'Confirm';
      $('lens-preview-content').textContent = 'Profile: ' + plan.profiles.join(', ') + '\nProvider: ' + plan.providers.join(', ') +
        '\nModel: ' + plan.models.join(', ') + '\nCalls: ' + plan.number_of_calls + '\nEvidence spans: ' + plan.evidence_ids_count +
        '\nEstimated input tokens: ' + plan.estimated_input_tokens + '\nMaximum cost: ' +
        (plan.max_cost_usd == null ? 'unknown' : '$' + plan.max_cost_usd.toFixed(4)) +
        '\nVersions: ' + lensVersionScope(allVersions) +
        ((plan.model_notices || []).length ? '\n' + plan.model_notices.join('\n') : '');
      dialog.returnValue = '';
      dialog.onclose = function () { resolve(dialog.returnValue === 'approved'); };
      $('lens-preview-confirm').onclick = function () { dialog.close('approved'); };
      $('lens-preview-cancel').onclick = function () { dialog.close('cancel'); };
      dialog.showModal();
    });
  }

  function lensAnswerSummary(answer) {
    var profile = lens.profiles.find(function (p) { return p.model === answer.model && p.provider === answer.provider; });
    return answer.sentences.length + ' sentences · ' + answer.evidence.length + ' evidence spans · ' +
      (answer.model ? answer.model + ' (' + (profile && profile.remote ? 'cloud' : 'local') + ')' : 'No model used');
  }

  function lensBusy(busy) {
    lens.busy = busy;
    document.querySelectorAll('[data-lens-work], #lens-matrix-form button[type=submit], #lens-add-column-form button[type=submit]').forEach(function (b) { b.disabled = busy; });
    $('lens-run-audit').disabled = busy || !lens.answer;
  }

  function startLensTask(task, path, extra, statusId, autoAudit, onDone) {
    if (lens.busy || !state.project) return;
    var project = state.project, statusNode = $(statusId);
    var body = Object.assign({ task: task, project: project }, extra);
    // Each run chooses its own scope; the library's "Show all versions" view toggle never widens it.
    body.all_versions = !!(extra && extra.all_versions === true);
    lensBusy(true);
    $('lens-ask-status').textContent = ''; $('lens-matrix-status').textContent = '';
    $('lens-run-audit').disabled = true;
    post('/api/preview', body).then(function (plan) {
      if (plan.blocked_reasons.length) throw new Error('Action blocked: ' + plan.blocked_reasons.join(', '));
      if (plan.remote || plan.requires_approval) return confirmLensPreview(plan, body.all_versions);
      // Local only: nothing leaves the computer, so inform without blocking (e.g. "starts llama-server").
      if ((plan.model_notices || []).length) statusNode.textContent = plan.model_notices.join(' · ');
      return null;
    }).then(function (approved) {
      if (approved === false || project !== state.project) { lensBusy(false); return; }
      if (approved === true) body.approved = true;
      if (!statusNode.textContent) statusNode.textContent = 'Starting…';
      if (task !== 'audit') statusNode.textContent += ' · ' + lensVersionScope(body.all_versions);
      return post(path, body).then(function (job) {
        (function tick() {
          api('/api/jobs/' + job.id).then(function (j) {
            if (project === state.project) statusNode.textContent = j.status === 'running' ?
              (j.messages[j.messages.length - 1] || 'Working…') : j.status === 'failed' ? j.error : task === 'matrix_extract' ?
                j.report.stats.extracted + ' cells extracted, ' + j.report.stats.cached + ' cached, ' + j.report.stats.flagged + ' flagged' :
                task === 'claim_extract' ? j.report.claims.length + ' claims · ' + j.report.cached + ' cached' :
                task === 'claim_relation' ? j.report.relations.length + ' relations assessed' :
                task === 'screen_fulltext' ? j.report.screened + ' screened · ' + j.report.needs_pdf.length + ' need PDF' :
                lensAnswerSummary(j.report);
            if (project === state.project && j.status === 'done' && j.report.budget_stopped)
              statusNode.textContent += ' · Stopped at budget';
            if (j.status === 'running') { setTimeout(tick, 900); return; }
            lensBusy(false);
            if (project !== state.project || j.status !== 'done') return;
            if (onDone) { onDone(j); return; }
            if (task === 'claim_extract' || task === 'claim_relation') { loadLensClaims(); return; }
            if (task === 'screen_fulltext') { if (state.runId) loadRun(state.runId); return; }
            if (task === 'matrix_extract') loadLensMatrix(body.matrix_id).catch(function (e) { toast(e.message); });
            else loadLensAnswer(j.run_id).then(function (answer) {
              if (answer && task === 'ask' && autoAudit && project === state.project) {
                startLensTask('audit', '/api/audit', { run_id: j.run_id, profile: body.profile }, statusId);
              }
            });
          }).catch(function (e) { lensBusy(false); toast(e.message); });
        })();
      });
    }).catch(function (e) { lensBusy(false); statusNode.textContent = e.message; toast(e.message); });
  }

  function lensColumnEditor(parent, removable) {
    var box = lensNode('div', null, 'lens-column');
    var grid = lensNode('div', null, 'lens-column-grid');
    [['key', 'Key (slug)'], ['label', 'Label'], ['instruction', 'Extraction instruction'], ['type', 'Type'],
      ['unit', 'Unit (optional)'], ['options', 'Categorical options (one per line)']].forEach(function (field) {
      var label = lensNode('label', field[1], 'f');
      var input = lensNode(field[0] === 'type' ? 'select' : field[0] === 'options' || field[0] === 'instruction' ? 'textarea' : 'input');
      input.dataset.columnField = field[0];
      if (input.tagName === 'INPUT') input.type = 'text';
      if (input.tagName === 'TEXTAREA') input.rows = 2;
      if (field[0] === 'type') ['text', 'number', 'categorical', 'boolean'].forEach(function (t) {
        var option = lensNode('option', t); option.value = t; input.appendChild(option);
      });
      input.required = ['key', 'label', 'instruction'].indexOf(field[0]) >= 0;
      label.appendChild(input); grid.appendChild(label);
    });
    box.appendChild(grid);
    if (removable) box.appendChild(lensButton('Remove column', function () { box.remove(); }));
    parent.appendChild(box);
  }

  function lensReadColumn(box) {
    var data = {};
    box.querySelectorAll('[data-column-field]').forEach(function (input) { data[input.dataset.columnField] = input.value.trim(); });
    data.options = data.options ? data.options.split('\n').map(function (s) { return s.trim(); }).filter(Boolean) : null;
    data.unit = data.unit || null; return data;
  }

  function loadLensMatrices(selected) {
    var project = state.project;
    return api('/api/matrices?project=' + encodeURIComponent(project || '')).then(function (items) {
      if (project !== state.project) return;
      var select = $('lens-matrices'); select.replaceChildren(lensNode('option', 'Choose a matrix'));
      select.firstChild.value = '';
      items.forEach(function (m) { var option = lensNode('option', m.name); option.value = m.id; select.appendChild(option); });
      select.value = selected || (lens.matrix ? lens.matrix.id : '');
      if (select.value) return loadLensMatrix(select.value);
    }).catch(function (e) { toast(e.message); });
  }

  function openLensCell(cell, matrix) {
    var col = matrix.columns.find(function (c) { return c.key === cell.column_key; });
    var paper = matrix.papers.find(function (p) { return p.id === cell.paper_id; });
    lensDrawer((paper ? paper.title : cell.paper_id) + ' · ' + col.label,
      matrix.evidence.filter(function (s) { return cell.evidence_ids.indexOf(s.id) >= 0; }),
      { kind: 'cell', id: cell.id, type: col.type, value: cell.value });
    var summary = lensNode('article', null, 'lens-row');
    summary.appendChild(lensNode('p', cell.value == null ? 'No reported value' : cell.value));
    lensBadges(summary, [cell.status, cell.review_status], cell.flags);
    if (cell.note) summary.appendChild(lensNode('p', cell.note, 'hint'));
    $('dbody').prepend(summary);
  }

  function loadLensMatrix(id) {
    var project = state.project, request = ++lens.matrixRequest;
    return api('/api/matrices/' + encodeURIComponent(id) +
      (state.libraryAllVersions ? '?all_versions=1' : '')).then(function (m) {
      if (project !== state.project || request !== lens.matrixRequest) return m;
      lens.matrix = m; $('lens-matrices').value = m.id; $('lens-matrix-detail').hidden = false;
      $('lens-matrix-title').textContent = m.name; $('lens-matrix-description').textContent = m.question;
      $('lens-warnings').textContent = m.warnings.map(function (w) {
        return w.replace(/\bnot_found\b/g, LENS_LABEL.not_found);
      }).join('\n');
      $('lens-exports').replaceChildren();
      ['md', 'csv', 'json'].forEach(function (fmt) {
        var a = lensNode('a', 'Export ' + fmt.toUpperCase(), 'btn small');
        a.href = '/api/matrices/' + encodeURIComponent(m.id) + '/export?format=' + fmt;
        a.download = ''; $('lens-exports').appendChild(a);
      });
      var table = $('lens-table'); table.replaceChildren();
      var head = lensNode('thead'), header = lensNode('tr'), first = lensNode('th', 'Paper');
      first.scope = 'col'; header.appendChild(first);
      m.columns.forEach(function (c) { var th = lensNode('th', c.label); th.scope = 'col'; header.appendChild(th); });
      head.appendChild(header); table.appendChild(head);
      var cells = {};
      m.cells.forEach(function (c) { cells[c.paper_id + '/' + c.column_key] = c; });
      var tbody = lensNode('tbody');
      m.papers.forEach(function (p) {
        var tr = lensNode('tr'), title = lensNode('th', p.title || p.id); title.scope = 'row'; tr.appendChild(title);
        m.columns.forEach(function (c) {
          var td = lensNode('td'), cell = cells[p.id + '/' + c.key];
          if (cell) {
            var b = lensButton(cell.value == null ? '—' : String(cell.value) + (cell.unit ? ' ' + cell.unit : ''), function () { openLensCell(cell, m); }, 'btn lens-cell');
            lensBadges(b, [cell.status, cell.review_status], cell.flags); td.appendChild(b);
          } else td.appendChild(lensNode('span', 'Not extracted', 'hint'));
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody); return m;
    });
  }

  function lensResultBadges(parent, paper) {
    if (paper.fulltext_screening) {
      var s = paper.fulltext_screening;
      var badge = lensNode('span', 'S2 · ' + lensLabel(s.decision),
        'badge ' + (s.decision === 'include' ? 'ok' : 'warning'));
      badge.title = (s.stale ? 'Criteria changed. ' : '') + (s.result.reason || ''); parent.appendChild(badge);
    }
    if (paper.review_queue) {
      var q = paper.review_queue;
      var badge = lensNode('span', 'Review ' + q.info.toFixed(3), 'badge neutral');
      badge.title = q.reason; parent.appendChild(badge);
      if (q.predicted_relevant != null) {
        var prediction = lensNode('span', 'Nearby marks: ' + Math.round(q.predicted_relevant * 100) + '% relevant', 'badge neutral');
        prediction.title = 'Prediction from nearby human marks; model decisions stay unchanged.'; parent.appendChild(prediction);
      }
    }
  }

  function lensScreeningDetail(parent, paper) {
    var s = paper.fulltext_screening;
    if (s) {
      var box = lensNode('section'); box.appendChild(lensNode('h3', 'Full-text screening (S2)'));
      lensBadges(box, [s.decision], s.stale ? ['Criteria changed'] : []);
      box.appendChild(lensNode('p', s.result.reason));
      ['inclusion', 'exclusion'].forEach(function (kind) {
        (s.result[kind] || []).forEach(function (c, index) {
          var citation = ((s.result.criterion_evidence || {})[kind] || [])[index];
          if (citation && citation.flags) lensBadges(box, [], citation.flags);
          box.appendChild(lensNode('p', c.criterion + ': ' + lensLabel(c.status)));
          if (c.evidence) box.appendChild(lensNode('blockquote', c.evidence));
          if (c.verified !== true && c.evidence) lensBadges(box, [], ['Quote not verified in cited span']);
        });
      });
      lensEvidence(box, s.evidence || []); parent.appendChild(box);
    }
    if ((paper.screening_history || []).length) {
      var history = lensNode('details'); history.appendChild(lensNode('summary', 'Screening history'));
      paper.screening_history.forEach(function (h) {
        history.appendChild(lensNode('p', h.stage + ' · ' + lensLabel(h.decision) + ' · ' + h.source +
          ' · ' + h.created_at + ' · criteria ' + h.criteria_hash + (h.model ? ' · ' + h.model : '') +
          (h.prompt_version ? ' · ' + h.prompt_version : '') + ': ' + h.reason));
      }); parent.appendChild(history);
    }
  }

  function loadLensClaims(cluster) {
    var project = state.project, request = ++lens.claimRequest;
    if (!project) return Promise.resolve();
    return api('/api/claims?project=' + encodeURIComponent(project) +
      (cluster ? '&cluster=' + encodeURIComponent(cluster) : '') +
      (state.libraryAllVersions ? '&all_versions=1' : '')).then(function (data) {
      if (project !== state.project || request !== lens.claimRequest) return;
      function paperLabel(id) {
        var paper = (data.papers || {})[id];
        return paper ? (paper.title || 'Untitled paper') + (paper.year != null ? ' (' + paper.year + ')' : '') : 'Unknown paper';
      }
      var list = $('lens-claims-clusters'), detail = $('lens-claims-detail');
      list.replaceChildren(); detail.replaceChildren();
      if (!data.clusters.length) list.appendChild(lensNode('p', 'Extract and cluster claims to see the landscape.', 'hint'));
      data.clusters.forEach(function (c) {
        var row = lensNode('article', null, 'lens-row');
        row.appendChild(lensButton(c.label, function () { loadLensClaims(c.cluster_id); }));
        lensBadges(row, c.single_source ? ['single_source'] : [], c.contradicting_papers ? ['contradicted'] : []);
        if (c.replication_count) lensBadges(row, ['Replicated ×' + c.replication_count], []);
        row.appendChild(lensNode('p', c.papers.length + ' papers · Evidence strength ' + c.strength +
          ' · ' + c.cited_by_count + ' citations · Origin: ' + paperLabel(c.origin_paper_id), 'hint'));
        if (c.later_citing_papers.length) row.appendChild(lensNode('p', 'Later citing papers: ' + c.later_citing_papers.map(paperLabel).join(', '), 'hint'));
        list.appendChild(row);
      });
      var evidence = {}; data.evidence.forEach(function (e) { evidence[e.id] = e; });
      data.claims.forEach(function (claim) {
        var row = lensNode('article', null, 'lens-row');
        row.appendChild(lensNode('h3', claim.text));
        row.appendChild(lensNode('p', paperLabel(claim.paper_id) + ' · ' + lensLabel(claim.type) + ' · ' + lensLabel(claim.polarity), 'meta'));
        lensBadges(row, [claim.status], []);
        var cards = lensNode('details'); cards.appendChild(lensNode('summary', 'Cited evidence'));
        lensEvidence(cards, claim.evidence_ids.map(function (id) { return evidence[id]; }).filter(Boolean));
        row.appendChild(cards);
        var actions = lensNode('div', null, 'lens-actions');
        ['accept', 'reject'].forEach(function (action) {
          actions.appendChild(lensButton(lensLabel(action), function () {
            post('/api/claims/review', { id: claim.id, action: action }).then(function () {
              if (project === state.project) loadLensClaims(cluster);
            }).catch(function (e) { toast(e.message); });
          }));
        }); row.appendChild(actions); detail.appendChild(row);
      });
      data.relations.forEach(function (r) {
        var row = lensNode('article', null, 'lens-row');
        var source = data.claims.find(function (c) { return c.id === r.source_claim_id; });
        var target = data.claims.find(function (c) { return c.id === r.target_claim_id; });
        row.appendChild(lensNode('h3', (source ? paperLabel(source.paper_id) + ': ' + source.text : 'Unknown claim') + ' → ' +
          (target ? paperLabel(target.paper_id) + ': ' + target.text : 'Unknown claim')));
        lensBadges(row, [r.relation, r.status], []); row.appendChild(lensNode('p', r.explanation));
        r.causes.forEach(function (cause) {
          var box = lensNode('section');
          lensBadges(box, ['hypothesis'], cause.unsupported_hypothesis ? ['unsupported_hypothesis'] : []);
          box.appendChild(lensNode('p', lensLabel(cause.factor) + ': ' + cause.detail));
          lensEvidence(box, cause.evidence_ids.map(function (id) { return evidence[id]; }).filter(Boolean)); row.appendChild(box);
        }); detail.appendChild(row);
      });
    }).catch(function (e) { if (project === state.project) toast(e.message); });
  }

  function renderLivingPanel(data) {
    $('living-snapshots').replaceChildren();
    data.snapshots.forEach(function (s) {
      var row = lensNode('article', null, 'snapshot-row');
      row.title = s.id + (s.content_hash ? ' · ' + s.content_hash : '');
      row.appendChild(lensNode('strong', s.label || 'Snapshot'));
      var date = new Date(s.created_at), time = lensNode('time', date.toLocaleString(), 'meta');
      time.dateTime = s.created_at; row.appendChild(time);
      var counts = s.counts;
      row.appendChild(lensNode('span', [plural(counts.papers, 'paper'), plural(counts.matrix, 'matrix cell'),
        plural(counts.claims, 'claim'), plural(counts.answers, 'answer')].join(' · '), 'meta'));
      $('living-snapshots').appendChild(row);
    });
    if (!data.snapshots.length) $('living-snapshots').appendChild(lensNode("p", "No snapshots yet.", "hint"));
    $('living-signals').replaceChildren();
    var signals = data.latest ? data.latest.signals : [];
    signals.forEach(function (s) { $('living-signals').appendChild(lensNode("p", s.message)); });
    if (!signals.length) $('living-signals').appendChild(lensNode("p", "No significant signals.", "hint"));
    $('living-report').textContent = data.report || "Run Refresh to produce a delta report.";
    if (data.latest) $('living-status').textContent = "Latest refresh: " + data.latest.status;
  }

  function loadLivingPanel() {
    var project = state.project;
    if (!project) return;
    return api("/api/snapshots?project=" + encodeURIComponent(project)).then(function (data) {
      if (project === state.project) renderLivingPanel(data);
    }).catch(function (e) { if (project === state.project) $('living-status').textContent = e.message; });
  }

  function refreshLivingProject() {
    if (!state.project || lens.busy) return;
    var project = state.project;
    lensBusy(true); $('living-status').textContent = "Starting refresh…";
    api('/api/models').then(function (info) {
      var tasks = ['screen', 'screen_fulltext', 'matrix_extract', 'claim_extract', 'claim_relation'];
      var remote = info.profiles.filter(function (profile) {
        return profile.remote && tasks.some(function (task) {
          return info.tasks[task] && info.tasks[task].profile === profile.name;
        });
      });
      var approved = remote.length > 0 && window.confirm('Refresh may send paper evidence to cloud models: ' +
        remote.map(function (profile) { return profile.model; }).join(', ') +
        '. Provider charges and configured budget limits apply. Approve cloud use for this refresh?');
      if (remote.length && !approved) { lensBusy(false); $('living-status').textContent = 'Refresh cancelled.'; return null; }
      return post('/api/refresh', { project: project, approved: approved });
    }).then(function (job) {
      if (!job) return;
      (function tick() {
        api("/api/jobs/" + job.id).then(function (j) {
          if (project === state.project) $('living-status').textContent = j.status === "running" ?
            (j.messages[j.messages.length - 1] || "Refreshing…") : j.status === "failed" ? j.error : "Refresh complete";
          if (j.status === "running") { setTimeout(tick, 900); return; }
          lensBusy(false);
          if (project === state.project) { loadLivingPanel(); loadLensClaims(); }
        }).catch(function (e) { lensBusy(false); if (project === state.project) $('living-status').textContent = e.message; });
      })();
    }).catch(function (e) { lensBusy(false); if (project === state.project) $('living-status').textContent = e.message; });
  }

  function reproEvidenceChips(parent, ids, evidence) {
    var chips = lensNode('span', null, 'lens-actions repro-citations');
    ids.forEach(function (id) {
      var span = evidence.find(function (s) { return s.id === id; });
      if (!span) return;
      var chip = lensButton((span.title || 'Evidence') + ' · p.' + span.page, function () {
        lensDrawer('Evidence', [span]);
      }, 'btn small repro-evidence-chip');
      chip.title = id; chips.appendChild(chip);
    });
    parent.appendChild(chips);
  }

  function renderReproCard(parent, card) {
    parent.replaceChildren();
    parent.appendChild(lensNode('h3', 'Reproducibility checklist'));
    card.checklist.forEach(function (item) {
      var row = lensNode('p', item.label + ': ' + item.state +
        (item.value == null ? '' : ' — ' + item.value), 'repro-item');
      reproEvidenceChips(row, item.evidence_ids || [], card.evidence);
      parent.appendChild(row);
    });
    parent.appendChild(lensNode('h3', 'Code and data links'));
    parent.appendChild(lensNode('p', 'Links are recorded from the paper and have not been checked.', 'hint'));
    card.links.forEach(function (link) {
      var row = lensNode('p', link.kind + ': ' + link.url, 'repro-link');
      reproEvidenceChips(row, [link.evidence_id], card.evidence); parent.appendChild(row);
    });
    if (!card.links.length) parent.appendChild(lensNode('p', 'No repository links found in the current text.', 'hint'));
    parent.appendChild(lensNode('h3', 'Reproduction brief'));
    card.sections.forEach(function (section) {
      var block = lensNode('section', null, 'repro-section');
      block.appendChild(lensNode('h4', section.title));
      section.items.forEach(function (item) {
        var row = lensNode('div', null, 'repro-item');
        var line = lensNode('p');
        line.appendChild(lensNode('strong', item.label + ': '));
        line.appendChild(lensNode('span', item.value)); row.appendChild(line);
        lensBadges(row, [item.status], []);
        reproEvidenceChips(row, item.evidence_ids, card.evidence);
        block.appendChild(row);
      });
      parent.appendChild(block);
    });
  }

  function attachReproTabs(paper, request) {
    var project = state.project, body = $('dbody');
    var overview = lensNode("div"), repro = lensNode("div"), tabs = lensNode("div", null, "tabs");
    overview.id = "paper-overview"; repro.id = "paper-repro"; repro.hidden = true;
    overview.setAttribute("role", "tabpanel"); repro.setAttribute("role", "tabpanel");
    while (body.firstChild) overview.appendChild(body.firstChild);
    var overviewButton = lensButton("Overview", function () { select(false); });
    var reproButton = lensButton("Repro", function () { select(true); });
    overviewButton.id = "paper-overview-tab"; reproButton.id = "paper-repro-tab";
    tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "Paper details");
    [overviewButton, reproButton].forEach(function (button, index) {
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", index ? repro.id : overview.id);
      button.onkeydown = function (e) {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(e.key) < 0) return;
        e.preventDefault(); var show = e.key === "End" || (e.key !== "Home" && button === overviewButton);
        select(show); (show ? reproButton : overviewButton).focus();
      };
      tabs.appendChild(button);
    });
    overview.setAttribute("aria-labelledby", overviewButton.id); repro.setAttribute("aria-labelledby", reproButton.id);
    var cardHost = lensNode("div"), status = lensNode("p", "", "hint");
    status.id = "paper-repro-status"; status.setAttribute("role", "status");
    if (project) {
      var extract = lensButton("Extract reproducibility", function () {
        if (request !== drawerRequest || project !== state.project || lens.busy) return;
        extract.disabled = true;
        post("/api/repro/matrix", { project: project }).then(function (matrix) {
          extract.disabled = false;
          if (request !== drawerRequest || project !== state.project) return;
          startLensTask("matrix_extract", "/api/matrices/" + matrix.id + "/extract",
            { matrix_id: matrix.id, paper_ids: [paper.id] }, status.id, false, function () {
              if (request === drawerRequest) load();
            });
        }).catch(function (e) { extract.disabled = false; status.textContent = e.message; });
      });
      extract.setAttribute("data-lens-work", ""); repro.appendChild(extract);
    }
    repro.appendChild(status); repro.appendChild(cardHost);
    function load() {
      status.textContent = "Loading reproducibility…";
      post("/api/papers/" + encodeURIComponent(paper.id) + "/repro", { project: project })
        .then(function (card) {
          if (request !== drawerRequest || project !== state.project) return;
          renderReproCard(cardHost, card); status.textContent = "";
        }).catch(function (e) { if (request === drawerRequest) status.textContent = e.message; });
    }
    var loaded = false;
    function select(showRepro) {
      overview.hidden = showRepro; repro.hidden = !showRepro;
      overviewButton.setAttribute("aria-selected", String(!showRepro)); reproButton.setAttribute("aria-selected", String(showRepro));
      overviewButton.tabIndex = showRepro ? -1 : 0; reproButton.tabIndex = showRepro ? 0 : -1;
      overviewButton.classList.toggle("on", !showRepro); reproButton.classList.toggle("on", showRepro);
      if (showRepro && !loaded) { loaded = true; load(); }
    }
    body.replaceChildren(tabs, overview, repro); select(false);
  }

  function refreshModelChoices() {
    api('/api/model-profiles').then(function (profiles) {
      lens.profiles = profiles;
      ['lens-profile', 'lens-matrix-profile', 'lens-claims-profile', 'lens-screen-profile', 'gaps-profile', 'console-profile'].forEach(function (id) {
        var select = $(id), previous = select.value;
        Array.from(select.children).forEach(function (option) { if (option.value) option.remove(); });
        profiles.forEach(function (p) {
          var option = lensNode('option', p.name + ' · ' + p.model + (p.remote ? ' (cloud)' : ''));
          option.value = p.name; $(id).appendChild(option);
        });
        select.value = previous;
      });
      if (lens.answer) $('lens-ask-status').textContent = lensAnswerSummary(lens.answer);
    }).catch(function (e) { toast(e.message); });
  }

  function initLens() {
    $('gaps-refresh').onclick = function () { loadGaps(); };
    $('living-take').onclick = function () {
      if (!state.project || lens.busy) return;
      var project = state.project;
      lensBusy(true);
      post("/api/snapshots", { project: project, label: $('living-label').value }).then(function () {
        if (project === state.project) { $('living-label').value = ""; loadLivingPanel(); }
      }).catch(function (e) { toast(e.message); }).finally(function () { lensBusy(false); });
    };
    $('living-refresh').onclick = refreshLivingProject;
    $('lens-claims-extract').onclick = function () {
      startLensTask('claim_extract', '/api/claims/extract', { profile: $('lens-claims-profile').value || null,
        force: $('lens-claims-force').checked, all_versions: $('lens-claims-all-versions').checked }, 'lens-claims-status');
    };
    $('lens-claims-cluster').onclick = function () {
      if (!state.project || lens.busy) return;
      var project = state.project; lensBusy(true);
      post('/api/claims/cluster', { project: project }).then(function () {
        if (project === state.project) loadLensClaims();
      }).catch(function (e) { toast(e.message); }).finally(function () { lensBusy(false); });
    };
    $('lens-claims-relate').onclick = function () {
      startLensTask('claim_relation', '/api/claims/relate', { profile: $('lens-claims-profile').value || null,
        max_pairs: Number($('lens-claims-pairs').value) }, 'lens-claims-status');
    };
    $('lens-screen-fulltext').onclick = function () {
      saveCriteria().then(function () {
        startLensTask('screen_fulltext', '/api/screen-fulltext', { profile: $('lens-screen-profile').value || null,
          top: Number($('screen-n').value), all_versions: $('lens-screen-all-versions').checked }, 'lens-screen-status');
      }).catch(function (e) { toast(e.message); });
    };

    refreshModelChoices();
    lensColumnEditor($('lens-columns'), true); lensColumnEditor($('lens-add-column'), false);
    $('lens-more-column').onclick = function () { lensColumnEditor($('lens-columns'), true); };
    $('lens-ask-form').onsubmit = function (e) {
      e.preventDefault(); startLensTask('ask', '/api/ask', { question: $('lens-question').value,
        profile: $('lens-profile').value.trim() || null, sensitive: $('lens-sensitive').checked,
        all_versions: $('lens-all-versions').checked }, 'lens-ask-status', $('lens-auto-audit').checked);
    };
    $('lens-run-audit').onclick = function () {
      if (!lens.answer) { toast('Ask a question first'); return; }
      startLensTask('audit', '/api/audit', { run_id: lens.answer.run_id, profile: $('lens-profile').value.trim() || null }, 'lens-ask-status');
    };
    document.querySelectorAll('[data-lens-tab]').forEach(function (b, index, tabs) {
      b.onclick = function () { lens.tab = b.dataset.lensTab; renderLensAnswer(); };
      b.onkeydown = function (e) {
        var next = e.key === 'ArrowRight' ? (index + 1) % tabs.length :
          e.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length :
          e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : -1;
        if (next >= 0) { e.preventDefault(); tabs[next].click(); tabs[next].focus(); }
      };
    });
    $('lens-audit-filter').onclick = function () {
      lens.audit = !lens.audit; this.classList.toggle('on', lens.audit); this.setAttribute('aria-pressed', String(lens.audit)); renderLensAnswer();
    };
    $('lens-matrices').onchange = function () {
      if (this.value) loadLensMatrix(this.value).catch(function (e) { toast(e.message); });
      else { lens.matrix = null; $('lens-matrix-detail').hidden = true; }
    };
    $('lens-matrix-form').onsubmit = function (e) {
      e.preventDefault();
      if (!state.project) { toast('Choose a project first'); return; }
      var columns = Array.from($('lens-columns').children).map(lensReadColumn);
      post('/api/matrices', { project: state.project, name: $('lens-matrix-name').value,
        question: $('lens-matrix-question').value, columns: columns }).then(function (m) {
        $('lens-create-panel').open = false; return loadLensMatrices(m.id);
      }).catch(function (err) { toast(err.message); });
    };
    $('lens-add-column-form').onsubmit = function (e) {
      e.preventDefault(); if (!lens.matrix) return;
      post('/api/matrices/' + encodeURIComponent(lens.matrix.id) + '/columns', lensReadColumn($('lens-add-column')))
        .then(function (m) { return loadLensMatrix(m.id); }).catch(function (err) { toast(err.message); });
    };
    $('lens-extract').onclick = function () {
      if (!lens.matrix) return;
      startLensTask('matrix_extract', '/api/matrices/' + encodeURIComponent(lens.matrix.id) + '/extract',
        { matrix_id: lens.matrix.id, profile: $('lens-matrix-profile').value.trim() || null, force: $('lens-force').checked,
          all_versions: $('lens-matrix-all-versions').checked }, 'lens-matrix-status');
    };
  }

  function init() {
    initUpdates();
    initModels();
    initSettings();
    initLens();
    wireWrite();
    // Each open page checks in; when none does for a while, the server stops itself (stop_when_idle).
    // If the server is gone (it stopped while the computer slept, say), the page says so instead of failing quietly.
    var beat = setInterval(function () {
      post('/api/heartbeat', {}).catch(function (e) {
        if (e instanceof TypeError) { clearInterval(beat); showStopped(); }  // fetch failed: no server answered
      });
    }, 15000);
    document.querySelectorAll('[data-mode]').forEach(function (b) { b.onclick = function () { setMode(b.dataset.mode); }; });
    $('pdfs-export').checked = store.get('pdfs-export', true);
    $('pdfs-export-scope').textContent = scopeWords();
    $('pdfs-export').onchange = function () { store.set('pdfs-export', this.checked); setMode(state.mode); };
    setMode(store.get('mode', 'similar'));
    if (location.hash === '#models') history.replaceState(null, '', '#settings');
    var linked = /^#run=([\w-]+)$/.exec(location.hash);  // a bookmarked search; never set automatically
    showView(/^#(recents|library|ask|matrix|claims|gaps|console|write|settings)$/.test(location.hash) ? location.hash.slice(1) : 'new');
    if (linked) loadRun(linked[1]);
    window.addEventListener('hashchange', function () {
      if (/^#(ask|matrix|claims|gaps|recents|library|console|write|settings)$/.test(location.hash)) showView(location.hash.slice(1));
    });
    $('sort').value = state.sort;

    var drop = $('drop'), dropHtml = drop.innerHTML;
    function showSeedPdfs() {
      drop.innerHTML = !state.seedPdfs.length ? dropHtml : state.seedPdfs.map(function (f, i) {
        return '<span class="file">' + esc(f.name) + '</span> · <a href="#" data-remove="' + i + '">remove</a>';
      }).join('<br>') + '<br><a href="#" id="pick">add more</a>';
      drop.querySelectorAll('[data-remove]').forEach(function (a) {
        a.onclick = function (e) { e.preventDefault(); state.seedPdfs.splice(Number(a.dataset.remove), 1); showSeedPdfs(); };
      });
      dropZone(drop, $('file'), $('pick'), /\.pdf$/i, function (files) {
        state.seedPdfs = addSeedPdfs(state.seedPdfs, files);
        $('file').value = '';
        showSeedPdfs();
      }, true);
    }
    showSeedPdfs();
    var dropList = $('drop-list'), listHtml = dropList.innerHTML;
    function resetList() { state.list = null; $('file-list').value = ''; dropList.innerHTML = listHtml; wireList(); }
    function wireList() {
      dropZone(dropList, $('file-list'), $('pick-list'), /\.(csv|bib|ris)$/i,
        function (f) { state.list = f; showFile(dropList, f, resetList); });
    }
    wireList();
    wirePdfs();
    $('project-name').oninput = function () {
      // Switching to another existing project shows its folder; for a new name the default follows the name.
      if (state.projects.some(function (x) { return x.name === this.value.trim(); }, this)) state.dirEdited = false;
      showPaths();
    };
    $('export-dir').oninput = function () { state.dirEdited = true; showPaths(); };

    $('btn-run').onclick = run;
    $('btn-more').onclick = function () { startSimilar({ from_relevant: true }); };
    $('console-form').onsubmit = function (e) {
      e.preventDefault();
      var request = $('console-request').value.trim();
      if (!request) return;
      $('console-plan').disabled = true;
      $('console-status').textContent = 'Planning with the closed action registry…';
      $('console-preview').replaceChildren();
      planConsole(request, false).then(function (result) { renderConsolePreview(result); })
        .catch(function (error) { $('console-status').textContent = error.message; })
        .then(function () { $('console-plan').disabled = false; });
    };
    $('console-run').onclick = function () {
      if (!state.consolePlan || !window.confirm('Execute the previewed steps?')) return;
      $('console-run').disabled = true;
      runConsolePlan(false).then(function (result) {
        state.consoleRunId = result.run_id;
        $('console-run').hidden = true;
        $('console-cancel').hidden = false;
        pollConsoleRun(result.run_id, true);
      }).catch(function (error) { $('console-status').textContent = error.message; })
        .then(function () { $('console-run').disabled = false; });
    };
    $('console-cancel').onclick = cancelConsoleRun;
    document.querySelectorAll('.sidebar [data-view]').forEach(function (b) {
      b.onclick = function () {
        if (b.dataset.view === 'new') { newSearch(); return; }
        if (b.dataset.view === 'recents') state.recentsProject = null;
        showView(b.dataset.view);
      };
    });
    $('recents-project').onchange = function () { state.recentsProject = this.value || null; loadRecents(); };
    $('library-filter').oninput = renderLibrary;
    $('library-pdf').onclick = function () {
      state.libraryPdf = !state.libraryPdf;
      this.classList.toggle('on', state.libraryPdf);
      renderLibrary();
    };
    $('library-unused').onclick = function () {
      state.libraryUnused = !state.libraryUnused;
      this.classList.toggle('on', state.libraryUnused);
      this.setAttribute('aria-pressed', String(!!state.libraryUnused));
      renderLibrary();
    };
    $('library-versions').classList.toggle('on', state.libraryAllVersions);
    $('library-versions').setAttribute('aria-pressed', String(!!state.libraryAllVersions));
    $('library-versions').onclick = function () {
      state.libraryAllVersions = !state.libraryAllVersions;
      store.set('library-all-versions', state.libraryAllVersions);
      this.classList.toggle('on', state.libraryAllVersions);
      this.setAttribute('aria-pressed', String(!!state.libraryAllVersions));
      loadLibrary();
      if (state.runId) loadRun(state.runId);
      if (lens.matrix) loadLensMatrix(lens.matrix.id);
      if (state.project) loadLensClaims();
    };
    $('library-remove-unused').onclick = function () { removeLibraryPapers(); };
    $('btn-missing').onclick = function () {
      var scope = $('export-scope').value;  // start from what export would cover
      $('missing-scope').value = scope === 'visible' ? 'all' : scope;
      $('missing-status').textContent = '';
      $('missing-report').hidden = true;
      state.fetchReasons = {};
      showMissing();
      api('/api/settings').then(function (s) { showMail(s.mailto); }).catch(function () { showMail(null); });
    };
    $('missing-fetch').onclick = fetchMissing;
    $('added-close').onclick = function () { $('added').close(); };
    $('mathsetup-close').onclick = $('mathsetup-cancel').onclick = function () {
      state.settingsVisionSetup = false;
      $('mathsetup').close();
    };
    $('mathsetup-medium').onclick = function () {
      $('mathsetup').close();
      $('export-math').value = 'medium';
      store.set('export-math', 'medium');
      $('btn-export').click();
    };
    $('mathsetup-install').onclick = function () {
      $('mathsetup').close();
      if (state.settingsVisionSetup) {
        state.settingsVisionSetup = false;
        $('tool-vision-progress').textContent = 'Setting up the image model…';
        post('/api/math-setup', {}).then(function (job) {
          return watchOptionalToolJob(job, $('tool-vision-progress'), loadOptionalTools);
        }).catch(function (e) { $('tool-vision-progress').textContent = e.message; });
        return;
      }
      setBusy(true);
      post('/api/math-setup', { project: state.project }).then(poll)
        .catch(function (e) { setBusy(false); toast(e.message); });
    };
    $('missing-scope').onchange = showMissing;
    $('missing-close').onclick = function () { $('missing').close(); };
    $('missing').onclick = function (e) { if (e.target === this) this.close(); };  // click outside
    $('missing-add').onclick = function (e) {
      e.preventDefault();
      $('missing').close();
      newSearch();
      setMode('pdfs');
    };
    $('export-scope').value = store.get('export-scope', 'all');
    $('pdfs-export-scope').textContent = scopeWords();
    $('export-scope').onchange = function () { store.set('export-scope', this.value); $('pdfs-export-scope').textContent = scopeWords(); };
    $('export-fetch').checked = store.get('export-fetch', false);
    $('export-fetch').onchange = function () { store.set('export-fetch', this.checked); };
    $('export-all-versions').checked = store.get('export-all-versions', false);
    $('export-all-versions').onchange = function () { store.set('export-all-versions', this.checked); };
    $('export-math').value = store.get('export-math', store.get('export-vision', false) ? 'high' : 'off');
    $('export-math').onchange = function () { store.set('export-math', this.value); };
    $('btn-export').onclick = function () {
      if (!state.project) { toast('No project yet'); return; }
      var scope = $('export-scope').value, body = { project: state.project };
      body.all_versions = $('export-all-versions').checked;
      if ($('project-name').value.trim() === state.project) body.export_dir = $('export-dir').value.trim();
      if (scope === 'visible' && !state.data) { toast('Open a run first'); return; }
      Object.assign(body, exportSelection());
      var math = $('export-math').value;
      if ($('export-fetch').checked || math !== 'off') {  // slow steps first: a background job
        var start = function () {
          setBusy(true);
          post('/api/export', Object.assign(body, { fetch: body.fetch === undefined ? $('export-fetch').checked : body.fetch, math: math, run_id: state.runId }))
            .then(poll).catch(function (e) { setBusy(false); toast(e.message); });
        };
        if (math !== 'high') { start(); return; }
        api('/api/math-setup').then(function (st) {  // Ollama or the model missing: offer to set them up
          if (st.disabled) { toast(st.message); return; }
          if (st.ready) {
            if (st.remote) {
              var previewPages = function () {
                post('/api/export-preview', body).then(function (preview) {
                  if (preview.blocked_reasons.length) { toast(preview.blocked_reasons.join('; ')); return; }
                  if (!window.confirm(preview.image_description + ' to ' + st.model + ' for transcription. ' +
                      'Estimated maximum cost $' + preview.max_cost_usd.toFixed(4) + '. Approve this run?')) return;
                  body.approved = true; start();
                }).catch(function (e) { setBusy(false); toast(e.message); });
              };
              if ($('export-fetch').checked) {
                // Fetch first so the approval includes the exact currently uncached page-image count.
                post('/api/fetch', body).then(function (job) {
                  setBusy(true);
                  (function waitForPDFs() {
                    api('/api/jobs/' + job.id).then(function (progress) {
                      if (progress.status === 'running') { setTimeout(waitForPDFs, 1000); return; }
                      setBusy(false);
                      if (progress.status === 'failed') { toast(progress.error); return; }
                      body.fetch = false; previewPages();
                    }).catch(function (e) { setBusy(false); toast(e.message); });
                  })();
                }).catch(function (e) { toast(e.message); });
              } else previewPages();
              return;
            }
            if (st.notice) toast(st.notice);
            start();
          } else showMathSetup(st);
        }).catch(function (e) { toast(e.message); });
        return;
      }
      post('/api/export', body)
        .then(function (r) {
          if (!r.papers) toast(scope === 'relevant' ? 'Nothing exported: no paper is marked ✓ yet' : 'Nothing to export');
          else toast('Exported ' + r.papers + ' papers to ' + r.path);
          return loadProjects();  // the folder just used is now the project's saved folder
        })
        .catch(function (e) { toast(e.message); });
    };

    $('bulk-top').onclick = function () {
      var n = Math.max(1, parseInt($('bulk-n').value, 10) || 0);
      bulkMark(state.view.slice(0, n), 'relevant', 'papers at the top of this list');
    };
    $('bulk-rest').onclick = function () {
      bulkMark(state.view.filter(function (p) { return !p.mark; }), 'irrelevant', 'unmarked papers');
    };
    document.querySelectorAll('[data-bulk]').forEach(function (b) {
      b.onclick = function () { bulkMark(state.view.slice(), b.dataset.bulk || null, 'papers in this list'); };
    });

    $('screen-panel').open = store.get('screen-open', false);
    $('screen-panel').ontoggle = function () { store.set('screen-open', this.open); };
    $('btn-help').onclick = openGuide;
    $('btn-quit').onclick = function () { quit(false); };
    $('guide-close').onclick = function () { $('guide').close(); };
    $('guide').onclick = function (e) { if (e.target === this) this.close(); };  // click outside
    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-help]')) { e.preventDefault(); openGuide(); }
    });
    wireTooltips();

    var THEMES = ['auto', 'light', 'dark'], THEME_LABEL = { auto: '◐ Auto', light: '☀ Light', dark: '☾ Dark' };
    function applyTheme(t) {
      if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
      $('theme-ico').textContent = THEME_LABEL[t].split(' ')[0];
      $('theme-label').textContent = THEME_LABEL[t].split(' ')[1];
      $('btn-theme').setAttribute('aria-label', 'Theme: ' + THEME_LABEL[t].split(' ')[1]);
      store.set('theme', t);
    }
    applyTheme(store.get('theme', 'auto'));
    // The sidebar folds to a strip of icons for more room (hidden entirely on narrow screens); the choice is
    // remembered. Ctrl/Cmd+\ toggles it.
    function applySidebar(hidden) {
      document.querySelector('.app').classList.toggle('side-hidden', hidden);
      $('btn-side-show').hidden = !hidden;
      store.set('sidebar-hidden', hidden);
      var label = hidden ? 'Expand the sidebar (Ctrl/Cmd+\\)' : 'Fold the sidebar to icons (Ctrl/Cmd+\\)';
      $('btn-side-hide').title = label;
      $('btn-side-hide').setAttribute('aria-label', label);
      $('btn-side-hide').setAttribute('aria-expanded', String(!hidden));
      $('btn-side-show').setAttribute('aria-expanded', String(!hidden));
    }
    applySidebar(!!store.get('sidebar-hidden', false));
    $('btn-side-hide').onclick = function () {
      applySidebar(!document.querySelector('.app').classList.contains('side-hidden'));
    };
    $('btn-side-show').onclick = function () { applySidebar(false); $('btn-side-hide').focus(); };
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key === '\\') {
        e.preventDefault(); applySidebar(!document.querySelector('.app').classList.contains('side-hidden'));
      }
    });
    $('btn-theme').onclick = function () {
      var cur = store.get('theme', 'auto');
      applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
    };
    // The shift-click anchor is a list position, so forget it whenever the list is reordered or refiltered.
    $('filter').oninput = function () { state.filter = this.value.trim().toLowerCase(); state.anchor = -1; renderList(); };
    [['t-oa', 'oa'], ['t-new', 'onlyNew'], ['t-hide', 'hide']].forEach(function (t) {
      $(t[0]).onclick = function () {
        state[t[1]] = !state[t[1]]; state.anchor = -1; this.classList.toggle('on', state[t[1]]); renderList();
      };
    });
    $('sort').onchange = function () { state.sort = this.value; store.set('sort', state.sort); state.anchor = -1; renderList(); };
    $('drawer-close').onclick = closeDrawer;
    $('screen-filter').onchange = function () { state.screenFilter = this.value; state.anchor = -1; renderList(); };
    $('screen-n').oninput = function () { this.dataset.touched = '1'; };
    $('btn-crit-save').onclick = function () {
      if (!state.project || state.criteriaFor !== state.project) { toast('Open a run first'); return; }
      saveCriteria().then(function () { toast('Criteria saved'); }).catch(function (e) { toast(e.message); });
    };
    $('btn-screen').onclick = function () {
      if (!state.runId || state.criteriaFor !== state.project) { toast('Open a run first'); return; }
      var n = parseInt($('screen-n').value, 10);  // the server rejects values below 1
      if (isNaN(n)) n = state.criteria ? state.criteria.default_top : 100;
      setBusy(true);
      saveCriteria().then(function () { return post('/api/screen', { run_id: state.runId, top: n }); })
        .then(function (job) {
          state.sort = 'screening';
          store.set('sort', 'screening');
          $('sort').value = 'screening';
          poll(job);
        })
        .catch(function (e) { setBusy(false); toast(e.message); });
    };

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !document.querySelector('dialog[open]')) { closeDrawer(); return; }
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.metaKey || e.ctrlKey) return;
      if (e.key === '?' && !document.querySelector('dialog[open]')) { openGuide(); e.preventDefault(); return; }
      if (document.querySelector('dialog[open]')) return;
      if (e.key === ',') { showView('settings'); e.preventDefault(); return; }
      if (!state.data || state.page !== 'results') return;
      if (e.key === 'j' || e.key === 'ArrowDown') { select(state.sel + 1, $('drawer').classList.contains('open')); e.preventDefault(); }
      else if (e.key === 'k' || e.key === 'ArrowUp') { select(state.sel - 1, $('drawer').classList.contains('open')); e.preventDefault(); }
      else if (e.key === 'Enter' && state.sel >= 0) openDrawer(state.view[state.sel]);
      else if (e.key === 'Escape') closeDrawer();
      else if ((e.key === 'r' || e.key === 'x') && state.sel >= 0) {
        toggleMark(state.view[state.sel], e.key === 'r' ? 'relevant' : 'irrelevant');
        state.anchor = state.sel;
      }
    });

    api('/api/info').then(function (info) {
      state.exportsRoot = info.exports_root;
      var medium = $('export-math').querySelector('option[value="medium"]');
      if (info.math && !info.math.medium) {  // Pix2Text is an optional install
        medium.disabled = true;
        medium.textContent = 'Math: medium (install in Settings → Optional tools)';
        medium.title = 'Install Pix2Text in Settings → Optional tools, or run uv sync --extra formulas --inexact';
        if ($('export-math').value === 'medium') $('export-math').value = 'off';
      }
    })
      .catch(function () { /* older server: the field just starts empty */ })
      .then(loadProjects).then(function () {
        if (!state.projects.length && !store.get('guide-seen', false)) openGuide();
      }).catch(function (e) { toast(e.message); });
  }

  init();
})();
