"""Exécution des plans par JMeter (binaire local ou image Docker avec le plugin préinstallé)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class JMeterNotFound(Exception):
    pass


def jmeter_command() -> list[str]:
    home = os.environ.get("JMETER_HOME")
    if home and Path(home, "bin", "jmeter").exists():
        return [str(Path(home, "bin", "jmeter"))]
    if shutil.which("jmeter"):
        return ["jmeter"]
    image = os.environ.get("ITF_JMETER_IMAGE")
    if image and shutil.which("docker"):
        return ["docker", "run", "--rm", "--network", os.environ.get("ITF_DOCKER_NETWORK", "host"),
                "-v", "{out}:/work", "-w", "/work", image]
    raise JMeterNotFound("JMeter introuvable : définir JMETER_HOME, ajouter jmeter au PATH ou ITF_JMETER_IMAGE")


def run(jmx: str | Path, out_dir: str | Path, host: str | None = None, port: int | None = None,
        timeout_s: int = 3600, extra_props: dict | None = None) -> dict:
    out = Path(out_dir).resolve()
    cmd = [c.replace("{out}", str(out)) for c in jmeter_command()]
    in_docker = cmd[0] == "docker"
    jmx_p = Path(jmx).name if in_docker else str(Path(jmx).resolve())
    base = "" if in_docker else str(out) + "/"
    jtl = out / "results.jtl"
    if jtl.exists():
        jtl.unlink()
    args = ["-n", "-t", jmx_p, "-l", f"{base}results.jtl", "-j", f"{base}jmeter.log",
            "-q", f"{base}keys.properties", f"-JITF_PACKAGER={base}packager.xml",
            "-Jjmeter.save.saveservice.output_format=csv",
            "-Jjmeter.save.saveservice.response_data.on_error=true"]
    if host:
        args.append(f"-JITF_HOST={host}")
    if port:
        args.append(f"-JITF_PORT={port}")
    for k, v in (extra_props or {}).items():
        args.append(f"-J{k}={v}")
    p = subprocess.run(cmd + args, cwd=str(out), capture_output=True, text=True, timeout=timeout_s)
    return {"returncode": p.returncode, "jtl": str(jtl), "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:],
            "command": " ".join(cmd + args)}
