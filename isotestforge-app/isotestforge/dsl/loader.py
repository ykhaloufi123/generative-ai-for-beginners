"""Lecture / écriture / validation des suites DSL (YAML)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Suite


class DslError(Exception):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


def parse(text: str) -> Suite:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise DslError(f"YAML invalide : {e}") from e
    try:
        return Suite.model_validate(data)
    except ValidationError as e:
        errs = [{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()]
        raise DslError("DSL invalide : " + "; ".join(f"{x['loc']}: {x['msg']}" for x in errs[:10]), errs) from e


def load(path: str | Path) -> Suite:
    return parse(Path(path).read_text(encoding="utf-8"))


def dump(suite: Suite) -> str:
    data = suite.model_dump(exclude_defaults=True, exclude_none=True)
    data.setdefault("suite", suite.suite)
    data.setdefault("host", suite.host)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def save(suite: Suite, path: str | Path) -> None:
    Path(path).write_text(dump(suite), encoding="utf-8")
