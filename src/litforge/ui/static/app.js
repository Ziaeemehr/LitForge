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
    sel: -1, view: [], page: 'new', pdf: null, list: null, pdfs: [], missing: [], fetchReasons: {}, job: null,
    anchor: -1, exportsRoot: '', dirEdited: false, consolePlan: null, consoleRunId: null
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
    el.appendChild(modelButton('Settings', function () { $('missing').close(); showView('settings'); }));
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
  function showMathSetup(st) {
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
    $('mathsetup-install').textContent = st.installed ? 'Download the model and export' : 'Install and export';
    var medium = $('export-math').querySelector('option[value="medium"]');
    $('mathsetup-medium').hidden = medium.disabled;
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
    return api('/api/library').then(function (papers) { state.library = papers; renderLibrary(); })
      .catch(function (e) { toast(e.message); });
  }

  function renderLibrary() {
    var f = $('library-filter').value.trim().toLowerCase(), pdfOnly = state.libraryPdf;
    var list = (state.library || []).filter(function (p) {
      if (pdfOnly && !p.has_pdf) return false;
      if (!f) return true;
      return [p.title, p.venue, p.doi, p.authors.join(' '), p.projects.join(' ')].join(' ').toLowerCase().indexOf(f) >= 0;
    });
    var all = state.library || [];
    $('library-count').textContent = all.length + ' papers · ' + all.filter(function (p) { return p.has_pdf; }).length + ' with PDF';
    var el = $('library-list');
    if (!list.length) { el.innerHTML = '<div class="empty">' + (all.length ? 'No papers match.' : 'The library is empty.') + '</div>'; return; }
    el.innerHTML = list.slice(0, 1000).map(function (p, i) {
      var meta = [p.year, p.venue, p.authors.join(', ')].filter(Boolean).map(esc).join(' · ');
      var chips = p.projects.map(function (n) { return '<span class="chip">' + esc(n) + '</span>'; }).join('');
      return '<div class="row" data-i="' + i + '"><div><div class="title">' + esc(p.title || '(no title)') + '</div>' +
        '<div class="meta">' + meta + '</div>' + (chips ? '<div class="chips">' + chips + '</div>' : '') + '</div>' +
        '<div class="badges">' + (p.has_pdf ? '<a class="btn small" target="_blank" rel="noopener" href="/api/papers/' +
          esc(p.id) + '/pdf">PDF</a>' : (p.oa_url ? '<span class="badge oa">open</span>' : '')) + '</div></div>';
    }).join('');
    el.querySelectorAll('.row').forEach(function (row) {
      row.onclick = function (e) { if (!e.target.closest('a')) openDrawer(list[+row.dataset.i]); };
    });
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
    if (history.replaceState) history.replaceState(null, '', /^(recents|library|ask|matrix|claims|gaps|console|settings)$/.test(view) ? '#' + view : location.pathname);
    ['new', 'recents', 'library', 'ask', 'matrix', 'claims', 'gaps', 'console', 'settings'].forEach(function (v) { $(('view-' + v)).hidden = v !== view; });
    $('results').hidden = view !== 'results';
    document.querySelectorAll('.sidebar [data-view]').forEach(function (b) { b.classList.toggle('on', b.dataset.view === view); });
    document.querySelectorAll('#side-recent a').forEach(function (a) { a.classList.remove('on'); });
    if (view !== 'results') closeDrawer();
    if (view === 'recents') { $('recents-project').value = state.recentsProject || ''; loadRecents(); }
    if (view === 'library') loadLibrary();
    if (view === 'ask' || view === 'matrix' || view === 'claims') enterLens(view);
    if (view === 'gaps') loadGaps();
    if (view === 'console') loadConsole();
    if (view === 'settings') loadSettings();
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
    return api('/api/runs/' + id).then(function (data) {
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
        '<div class="meta">' + authors + '</div><div class="meta">' + meta + '</div>' +
        '<div class="links">' + paperLinks(d) + '</div>' +
        '<div class="dactions">' + (state.page === 'results' ? '<span class="marks">' +
        '<button class="rel' + (p.mark === 'relevant' ? ' on' : '') + '" data-mark="relevant" title="Relevant (r)">✓</button>' +
        '<button class="irr' + (p.mark === 'irrelevant' ? ' on' : '') + '" data-mark="irrelevant" title="Not relevant (x)">✗</button></span>' : '') +
        '<button class="btn small" id="btn-like" title="Start a new similar-paper search from this paper">Find similar to this</button>' +
        (d.markdown ? '<button class="btn small" id="btn-full" title="Show or hide the text extracted from its PDF">Full text</button>' : '') +
        (d.pdf_url ? '<a class="btn small" target="_blank" rel="noopener" href="' + esc(d.pdf_url) + '" title="Open the PDF in a new tab">Open PDF</a>' : '') + '</div>';
      $('dhead').querySelectorAll('button[data-mark]').forEach(function (b) {
        b.onclick = function () { toggleMark(p, b.dataset.mark); };
      });
      $('btn-like').onclick = function () { startSimilar({ seeds: [d.id] }); };

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
      html += '<h3>Abstract</h3><div class="md" id="abstract-md"></div>';
      if (d.markdown) html += '<div id="full" hidden><h3>Full text</h3><div class="md" id="full-md"></div></div>';
      $('dbody').innerHTML = html;
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
      attachReproTabs(d, request);
    }).catch(function (e) { if (request === drawerRequest) $('dbody').textContent = e.message; });
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
    return api('/api/gaps?project=' + encodeURIComponent(project)).then(function (items) {
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
    running.textContent = 'working…';
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
        if (j.status === 'failed') { logLine(j.error, 'err'); toast({ export: 'Export failed', pdfs: 'Adding PDFs failed', screen: 'Screening failed', setup: 'Setting up the vision model failed' }[j.kind] || 'Search failed'); return; }
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

  function uploadPdf() {
    if (!state.pdf) return Promise.resolve(null);
    logLine('uploading ' + state.pdf.name);
    return api('/api/upload?name=' + encodeURIComponent(state.pdf.name), { method: 'POST', body: state.pdf })
      .then(function (r) { return r.path; });
  }

  function run() {
    if (state.mode === 'similar') {
      var seed = $('seed').value.trim(), title = $('title').value.trim(), abstract = $('abstract').value.trim();
      if (!seed && !title && !abstract && !state.pdf) { toast('Give a DOI, a PDF, a title or an abstract'); return; }
      setBusy(true);
      $('progress').innerHTML = '';
      uploadPdf().then(function (path) {
        var seeds = seed ? seed.split(/[\s,]+/).filter(Boolean) : [];
        if (path) seeds.push(path);
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
  function addPdfs() {
    var folder = $('pdf-folder').value.trim(), files = state.pdfs.slice();
    if (!folder && !files.length) { toast('Give a folder or drop PDF files'); return; }
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

  function isPdf(f) { return /\.pdf$/i.test(f.name); }

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
      if (!picked.length) { toast('No PDF files found'); return; }
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

  function dropZone(zone, input, pick, accept, onFile) {
    pick.onclick = function (e) { e.preventDefault(); input.click(); };
    input.onchange = function () { if (input.files[0]) onFile(input.files[0]); };
    zone.ondragover = function (e) { e.preventDefault(); zone.classList.add('over'); };
    zone.ondragleave = function () { zone.classList.remove('over'); };
    zone.ondrop = function (e) {
      e.preventDefault();
      zone.classList.remove('over');
      var f = e.dataTransfer.files[0];
      if (f && accept.test(f.name)) onFile(f); else toast('Unsupported file');
    };
  }

  function showFile(zone, file, clear) {
    zone.innerHTML = '<span class="file">' + esc(file.name) + '</span> · <a href="#">remove</a>';
    zone.querySelector('a').onclick = function (e) { e.preventDefault(); clear(); };
  }

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
    gap_design: 'Design research gaps', console_plan: 'Plan console workflows', page_rewrite: 'Rewrite pages'
  };

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
      $('models-server').textContent = info.server.foreign ? 'Port is used by another server.' :
        info.server.running ? 'llama-server running' + (info.server.owned ? ' (managed by LitForge)' : ' (external)') +
          ' · ' + (info.server.preset_models || []).map(function (name) {
            var m = info.server.models.find(function (entry) { return entry.id === name; }) || { id: name };
            return m.id + ': ' + (typeof m.status === 'object' ? m.status.value : m.status || 'unknown');
          }).join(' · ') : 'llama-server stopped';
      $('models-tasks').textContent = JSON.stringify(info.tasks, null, 2);
      $('models-profiles').replaceChildren();
      info.profiles.forEach(function (p) {
        var row = document.createElement('div'), label = document.createElement('span');
        row.className = 'models-profile';
        label.textContent = p.name + ' · ' + p.model + (p.remote ? ' (cloud)' : ' (local)') + (p.vision ? ' · images' : '');
        row.appendChild(label);
        row.appendChild(modelButton('Test', function () { testSettingsModel(p); }));
        row.appendChild(modelButton('Assign tasks…', function () { assignSettingsTasks(p.name); }));
        if (p.name !== 'local' && p.name !== 'local_vision') row.appendChild(modelButton('Remove', function () {
          reviewModelsChange({ action: 'remove', name: p.name, delete_files: !p.remote && window.confirm('Also delete downloaded files owned by this profile?') });
        }));
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
        text.textContent = modelTaskLabels[task] || task.replace(/_/g, ' ');
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

  function testSettingsModel(profile) {
    var approved = false;
    if (profile.remote) {
      approved = window.confirm('Send a fixed diagnostic prompt' + (profile.vision ? ' and a generated test image' : '') +
        ' to ' + profile.name + '? This uses your API account and budget. No library evidence is included.');
      if (!approved) return;
    }
    $('models-status').textContent = 'Testing ' + profile.name + '…';
    post('/api/models/test', { profile: profile.name, vision: !!profile.vision, approved: approved }).then(function (job) {
      (function tick() {
        api('/api/jobs/' + job.id).then(function (j) {
          $('models-status').textContent = j.status === 'running' ? j.messages[j.messages.length - 1] || 'Testing…' :
            j.status === 'failed' ? j.error : JSON.stringify(j.report, null, 2);
          if (j.status === 'running') setTimeout(tick, 1000);
          else loadModels();
        }).catch(function (e) { $('models-status').textContent = e.message; });
      })();
    }).catch(function (e) { $('models-status').textContent = e.message; });
  }

  function initModels() {
    $('models-refresh').onclick = loadModels;
    $('models-tier').onchange = function () {
      $('models-apply').disabled = false;
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

  /* ---------- Settings ---------- */
  var localRepo = null, activeDownload = null, catalogueGeneration = 0, localBackend = 'hf', settingsAssignment = null;

  function settingsError(e) { $('models-status').textContent = e.message; }

  function renderTaskChecks(id, tasks) {
    var container = $(id), chosen = Array.from(container.querySelectorAll('input:checked')).map(function (i) { return i.value; });
    container.replaceChildren();
    tasks.forEach(function (task) {
      var label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      label.className = 'check'; input.type = 'checkbox'; input.value = task; input.checked = chosen.indexOf(task) >= 0;
      text.textContent = modelTaskLabels[task] || task;
      label.appendChild(input); label.appendChild(text); container.appendChild(label);
    });
  }
  function selectedLocalTasks() {
    return Array.from($('local-tasks').querySelectorAll('input:checked')).map(function (input) { return input.value; });
  }
  function assignSettingsTasks(name) {
    if (settingsAssignment) settingsAssignment.remove();
    var panel = document.createElement('div'), checks = document.createElement('div');
    settingsAssignment = panel;
    checks.id = 'settings-assign-checks'; checks.className = 'task-checks'; panel.appendChild(checks);
    $('models-profiles').appendChild(panel);
    renderTaskChecks(checks.id, modelsInfo.task_names || []);
    panel.appendChild(modelButton('Review assignments to ' + name, function () {
      var tasks = Array.from(checks.querySelectorAll('input:checked')).map(function (input) { return input.value; });
      reviewModelsChange({ action: 'assign', name: name, tasks: tasks });
    }));
  }

  function loadKeys() {
    return api('/api/keys').then(function (info) {
      $('keys-list').replaceChildren(); $('keys-name').replaceChildren();
      (info.keys || []).forEach(function (key) {
        var row = document.createElement('div'), text = document.createElement('span'), option = document.createElement('option');
        row.className = 'models-profile'; text.textContent = key.name + ' · ' + key.source; row.appendChild(text);
        option.value = key.name; option.textContent = key.name; $('keys-name').appendChild(option);
        if (info.available) row.appendChild(modelButton('Remove keychain entry', function () {
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
    return Promise.all([loadModels(), loadKeys()]).then(function () {
      if (modelsInfo && modelsInfo.proposal && modelsInfo.proposal.recommended_tier === 'cloud' && !$('cloud-cheap').value) return searchCatalogue();
    });
  }

  function renderLocalRepos(repos) {
    $('local-results').replaceChildren(); $('local-files').replaceChildren();
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
        if (report.status === 'running') setTimeout(tick, 750);
        else if (info.state === 'pulled' && window.confirm(info.diff + '\nRegister this Ollama profile?')) {
          post('/api/models/apply', { token: info.token, confirmed: true }).then(loadSettings).catch(settingsError);
        } else if (info.state === 'registered') { loadSettings(); refreshModelChoices(); }
      }).catch(settingsError);
    })();
  }
  function initSettings() {
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

  function confirmLensPreview(plan) {
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
    lensBusy(true);
    $('lens-ask-status').textContent = ''; $('lens-matrix-status').textContent = '';
    $('lens-run-audit').disabled = true;
    post('/api/preview', body).then(function (plan) {
      if (plan.blocked_reasons.length) throw new Error('Action blocked: ' + plan.blocked_reasons.join(', '));
      if (plan.remote || plan.requires_approval) return confirmLensPreview(plan);
      // Local only: nothing leaves the computer, so inform without blocking (e.g. "starts llama-server").
      if ((plan.model_notices || []).length) statusNode.textContent = plan.model_notices.join(' · ');
      return null;
    }).then(function (approved) {
      if (approved === false || project !== state.project) { lensBusy(false); return; }
      if (approved === true) body.approved = true;
      if (!statusNode.textContent) statusNode.textContent = 'Starting…';
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
    return api('/api/matrices/' + encodeURIComponent(id)).then(function (m) {
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
      (cluster ? '&cluster=' + encodeURIComponent(cluster) : '')).then(function (data) {
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
    post("/api/refresh", { project: project }).then(function (job) {
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
      api("/api/papers/" + encodeURIComponent(paper.id) + "/repro?project=" + encodeURIComponent(project || ""))
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
        force: $('lens-claims-force').checked }, 'lens-claims-status');
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
          top: Number($('screen-n').value) }, 'lens-screen-status');
      }).catch(function (e) { toast(e.message); });
    };

    refreshModelChoices();
    lensColumnEditor($('lens-columns'), true); lensColumnEditor($('lens-add-column'), false);
    $('lens-more-column').onclick = function () { lensColumnEditor($('lens-columns'), true); };
    $('lens-ask-form').onsubmit = function (e) {
      e.preventDefault(); startLensTask('ask', '/api/ask', { question: $('lens-question').value,
        profile: $('lens-profile').value.trim() || null, sensitive: $('lens-sensitive').checked }, 'lens-ask-status', $('lens-auto-audit').checked);
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
        { matrix_id: lens.matrix.id, profile: $('lens-matrix-profile').value.trim() || null, force: $('lens-force').checked }, 'lens-matrix-status');
    };
  }

  function init() {
    initModels();
    initSettings();
    initLens();
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
    showView(/^#(recents|library|ask|matrix|claims|gaps|console|settings)$/.test(location.hash) ? location.hash.slice(1) : 'new');
    if (linked) loadRun(linked[1]);
    window.addEventListener('hashchange', function () {
      if (/^#(ask|matrix|claims|gaps|recents|library|console|settings)$/.test(location.hash)) showView(location.hash.slice(1));
    });
    $('sort').value = state.sort;

    var drop = $('drop'), dropHtml = drop.innerHTML;
    function resetPdf() { state.pdf = null; $('file').value = ''; drop.innerHTML = dropHtml; wirePdf(); }
    function wirePdf() {
      dropZone(drop, $('file'), $('pick'), /\.pdf$/i, function (f) { state.pdf = f; showFile(drop, f, resetPdf); });
    }
    wirePdf();
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
    $('mathsetup-close').onclick = $('mathsetup-cancel').onclick = function () { $('mathsetup').close(); };
    $('mathsetup-medium').onclick = function () {
      $('mathsetup').close();
      $('export-math').value = 'medium';
      store.set('export-math', 'medium');
      $('btn-export').click();
    };
    $('mathsetup-install').onclick = function () {
      $('mathsetup').close();
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
    $('export-math').value = store.get('export-math', store.get('export-vision', false) ? 'high' : 'off');
    $('export-math').onchange = function () { store.set('export-math', this.value); };
    $('btn-export').onclick = function () {
      if (!state.project) { toast('No project yet'); return; }
      var scope = $('export-scope').value, body = { project: state.project };
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
      $('btn-theme').textContent = THEME_LABEL[t];
      store.set('theme', t);
    }
    applyTheme(store.get('theme', 'auto'));
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
        medium.textContent = 'Math: medium (install Pix2Text)';
        medium.title = 'Run `uv sync --extra formulas-gpu` (NVIDIA GPU) or `uv sync --extra formulas` (CPU), then restart';
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
