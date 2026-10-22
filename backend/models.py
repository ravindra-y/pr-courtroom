from pydantic import BaseModel
from typing import Literal, Optional

class Charge(BaseModel):
    id: str                      # "C1"
    title: str
    severity: Literal["low", "medium", "high"]
    explanation: str
    target_symbol: str           # e.g. "shop.cart.apply_discount"
    intended_behavior: str       # PR text or docstring the test is based on
    test_code: str               # full pytest function(s)

class ProsecutorOutput(BaseModel):
    charges: list[Charge]        # max 3

class DefenseResponse(BaseModel):
    charge_id: str
    stance: Literal["rebut", "fix"]
    reasoning: str
    quoted_intent: Optional[str] = None   # required when stance == "rebut"
    fixed_code: Optional[str] = None      # full replacement file content
    fixed_file_path: Optional[str] = None

class DefenderOutput(BaseModel):
    responses: list[DefenseResponse]

class RebuttalRuling(BaseModel):
    charge_id: str
    upheld: bool                 # True = rebuttal accepted, charge dismissed
    reason: str

class JudgeOutput(BaseModel):
    rebuttal_rulings: list[RebuttalRuling]
    ruling: str                  # 2 to 3 sentences
    summary: str

class TestResult(BaseModel):     # produced by sandbox, never by Gemini
    charge_id: str
    status: Literal["fail_valid", "pass", "error", "timeout", "fail_preexisting"]
    new_code: bool
    output: str
    phase: Literal["initial", "after_fix"]
