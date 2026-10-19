(function (root) {
  'use strict';

  var ignored = /^(?:ref|eqref|pageref|autoref|cref|Cref|nameref|label|usepackage|documentclass|begin|end|includegraphics|input|include|bibliography|bibliographystyle|url|newcommand|renewcommand|def|settextfont|setlatintextfont|setmathfont|color|hspace|vspace|setlength|addtolength)$/;
  var mathEnvironment = /^(?:equation|align|gather|multline|eqnarray)\*?$|^(?:math|displaymath)$/;
  // Required arguments to skip (default 1); environments whose \begin takes more (column specs, widths).
  var arity = { newcommand: 2, renewcommand: 2, setlength: 2, addtolength: 2 };
  var envArity = { tabular: 1, 'tabular*': 2, tabularx: 2, array: 1, longtable: 1, minipage: 1, multicols: 1 };

  function english(c) { return /[A-Za-z]/.test(c || ''); }
  function persian(c) {
    var n = c ? c.charCodeAt(0) : 0;
    return n !== 0x0670 && ((n >= 0x0621 && n <= 0x064a) ||
      (n >= 0x066e && n <= 0x06d3) || n === 0x06d5 ||
      (n >= 0x06ee && n <= 0x06ef) || (n >= 0x06fa && n <= 0x06ff));
  }
  function diacritic(c) {
    var n = c ? c.charCodeAt(0) : 0;
    return (n >= 0x064b && n <= 0x065f) || n === 0x0670;
  }
  function digit(c) { return /[0-9\u0660-\u0669\u06f0-\u06f9]/.test(c || ''); }
  function splitZwnj(word) { return word.split('\u200c').filter(function (part) { return part.length > 0; }); }

  function latexWords(text, offset) {
    offset = offset === undefined ? 0 : offset;
    var result = [], i = 0, length = text.length;
    function comment(pos) {
      while (pos < length && text[pos] !== '\n' && text[pos] !== '\r') pos++;
      return pos;
    }
    function space(pos) {
      while (pos < length) {
        if (/\s/.test(text[pos])) pos++;
        else if (text[pos] === '%') pos = comment(pos);
        else break;
      }
      return pos;
    }
    // A stack handles nested braces and optional groups without recursion.
    function group(pos) {
      var stack = [text[pos] === '{' ? '}' : ']'];
      pos++;
      while (pos < length && stack.length) {
        var c = text[pos++];
        if (c === '\\') { if (pos < length) pos++; }
        else if (c === '%') pos = comment(pos);
        else if (c === '{') stack.push('}');
        else if (c === '[' && stack[stack.length - 1] === ']') stack.push(']');
        else if (c === stack[stack.length - 1]) stack.pop();
      }
      return pos;
    }
    function command(pos) {
      var start = ++pos;
      if (english(text[pos])) {
        while (english(text[pos])) pos++;
        var name = text.slice(start, pos);
        if (text[pos] === '*') pos++;
        return { name: name, end: pos };
      }
      return { name: text[pos] || '', end: Math.min(length, pos + 1) };
    }
    function math(pos, closing) {
      while (pos < length) {
        if (text[pos] === '%') { pos = comment(pos); continue; }
        if (text.slice(pos, pos + closing.length) === closing) return pos + closing.length;
        if (text[pos] === '\\') pos = Math.min(length, pos + 2);
        else pos++;
      }
      return length;
    }
    function environment(pos, name) {
      while (pos < length) {
        if (text[pos] === '%') { pos = comment(pos); continue; }
        if (text[pos] !== '\\') { pos++; continue; }
        var cmd = command(pos);
        pos = cmd.end;
        if (cmd.name === 'end') {
          var start = space(pos);
          if (text[start] === '{') {
            pos = group(start);
            if (text.slice(start + 1, pos - 1) === name) return pos;
          }
        }
      }
      return length;
    }
    while (i < length) {
      var c = text[i], start = i, word;
      if (c === '%') { i = comment(i); continue; }
      if (c === '$') {
        var delimiter = text[i + 1] === '$' ? '$$' : '$';
        i = math(i + delimiter.length, delimiter);
        continue;
      }
      if (c === '\\') {
        var cmd = command(i);
        i = cmd.end;
        if (cmd.name === '(' || cmd.name === '[') {
          i = math(i, cmd.name === '(' ? '\\)' : '\\]');
          continue;
        }
        var skip = ignored.test(cmd.name) || /cite/i.test(cmd.name);
        var firstOnly = cmd.name === 'href' || cmd.name === 'textcolor';
        if (skip || firstOnly) {
          // Skip only the command's own arguments: optional [...] groups and its required {...} groups.
          var need = firstOnly ? 1 : (arity[cmd.name] || 1), inMath = false;
          i = space(i);
          if ((cmd.name === 'def' || cmd.name === 'newcommand' || cmd.name === 'renewcommand') && text[i] === '\\') {
            i = command(i).end;
            need = cmd.name === 'def' ? 1 : need - 1;
            if (cmd.name === 'def') while (i < length && text[i] !== '{' && text[i] !== '\n') i++;
          }
          while (i < length && need > 0) {
            i = space(i);
            if (text[i] === '[') { i = group(i); continue; }
            if (text[i] !== '{') break;
            start = i;
            i = group(i);
            need--;
            if (cmd.name === 'begin') {
              var env = text.slice(start + 1, i - 1);
              if (mathEnvironment.test(env)) { i = environment(i, env); inMath = true; break; }
              need += envArity[env] || 0;
            }
          }
          if (cmd.name === 'begin' && !inMath) {  // \begin{figure}[h]
            var after = space(i);
            if (text[after] === '[') i = group(after);
          }
        }
        continue;
      }
      if (english(c)) {
        i++;
        while (english(text[i]) || (text[i] === "'" && english(text[i + 1]))) i++;
        word = text.slice(start, i);
        if (word.replace(/'/g, '').length >= 2 && !/[A-Z]/.test(word.slice(1)) &&
            !digit(text[start - 1]) && !digit(text[i])) {
          result.push({ from: start + offset, to: i + offset, word: word, lang: 'en' });
        }
        continue;
      }
      if (persian(c)) {
        i++;
        // Diacritics (short vowels) stay inside the word's span but are left out of the word that is checked.
        while (persian(text[i]) || diacritic(text[i]) ||
               (text[i] === '\u200c' && persian(text[i - 1]) && persian(text[i + 1]))) i++;
        word = text.slice(start, i).replace(/[\u064b-\u065f\u0670]/g, '');
        if (word.length > 1) result.push({ from: start + offset, to: i + offset, word: word, lang: 'fa' });
        continue;
      }
      i++;
    }
    return result;
  }

  var api = { latexWords: latexWords, splitZwnj: splitZwnj };
  root.LitForgeWords = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(this);
