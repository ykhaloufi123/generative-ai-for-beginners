import asyncio
import shutil
import socket
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path):
    """Copie isolée du projet (référentiel, config, exemples) pour chaque test."""
    for d in ("data/referential", "data/packagers", "data/knowledge", "config", "examples"):
        shutil.copytree(ROOT / d, tmp_path / d)
    return tmp_path


@pytest.fixture()
def ref(workspace):
    from isotestforge.referential.repository import Referential
    return Referential(workspace / "data/referential", workspace)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def simulator(ref):
    from isotestforge.simulator.host import HostSimulator, SimConfig
    cfg = SimConfig.load(ROOT / "config/simulator.yaml")
    cfg.trace_file = None
    sim = HostSimulator(ref, cfg)
    port = free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(sim.serve("127.0.0.1", port))
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    yield sim, port
    loop.call_soon_threadsafe(loop.stop)
