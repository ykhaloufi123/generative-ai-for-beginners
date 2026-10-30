"""Modèle des intentions de test (sortie de l'Analyste, entrée du Concepteur)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Operation = Literal["purchase", "preauth", "completion", "refund", "withdrawal", "balance_inquiry", "reversal",
                    "network", "key_exchange", "other"]
Channel = Literal["emv", "contactless", "magstripe", "ecom", "manual", "atm", "any"]

OPERATION_TEMPLATE = {"purchase": "PURCHASE", "preauth": "PREAUTH", "refund": "REFUND",
                      "withdrawal": "WITHDRAWAL", "balance_inquiry": "BALANCE_INQUIRY", "network": "ECHO",
                      "key_exchange": "KEY_EXCHANGE"}


class Intent(BaseModel):
    id: str
    requirement: str
    summary: str = Field(..., description="Phrase courte décrivant le test")
    operation: Operation
    channel: Channel = "any"
    scheme: Literal["visa", "mastercard", "local", "any"] = "any"
    amount: float | None = None
    amount_condition: Literal["below_cvm_limit", "above_cvm_limit", "over_balance", "zero", "none"] = "none"
    followed_by: Literal["reversal", "completion", "reversal_advice", "none"] = "none"
    negative: str | None = Field(None, description="Nom d'un mutateur du catalogue si cas négatif")
    expect: str = Field("approved", description="Code sémantique attendu (approved, wrong_pin...)")
    priority: Literal["P1", "P2", "P3"] = "P2"
    complex: bool = Field(False, description="True si le cas sort des schémas standards")


class AnalysisResult(BaseModel):
    intents: list[Intent]
    questions: list[str] = Field(default_factory=list, description="Ambiguïtés à faire trancher par le testeur")


def analysis_schema() -> dict:
    return AnalysisResult.model_json_schema()
