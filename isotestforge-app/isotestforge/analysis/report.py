"""Rapports : HTML autonome (partageable) et export Excel pour les recettes formelles."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .results import RunAnalysis

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--ok:#1f7a4d;--ko:#b3261e;--na:#8a6d00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--ok:#5cc28f;--ko:#ff8a80;--na:#e0c25a}}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:24px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.passed,.true{color:var(--ok);font-weight:600}.failed,.false{color:var(--ko);font-weight:600}.not_run,.partial,.uncovered{color:var(--na);font-weight:600}
details{margin:2px 0}code{font-size:12px}.diag{font-size:12.5px;color:var(--muted)}
.wrap{overflow-x:auto}
"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def render_html(an: RunAnalysis, coverage: dict | None = None, history: list[dict] | None = None,
                title: str | None = None) -> str:
    t = an.totals
    out = [f"<!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>{_e(title or 'Rapport ' + an.suite)}</title>"
           f"<style>{CSS}</style></head><body><main>",
           f"<h1>{_e(title or 'Rapport de campagne — ' + an.suite)}</h1>",
           f"<div class='muted'>Généré le {datetime.now():%d/%m/%Y %H:%M} par ISO-TestForge</div>",
           "<div class='tiles'>",
           f"<div class='tile'>Réussite<b>{t['pass_rate']} %</b></div>",
           f"<div class='tile'>Scénarios OK<b class='passed'>{t['passed']}</b></div>",
           f"<div class='tile'>En échec<b class='failed'>{t['failed']}</b></div>",
           f"<div class='tile'>Non exécutés<b class='not_run'>{t['not_run']}</b></div>",
           f"<div class='tile'>Temps moyen<b>{t['avg_ms']} ms</b></div>", "</div>"]
    if history:
        out.append("<h2>Tendance</h2>" + _trend_svg(history))
    out.append("<h2>Exigences</h2><div class='wrap'><table><tr><th>Exigence</th><th>Statut</th>"
               "<th>Scénarios</th></tr>")
    req_scn: dict[str, list[str]] = {}
    for s in an.scenarios:
        for r in s.requirements:
            req_scn.setdefault(r, []).append(s.id)
    for r in sorted(an.by_requirement):
        st = an.by_requirement[r]
        out.append(f"<tr><td>{_e(r)}</td><td class='{st}'>{_e(st)}</td><td>{_e(', '.join(req_scn.get(r, [])))}</td></tr>")
    out.append("</table></div><h2>Scénarios</h2><div class='wrap'><table><tr><th>Scénario</th><th>Statut</th>"
               "<th>Étapes</th></tr>")
    for s in an.scenarios:
        steps = []
        for st in s.steps:
            d = ""
            if st.diagnostic:
                d = (f"<div class='diag'>Cause probable : {_e(st.diagnostic['cause'])}<br>Action : "
                     f"{_e(st.diagnostic['action'])} <i>({_e(st.diagnostic.get('source', ''))})</i></div>")
            steps.append(f"<div><code>{_e(st.step)}</code> DE39={_e(st.got)} "
                         f"(attendu {_e('|'.join(st.expected))}) <span class='{str(st.success).lower()}'>"
                         f"{'OK' if st.success else 'KO'}</span> {st.elapsed_ms} ms"
                         f"{'' if st.success else '<br><span class=diag>' + _e(st.message) + '</span>'}{d}</div>")
        out.append(f"<tr><td><b>{_e(s.id)}</b><br><span class='muted'>{_e(s.title)}</span></td>"
                   f"<td class='{s.status}'>{_e(s.status)}</td><td>{''.join(steps) or '—'}</td></tr>")
    out.append("</table></div>")
    if an.infra:
        out.append("<h2>Messages techniques</h2><ul>" + "".join(
            f"<li>{_e(s.label)} : DE39={_e(s.got)} <span class='{str(s.success).lower()}'>"
            f"{'OK' if s.success else 'KO'}</span></li>" for s in an.infra) + "</ul>")
    if coverage:
        out.append(f"<h2>Couverture — score {coverage.get('score')} / 100</h2>")
        mx = coverage.get("matrix", {})
        if mx:
            ops = list(next(iter(mx.values())).keys())
            out.append("<div class='wrap'><table><tr><th>Carte</th>" + "".join(f"<th>{_e(o)}</th>" for o in ops) +
                       "<th>Annulation</th></tr>")
            for ct, row in mx.items():
                rev = coverage.get("reversal_by_card_type", {}).get(ct)
                out.append(f"<tr><td>{_e(ct)}</td>" + "".join(
                    f"<td class='{'passed' if n else 'not_run'}'>{n or '—'}</td>" for n in row.values()) +
                    f"<td class='{'passed' if rev else 'not_run'}'>{'oui' if rev else 'non'}</td></tr>")
            out.append("</table></div>")
        if coverage.get("gaps"):
            out.append("<h2>Trous de couverture</h2><ul>" +
                       "".join(f"<li>{_e(g)}</li>" for g in coverage["gaps"]) + "</ul>")
    out.append("</main></body></html>")
    return "".join(out)


def _trend_svg(history: list[dict]) -> str:
    h = history[-20:]
    w, hh = 640, 120
    bw = w / max(1, len(h))
    bars = []
    for i, r in enumerate(h):
        v = float(r.get("pass_rate", 0))
        bh = hh * v / 100
        bars.append(f"<rect x='{i * bw + 2:.1f}' y='{hh - bh:.1f}' width='{bw - 4:.1f}' height='{bh:.1f}' rx='3' "
                    f"fill='var(--ok)'><title>{_e(r.get('label', ''))} : {v} %</title></rect>")
    return (f"<svg viewBox='0 0 {w} {hh + 4}' width='100%' role='img' aria-label='Taux de réussite des derniers runs'>"
            + "".join(bars) + "</svg>")


def write_html(an: RunAnalysis, path: str | Path, **kw) -> str:
    Path(path).write_text(render_html(an, **kw), encoding="utf-8")
    return str(path)


def write_excel(an: RunAnalysis, path: str | Path, coverage: dict | None = None) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Synthèse"
    for k, v in an.totals.items():
        ws.append([k, v])
    ws2 = wb.create_sheet("Exécution")
    ws2.append(["Scénario", "Titre", "Exigences", "Statut", "Étape", "DE39 reçu", "DE39 attendu", "Résultat",
                "Temps (ms)", "Message", "Cause probable", "Action"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    fills = {"OK": PatternFill("solid", fgColor="D8F0E3"), "KO": PatternFill("solid", fgColor="F9DEDC")}
    for s in an.scenarios:
        for st in s.steps or [None]:
            if st is None:
                ws2.append([s.id, s.title, ", ".join(s.requirements), s.status])
                continue
            res = "OK" if st.success else "KO"
            ws2.append([s.id, s.title, ", ".join(s.requirements), s.status, st.step, st.got, "|".join(st.expected),
                        res, st.elapsed_ms, st.message, (st.diagnostic or {}).get("cause", ""),
                        (st.diagnostic or {}).get("action", "")])
            ws2.cell(ws2.max_row, 8).fill = fills[res]
    ws3 = wb.create_sheet("Exigences")
    ws3.append(["Exigence", "Statut"])
    for r, st in sorted(an.by_requirement.items()):
        ws3.append([r, st])
    if coverage:
        ws4 = wb.create_sheet("Couverture")
        ws4.append(["Score", coverage.get("score")])
        for g in coverage.get("gaps", []):
            ws4.append(["Trou", g])
    for sheet in wb.worksheets:
        for col in sheet.columns:
            width = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[col[0].column_letter].width = min(60, max(10, width + 2))
    wb.save(str(path))
    return str(path)
