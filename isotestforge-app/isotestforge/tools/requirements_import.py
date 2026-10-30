"""Import des besoins : texte, Markdown, Word, PDF, Excel, Jira (identifiants conservés pour la traçabilité)."""
from __future__ import annotations

import os
from pathlib import Path

from ..agents.heuristics import split_requirements


def from_text(text: str, prefix: str = "REQ") -> list[dict]:
    return [{"id": rid, "text": body, "source": "texte"} for rid, body in split_requirements(text, prefix)]


def from_file(path: str | Path, prefix: str = "REQ") -> list[dict]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".txt", ".md"):
        return [r | {"source": p.name} for r in from_text(p.read_text(encoding="utf-8"), prefix)]
    if ext == ".docx":
        import docx
        doc = docx.Document(str(p))
        text = "\n\n".join(par.text for par in doc.paragraphs if par.text.strip())
        for t in doc.tables:   # tableaux d'exigences : 1re colonne = id, 2e = texte
            for row in t.rows[1:]:
                cells = [c.text.strip() for c in row.cells]
                if len(cells) >= 2 and cells[0] and cells[1]:
                    text += f"\n\n{cells[0]} : {cells[1]}"
        return [r | {"source": p.name} for r in from_text(text, prefix)]
    if ext == ".pdf":
        import pdfplumber
        with pdfplumber.open(str(p)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return [r | {"source": p.name} for r in from_text(text, prefix)]
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(str(p), read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h or "").lower() for h in rows[0]]
        id_col = next((i for i, h in enumerate(head) if h in ("id", "exigence", "ref", "référence", "reference")), 0)
        txt_col = next((i for i, h in enumerate(head) if h in ("texte", "text", "description", "besoin",
                                                                 "libellé", "libelle")), 1)
        return [{"id": str(r[id_col]).strip(), "text": str(r[txt_col]).strip(), "source": p.name}
                for r in rows[1:] if r and r[id_col] and r[txt_col]]
    raise ValueError(f"Format de besoins non supporté : {ext}")


def from_jira(jql: str, max_results: int = 50) -> list[dict]:
    """Jira Cloud/Server : JIRA_URL, JIRA_USER, JIRA_TOKEN dans l'environnement."""
    import requests
    url, user, token = os.environ.get("JIRA_URL"), os.environ.get("JIRA_USER"), os.environ.get("JIRA_TOKEN")
    if not (url and token):
        raise RuntimeError("JIRA_URL / JIRA_TOKEN non définis")
    auth = (user, token) if user else None
    headers = {} if user else {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{url.rstrip('/')}/rest/api/2/search", params={"jql": jql, "maxResults": max_results,
                                                                    "fields": "summary,description"},
                     auth=auth, headers=headers, timeout=30)
    r.raise_for_status()
    return [{"id": i["key"], "text": f"{i['fields']['summary']}\n{i['fields'].get('description') or ''}".strip(),
             "source": "jira"} for i in r.json().get("issues", [])]
