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
    sel: -1, view: [], pdf: null, list: null, job: null, anchor: -1
  };

  var CHANNEL_LABEL = {
    references: 'Cited by the seed', cited_by: 'Cites the seed', related: 'Related (OpenAlex)',
    keywords: 'Key-phrase search', neighbours: 'Citations of close matches', query: 'Search query',
    'import': 'Imported list', seed: 'Seed'
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
        if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
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
      if (!state.project && projects.length) state.project = projects[0].name;
      var sel = $('project-select');
      sel.innerHTML = projects.length ? '' : '<option value="">(no projects yet)</option>';
      projects.forEach(function (p) {
        var o = document.createElement('option');
        o.value = p.name;
        o.textContent = p.name + '  ·  ' + p.papers + ' papers';
        sel.appendChild(o);
      });
      sel.value = state.project || '';
      $('project-list').innerHTML = projects.map(function (p) { return '<option value="' + esc(p.name) + '">'; }).join('');
      syncProjectFields();
      return loadRuns();
    });
  }

  function syncProjectFields() {
    var p = state.projects.find(function (x) { return x.name === state.project; });
    $('project-name').value = state.project || '';
    $('export-dir').value = p ? p.export_dir : '';
  }

  function loadRuns() {
    if (!state.project) { $('runs').innerHTML = '<small class="hint">No runs yet.</small>'; return Promise.resolve(); }
    return api('/api/runs?project=' + encodeURIComponent(state.project)).then(function (runs) {
      $('runs').innerHTML = runs.length ? '' : '<small class="hint">No runs yet.</small>';
      runs.forEach(function (r) {
        var a = document.createElement('a');
        a.href = '#';
        a.className = r.id === state.runId ? 'on' : '';
        a.innerHTML = esc(r.label) + '<small>' + esc(r.kind) + ' · ' + r.papers + ' papers · ' +
          esc((r.started_at || '').replace('T', ' ').slice(0, 16)) + (r.status !== 'done' ? ' · ' + esc(r.status) : '') + '</small>';
        a.onclick = function (e) { e.preventDefault(); loadRun(r.id); };
        $('runs').appendChild(a);
      });
      if (!state.runId && runs.length) loadRun(runs[0].id);
    });
  }

  function loadRun(id) {
    state.runId = id;
    return api('/api/runs/' + id).then(function (data) {
      state.data = data;
      state.channel = null;
      state.anchor = -1;
      state.sel = -1;
      render();
      Array.prototype.forEach.call($('runs').children, function (a) { a.classList.remove('on'); });
      loadRuns();
    }).catch(function (e) { toast(e.message); });
  }

  /* ---------- results ---------- */

  function render() {
    var d = state.data;
    $('empty').hidden = !!d;
    $('results').hidden = !d;
    if (!d) return;
    var run = d.run, s = d.stats, params = run.params || {};
    $('run-title').textContent = String(run.label || run.kind).slice(0, 120);
    var cost = run.stats && run.stats.cost_usd ? ' · API cost $' + run.stats.cost_usd.toFixed(4) : '';
    $('run-meta').innerHTML = esc(run.kind) + ' · ' + esc((run.started_at || '').replace('T', ' ').slice(0, 16)) +
      ' · ' + esc(run.status) + cost + (s.retracted ? ' · <b style="color:var(--bad)">' + s.retracted + ' retracted</b>' : '') +
      ' <button class="btn small" id="btn-expand" title="Run a similar-paper search seeded with the top papers of this list">Find similar to these</button>';
    $('btn-expand').onclick = function () { startSimilar({ seed_run: state.runId }); };

    var years = s.year_min ? (s.year_min === s.year_max ? s.year_min : s.year_min + '–' + s.year_max) : '–';
    var cards = [
      [s.papers, 'papers'], [s.new, 'new to project'], [s.high_impact, 'top-10% cited in field'],
      [s.open_access, 'open access'], [years, 'years'], [s.relevant + ' / ' + s.irrelevant, 'marked ✓ / ✗']
    ];
    $('stats').innerHTML = cards.map(function (c) {
      return '<div class="card stat"><b>' + esc(c[0]) + '</b><span>' + esc(c[1]) + '</span></div>';
    }).join('');
    renderYears(s.years);
    renderBars($('chart-channels'), s.channels.map(function (c) { return [c[0], CHANNEL_LABEL[c[0]] || c[0], c[1]]; }), true);
    renderBars($('chart-venues'), s.venues.map(function (v) { return [v[0], v[0], v[1]]; }), false);
    renderList();
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
    svg += '<text x="0" y="59">' + bins[0][0] + '</text><text x="100" y="59" text-anchor="end">' + bins[bins.length - 1][0] + '</text></svg>';
    el.innerHTML = svg;
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
      if (f) {
        var hay = [p.title, p.venue, (p.authors || []).join(' '), p.doi, (p.reasons || []).join(' ')].join(' ').toLowerCase();
        if (hay.indexOf(f) < 0) return false;
      }
      return true;
    });
    var key = state.sort;
    list.sort(function (a, b) {
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
    if (!view.length) { el.innerHTML = '<div class="empty">No papers match the filters.</div>'; return; }
    el.innerHTML = view.map(function (p, i) {
      var isSeed = (p.reasons || [])[0] === 'seed paper';
      var meta = [p.year, p.venue, p.cited_by_count != null ? p.cited_by_count + ' citations' : null,
        (p.authors || []).join(', ')].filter(Boolean).map(esc).join(' · ');
      var chips = (p.reasons || []).filter(function (r) { return r !== 'seed paper'; })
        .map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('') +
        (p.impact_notes || []).filter(function (n) { return /^top|in this set|RETRACTED/.test(n); })
          .map(function (n) { return '<span class="chip impact' + (n === 'RETRACTED' ? ' bad' : '') + '">' + esc(n) + '</span>'; }).join('');
      var badges = (isSeed ? '<span class="badge seed">seed</span>' : '') +
        (p.new_to_project && !isSeed ? '<span class="badge new">new</span>' : '') +
        (p.has_pdf ? '<span class="badge pdf">pdf</span>' : '') +
        (p.oa_url && !p.has_pdf ? '<span class="badge oa">open</span>' : '');
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
    var ids = papers.map(function (p) { return p.id; });
    return post('/api/marks', { project: state.project, paper_ids: ids, mark: mark }).then(function () {
      papers.forEach(function (p) { p.mark = mark; });
      if (state.hide && mark === 'irrelevant') state.anchor = -1;  // those rows just left the list
      state.data.stats.relevant = state.data.papers.filter(function (x) { return x.mark === 'relevant'; }).length;
      state.data.stats.irrelevant = state.data.papers.filter(function (x) { return x.mark === 'irrelevant'; }).length;
      render();
      var open = state.view[state.sel];
      if ($('drawer').classList.contains('open') && open && papers.indexOf(open) >= 0) openDrawer(open);
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

  function openDrawer(p) {
    var drawer = $('drawer');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    $('dhead').innerHTML = '<h2>' + esc(p.title || '(no title)') + '</h2><div class="meta">loading…</div>';
    $('dbody').innerHTML = '';
    api('/api/papers/' + p.id + '?project=' + encodeURIComponent(state.project || '')).then(function (d) {
      var meta = [d.year, d.venue, d.cited_by_count != null ? d.cited_by_count + ' citations' : null]
        .filter(Boolean).map(esc).join(' · ');
      var authors = d.authors.length ? esc(d.authors.slice(0, 12).join(', ') + (d.authors.length > 12 ? ', et al.' : '')) : '';
      $('dhead').innerHTML = '<h2>' + esc(d.title || '(no title)') + '</h2>' +
        '<div class="meta">' + authors + '</div><div class="meta">' + meta + '</div>' +
        '<div class="links">' + paperLinks(d) + '</div>' +
        '<div class="dactions"><span class="marks">' +
        '<button class="rel' + (p.mark === 'relevant' ? ' on' : '') + '" data-mark="relevant" title="Relevant (r)">✓</button>' +
        '<button class="irr' + (p.mark === 'irrelevant' ? ' on' : '') + '" data-mark="irrelevant" title="Not relevant (x)">✗</button></span>' +
        '<button class="btn small" id="btn-like">Find similar to this</button>' +
        (d.markdown ? '<button class="btn small" id="btn-full">Full text</button>' : '') + '</div>';
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
      if ((p.reasons || []).length) {
        html += '<h3>Found because</h3><ul class="reasons">' + p.reasons.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>';
      }
      html += '<h3>Abstract</h3><div class="md" id="abstract-md"></div>';
      if (d.markdown) html += '<div id="full" hidden><h3>Full text</h3><div class="md" id="full-md"></div></div>';
      $('dbody').innerHTML = html;
      window.renderMarkdown($('abstract-md'), d.abstract || '_No abstract available._');
      if (d.markdown) {
        $('btn-full').onclick = function () {
          var full = $('full');
          if (full.hidden && !$('full-md').innerHTML) window.renderMarkdown($('full-md'), d.markdown);
          full.hidden = !full.hidden;
          if (!full.hidden) full.scrollIntoView({ behavior: 'smooth' });
        };
      }
    }).catch(function (e) { $('dbody').textContent = e.message; });
  }

  function closeDrawer() {
    $('drawer').classList.remove('open');
    $('drawer').setAttribute('aria-hidden', 'true');
  }

  /* ---------- running searches ---------- */

  function setBusy(busy) {
    $('btn-run').disabled = busy;
    $('btn-more').disabled = busy;
    $('btn-fetch').disabled = busy;
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
        if (j.status === 'failed') { logLine(j.error, 'err'); toast(j.kind === 'fetch' ? 'Download failed' : 'Search failed'); return; }
        state.project = j.project;
        store.set('project', j.project);
        state.runId = null;
        loadProjects().then(function () { if (j.run_id) loadRun(j.run_id); });
      }).catch(function (e) { setBusy(false); logLine(e.message, 'err'); });
    })();
  }

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

  /* ---------- wiring ---------- */

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll('.tabs button').forEach(function (b) { b.classList.toggle('on', b.dataset.mode === mode); });
    $('form-similar').hidden = mode !== 'similar';
    $('form-search').hidden = mode !== 'search';
    $('form-import').hidden = mode !== 'import';
    $('emb-wrap').hidden = mode !== 'similar';
    $('btn-more').hidden = mode !== 'similar';
    $('btn-run').textContent = { similar: 'Find similar papers', search: 'Search', 'import': 'Import list' }[mode];
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

  function init() {
    document.querySelectorAll('.tabs button').forEach(function (b) { b.onclick = function () { setMode(b.dataset.mode); }; });
    setMode(store.get('mode', 'similar'));
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

    $('btn-run').onclick = run;
    $('btn-more').onclick = function () { startSimilar({ from_relevant: true }); };
    $('project-select').onchange = function () {
      state.project = this.value;
      store.set('project', state.project);
      state.runId = null;
      state.data = null;
      render();
      syncProjectFields();
      loadRuns();
    };
    $('export-scope').value = store.get('export-scope', 'all');
    $('export-scope').onchange = function () { store.set('export-scope', this.value); };
    // Get PDFs and Export both act on the papers chosen in the scope menu next to them.
    function scopeBody(extra) {
      if (!state.project) { toast('No project yet'); return null; }
      var scope = $('export-scope').value, body = Object.assign({ project: state.project }, extra);
      if (scope === 'visible') {
        if (!state.data) { toast('Open a run first'); return null; }
        body.paper_ids = state.view.map(function (p) { return p.id; });
      } else {
        body.only = scope;
      }
      return body;
    }
    $('btn-fetch').onclick = function () {
      var body = scopeBody({ run_id: state.runId });
      if (!body) return;
      setBusy(true);
      post('/api/fetch', body).then(poll).catch(function (e) { setBusy(false); toast(e.message); });
    };
    $('btn-export').onclick = function () {
      var body = scopeBody({ export_dir: $('export-dir').value.trim() });
      if (!body) return;
      var scope = $('export-scope').value;
      post('/api/export', body)
        .then(function (r) {
          if (!r.papers) toast(scope === 'relevant' ? 'Nothing exported: no paper is marked ✓ yet' : 'Nothing to export');
          else toast('Exported ' + r.papers + ' papers to ' + r.path);
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

    document.addEventListener('keydown', function (e) {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.metaKey || e.ctrlKey || !state.data) return;
      if (e.key === 'j' || e.key === 'ArrowDown') { select(state.sel + 1, $('drawer').classList.contains('open')); e.preventDefault(); }
      else if (e.key === 'k' || e.key === 'ArrowUp') { select(state.sel - 1, $('drawer').classList.contains('open')); e.preventDefault(); }
      else if (e.key === 'Enter' && state.sel >= 0) openDrawer(state.view[state.sel]);
      else if (e.key === 'Escape') closeDrawer();
      else if ((e.key === 'r' || e.key === 'x') && state.sel >= 0) {
        toggleMark(state.view[state.sel], e.key === 'r' ? 'relevant' : 'irrelevant');
        state.anchor = state.sel;
      }
    });

    loadProjects().catch(function (e) { toast(e.message); });
  }

  init();
})();
