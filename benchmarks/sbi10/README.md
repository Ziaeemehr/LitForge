# sbi10 gold benchmark

Ten open-access arXiv papers: five on simulation-based inference (Cranmer et al. review, sbi toolkit,
APT, SNL, SNPE-A) and five widely used machine-learning papers (MAF, Neural ODEs, ResNet, Adam,
Transformer). The mix gives the screening labels clear negatives and the matrix many "not reported"
and "not applicable" cells, where models tend to invent values.

| Part | Contents |
|---|---|
| `questions` | 30 English questions, each with a short reference answer, and 20 Persian questions that mirror English ones (`fa01` ~ `q01`) and share their targets; 88 targets in all |
| `matrix_columns`, `matrix` | 4 columns × 10 papers: code availability, optimizer, inference target, GPUs used (34 labelled cells) |
| `criteria`, `screening` | One inclusion and one exclusion criterion; 5 relevant and 5 irrelevant papers |
| `claims`, `relations` | 14 claims from the SBI papers; 7 relations between different papers (3 supports, 2 contradicts, 2 qualifies) |

Every target names the arXiv id, the PDF page and a short quote from that page. `papers.txt` pins
each paper's version and sha256, and the fetch script refuses a different file, since a new arXiv
version moves pages and quotes.

## How the labels were checked

Every quote was checked against the raw text of its PDF page (PyMuPDF) and against LitForge's
indexed chunks (`litforge eval check`); both passes found all 101 quotes. Each "not reported" matrix
cell was checked by searching the whole paper for the missing detail. The first check caught two
wrong page numbers and one quote taken from a section heading, which the index keeps as the section
name rather than as text; those labels were corrected.

## Scope and limits

- Relations are only scored between claims of different papers, because the claim graph compares
  papers, not sentences within one paper.
- A claim counts as found when an extracted claim of that paper cites the labelled quote on its
  page; paraphrased claims citing a different passage are not matched.
- `S1` screening needs a discovery run, so on this benchmark only `S2` (full text) is measured.
- The labels were written and checked by Claude (Anthropic) from the papers' text, not by a domain
  expert panel; treat a disagreement with a model as a reason to look, not as proof.
