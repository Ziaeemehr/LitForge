# Paper versions: preprint, accepted and published copies

## Problem

Real case: an eLife paper (`10.7554/elife.106194`) got the bioRxiv preprint PDF attached silently, because
`fetch.candidate_urls` tries repository copies before publisher copies and OpenAlex lists the bioRxiv copy as a
location of the eLife work. Evidence, quotes and numbers were then cited as "the eLife paper" while coming from
the draft. The same project held 7 records for what are 2 works: the preprint, four eLife reviewed versions
(`.1`..`.4`), the version of record, and an unrelated JOSS paper.

Versions must not be merged blindly (a draft and the published paper can differ), and a draft must never be
presented as the published version.

## 1. Know which version a PDF is

- Each artifact records `version` (`published` | `accepted` | `submitted` | `unknown`), `version_source`
  (`openalex` | `unpaywall` | `host` | `pdf_text` | `user` | `unknown`) and `source_url`
  (`artifacts.version_label` exists but is unused; add the new columns in a migration).
- Signals, strongest first: OpenAlex `location.version` (`publishedVersion`, `acceptedVersion`,
  `submittedVersion`); Unpaywall `oa_locations[].version`; the host (arXiv, bioRxiv, medRxiv, SSRN, Research
  Square, OSF/PsyArXiv, chemRxiv, Preprints.org -> submitted); the first page text ("preprint",
  "not peer-reviewed", "not certified by peer review" -> submitted). A user override wins and is kept.
- Manually added PDFs start as `unknown` (the first-page check may set `submitted`).

## 2. Download the best version

- `fetch` orders candidates published > accepted > submitted > unknown, arXiv included in that order (arXiv is
  `submitted` unless OpenAlex says otherwise). Within a rank keep today's preference (direct PDF links first).
- If the best copy available is a draft, it is still downloaded (better than nothing), but labelled.
- Report and UI say which version was downloaded ("preprint downloaded; the published version is not openly
  available").

## 3. Link versions of one work (never merge)

- New table `paper_versions(group_id, paper_id, role, evidence, confirmed, created_at)`; one group per work.
  `role`: `published`, `accepted`, `preprint`, `revision` (e.g. eLife `.N`), `unknown`.
- Automatic links only from explicit signals:
  - Crossref `relation` (`is-preprint-of`, `has-preprint`, `is-version-of`, `has-version`), queried by DOI
    (cached like other Crossref calls);
  - bioRxiv/medRxiv `published` DOI (api.biorxiv.org details endpoint);
  - arXiv record DOI / `journal_ref` matching a library DOI;
  - DOI version suffix: `<doi>.<n>` with the same base DOI (eLife reviewed preprints);
  - OpenAlex: two library records with the same OpenAlex work id.
- Title + authors + year similarity only creates a *suggested* link (`confirmed = 0`), shown for the user to
  confirm or reject; never used automatically. Rejected suggestions are remembered.
- Runs during enrich/fetch and on demand (`litforge versions link`), offline-safe (failures leave no links).

## 4. Which version analyses use

- Each group has a *primary* paper: the user's choice, else the best available full text by
  published > accepted > preprint > revision.
- Project views (library, papers list, matrix rows, screening queue, export) show one row per group with a
  version badge and a "versions" list; non-primary members stay in the project but are hidden by default
  ("show all versions" toggle). Retrieval for Ask/matrix/claims uses only primary members unless the user asks
  for all versions. Existing runs and evidence are untouched (immutable ledger).
- Citations, quotes and exported notes carry the version of their source ("bioRxiv preprint, v1").

## 5. Surfaces

- CLI: `litforge versions [--project]` (groups, roles, primary, PDF version), `versions link`,
  `versions set-primary <paper>`, `versions confirm|reject <a> <b>`, `versions set <paper> --version published`.
- UI: version badge on rows and in the drawer; drawer section "Versions" with roles, PDF status, set-primary,
  confirm/reject suggestions; Ask evidence chips show "preprint" when applicable.
- Export: front matter `version`, `version_source`, `versions` (paper ids/DOIs of the group), README column.

## Tests

Fixtures for OpenAlex locations with versions, Unpaywall versions, Crossref relations, bioRxiv published DOI,
eLife DOI suffix; fetch order; labelling of a preprint-only download; linking rules (explicit vs suggested);
primary selection and retrieval filtering; migrations on existing data (artifacts get `unknown`, no groups);
UI badge/drawer; export fields. Regression: the eLife case above ends with the eLife record either holding a
published PDF or holding none, and the bioRxiv PDF attached to the bioRxiv record, labelled `submitted`.
