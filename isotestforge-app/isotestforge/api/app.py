"""API REST + interface web (Starlette, ASGI). Lancement : itf serve  (ou uvicorn isotestforge.api.app:app)."""
from __future__ import annotations

import base64
import contextlib
import hmac
import os
import tempfile
from pathlib import Path

import yaml
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from ..analysis import coverage as covmod
from ..dsl import loader
from ..dsl.models import json_schema
from ..engine import cardgen, mutators
from ..engine.binder import Binder
from ..engine.expand import expand_suite
from ..iso.packager import GenericPackager
from ..orchestrator.pipeline import Pipeline
from ..simulator.host import HostSimulator, SimConfig
from ..tools import decoder, requirements_import, spec_diff, trace_import

BASE = Path(os.environ.get("ITF_HOME", Path(__file__).resolve().parents[2]))
STATIC = Path(__file__).parent / "static"
P = Pipeline(BASE, use_llm=os.environ.get("ITF_NO_LLM") != "1")
SIM: dict = {"server": None, "sim": None}


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


def err(msg, status=400):
    return JSONResponse({"error": str(msg)}, status_code=status)


def handle(fn):
    async def wrapper(request: Request):
        try:
            return await fn(request)
        except KeyError as e:
            return err(e, 404)
        except Exception as e:   # erreurs métier remontées telles quelles à l'interface
            return err(f"{type(e).__name__}: {e}", 400)
    return wrapper


async def body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ------------------------------------------------------------------ pages
async def index(request):
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ référentiel
@handle
async def referential(request):
    P.ref.reload()
    return ok(P.ref.summary() | {"mutators": [{"name": m.name, "description": m.description, "expect": m.expect}
                                              for m in mutators.CATALOG.values()]})


@handle
async def generate_cards(request):
    b = await body(request)
    cards = cardgen.generate(b["bin"], int(b.get("count", 1)), b.get("card_type", "emv"), b.get("expiry", "3012"),
                             pin=b.get("pin", "1234"), keyset=b.get("keyset"), cvn=int(b.get("cvn", 10)),
                             start_seq=int(b.get("start_seq", 100)), id_prefix=b.get("id_prefix"))
    for c in cards:
        P.ref.add_card(c)
    return ok({"created": [c.id for c in cards]})


async def dsl_schema(request):
    return ok(json_schema())


# ------------------------------------------------------------------ besoins & workflows
@handle
async def parse_requirements(request):
    ctype = request.headers.get("content-type", "")
    if "multipart" in ctype:
        form = await request.form()
        up = form["file"]
        suffix = Path(up.filename).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(await up.read())
        try:
            return ok(requirements_import.from_file(fh.name))
        finally:
            os.unlink(fh.name)
    b = await body(request)
    if b.get("jql"):
        return ok(await run_in_threadpool(requirements_import.from_jira, b["jql"]))
    return ok(requirements_import.from_text(b.get("text", ""), b.get("prefix", "REQ")))


@handle
async def workflows(request):
    if request.method == "GET":
        return ok(P.store.workflows())
    b = await body(request)
    if b.get("dsl"):
        wid = P.start(b["name"], b["host"], b.get("requirements", []), auto=False)
        P.store.update_workflow(wid, "designed", dsl=b["dsl"], dsl_generated=b["dsl"])
        return ok(await run_in_threadpool(P.check, wid))
    wid = P.start(b["name"], b["host"], b["requirements"], auto=bool(b.get("auto")))
    wf = await run_in_threadpool(P.analyse, wid, bool(b.get("force_llm")))
    return ok(wf)


@handle
async def workflow(request):
    wid = request.path_params["wid"]
    wf = P.store.get_workflow(wid)
    wf["cost"] = P.cost_summary(wid)
    return ok(wf)


@handle
async def workflow_action(request):
    wid, action = request.path_params["wid"], request.path_params["action"]
    b = await body(request)
    if action == "analyse":
        r = await run_in_threadpool(P.analyse, wid, bool(b.get("force_llm")))
    elif action == "approve-intents":
        r = await run_in_threadpool(P.approve_intents, wid, b.get("intents"))
    elif action == "check":
        r = await run_in_threadpool(P.check, wid, b.get("dsl"))
    elif action == "approve-dsl":
        r = await run_in_threadpool(P.approve_dsl, wid, b.get("dsl"))
    elif action == "compile":
        r = await run_in_threadpool(P.compile, wid)
    elif action == "execute":
        r = await run_in_threadpool(P.execute, wid, b.get("mode", "native"), b.get("host"),
                                    int(b["port"]) if b.get("port") else None)
    else:
        return err(f"action inconnue : {action}", 404)
    return ok(r)


@handle
async def workflow_file(request):
    wid, name = request.path_params["wid"], request.path_params["name"]
    d = (BASE / "data/workflows" / wid).resolve()
    candidates = [d / name, d / "out" / name]
    for c in candidates:
        c = c.resolve()
        if c.is_file() and str(c).startswith(str(d)):
            if c.suffix == ".html":
                return HTMLResponse(c.read_text(encoding="utf-8"))
            return FileResponse(str(c), filename=c.name)
    return err("fichier introuvable", 404)


# ------------------------------------------------------------------ outils
@handle
async def decode(request):
    b = await body(request)
    pk = P.ref.packager(b.get("host") or next(iter(P.ref.hosts)))
    d = decoder.decode(b["text"], pk, mask=b.get("mask", True))
    return ok(d | {"text": decoder.to_text(d)})


@handle
async def import_traces(request):
    form = await request.form()
    up = form["file"]
    host = form.get("host") or next(iter(P.ref.hosts))
    with tempfile.NamedTemporaryFile(suffix=Path(up.filename).suffix, delete=False) as fh:
        fh.write(await up.read())
    try:
        suite, stats = trace_import.import_traces(fh.name, P.ref, host, form.get("name") or "TRACE_REGRESSION",
                                                  P.ref.packager(host), form.get("requirement"))
    finally:
        os.unlink(fh.name)
    return ok({"stats": stats, "dsl": loader.dump(suite)})


@handle
async def diff_specs(request):
    form = await request.form()
    paths = []
    for k in ("old", "new"):
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
            fh.write(await form[k].read())
            paths.append(fh.name)
    try:
        old, new = GenericPackager.load(paths[0]), GenericPackager.load(paths[1])
        changes = spec_diff.diff_packagers(old, new)
    finally:
        for p in paths:
            os.unlink(p)
    impacted = []
    for w in P.store.workflows(200):
        wf = P.store.get_workflow(w["id"])
        if wf["data"].get("dsl"):
            try:
                suite = loader.parse(wf["data"]["dsl"])
                scns, _ = expand_suite(suite, P.ref)
                bound = Binder(P.ref, suite).bind(scns)
                fields = {f"{s.id}.{st.id}": {int(f.key.split('.')[0]) for f in st.fields}
                          for s in bound.scenarios for st in s.steps}
                man = {"scenarios": [{"id": s.id, "steps": [{"id": st.id, "mti": st.mti, "expect": st.expect}
                                                            for st in s.steps]} for s in bound.scenarios]}
                for i in spec_diff.impacted([man], fields, changes, []):
                    impacted.append(i | {"workflow": w["id"], "suite": suite.suite})
            except Exception:
                continue
    return ok({"changes": changes, "impacted": impacted})


@handle
async def coverage(request):
    manifests, reqs = [], P.store.requirements()
    for w in P.store.workflows(200):
        m = BASE / "data/workflows" / w["id"] / "out/manifest.json"
        if m.exists():
            import json
            manifests.append(json.loads(m.read_text()))
    return ok(covmod.compute(manifests, P.ref, reqs, P.store.last_analyses()).to_dict())


async def usage(request):
    return ok(P.router.usage_report() if P.router else {"calls": 0, "note": "LLM désactivé"})


async def history(request):
    return ok(P.store.history(request.query_params.get("suite")))


@handle
async def diagnostics(request):
    if request.method == "POST":
        b = await body(request)
        return ok(P.kb.add(b["when"], b["cause"], b["action"]))
    return ok(P.kb.rules)


async def library(request):
    return ok({"library": P.store.library_list(), "corrections": P.store.corrections(20)})


@handle
async def proposals(request):
    if request.method == "POST":
        pid = request.path_params.get("pid")
        prop = next(p for p in P.store.proposals() if p["id"] == pid)
        tpl = yaml.safe_load(prop["content"])
        items = [t.model_dump() for t in P.ref.templates.values()] + [tpl]
        P.ref.save("templates", items)
        P.store.db.execute("UPDATE proposals SET status='accepted' WHERE id=?", (pid,))
        P.store.db.commit()
        return ok({"accepted": tpl["id"]})
    return ok(P.store.proposals())


# ------------------------------------------------------------------ simulateur intégré
@handle
async def simulator(request):
    action = request.path_params.get("action", "status")
    if action == "start" and SIM["server"] is None:
        b = await body(request)
        await _start_simulator(b.get("host", "0.0.0.0"), int(b.get("port", 8583)))
    elif action == "stop" and SIM["server"] is not None:
        SIM["server"].close()
        await SIM["server"].wait_closed()
        SIM["server"] = SIM["sim"] = None
    s = SIM["sim"]
    return ok({"running": s is not None, "stats": s.stats if s else None,
               "port": SIM["server"].sockets[0].getsockname()[1] if SIM["server"] else None})


async def simulator_traces(request):
    p = BASE / "data/traces/simulator.jsonl"
    if not p.exists():
        return err("aucune trace (exécuter une campagne d'abord)", 404)
    return FileResponse(str(p), filename="simulator_traces.jsonl")


async def health(request):
    return PlainTextResponse("ok")


routes = [
    Route("/", index), Route("/health", health),
    Route("/api/referential", referential), Route("/api/referential/cards/generate", generate_cards, methods=["POST"]),
    Route("/api/dsl/schema", dsl_schema),
    Route("/api/requirements/parse", parse_requirements, methods=["POST"]),
    Route("/api/workflows", workflows, methods=["GET", "POST"]),
    Route("/api/workflows/{wid}", workflow),
    Route("/api/workflows/{wid}/files/{name:path}", workflow_file),
    Route("/api/workflows/{wid}/{action}", workflow_action, methods=["POST"]),
    Route("/api/decode", decode, methods=["POST"]),
    Route("/api/traces/import", import_traces, methods=["POST"]),
    Route("/api/spec-diff", diff_specs, methods=["POST"]),
    Route("/api/coverage", coverage), Route("/api/usage", usage), Route("/api/history", history),
    Route("/api/diagnostics", diagnostics, methods=["GET", "POST"]),
    Route("/api/library", library),
    Route("/api/proposals", proposals), Route("/api/proposals/{pid}/accept", proposals, methods=["POST"]),
    Route("/api/simulator/traces", simulator_traces),
    Route("/api/simulator", simulator), Route("/api/simulator/{action}", simulator, methods=["POST"]),
]

async def _start_simulator(host: str, port: int) -> None:
    cfg = SimConfig.load(BASE / "config/simulator.yaml")
    if cfg.trace_file:
        cfg.trace_file = str(BASE / cfg.trace_file)
    sim = HostSimulator(P.ref, cfg)
    SIM["server"] = await sim.serve(host, port)
    SIM["sim"] = sim


@contextlib.asynccontextmanager
async def lifespan(app):
    # Sur un hébergeur HTTP (Hugging Face, Render...), le simulateur tourne dans le même conteneur :
    # le runner natif l'atteint sur 127.0.0.1:8583 sans exposer de port TCP supplémentaire.
    if os.environ.get("ITF_AUTOSTART_SIMULATOR", "1") == "1":
        try:
            await _start_simulator(os.environ.get("ITF_SIM_LISTEN", "0.0.0.0"),
                                   int(os.environ.get("ITF_SIM_PORT", "8583")))
        except OSError as e:   # port déjà pris (simulateur lancé à part) : on continue sans
            print(f"Simulateur intégré non démarré : {e}")
    yield
    if SIM["server"] is not None:
        SIM["server"].close()


class BasicAuth:
    """Protection minimale d'une instance publique : ITF_BASIC_AUTH=utilisateur:motdepasse."""

    def __init__(self, app, credentials: str):
        self.app = app
        self.expected = "Basic " + base64.b64encode(credentials.encode()).decode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(given, self.expected):
                resp = PlainTextResponse("Authentification requise", status_code=401,
                                         headers={"WWW-Authenticate": 'Basic realm="ISO-TestForge"'})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


middleware = []
if os.environ.get("ITF_BASIC_AUTH"):
    middleware.append(Middleware(BasicAuth, credentials=os.environ["ITF_BASIC_AUTH"]))

app = Starlette(routes=routes, lifespan=lifespan, middleware=middleware)
