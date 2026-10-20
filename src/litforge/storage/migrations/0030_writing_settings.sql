-- Compile settings per document: the file to compile (Overleaf projects and theses often use another name) and a
-- draft mode (one engine pass, for speed while writing).
ALTER TABLE writing_documents ADD COLUMN main_file TEXT NOT NULL DEFAULT 'main.tex';
ALTER TABLE writing_documents ADD COLUMN draft INTEGER NOT NULL DEFAULT 0;
