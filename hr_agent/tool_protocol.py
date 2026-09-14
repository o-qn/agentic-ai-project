"""Schema-constrained tool envelopes for local models; execution still uses per-tool validators."""
from .schemas import TOOL_MODELS

def envelope_schema(allowed=None):
    definitions={}
    functions=[]
    for name,model in TOOL_MODELS.items():
        if allowed is not None and name not in allowed:
            continue
        parameters=model.model_json_schema()
        definitions.update(parameters.pop('$defs',{}))
        functions.append({'type':'object','additionalProperties':False,'properties':{
            'name':{'const':name},'arguments':parameters},'required':['name','arguments']})
    return {'type':'object','additionalProperties':False,'$defs':definitions,'properties':{
      'tool_calls':{'type':'array','minItems':1,'maxItems':4,'items':{
        'type':'object','additionalProperties':False,'properties':{'function':{'oneOf':functions}},'required':['function']}}},'required':['tool_calls']}

PROTOCOL='''Return exactly one JSON object with tool_calls, an array of {function:{name,arguments}}.
Allowed calls and arguments:
get_role_rubric: {role_id}; get_cv_outline: {application_id}; read_cv_sections: {application_id,section_ids}.
check_requirement_evidence and submit_assessment: {assessment:{findings:[{criterion_id,level,evidence:[{section_id,quote}],missing_information,explanation}],covered_sections:[section_id]}}.
Levels: supported, partial, not_demonstrated. Evidence must quote inspected source sections exactly.
request_hr_review: {reason,evidence:[{section_id,quote}]}.
The role rubric and outline are already provided. If sections have not been read, read them first.
When all sections are inspected, submit a concise assessment or request review. Do not repeat calls indefinitely.
No other keys or tools are allowed. Keep explanations to one short sentence per criterion.'''
