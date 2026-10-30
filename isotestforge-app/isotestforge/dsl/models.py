"""DSL intermédiaire : contrat entre les agents IA et le moteur déterministe.

Le LLM ne produit QUE ce format (validé par JSON Schema). Il ne voit jamais de PAN, PIN ou clé :
seulement des identifiants de profils, de templates et de mutateurs.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExpectValue = Union[str, list[str]]
_ID = re.compile(r"^[A-Za-z0-9_\-\.]+$")


class Profiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: str | None = None
    terminal: str | None = None
    keys: str | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    template: str | None = Field(None, description="Identifiant de template du référentiel (PURCHASE, REVERSAL...)")
    mti: str | None = Field(None, pattern=r"^\d{4}$")
    description: str = ""
    fields: dict[str, str] = Field(default_factory=dict, description="DE -> valeur ; '55.9F02' pour un tag EMV")
    remove: list[str] = Field(default_factory=list, description="Champs à retirer du template")
    crypto: list[Literal["auto", "pin", "mac", "arqc", "none"]] | None = None
    ref: str | None = Field(None, description="Étape d'origine pour le chaînage (reversal, complétion)")
    expect: dict[str, ExpectValue] = Field(default_factory=dict,
                                           description="DE -> code/semantique, 'present', 'absent', 're:...', 'timeout'")
    amount: float | None = None
    timeout_ms: int | None = None
    delay_ms: int = 0
    pin: str | None = Field(None, description="Surcharge de PIN (cas négatifs)")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("identifiant d'étape : lettres, chiffres, _ - .")
        return v

    @model_validator(mode="after")
    def _mti_or_template(self):
        if not self.template and not self.mti:
            raise ValueError(f"étape {self.id} : 'template' ou 'mti' obligatoire")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    requirement: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    priority: Literal["P1", "P2", "P3"] = "P2"
    group: str | None = Field(None, description="Groupe (Thread Group) ; défaut = premier tag")
    profiles: Profiles = Field(default_factory=Profiles)
    variables: dict[str, str | float | int] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    derived_from: str | None = Field(None, description="'SCENARIO' ou 'SCENARIO.etape' à copier")
    mutation: str | None = Field(None, description="Mutateur du catalogue (wrong_pin, expired_card...)")
    mutation_args: dict[str, str | int | float] = Field(default_factory=dict)
    expect: dict[str, ExpectValue] = Field(default_factory=dict, description="Surcharge sur la dernière étape")
    expand: dict[str, list[str | float]] = Field(
        default_factory=dict, description="Dimensions combinatoires (card, terminal, amount) -> pairwise")
    origin: Literal["manual", "ai", "heuristic", "combinatorial", "mutation", "trace", "library"] = "manual"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("identifiant de scénario : lettres, chiffres, _ - .")
        return v

    @model_validator(mode="after")
    def _steps_or_derived(self):
        if not self.steps and not self.derived_from:
            raise ValueError(f"scénario {self.id} : 'steps' ou 'derived_from' obligatoire")
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError(f"scénario {self.id} : identifiants d'étapes en double")
        return self


class LoadProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tps: float = 10
    threads: int = 10
    ramp_up_s: int = 30
    duration_s: int = 300
    template: str = "PURCHASE"
    cards: list[str] = Field(default_factory=list)
    terminals: list[str] = Field(default_factory=list)
    amounts: list[float] = Field(default_factory=lambda: [10.0, 55.5, 120.0])


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: str | None = None
    terminal: str | None = None
    keys: str | None = None
    amount: float = 100.0
    timeout_ms: int | None = None


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: str
    host: str
    description: str = ""
    spec_version: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    scenarios: list[Scenario] = Field(default_factory=list)
    load: LoadProfile | None = None

    @model_validator(mode="after")
    def _unique(self):
        ids = [s.id for s in self.scenarios]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"scénarios en double : {sorted(dup)}")
        return self


def json_schema() -> dict:
    return Suite.model_json_schema()


def scenarios_json_schema() -> dict:
    """Schéma restreint fourni au LLM (liste de scénarios seulement)."""
    sch = Scenario.model_json_schema()
    return {"type": "object", "properties": {"scenarios": {"type": "array", "items": sch}},
            "required": ["scenarios"], "$defs": sch.pop("$defs", {})}


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, ensure_ascii=False))
