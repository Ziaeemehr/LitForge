// LitForge editor bundle: the CodeMirror 6 pieces the Write view uses, exposed as window.LitForgeCM.
import { EditorState, Compartment, StateEffect, StateField } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
  Decoration, placeholder } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab, undo, redo } from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches, openSearchPanel } from "@codemirror/search";
import { StreamLanguage, syntaxHighlighting, defaultHighlightStyle, bracketMatching, foldGutter,
  indentOnInput } from "@codemirror/language";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { autocompletion, closeBrackets, acceptCompletion, startCompletion } from "@codemirror/autocomplete";
import { classHighlighter } from "@lezer/highlight";

window.LitForgeCM = { EditorState, Compartment, StateEffect, StateField, EditorView, keymap, lineNumbers,
  highlightActiveLine, highlightActiveLineGutter, drawSelection, Decoration, placeholder, defaultKeymap, history,
  historyKeymap, indentWithTab, undo, redo, searchKeymap, highlightSelectionMatches, openSearchPanel,
  StreamLanguage, syntaxHighlighting, defaultHighlightStyle, bracketMatching, foldGutter, indentOnInput, stex,
  autocompletion, closeBrackets, acceptCompletion, startCompletion, classHighlighter };
