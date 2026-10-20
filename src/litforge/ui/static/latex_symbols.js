/* Math symbols for the Write view's palette: [command, glyph, group, search words]. The glyph is the Unicode
   character the command prints, so the palette needs no fonts or rendering. Commands need only amsmath/amssymb,
   which both templates load. */
(function (root) {
  'use strict';

  var S = [
    // Greek
    ['\\alpha', 'α', 'Greek', 'alpha'], ['\\beta', 'β', 'Greek', 'beta'], ['\\gamma', 'γ', 'Greek', 'gamma'],
    ['\\delta', 'δ', 'Greek', 'delta'], ['\\epsilon', 'ϵ', 'Greek', 'epsilon'],
    ['\\varepsilon', 'ε', 'Greek', 'epsilon varepsilon'], ['\\zeta', 'ζ', 'Greek', 'zeta'],
    ['\\eta', 'η', 'Greek', 'eta'], ['\\theta', 'θ', 'Greek', 'theta'], ['\\vartheta', 'ϑ', 'Greek', 'theta vartheta'],
    ['\\iota', 'ι', 'Greek', 'iota'], ['\\kappa', 'κ', 'Greek', 'kappa'], ['\\lambda', 'λ', 'Greek', 'lambda'],
    ['\\mu', 'μ', 'Greek', 'mu micro'], ['\\nu', 'ν', 'Greek', 'nu'], ['\\xi', 'ξ', 'Greek', 'xi'],
    ['\\pi', 'π', 'Greek', 'pi'], ['\\varpi', 'ϖ', 'Greek', 'pi varpi'], ['\\rho', 'ρ', 'Greek', 'rho'],
    ['\\varrho', 'ϱ', 'Greek', 'rho varrho'], ['\\sigma', 'σ', 'Greek', 'sigma'],
    ['\\varsigma', 'ς', 'Greek', 'sigma varsigma'], ['\\tau', 'τ', 'Greek', 'tau'],
    ['\\upsilon', 'υ', 'Greek', 'upsilon'], ['\\phi', 'ϕ', 'Greek', 'phi'], ['\\varphi', 'φ', 'Greek', 'phi varphi'],
    ['\\chi', 'χ', 'Greek', 'chi'], ['\\psi', 'ψ', 'Greek', 'psi'], ['\\omega', 'ω', 'Greek', 'omega'],
    ['\\Gamma', 'Γ', 'Greek', 'gamma capital'], ['\\Delta', 'Δ', 'Greek', 'delta capital'],
    ['\\Theta', 'Θ', 'Greek', 'theta capital'], ['\\Lambda', 'Λ', 'Greek', 'lambda capital'],
    ['\\Xi', 'Ξ', 'Greek', 'xi capital'], ['\\Pi', 'Π', 'Greek', 'pi capital'],
    ['\\Sigma', 'Σ', 'Greek', 'sigma capital'], ['\\Upsilon', 'Υ', 'Greek', 'upsilon capital'],
    ['\\Phi', 'Φ', 'Greek', 'phi capital'], ['\\Psi', 'Ψ', 'Greek', 'psi capital'],
    ['\\Omega', 'Ω', 'Greek', 'omega capital ohm'],
    // Relations
    ['\\leq', '≤', 'Relations', 'less or equal le'], ['\\geq', '≥', 'Relations', 'greater or equal ge'],
    ['\\neq', '≠', 'Relations', 'not equal ne'], ['\\approx', '≈', 'Relations', 'approximately'],
    ['\\sim', '∼', 'Relations', 'similar tilde distributed'], ['\\simeq', '≃', 'Relations', 'similar equal'],
    ['\\cong', '≅', 'Relations', 'congruent isomorphic'], ['\\equiv', '≡', 'Relations', 'equivalent identical'],
    ['\\propto', '∝', 'Relations', 'proportional'], ['\\ll', '≪', 'Relations', 'much less'],
    ['\\gg', '≫', 'Relations', 'much greater'], ['\\prec', '≺', 'Relations', 'precedes'],
    ['\\succ', '≻', 'Relations', 'succeeds'], ['\\perp', '⊥', 'Relations', 'perpendicular orthogonal'],
    ['\\parallel', '∥', 'Relations', 'parallel'], ['\\mid', '∣', 'Relations', 'divides given bar'],
    ['\\models', '⊨', 'Relations', 'models entails'], ['\\vdash', '⊢', 'Relations', 'proves turnstile'],
    ['\\doteq', '≐', 'Relations', 'approaches dot equal'], ['\\coloneqq', '≔', 'Relations', 'defined as colon equal'],
    // Sets and logic
    ['\\in', '∈', 'Sets', 'element in'], ['\\notin', '∉', 'Sets', 'not element'], ['\\ni', '∋', 'Sets', 'contains'],
    ['\\subset', '⊂', 'Sets', 'subset'], ['\\subseteq', '⊆', 'Sets', 'subset equal'],
    ['\\supset', '⊃', 'Sets', 'superset'], ['\\supseteq', '⊇', 'Sets', 'superset equal'],
    ['\\cup', '∪', 'Sets', 'union cup'], ['\\cap', '∩', 'Sets', 'intersection cap'],
    ['\\setminus', '∖', 'Sets', 'set minus difference'], ['\\emptyset', '∅', 'Sets', 'empty set'],
    ['\\varnothing', '∅', 'Sets', 'empty set varnothing'], ['\\forall', '∀', 'Sets', 'for all'],
    ['\\exists', '∃', 'Sets', 'exists'], ['\\nexists', '∄', 'Sets', 'not exists'], ['\\neg', '¬', 'Sets', 'not negation'],
    ['\\land', '∧', 'Sets', 'and wedge logical'], ['\\lor', '∨', 'Sets', 'or vee logical'],
    ['\\Rightarrow', '⇒', 'Sets', 'implies'], ['\\Leftrightarrow', '⇔', 'Sets', 'if and only if iff equivalent'],
    ['\\mathbb{R}', 'ℝ', 'Sets', 'real numbers'], ['\\mathbb{N}', 'ℕ', 'Sets', 'natural numbers'],
    ['\\mathbb{Z}', 'ℤ', 'Sets', 'integers'], ['\\mathbb{Q}', 'ℚ', 'Sets', 'rational numbers'],
    ['\\mathbb{C}', 'ℂ', 'Sets', 'complex numbers'], ['\\mathbb{E}', '𝔼', 'Sets', 'expectation expected value'],
    // Operators
    ['\\pm', '±', 'Operators', 'plus minus'], ['\\mp', '∓', 'Operators', 'minus plus'],
    ['\\times', '×', 'Operators', 'times multiply cross'], ['\\div', '÷', 'Operators', 'divide'],
    ['\\cdot', '⋅', 'Operators', 'dot multiply'], ['\\ast', '∗', 'Operators', 'asterisk star convolution'],
    ['\\circ', '∘', 'Operators', 'compose circle'], ['\\bullet', '∙', 'Operators', 'bullet'],
    ['\\oplus', '⊕', 'Operators', 'direct sum xor plus circle'], ['\\otimes', '⊗', 'Operators', 'tensor product'],
    ['\\odot', '⊙', 'Operators', 'hadamard dot circle'], ['\\star', '⋆', 'Operators', 'star'],
    ['\\dagger', '†', 'Operators', 'dagger adjoint'], ['\\nabla', '∇', 'Operators', 'nabla gradient del'],
    ['\\partial', '∂', 'Operators', 'partial derivative'], ['\\infty', '∞', 'Operators', 'infinity'],
    ['\\hbar', 'ℏ', 'Operators', 'h bar planck'], ['\\ell', 'ℓ', 'Operators', 'ell script l'],
    ['\\Re', 'ℜ', 'Operators', 'real part'], ['\\Im', 'ℑ', 'Operators', 'imaginary part'],
    ['\\angle', '∠', 'Operators', 'angle'], ['\\degree', '°', 'Operators', 'degree'],
    ['\\langle', '⟨', 'Operators', 'left angle bracket'], ['\\rangle', '⟩', 'Operators', 'right angle bracket'],
    ['\\lfloor', '⌊', 'Operators', 'floor'], ['\\rceil', '⌉', 'Operators', 'ceiling ceil'],
    ['\\|', '‖', 'Operators', 'norm double bar'],
    // Big operators and structures
    ['\\sum', '∑', 'Big', 'sum sigma'], ['\\prod', '∏', 'Big', 'product'], ['\\coprod', '∐', 'Big', 'coproduct'],
    ['\\int', '∫', 'Big', 'integral'], ['\\iint', '∬', 'Big', 'double integral'],
    ['\\iiint', '∭', 'Big', 'triple integral'], ['\\oint', '∮', 'Big', 'contour integral'],
    ['\\bigcup', '⋃', 'Big', 'big union'], ['\\bigcap', '⋂', 'Big', 'big intersection'],
    ['\\lim', 'lim', 'Big', 'limit'], ['\\max', 'max', 'Big', 'maximum'], ['\\min', 'min', 'Big', 'minimum'],
    ['\\arg\\max', 'argmax', 'Big', 'argmax'], ['\\sup', 'sup', 'Big', 'supremum'], ['\\inf', 'inf', 'Big', 'infimum'],
    ['\\frac{}{}', '½', 'Big', 'fraction'], ['\\sqrt{}', '√', 'Big', 'square root'],
    ['\\binom{}{}', '(ⁿₖ)', 'Big', 'binomial choose'],
    // Arrows
    ['\\to', '→', 'Arrows', 'to right arrow maps'], ['\\rightarrow', '→', 'Arrows', 'right arrow'],
    ['\\leftarrow', '←', 'Arrows', 'left arrow'], ['\\leftrightarrow', '↔', 'Arrows', 'left right arrow'],
    ['\\uparrow', '↑', 'Arrows', 'up arrow'], ['\\downarrow', '↓', 'Arrows', 'down arrow'],
    ['\\mapsto', '↦', 'Arrows', 'maps to'], ['\\Leftarrow', '⇐', 'Arrows', 'implied by'],
    ['\\longrightarrow', '⟶', 'Arrows', 'long right arrow'], ['\\rightleftharpoons', '⇌', 'Arrows', 'equilibrium'],
    ['\\nearrow', '↗', 'Arrows', 'north east arrow increase'], ['\\searrow', '↘', 'Arrows', 'south east arrow decrease'],
    // Accents (applied to x as an example)
    ['\\hat{}', 'x̂', 'Accents', 'hat estimate'], ['\\bar{}', 'x̄', 'Accents', 'bar mean average'],
    ['\\tilde{}', 'x̃', 'Accents', 'tilde'], ['\\vec{}', 'x⃗', 'Accents', 'vector arrow'],
    ['\\dot{}', 'ẋ', 'Accents', 'dot time derivative'], ['\\ddot{}', 'ẍ', 'Accents', 'double dot second derivative'],
    ['\\overline{}', 'x̅', 'Accents', 'overline'], ['\\mathbf{}', '𝐱', 'Accents', 'bold vector matrix'],
    ['\\mathcal{}', '𝒳', 'Accents', 'calligraphic script'], ['\\boldsymbol{}', '𝛂', 'Accents', 'bold symbol greek'],
    // Dots
    ['\\ldots', '…', 'Dots', 'dots ellipsis low'], ['\\cdots', '⋯', 'Dots', 'dots centered'],
    ['\\vdots', '⋮', 'Dots', 'vertical dots'], ['\\ddots', '⋱', 'Dots', 'diagonal dots']
  ];

  var symbols = S.map(function (s) { return { command: s[0], glyph: s[1], group: s[2], words: s[3] }; });

  // Symbols matching every word typed (by name, command or glyph), best first: a command starting with the query.
  function search(query) {
    var terms = String(query || '').toLowerCase().replace(/\\/g, '').split(/\s+/).filter(Boolean);
    if (!terms.length) return symbols.slice();
    return symbols.filter(function (s) {
      var hay = (s.words + ' ' + s.command.replace(/\\/g, '') + ' ' + s.group).toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0 || s.glyph === t; });
    }).sort(function (a, b) {
      var q = terms[0], ah = a.command.slice(1).toLowerCase().indexOf(q) === 0, bh = b.command.slice(1).toLowerCase().indexOf(q) === 0;
      return ah === bh ? 0 : ah ? -1 : 1;
    });
  }

  var api = { symbols: symbols, search: search };
  root.LitForgeSymbols = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(this);
