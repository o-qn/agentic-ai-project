"""Read-only, role-scoped retrieval. Answers use source quotes and database scores."""
import copy
import json
import math
import re
import time
from .scoring import ranked
from .reports import cv_link
from .embedding import make_embedder
from . import pgvector

STOP = set('who which candidates candidate applicants applicant applications application have has with experience evidence of in and or the a an is are show me all for role skills knows know compare why ranked rank score scores top second first third list please show tell find this that these those two three completed review queued duplicate removed failed needs pending supported partially partial not demonstrated'.split())


def embedding_config(config, db):
    """Return the provider selected in the dashboard, without changing assessment settings."""
    selected = db.setting('embedding_provider', config.embed_provider)
    if selected not in {'ollama', 'voyage'} or selected == config.embed_provider:
        return config
    selected_config = copy.copy(config)
    selected_config.embed_provider = selected
    return selected_config


def index_one(config,db,ollama):
    with db.lease('indexer') as acquired:
        if not acquired:
            return False
        embedder=make_embedder(embedding_config(config, db),ollama)
        try:
            embedding_identity=embedder.identity()
        except Exception as exc:
            db.set('index_configuration_error',{'type':type(exc).__name__,'at':time.time()})
            return False
        # A model/digest change invalidates embeddings only, never assessments.
        db.execute("""UPDATE applications SET index_status='pending',index_attempts=0,index_next=0
         WHERE index_model IS NOT NULL AND index_model!=? AND index_status='ready'""",(embedding_identity,))
        app = db.one("""SELECT a.* FROM applications a JOIN roles r ON r.id=a.role_id WHERE a.active=1 AND r.active=1
         AND a.sections IS NOT NULL AND a.duplicate_of IS NULL AND a.status='completed'
         AND a.index_status IN ('pending','retry') AND a.index_next<=? ORDER BY a.id LIMIT 1""",(time.time(),))
        if not app:
            return False
        try:
            section_list=json.loads(app['sections'])
            for position,section in enumerate(section_list):
                present = db.one('SELECT id FROM chunks WHERE application_id=? AND version=? AND section_id=? AND model=?',
                                 (app['id'],app['version'],section['id'],embedding_identity))
                if present:
                    continue
                db.set('active_index',{'application_id':app['id'],'filename':app['filename'],
                       'section':position+1,'sections':len(section_list),'started':time.time()})
                try:
                    vector = embedder.embed(section['text'])
                finally:
                    db.set('active_index',None)
                if not vector or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in vector):
                    raise ValueError('Invalid embedding')
                with db.tx() as conn:
                    current = conn.execute('SELECT version,active FROM applications WHERE id=?',(app['id'],)).fetchone()
                    if not current['active'] or current['version']!=app['version']:
                        return True
                    conn.execute('INSERT OR REPLACE INTO chunks(application_id,role_id,version,section_id,location,text,model,embedding) VALUES(?,?,?,?,?,?,?,?)',
                                 (app['id'],app['role_id'],app['version'],section['id'],section['location'],section['text'],embedding_identity,json.dumps(vector)))
                if position<len(section_list)-1:
                    return True  # One embedding per tick; new CV jobs retain priority.
            with db.tx() as conn:
                conn.execute("""UPDATE applications SET index_status='ready',index_model=?,index_error=NULL,index_attempts=0
                  WHERE id=? AND version=? AND active=1""",(embedding_identity,app['id'],app['version']))
                conn.execute('DELETE FROM chunks WHERE application_id=? AND model!=?',(app['id'],embedding_identity))
        except Exception as exc:
            attempts = app['index_attempts']+1
            db.execute("""UPDATE applications SET index_status=?,index_attempts=?,index_next=?,index_error=?
               WHERE id=? AND version=?""",('failed' if attempts>=5 else 'retry',attempts,
               time.time()+min(3600,30*2**attempts),type(exc).__name__,app['id'],app['version']))
        return True

def cosine(a,b):
    if len(a)!=len(b):
        return 0
    denom = math.sqrt(sum(v*v for v in a)*sum(v*v for v in b))
    return sum(x*y for x,y in zip(a,b))/denom if denom else 0

def _semantic_matches(config,db,role_id,query,embedding_identity,application_ids=None,k=8):
    """Nearest passages for a query, filtered by role/applicant/active/version/model.

    Uses the pgvector mirror only when it is configured AND a passing validation enabled it;
    any Postgres error falls back to the live SQLite cosine path, which stays authoritative.
    Returns passage matches only — rubric scores come from the separate ranked() path.
    """
    if pgvector.enabled(config,db):
        try:
            return pgvector.semantic_search(config,db,role_id,query,embedding_identity,application_ids,k)
        except Exception as exc:
            db.set('pg_search_error',{'type':type(exc).__name__,'at':time.time()})
    chunks = db.rows('''SELECT c.* FROM chunks c JOIN applications a ON a.id=c.application_id
      WHERE c.role_id=? AND c.version=a.version AND a.active=1 AND a.duplicate_of IS NULL
      AND c.model=?''',(role_id,embedding_identity))
    if application_ids is not None:
        allowed=set(application_ids)
        chunks=[c for c in chunks if c['application_id'] in allowed]
    return sorted(chunks,key=lambda c:cosine(query,json.loads(c['embedding'])),reverse=True)[:k]

def answer(config,db,ollama,role_id,question,application_ids=None):
    if not isinstance(question,str) or not question.strip() or len(question)>1000:
        raise ValueError('Question must contain 1–1000 characters')
    role = db.one('SELECT * FROM roles WHERE id=? AND active=1',(role_id,))
    if not role:
        raise ValueError('Unknown role')
    embedder=make_embedder(embedding_config(config, db),ollama)
    try:
        embedding_identity=embedder.identity()
    except Exception:
        embedding_identity=None
    apps = db.rows('SELECT * FROM applications WHERE role_id=? AND active=1',(role_id,))
    scope = {app['id']:app for app in apps}
    selected = application_ids or []
    if any(i not in scope for i in selected):
        raise ValueError('Application outside the selected role')
    rankings = {row['id']:row for row in ranked(db,role_id)}
    lower = question.lower()
    list_query=bool(re.search(r'\b(list|show)\b.*\b(all )?(applicants|candidates|applications)\b',lower))
    requested_status=next((status for status in ['completed','review','queued','duplicate','failed'] if re.search(r'\b'+status+r'\b',lower)),None)
    failed_ids={row['application_id'] for row in db.rows("""SELECT j.application_id FROM jobs j
      JOIN applications a ON a.id=j.application_id
      JOIN roles r ON r.id=a.role_id
      WHERE j.state='failed' AND a.version=j.version AND j.rubric_id=COALESCE(r.rubric_id,0)""")}
    rank_query = bool(re.search(r'\b(rank|ranked|score|scores|compare|top)\b',lower))
    ordinal_requested=bool(re.search(r'\b(first|second|third)\b',lower)) and rank_query
    if re.search(r'\bsecond\b',lower) and rank_query and not selected:
        selected = [i for i,row in rankings.items() if row['rank']==2]
    elif re.search(r'\bfirst\b',lower) and rank_query and not selected:
        selected = [i for i,row in rankings.items() if row['rank']==1]
    elif re.search(r'\bthird\b',lower) and rank_query and not selected:
        selected = [i for i,row in rankings.items() if row['rank']==3]
    # Match names only for explicit question selection; this never merges applicant identities.
    if not selected and 'compare' in lower:
        selected = [a['id'] for a in apps if json.loads(a['contact']).get('name') and json.loads(a['contact'])['name'].lower() in lower]
        if not selected:
            # First names are selections only when unique in this role, never identity merges.
            by_first={}
            for app in apps:
                name=json.loads(app['contact']).get('name') or ''
                if name:
                    by_first.setdefault(name.lower().split()[0],[]).append(app['id'])
            for first,ids in by_first.items():
                if len(ids)==1 and re.search(r'(?<!\w)'+re.escape(first)+r'(?!\w)',lower):
                    selected.extend(ids)
    comparison_unresolved='compare' in lower and len(selected)<2
    quoted = re.findall(r'"([^"]+)"',lower)
    role_words=set(re.findall(r'\w+',role['name'].lower()))
    terms = quoted or [t.rstrip('.') for t in re.findall(r'[\w+#.]+',lower)
                       if t.rstrip('.') not in STOP|role_words and not t.isdigit() and len(t.rstrip('.'))>1]
    terms = terms[:12]
    operator='OR' if re.search(r'\bor\b',lower) else 'AND'
    level_filter=('not_demonstrated' if 'not demonstrated' in lower else
                  'partial' if re.search(r'\b(partial|partially supported)\b',lower) else
                  'supported' if re.search(r'\bsupported\b',lower) else None)
    rubric_row=db.one('SELECT body FROM rubrics WHERE id=?',(role['rubric_id'],))
    criteria=json.loads(rubric_row['body'])['criteria'] if rubric_row else []
    criterion_terms={term:{c['id'] for c in criteria if re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',
                      c['id'].replace('_',' ')+' '+c['description'],re.I)} for term in terms}
    cards = []
    semantic = False
    if rank_query:
        apps.sort(key=lambda a:rankings.get(a['id'],{}).get('rank',10**9))
    for app in apps:
        if comparison_unresolved:
            continue
        if requested_status and (app['id'] not in failed_ids if requested_status=='failed' else app['status']!=requested_status):
            continue
        if ordinal_requested and not selected:
            continue
        if selected and app['id'] not in selected:
            continue
        if not selected and not list_query and app['duplicate_of'] is not None:
            continue
        if rank_query and 'compare' not in lower and not selected and app['id'] not in rankings:
            continue
        sections = json.loads(app['sections'] or '[]')
        findings = json.loads(rankings[app['id']]['body'])['findings'] if app['id'] in rankings else []
        hits = {term:[s for s in sections if re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',s['text'],re.I)] for term in terms}
        if terms and not selected:
            if level_filter:
                matches=[any(f['criterion_id'] in criterion_terms[term] and f['level']==level_filter for f in findings) for term in terms]
            else:
                matches=list(hits.values())
            if not (any(matches) if operator=='OR' else all(matches)):
                continue
        if rank_query or selected or list_query or requested_status or level_filter:
            citations = [{'quote':c['quote'],'section_id':c['section_id'],'location':next((s['location'] for s in sections if s['id']==c['section_id']),'' )}
                         for finding in findings for c in finding['evidence']]
            include = True
        else:
            include = bool(terms) and (any(hits.values()) if operator=='OR' else all(hits.values()))
            citations = [{'quote':s['text'],'section_id':s['id'],'location':s['location']} for s in sections if any(s in v for v in hits.values())]
        if include:
            ranking = rankings.get(app['id'])
            cards.append({'application_id':app['id'],'name':json.loads(app['contact']).get('name') or 'Name not provided',
                          'score':ranking['score'] if ranking else None,'rank':ranking['rank'] if ranking else None,
                          'tie':ranking['tie'] if ranking else False,'status':app['status'],
                          'cv_url':cv_link(app['file_id']),'citations':citations,'findings':findings})
    if not cards and not rank_query and not selected and not list_query and not requested_status and not level_filter:
        try:
            query = embedder.embed(question,purpose='query')
            # Preserve an explicit applicant scope during semantic fallback;
            # lexical misses must not widen a targeted question to the whole role.
            matches = _semantic_matches(config,db,role_id,query,embedding_identity,
                                        application_ids=selected or None)
            semantic = bool(matches)
            by_application={}
            for chunk in matches:
                # A validated Postgres mirror can lag behind a local reset or
                # source removal. Ignore stale passages instead of dropping the
                # entire answer through a KeyError.
                app = scope.get(chunk['application_id'])
                if not app:
                    continue
                if app['id'] not in by_application:
                    ranking=rankings.get(app['id'])
                    by_application[app['id']]={'application_id':app['id'],'name':json.loads(app['contact']).get('name') or 'Name not provided',
                              'score':ranking['score'] if ranking else None,'rank':ranking['rank'] if ranking else None,
                              'status':app['status'],'cv_url':cv_link(app['file_id']),'citations':[], 'findings':[]}
                by_application[app['id']]['citations'].append({'quote':chunk['text'],'section_id':chunk['section_id'],'location':chunk['location']})
            cards.extend(by_application.values())
        except Exception:
            pass
    tie_beyond_limit=False
    if re.search(r'\btop (three|3)\b',lower):
        tie_beyond_limit=len(cards)>3 and cards[2]['rank']==cards[3]['rank']
        cards=cards[:3]
    unreadable = sum(not app['sections'] for app in apps)
    unindexed = sum(app['index_status']!='ready' or app['index_model']!=embedding_identity for app in apps)
    return {'question':question,'role':role['name'],'candidates':cards,'searched_applications':len(apps),
            'unreadable_applications':unreadable,'unindexed_applications':unindexed,'terms':terms,
            'tie_beyond_limit':tie_beyond_limit,'operator':operator,'match_level_filter':level_filter,
            'mode':'semantic excerpt suggestions' if semantic else ('database scores and cited findings' if rank_query or selected or list_query or requested_status or level_filter else f'all extracted CVs: exact terms, {operator} match'),
            'notice':('Specify two unambiguous candidate names or select application IDs for comparison. ' if comparison_unresolved else '')
                     +('More applicants share the third card’s rank; see the full ranking for all ties. ' if tie_beyond_limit else '')
                     +('Semantic suggestions are partial retrieval, not an exhaustive applicant list. ' if semantic else
                      'Search covers the extracted text of every current applicant in this role; exact-term matching can miss synonyms. ')
                     +f'{unreadable} applications lack extracted text; {unindexed} lack a current semantic index. '
                     +'Quotes are CV claims, not independently verified proficiency. Scores reflect the approved rubric, not probability of job performance.'}
