/* Markdown + LaTeX rendering, adapted from MDviewer (https://github.com/Ziaeemehr/MDviewer, MIT).
   Math is tokenized inside marked so Markdown never mangles the TeX; the HTML is sanitized with
   DOMPurify before KaTeX fills in the formulas. */
(function () {
  'use strict';

  function escAttr(s) {
    return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  var blockRules = [
    /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n+|$)/,
    /^ {0,3}\\\[([\s\S]+?)\\\][ \t]*(?:\n+|$)/
  ];
  var inlineRules = [
    [/^\$\$([\s\S]+?)\$\$/, true],
    [/^\\\[([\s\S]+?)\\\]/, true],
    [/^\\\(([\s\S]+?)\\\)/, false],
    // $...$: no space just inside the dollars and no digit right after, so "$5 and $10" stays text
    [/^\$(?!\s)((?:\\.|[^$\\\n])*?(?:\\.|[^$\\\n\s]))\$(?!\d)/, false]
  ];

  marked.use({
    gfm: true,
    extensions: [
      {
        name: 'mathBlock', level: 'block',
        start: function (src) { var m = src.match(/^ {0,3}(?:\$\$|\\\[)/m); return m ? m.index : undefined; },
        tokenizer: function (src) {
          for (var i = 0; i < blockRules.length; i++) {
            var m = blockRules[i].exec(src);
            if (m) return { type: 'mathBlock', raw: m[0], tex: m[1].trim() };
          }
        },
        renderer: function (t) { return '<div class="math-block" data-tex="' + escAttr(t.tex) + '"></div>\n'; }
      },
      {
        name: 'mathInline', level: 'inline',
        start: function (src) { var i = src.search(/\$|\\[(\[]/); return i < 0 ? undefined : i; },
        tokenizer: function (src) {
          for (var i = 0; i < inlineRules.length; i++) {
            var m = inlineRules[i][0].exec(src);
            if (m) return { type: 'mathInline', raw: m[0], tex: m[1].trim(), display: inlineRules[i][1] };
          }
        },
        renderer: function (t) {
          return '<span class="math-inline" data-tex="' + escAttr(t.tex) + '"' + (t.display ? ' data-display="1"' : '') + '></span>';
        }
      }
    ]
  });

  window.renderMarkdown = function (el, text) {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    el.querySelectorAll('.math-block, .math-inline').forEach(function (m) {
      var display = m.classList.contains('math-block') || m.dataset.display === '1';
      try { katex.render(m.dataset.tex || '', m, { displayMode: display, throwOnError: false }); }
      catch (e) { m.textContent = m.dataset.tex; }
    });
    el.querySelectorAll('a[href^="http"]').forEach(function (a) { a.target = '_blank'; a.rel = 'noopener'; });
  };
})();
