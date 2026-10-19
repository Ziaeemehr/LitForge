'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { performance } = require('node:perf_hooks');
const vm = require('node:vm');
const fs = require('node:fs');
const { latexWords, splitZwnj } = require('../../src/litforge/ui/static/latex_words.js');

const en = (word, from, to) => ({ from, to, word, lang: 'en' });
const fa = (word, from, to) => ({ from, to, word, lang: 'fa' });

test('mixed Persian and English prose retains document offsets', () => {
  const text = 'متن را در \\lr{English text} و \\cite{deco2012} با $x_i^2$ می‌نویسیم.';
  assert.deepEqual(latexWords(text), [
    fa('متن', 0, 3), fa('را', 4, 6), fa('در', 7, 9),
    en('English', 14, 21), en('text', 22, 26),  // one-letter words such as «و» are not checked
    fa('با', 46, 48), fa('می‌نویسیم', 57, 66)
  ]);
});

test('comments, escaped percent, control symbols and control words', () => {
  assert.deepEqual(latexWords('Hello % hidden words\nworld \\% prose \\& next \\unknown*{visible}'), [
    en('Hello', 0, 5), en('world', 21, 26), en('prose', 30, 35),
    en('next', 39, 43), en('visible', 54, 61)
  ]);
  assert.deepEqual(latexWords('word \\\\% comment\nlast'), [en('word', 0, 4), en('last', 17, 21)]);
});

test('all nonprose commands consume optional and balanced nested arguments', () => {
  const names = ['cite', 'citep', 'parencite', 'nocite', 'customcitecommand',
    'ref', 'eqref', 'pageref', 'autoref', 'cref', 'Cref', 'nameref', 'label',
    'usepackage', 'documentclass', 'begin', 'end', 'includegraphics', 'input',
    'include', 'bibliography', 'bibliographystyle', 'url', 'newcommand',
    'renewcommand', 'def', 'settextfont', 'setlatintextfont', 'setmathfont',
    'color', 'hspace', 'vspace', 'setlength', 'addtolength'];
  for (const name of names) {
    const prefix = '\\' + name + '* [option {nested}] {hidden {deep} words}{more} ';
    assert.deepEqual(latexWords(prefix + 'Visible'), [en('Visible', prefix.length, prefix.length + 7)], name);
  }
  const prefix = '\\cite{hidden \\} still {nested} % } ignored\n more} ';
  assert.deepEqual(latexWords(prefix + 'Prose'), [en('Prose', prefix.length, prefix.length + 5)]);
  assert.deepEqual(latexWords('\\def\\foo#1{hidden #1} prose'), [en('prose', 22, 27)]);
  assert.deepEqual(latexWords('\\newcommand\\foo[1]{hidden} prose'), [en('prose', 27, 32)]);
  assert.deepEqual(latexWords('\\renewcommand{\\foo}[1]{hidden} prose'), [en('prose', 31, 36)]);
});

test('href and textcolor consume only their first required argument', () => {
  assert.deepEqual(latexWords('\\href[option]{https://hidden/{nested}}{Link text}'), [
    en('Link', 39, 43), en('text', 44, 48)
  ]);
  assert.deepEqual(latexWords('\\textcolor[model]{red}{Visible words}'), [
    en('Visible', 23, 30), en('words', 31, 36)
  ]);
});

test('ordinary command arguments remain prose, including nested commands', () => {
  for (const name of ['section', 'textbf', 'emph', 'lr', 'footnote', 'caption', 'title']) {
    const prefix = '\\' + name + '*{';
    assert.deepEqual(latexWords(prefix + 'Hello \\emph{world}}'), [
      en('Hello', prefix.length, prefix.length + 5),
      en('world', prefix.length + 12, prefix.length + 17)
    ]);
  }
});

test('all math delimiters suppress their contents', () => {
  for (const [open, close] of [['$', '$'], ['$$', '$$'], ['\\(', '\\)'], ['\\[', '\\]']]) {
    const prefix = 'Before ' + open + 'hidden \\alpha + words' + close + ' ';
    assert.deepEqual(latexWords(prefix + 'After'), [en('Before', 0, 6), en('After', prefix.length, prefix.length + 5)]);
  }
  assert.deepEqual(latexWords('$hidden \\$ still$ prose'), [en('prose', 18, 23)]);
  assert.deepEqual(latexWords('$hidden % $ ignored\n still$ prose'), [en('prose', 28, 33)]);
});

test('math environments are skipped through their matching end command', () => {
  for (const env of ['equation', 'equation*', 'align', 'align*', 'gather', 'gather*',
    'multline', 'multline*', 'eqnarray', 'eqnarray*', 'math', 'displaymath']) {
    const prefix = '\\begin{' + env + '}hidden \\text{words} \\end{other} more \\end{' + env + '} ';
    assert.deepEqual(latexWords(prefix + 'After'), [en('After', prefix.length, prefix.length + 5)], env);
  }
  assert.deepEqual(latexWords('\\begin{document}Hello\\end{document}'), [en('Hello', 16, 21)]);
  const prefix = '\\begin {align} hidden % \\end{align}\n more \\end {align} ';
  assert.deepEqual(latexWords(prefix + 'After'), [en('After', prefix.length, prefix.length + 5)]);
});

test('English apostrophes, short words, digits, names and acronyms', () => {
  assert.deepEqual(latexWords("a I go can't O'Brien TVB fMRI McIntosh Normal x2 3D abc4 5word"), [
    en('go', 4, 6), en("can't", 7, 12), en('Normal', 39, 45)
  ]);
  assert.deepEqual(latexWords("'hello' dogs' can't've abc۲ ٣word"), [
    en('hello', 1, 6), en('dogs', 8, 12), en("can't've", 14, 22)
  ]);
});

test('Persian letter ranges, punctuation, digits, diacritics and ZWNJ boundaries', () => {
  assert.deepEqual(latexWords('‌متن‌،را؛در؟«با»۱۲۳۴۵۶۷۸۹۰١٢٣٤٥٦٧٨٩٠ می‌نویسیم'), [
    fa('متن', 1, 4), fa('را', 6, 8), fa('در', 9, 11), fa('با', 13, 15), fa('می‌نویسیم', 37, 46)
  ]);
  assert.deepEqual(latexWords('ب ت و متنَراٰدر می‌‌شود'), [
    // diacritics stay inside the word (its span) and are left out of the word that is checked
    fa('متنرادر', 6, 15), fa('می', 16, 18), fa('شود', 20, 23)
  ]);
  for (const code of [0x0621, 0x064a, 0x066e, 0x066f, 0x0671, 0x06d3, 0x06d5, 0x06ee, 0x06ef, 0x06fa, 0x06ff]) {
    const word = String.fromCharCode(code).repeat(2);
    assert.deepEqual(latexWords(word), [fa(word, 0, 2)]);
  }
  for (let code = 0x064b; code <= 0x065f; code++) {
    assert.deepEqual(latexWords('متن' + String.fromCharCode(code) + 'را'), [fa('متنرا', 0, 6)]);
  }
});

test('malformed input terminates and unclosed skipped regions extend to EOF', () => {
  for (const suffix of ['\\cite{hidden {nested}', '\\cite[hidden', '$hidden', '$$hidden',
    '\\(hidden', '\\[hidden', '\\begin{align}hidden', '\\href{hidden', '\\textcolor{hidden']) {
    assert.deepEqual(latexWords('Hello ' + suffix), [en('Hello', 0, 5)], suffix);
  }
  assert.deepEqual(latexWords('Hello \\'), [en('Hello', 0, 5)]);
  assert.deepEqual(latexWords('\\section{Hello'), [en('Hello', 9, 14)]);
  assert.deepEqual(latexWords(''), []);
  assert.deepEqual(latexWords('\\cite{' + '{'.repeat(20000)), []);
});

test('offset parameter uses UTF-16 positions even after surrogate pairs', () => {
  assert.deepEqual(latexWords('😀 Hello متن', 100), [en('Hello', 103, 108), fa('متن', 109, 112)]);
  assert.deepEqual(latexWords('Hello', -2), [en('Hello', -2, 3)]);
});

test('splitZwnj removes empty parts', () => {
  assert.deepEqual(splitZwnj('می‌نویسیم'), ['می', 'نویسیم']);
  assert.deepEqual(splitZwnj('‌می‌‌نویسیم‌'), ['می', 'نویسیم']);
  assert.deepEqual(splitZwnj('متن'), ['متن']);
  assert.deepEqual(splitZwnj('‌‌'), []);
  assert.deepEqual(splitZwnj(''), []);
});

test('plain browser script exposes the same API without module or imports', () => {
  const context = {};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../src/litforge/ui/static/latex_words.js'), 'utf8'), context);
  assert.equal(typeof context.LitForgeWords.latexWords, 'function');
  assert.equal(JSON.stringify(context.LitForgeWords.latexWords('Hello')), JSON.stringify([en('Hello', 0, 5)]));
});

test('200 KB mixed document completes well under 200 ms', () => {
  const chunk = 'Hello متن \\textbf{world} \\cite{hidden {nested}} $hidden$ % comment\n';
  const text = chunk.repeat(Math.ceil(200 * 1024 / Buffer.byteLength(chunk)));
  latexWords(chunk); // Warm the scanner before timing the document.
  const start = performance.now();
  const words = latexWords(text);
  const elapsed = performance.now() - start;
  const expected = [];
  for (let from = 0; from < text.length; from += chunk.length) {
    expected.push(en('Hello', from, from + 5), fa('متن', from + 6, from + 9), en('world', from + 18, from + 23));
  }
  assert.deepEqual(words, expected);
  assert.ok(elapsed < 150, `200 KB took ${elapsed.toFixed(1)} ms`);
});

test('a diacritic inside a Persian word does not split it', () => {
  assert.deepEqual(latexWords('مُدل خوب'), [fa('مدل', 0, 4), fa('خوب', 5, 8)]);
});
