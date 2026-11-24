/* Turn a rendered result (answer, claims, matrix, gaps, paper details) into Markdown, so it can be copied or saved
   as a .md file and added to another tool, such as NotebookLM, as a source. Works on any DOM-like tree (tests use
   plain objects). */
(function (root) {
  'use strict';

  var SKIP = { BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, SCRIPT: 1, STYLE: 1, SVG: 1, DIALOG: 1, FORM: 1 };
  var BLOCK = { P: 1, DIV: 1, SECTION: 1, ARTICLE: 1, UL: 1, OL: 1, DETAILS: 1, SUMMARY: 1, FIGURE: 1, HEADER: 1,
    FOOTER: 1, ASIDE: 1, PRE: 1, BLOCKQUOTE: 1, H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, TABLE: 1, HR: 1 };

  function tag(n) { return (n.tagName || '').toUpperCase(); }
  function attr(n, name) { return n.getAttribute ? n.getAttribute(name) : null; }
  function hiddenNode(n) {
    return n.hidden || attr(n, 'aria-hidden') === 'true' || attr(n, 'data-share') === 'skip';
  }
  function kids(n) { return Array.prototype.slice.call(n.childNodes || []); }

  function inline(n) {
    if (n.nodeType === 3) return (n.textContent || '').replace(/\s+/g, ' ');
    if (n.nodeType !== 1 || SKIP[tag(n)] || hiddenNode(n)) return '';
    var t = tag(n), inner = kids(n).map(inline).join('');
    if (t === 'BR') return '\n';
    if (t === 'STRONG' || t === 'B') return inner.trim() ? '**' + inner.trim() + '** ' : '';
    if (t === 'EM' || t === 'I') return inner.trim() ? '*' + inner.trim() + '* ' : '';
    if (t === 'CODE') return inner ? '`' + inner + '`' : '';
    if (t === 'A') {
      var href = attr(n, 'href') || '';
      return /^https?:/.test(href) && inner.trim() && inner.trim() !== href ? '[' + inner.trim() + '](' + href + ')' : inner;
    }
    if (BLOCK[t]) return ' ' + block(n).replace(/\s+/g, ' ').trim() + ' ';
    return inner;
  }

  function cell(n) { return inline(n).replace(/\|/g, '\\|').replace(/\s+/g, ' ').trim(); }

  function table(n) {
    var rows = [];
    (function collect(x) {
      kids(x).forEach(function (c) {
        if (c.nodeType !== 1 || hiddenNode(c)) return;
        if (tag(c) === 'TR') rows.push(kids(c).filter(function (d) { return d.nodeType === 1 && /^T[HD]$/.test(tag(d)); }).map(cell));
        else collect(c);
      });
    })(n);
    if (!rows.length) return '';
    var width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
    var line = function (r) { while (r.length < width) r.push(''); return '| ' + r.join(' | ') + ' |'; };
    var out = [line(rows[0]), '|' + new Array(width + 1).join(' --- |')];
    rows.slice(1).forEach(function (r) { out.push(line(r)); });
    return out.join('\n');
  }

  // Block-level Markdown of a node's children, paragraphs separated by blank lines.
  function block(n) {
    var parts = [], buf = '';
    function flush() { if (buf.trim()) parts.push(buf.replace(/[ \t]+\n/g, '\n').replace(/ +/g, ' ').trim()); buf = ''; }
    kids(n).forEach(function (c) {
      if (c.nodeType === 1 && (SKIP[tag(c)] || hiddenNode(c))) return;
      var t = c.nodeType === 1 ? tag(c) : '';
      if (!BLOCK[t]) { buf += inline(c); return; }
      flush();
      var h = /^H([1-6])$/.exec(t);
      if (h) { var ht = inline(c).trim(); if (ht) parts.push(new Array(+h[1] + 1).join('#') + ' ' + ht); return; }
      if (t === 'TABLE') { var tb = table(c); if (tb) parts.push(tb); return; }
      if (t === 'HR') { parts.push('---'); return; }
      if (t === 'PRE') { parts.push('```\n' + (c.textContent || '').replace(/\n+$/, '') + '\n```'); return; }
      if (t === 'UL' || t === 'OL') {
        var i = 0, items = kids(c).filter(function (li) { return li.nodeType === 1 && !hiddenNode(li); }).map(function (li) {
          i += 1;
          var body = block(li).replace(/\n/g, '\n   ');
          return body ? (t === 'OL' ? i + '. ' : '- ') + body : '';
        }).filter(Boolean);
        if (items.length) parts.push(items.join('\n'));
        return;
      }
      if (t === 'BLOCKQUOTE') { var q = block(c); if (q) parts.push('> ' + q.replace(/\n/g, '\n> ')); return; }
      var inner = block(c); if (inner) parts.push(inner);
    });
    flush();
    return parts.join('\n\n');
  }

  function toMarkdown(nodes, title) {
    var body = (Array.isArray(nodes) ? nodes : [nodes]).filter(Boolean).map(block).filter(Boolean).join('\n\n');
    if (!body) return '';
    return (title ? '# ' + title + '\n\n' : '') + body.replace(/\n{3,}/g, '\n\n') + '\n';
  }

  function slug(s) {
    return (s || 'litforge').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'litforge';
  }

  var api = { toMarkdown: toMarkdown, slug: slug };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LFShare = api;
})(this);
