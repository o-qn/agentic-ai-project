from .schemas import Assessment, Rubric

FACTORS = {'supported':1.0,'partial':0.5,'not_demonstrated':0.0}

def validate_evidence(assessment,rubric,sections,seen):
    a = Assessment.model_validate(assessment)
    r = Rubric.model_validate(rubric)
    originals = {s['id']:s['text'] for s in sections}
    errors = []
    ids = [f.criterion_id for f in a.findings]
    if len(ids) != len(set(ids)) or set(ids) != {c.id for c in r.criteria}:
        errors.append('Address each rubric criterion exactly once')
    if set(a.covered_sections) != set(originals) or not set(originals).issubset(seen):
        errors.append('Read and cover all extracted sections')
    for finding in a.findings:
        if finding.level != 'not_demonstrated' and not finding.evidence:
            errors.append(f'{finding.criterion_id}: positive match needs evidence')
        for citation in finding.evidence:
            if citation.section_id not in seen or citation.quote not in originals.get(citation.section_id,''):
                errors.append(f'{finding.criterion_id}: citation is not in the inspected source section')
    return errors

def score(assessment,rubric):
    a = Assessment.model_validate(assessment)
    r = Rubric.model_validate(rubric)
    findings = {f.criterion_id:f for f in a.findings}
    if len(findings)!=len(a.findings) or set(findings)!={c.id for c in r.criteria}:
        raise ValueError('Assessment/rubric mismatch')
    return round(sum(c.weight*FACTORS[findings[c.id].level] for c in r.criteria),4)

def ranked(db,role_id):
    rows = db.rows('''SELECT a.*,s.score,s.body,s.created AS assessed_at,s.rubric_id FROM applications a
      JOIN roles r ON r.id=a.role_id JOIN assessments s ON s.application_id=a.id
      AND s.version=a.version AND s.rubric_id=r.rubric_id
      WHERE a.role_id=? AND a.active=1 AND r.active=1 AND r.paused=0 AND a.duplicate_of IS NULL
      AND a.status='completed' ORDER BY s.score DESC,a.id''',(role_id,))
    previous = None
    rank = 0
    counts = {}
    for row in rows:
        counts[row['score']] = counts.get(row['score'],0)+1
    for i,row in enumerate(rows,1):
        if row['score'] != previous:
            rank = i
            previous = row['score']
        row.update(rank=rank,tie=counts[row['score']]>1)
    return rows
