# Project wrap-up: HR screening application.
"""Read-only job charts built from current, comparable assessment results."""
import json


def role_summary(applications, ranking, rubric, jobs):
    # One application per stage; a historical failed job must not count twice.
    latest = {}
    for job in sorted(jobs, key=lambda row: row['id'], reverse=True):
        latest.setdefault(job['application_id'], job)
    stages = {'pending': 0, 'completed': 0, 'review': 0, 'duplicate': 0, 'failed': 0}
    for application in applications:
        state = application['status']
        if state not in {'completed', 'review', 'duplicate'}:
            state = 'failed' if latest.get(application['id'], {}).get('state') == 'failed' else 'pending'
        stages[state] += 1
    bins = [{'label': label, 'min': low, 'max': high, 'count': 0}
            for label, low, high in [('0–19', 0, 20), ('20–39', 20, 40),
                                     ('40–59', 40, 60), ('60–79', 60, 80), ('80–100', 80, 101)]]
    for application in ranking:
        for bucket in bins:
            if bucket['min'] <= application['score'] < bucket['max']:
                bucket['count'] += 1
                break
    criteria = []
    findings = [json.loads(application['body'])['findings'] for application in ranking]
    for criterion in (json.loads(rubric['body'])['criteria'] if rubric else []):
        counts = dict(supported=0, partial=0, not_demonstrated=0)
        for assessment in findings:
            finding = next((item for item in assessment if item['criterion_id'] == criterion['id']), None)
            if finding:
                counts[finding['level']] += 1
        criteria.append(dict(id=criterion['id'], description=criterion['description'],
                             weight=criterion['weight'], **counts))
    return {'total': len(applications), 'comparable': len(ranking), 'pipeline': stages,
            'average_score': round(sum(item['score'] for item in ranking) / len(ranking), 1) if ranking else None,
            'score_distribution': bins, 'criteria': criteria}
