from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid',str_max_length=8000)

class Citation(Strict):
    section_id: str
    quote: str = Field(min_length=1,max_length=1200)

class Criterion(Strict):
    id: str = Field(pattern=r'^[a-z0-9_]{1,40}$')
    description: str = Field(min_length=5,max_length=1000)
    weight: float = Field(gt=0,le=100)
    supported: str = Field(min_length=5,max_length=1000)
    partial: str = Field(min_length=5,max_length=1000)
    not_demonstrated: str = Field(min_length=5,max_length=1000)

class Rubric(Strict):
    criteria: list[Criterion] = Field(min_length=1,max_length=15)
    @model_validator(mode='after')
    def weights(self):
        if abs(sum(c.weight for c in self.criteria)-100)>0.00001:
            raise ValueError('Criterion weights must total 100')
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError('Criterion IDs must be unique')
        return self

class Finding(Strict):
    criterion_id: str
    level: Literal['supported','partial','not_demonstrated']
    evidence: list[Citation] = Field(max_length=8)
    missing_information: str = Field(max_length=1000)
    explanation: str = Field(min_length=1,max_length=1200)

class Assessment(Strict):
    findings: list[Finding] = Field(min_length=1,max_length=15)
    covered_sections: list[str] = Field(max_length=200)

class RubricArgs(Strict):
    role_id: str
class OutlineArgs(Strict):
    application_id: int
class ReadArgs(OutlineArgs):
    section_ids: list[str] = Field(min_length=1,max_length=4)
class AssessmentArgs(Strict):
    assessment: Assessment
class ReviewArgs(Strict):
    reason: str = Field(min_length=5,max_length=1000)
    evidence: list[Citation] = Field(max_length=8)

TOOL_MODELS = {'get_role_rubric':RubricArgs,'get_cv_outline':OutlineArgs,'read_cv_sections':ReadArgs,
               'check_requirement_evidence':AssessmentArgs,'submit_assessment':AssessmentArgs,'request_hr_review':ReviewArgs}

# Grounded-answer (RAG) tool — separate from the assessment tools above. Each generated claim
# must cite retrieved passages by their citation_id; extra='forbid' stops a model smuggling in
# scores, ranks or applicant identities that did not come from retrieval.
class GroundedClaim(Strict):
    text: str = Field(min_length=1,max_length=1200)
    citation_ids: list[str] = Field(min_length=1,max_length=8)
class GroundedAnswer(Strict):
    claims: list[GroundedClaim] = Field(max_length=20)
    insufficient_evidence: bool = False
class GroundedArgs(Strict):
    answer: GroundedAnswer
