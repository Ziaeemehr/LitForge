"""Produce the real matrix API response for the JavaScript rendering contract test."""
import json
import sqlite3

from litforge.analysis.matrix import MatrixColumn, create_matrix
from litforge.storage.db import migrate
from litforge.ui.api import lens_matrix

with sqlite3.connect(':memory:') as conn:
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('proj','demo','now')")
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('p','Synthetic paper','now','now')")
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) VALUES ('proj','p','manual','now')")
    matrix = create_matrix(conn, 'proj', 'Contract matrix', 'What is the gain?',
                           [MatrixColumn(key='gain', label='Gain', instruction='gain', type='number')])
    print(json.dumps(lens_matrix(conn, matrix.id)))
