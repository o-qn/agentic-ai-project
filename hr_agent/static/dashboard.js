'use strict';
let current = '', detail = null, status = null, reviewId = null, editRole = null, draftHash = null;
let refreshing = false, roleRequest = 0, scoreRange = null, criteria = [], lastRefresh = 0;
const stageNames = { pending: 'Awaiting screening', completed: 'Assessed', review: 'Needs review', duplicate: 'Duplicate CV', failed: 'Needs attention' };
const viewTitles = { overview: ['Job overview', 'Know your talent pool. Find the people worth a closer look.'], candidates: ['Candidates', 'A clear view of every applicant, with the CV evidence to back it up.'], reviews: ['Needs review', 'Give flagged applications a thoughtful second look.'], criteria: ['Job criteria', 'Define what a strong application looks like for this job.'] };
function showView(view, focus = false) {
  if (!viewTitles[view]) view = 'overview';
  document.querySelectorAll('[data-view]').forEach(element => element.hidden = element.dataset.view !== view);
  document.querySelectorAll('.nav-item[data-view-nav]').forEach(element => { const active = element.dataset.viewNav === view; element.classList.toggle('active', active); if (active) element.setAttribute('aria-current', 'page'); else element.removeAttribute('aria-current'); });
  $('#page-title').textContent = viewTitles[view][0]; $('#page-subtitle').textContent = viewTitles[view][1];
  if (focus) { history.replaceState(null, '', '#' + view); $('#main').focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: 'instant' }); }
}
document.querySelectorAll('[data-view-nav]').forEach(element => element.onclick = event => { event.preventDefault(); showView(element.dataset.viewNav, true); });
window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
showView(location.hash.slice(1));
function label(application) { return application.contact.name || 'Name not provided'; }
function stage(application) { if (['completed', 'review', 'duplicate'].includes(application.status)) return application.status; const job = detail.jobs.find(item => item.application_id === application.id); return job?.state === 'failed' ? 'failed' : 'pending'; }
function sourceURL(application) { return status.demo ? '/api/applications/' + application.id + '/source' : 'https://drive.google.com/file/d/' + encodeURIComponent(application.file_id) + '/view'; }
function avatar(name) { return node('span', name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join('').toUpperCase(), 'avatar'); }
function resetFilters() { $('#filter').value = ''; $('#status-filter').value = 'all'; $('#sort').value = 'score'; scoreRange = null; table(); }
function table() {
  if (!detail) return;
  const rankings = new Map(detail.ranked.map(application => [application.id, application]));
  const search = $('#filter').value.trim().toLowerCase(), filter = $('#status-filter').value, sort = $('#sort').value;
  let applications = detail.applications.filter(application => (label(application) + ' ' + (application.contact.email || '') + ' ' + application.filename).toLowerCase().includes(search) && (filter === 'all' || stage(application) === filter));
  if (scoreRange) applications = applications.filter(application => { const rank = rankings.get(application.id); return rank && rank.score >= scoreRange.min && rank.score < scoreRange.max; });
  applications.sort((a, b) => sort === 'name' ? label(a).localeCompare(label(b)) : sort === 'newest' ? b.created - a.created : (rankings.get(b.id)?.score ?? -1) - (rankings.get(a.id)?.score ?? -1) || a.id - b.id);
  const tbody = $('#application-rows'); clear(tbody);
  $('#candidate-count').textContent = applications.length + ' of ' + detail.applications.length;
  $('#range-filter').hidden = !scoreRange; $('#range-filter').textContent = scoreRange ? 'Showing scores ' + scoreRange.label + '. Select Clear filters to return to the full pool.' : '';
  for (const application of applications) {
    const row = node('tr'), nameCell = node('td'), identity = node('div', undefined, 'candidate-name'), text = node('div');
    text.append(node('strong', label(application)), node('small', application.contact.email || application.filename)); identity.append(avatar(label(application)), text); nameCell.append(identity);
    const rank = rankings.get(application.id), scoreCell = node('td'), statusCell = node('td'), actionCell = node('td'), actions = node('div', undefined, 'table-actions');
    scoreCell.append(node('span', rank ? rank.score + '/100' : '—', rank ? 'table-score' : 'quiet')); if (rank) scoreCell.append(node('small', ' · #' + rank.rank + (rank.tie ? ' tied' : '')));
    statusCell.append(node('span', stageNames[stage(application)], 'status-pill status-' + stage(application)));
    actions.append(btn('View profile', () => evidence(application.id))); if (stage(application) === 'review' || stage(application) === 'failed') actions.append(btn('Review', () => openReview(application)));
    actionCell.append(actions); row.append(nameCell, scoreCell, statusCell, actionCell); tbody.append(row);
  }
  if (!applications.length) { const row = node('tr'), cell = node('td', detail.applications.length ? 'No candidates match these filters. Try clearing your filters.' : 'No CVs yet. Upload a CV or sync the job’s Drive folder.'); cell.colSpan = 4; row.append(cell); tbody.append(row); }
}
function renderCharts() {
  const analytics = detail.analytics;
  clear($('#stats'));
  const stats = [['Applications', number(analytics.total), 'All CVs for this job'], ['Assessed', number(analytics.pipeline.completed), 'Screening completed'], ['Needs review', number(analytics.pipeline.review), 'Waiting for your team'], ['Average match', analytics.average_score === null ? '—' : analytics.average_score + '/100', 'Current comparable assessments']];
  for (const [name, value, description] of stats) $('#stats').append(stat(name, value, description));
  const pipeline = $('#pipeline-chart'); clear(pipeline);
  for (const key of Object.keys(stageNames)) {
    const count = analytics.pipeline[key];
    const row = btn('', () => { resetFilters(); $('#status-filter').value = key; table(); showView('candidates', true); }, 'chart-row');
    const header = node('span', undefined, 'chart-row-heading'); header.append(node('span', stageNames[key]), node('strong', number(count)));
    row.append(header, bar([[count, key]], analytics.total, stageNames[key] + ': ' + count + ' of ' + analytics.total)); row.setAttribute('aria-label', stageNames[key] + ': ' + count + '. View candidates'); pipeline.append(row);
  }
  $('#score-sample').textContent = analytics.comparable + ' assessed'; $('#criteria-sample').textContent = analytics.comparable + ' assessed';
  const chart = $('#score-chart'); clear(chart);
  if (!analytics.comparable) chart.append(node('div', 'Score insights appear when current assessments are available.', 'empty'));
  else {
    const svg = svgElement('svg', { viewBox: '0 0 300 136', role: 'img', 'aria-label': 'Candidate counts by score: ' + analytics.score_distribution.map(bucket => bucket.label + ': ' + bucket.count).join(', '), class: 'histogram' });
    const maximum = Math.max(1, ...analytics.score_distribution.map(bucket => bucket.count));
    for (const y of [31, 65, 99, 122]) svg.append(svgElement('line', { x1: 0, x2: 300, y1: y, y2: y, class: 'axis-line' }));
    analytics.score_distribution.forEach((bucket, index) => { const height = bucket.count / maximum * 94, x = 12 + index * 60; svg.append(svgElement('rect', { x, y: 122 - height, width: 36, height, rx: 5, class: 'supported' })); const text = svgElement('text', { x: x + 18, y: 114 - height, 'text-anchor': 'middle', class: 'count-text' }); text.textContent = bucket.count; svg.append(text); });
    const labels = node('div', undefined, 'histogram-labels'); for (const bucket of analytics.score_distribution) { const button = btn(bucket.label, () => { resetFilters(); scoreRange = bucket; table(); showView('candidates', true); }); button.setAttribute('aria-label', 'Scores ' + bucket.label + ': ' + bucket.count + ' candidates. View candidates'); labels.append(button); }
    chart.append(svg, labels);
  }
  const coverage = $('#criteria-chart'); clear(coverage);
  if (!analytics.comparable || !analytics.criteria.length) coverage.append(node('div', 'Requirement insights appear after criteria are approved and candidates are assessed.', 'empty'));
  else for (const criterion of analytics.criteria) {
    const row = node('div', undefined, 'criterion-row'), header = node('div', undefined, 'chart-row-heading');
    header.append(node('span', criterion.description), node('strong', Math.round(criterion.supported / analytics.comparable * 100) + '%'));
    const description = criterion.supported + ' supported · ' + criterion.partial + ' partial · ' + criterion.not_demonstrated + ' not demonstrated';
    row.append(header, bar([[criterion.supported, 'supported'], [criterion.partial, 'partial'], [criterion.not_demonstrated, 'missing']], analytics.comparable, criterion.description + ': ' + description), node('small', description)); coverage.append(row);
  }
  $('#chart-note').textContent = 'Score and requirement charts use ' + analytics.comparable + ' current comparable assessments. Review flags, duplicates and outdated assessments are excluded. Scores guide CV review; they are not hiring decisions.';
}
function renderTop() {
  clear($('#top'));
  const definitions = new Map((detail.rubric ? JSON.parse(detail.rubric.body).criteria : []).map(criterion => [criterion.id, criterion.description]));
  for (const application of detail.top) {
    const card = node('article', undefined, 'card'), top = node('div', undefined, 'card-top'), score = node('span', application.score, 'score'); score.append(node('small', ' /100')); top.append(node('span', '#' + application.rank + (application.tie ? ' · tied' : ''), 'rank'), score);
    card.append(top, avatar(label(application)), node('h3', label(application)), node('small', application.filename, 'candidate-file'));
    const findings = JSON.parse(application.body).findings, pills = node('div', undefined, 'pills');
    for (const finding of findings.filter(finding => finding.level === 'supported').slice(0, 2)) pills.append(node('span', definitions.get(finding.criterion_id) || finding.criterion_id.replaceAll('_', ' '), 'pill'));
    const gaps = findings.filter(finding => finding.level !== 'supported');
    card.append(pills, node('p', gaps.length ? gaps.length + ' requirement' + (gaps.length === 1 ? '' : 's') + ' to explore further. Check the evidence before deciding.' : 'The CV provides evidence for all approved requirements.'));
    const actions = node('div', undefined, 'card-actions'); actions.append(link('Original CV ↗', sourceURL(application)), btn('View profile →', () => evidence(application.id))); card.append(actions); $('#top').append(card);
  }
  if (!detail.top.length) $('#top').append(node('div', detail.role.paused ? 'Approve this job’s criteria to start comparing candidates.' : 'Candidate profiles will appear here once screening is complete.', 'empty'));
  if (detail.ranked.length > 3 && detail.ranked[2].score === detail.ranked[3].score) $('#top').append(node('p', 'More candidates share the third score. View all candidates to see everyone with that rank.', 'quiet'));
}
async function evidence(id) {
  const selectedRole = current, selectedDetail = detail;
  const data = await api('/api/applications/' + id); if (selectedRole !== current) return;
  const panel = $('#evidence'); clear(panel); const contact = JSON.parse(data.contact), sections = JSON.parse(data.sections || '[]');
  panel.append(node('h2', contact.name || 'Name not provided'), node('p', [contact.email, contact.phone].filter(Boolean).join(' · ')), link('Open original CV ↗', sourceURL(data)));
  const assessment = data.assessments.find(item => item.version === data.version && item.rubric_id === selectedDetail.role.rubric_id);
  const rubric = selectedDetail.rubric ? JSON.parse(selectedDetail.rubric.body) : { criteria: [] };
  if (assessment) {
    panel.append(node('p', 'Job match: ' + assessment.score + '/100 · ' + (stageNames[data.status] || 'Awaiting screening') + '. Review the supporting CV evidence.'));
    for (const finding of JSON.parse(assessment.body).findings) {
      const criterion = rubric.criteria.find(item => item.id === finding.criterion_id), card = node('article', undefined, 'answer');
      const points = (criterion?.weight || 0) * ({ supported: 1, partial: .5, not_demonstrated: 0 }[finding.level]);
      card.append(node('h3', criterion?.description || finding.criterion_id), node('p', finding.level.replaceAll('_', ' ') + ' · ' + points + ' of ' + (criterion?.weight || 0) + ' points'), node('p', finding.explanation));
      if (finding.missing_information) card.append(node('p', 'Follow up: ' + finding.missing_information));
      for (const citation of finding.evidence) card.append(node('blockquote', citation.quote), node('small', sections.find(section => section.id === citation.section_id)?.location || 'CV passage'));
      panel.append(card);
    }
  } else panel.append(node('p', data.review_reason || 'There is no current assessment yet.'));
  const source = node('details'); source.append(node('summary', 'Read the extracted CV text')); for (const section of sections) source.append(node('h4', section.location), node('pre', section.text)); panel.append(source);
  if (data.status === 'review') panel.append(btn('Review this application', () => { $('#evidence-panel').close(); openReview({ ...data, contact }); }, 'primary'));
  if (!$('#evidence-panel').open) $('#evidence-panel').showModal();
}
$('#close-evidence').onclick = () => $('#evidence-panel').close();
function openReview(application) { reviewId = application.id; showView('reviews', true); $('#review-controls').hidden = false; $('#review-title').textContent = 'Review ' + label(application); $('#review-reason').value = ''; $('#review-controls').scrollIntoView({ behavior: 'smooth' }); $('#review-actor').focus({ preventScroll: true }); }
function renderReviews() {
  const applications = detail.applications.filter(application => ['review', 'failed'].includes(stage(application)));
  $('#review-count').textContent = applications.length;
  const box = $('#review-list'); clear(box);
  for (const application of applications) { const row = node('div', undefined, 'review-item'), text = node('div'); text.append(node('strong', label(application)), node('p', application.review_reason || 'Screening could not finish. Inspect the CV and retry if appropriate.')); row.append(text, btn('Inspect & review', async () => { openReview(application); await evidence(application.id); })); box.append(row); }
  if (!applications.length) box.append(node('div', 'You’re all caught up. No applications need review for this job.', 'empty'));
  if (reviewId && !detail.applications.some(application => application.id === reviewId)) { reviewId = null; $('#review-controls').hidden = true; }
}
function readCriteria() {
  return [...$('#criteria-editor').children].map((element, index) => { const criterion = { ...criteria[index] }; element.querySelectorAll('[data-field]').forEach(input => criterion[input.dataset.field] = input.dataset.field === 'weight' ? Number(input.value) : input.value.trim()); return criterion; });
}
function weightTotal() { const total = readCriteria().reduce((sum, criterion) => sum + criterion.weight, 0); $('#weight-total').textContent = total + ' / 100 points'; $('#weight-total').classList.toggle('invalid', Math.abs(total - 100) > .00001); }
function renderEditor() {
  clear($('#criteria-editor'));
  for (const criterion of criteria) {
    const card = node('article', undefined, 'criterion-editor'), fields = node('div', undefined, 'form-grid');
    function field(key, title, type = 'text') { const wrapper = node('label', title), input = node('input'); input.type = type; input.dataset.field = key; input.value = criterion[key]; input.required = true; if (type === 'number') { input.min = '.01'; input.max = '100'; input.step = '.01'; } else { input.minLength = 5; input.maxLength = 1000; } input.oninput = weightTotal; wrapper.append(input); return wrapper; }
    fields.append(field('description', 'Requirement'), field('weight', 'Points', 'number')); card.append(fields);
    const rules = node('details'); rules.append(node('summary', 'Define the evidence for each level'), field('supported', 'Supported evidence'), field('partial', 'Partial evidence'), field('not_demonstrated', 'Not demonstrated')); card.append(rules, btn('Remove requirement', () => { criteria = readCriteria().filter(item => item.id !== criterion.id); renderEditor(); })); $('#criteria-editor').append(card);
  }
  weightTotal(); $('#add-criterion').disabled = criteria.length >= 15;
}
async function refreshRole() {
  if (!current) return; const roleId = current, requestId = ++roleRequest;
  const result = await api('/api/roles/' + encodeURIComponent(roleId)); if (roleId !== current || requestId !== roleRequest) return;
  detail = result; const role = detail.role;
  $('#breadcrumb').textContent = role.name;
  $('#report-link').href = role.report_id ? 'https://drive.google.com/file/d/' + encodeURIComponent(role.report_id) + '/view' : 'https://drive.google.com/drive/folders/' + encodeURIComponent(role.id);
  $('#report-link').textContent = role.report_id ? 'Open report ↗' : 'Job folder ↗';
  if (detail.local_report_ready && (status.demo || (status.poc_mode && role.revision > role.synced_revision))) { $('#report-link').href = '/api/roles/' + encodeURIComponent(current) + '/xlsx'; $('#report-link').textContent = 'Download report ↓'; }
  $('#csv-link').href = '/api/roles/' + encodeURIComponent(current) + '/csv';
  clear($('#folder-links')); if (!status.demo) for (const [name, id] of Object.entries(JSON.parse(role.folders))) $('#folder-links').append(link(name + ' ↗', 'https://drive.google.com/drive/folders/' + encodeURIComponent(id)));
  $('#jd').textContent = role.jd || 'Add this job’s description to its Drive folder, then sync CVs.';
  if (editRole !== current) { criteria = detail.rubric ? JSON.parse(detail.rubric.body).criteria : []; editRole = current; draftHash = detail.rubric?.jd_hash || role.jd_hash; renderEditor(); }
  renderCharts(); renderTop(); table(); renderReviews(); renderNotice(); renderProgress();
}
function renderNotice() {
  const notes = [];
  if (status.demo) notes.push('Demo workspace · Sample candidates and scores.');
  if (detail?.role.paused) notes.push('This job needs approved criteria. Open Job criteria to get screening started.');
  if (!status.drive_authorized && !status.demo) notes.push('Drive needs reconnecting. Contact the workspace owner.');
  if (serviceOffline(status)) notes.push('CV syncing is currently offline. Contact the workspace owner to resume updates.');
  if (status.scan_error) notes.push('CV sync needs attention. The workspace owner can see the details.');
  if (status.automatic_pause) notes.push('Screening is temporarily paused. Contact the workspace owner.');
  if (status.poc_mode && !status.demo) notes.push('Trial workspace · Review draft scores and criteria before making decisions.');
  $('#notice').classList.toggle('warning', !!(detail?.role.paused || (!status.drive_authorized && !status.demo) || status.scan_error || status.automatic_pause || serviceOffline(status)));
  $('#notice').textContent = notes.join(' ') || 'Workspace up to date · Your team reviews the evidence and makes the hiring decisions.';
}
async function refreshHolds() {
  const result = await api('/api/blacklist'); clear($('#blacklist-list'));
  for (const entry of result.entries) { const row = node('div', undefined, 'review-item'); row.append(node('span', entry.reason + ' · ' + (entry.active ? 'On hold' : 'Restored') + ' · expires ' + date(entry.expires))); if (entry.active) row.append(btn('Restore document', async () => { const actor = $('#hold-actor').value.trim(); if (!actor) throw Error('Enter your name before restoring a document.'); await api('/api/blacklist/' + entry.id + '/restore', { actor }); await refreshHolds(); })); $('#blacklist-list').append(row); }
  if (!result.entries.length) $('#blacklist-list').append(node('p', 'No document holds.'));
}
$('#blacklist').ontoggle = () => { if ($('#blacklist').open) refreshHolds().catch(error => toast(error.message)); };
async function refresh() {
  if (refreshing) return; refreshing = true;
  try {
    status = await api('/api/status'); lastRefresh = Date.now(); const select = $('#role');
    const options = status.roles.map(role => [role.id, role.name]);
    if (JSON.stringify([...select.options].map(option => [option.value, option.text])) !== JSON.stringify(options)) { clear(select); for (const [id, name] of options) { const option = node('option', name); option.value = id; select.append(option); } }
    if (!options.some(([id]) => id === current)) { current = options[0]?.[0] || ''; detail = null; editRole = null; reviewId = null; $('#review-controls').hidden = true; resetFilters(); }
    select.value = current; $('#no-roles').hidden = !!current; $('#job-workspace').hidden = !current;
    $('#upload').disabled = !current || !!status.demo; $('#check').disabled = !!status.demo;
    $('#automatic').checked = status.automatic || !!status.automatic_pause; $('#automatic').disabled = !!status.demo;
    $('#report-link').hidden = !current; $('#csv-link').hidden = !current;
    await refreshRole(); renderNotice();
  } catch (error) { $('#notice').textContent = 'Workspace connection interrupted. Reconnecting…'; $('#notice').classList.add('warning'); }
  finally { refreshing = false; }
}
$('#role').onchange = async () => { current = $('#role').value; detail = null; reviewId = null; $('#review-controls').hidden = true; $('#evidence-panel').close(); $('#chat-answer').replaceChildren(); resetFilters(); try { await refreshRole(); } catch (error) { toast(error.message); } };
$('#filter').oninput = table; $('#sort').onchange = table; $('#status-filter').onchange = table; $('#clear-filters').onclick = resetFilters;
$('#check').onclick = async () => { try { await api('/api/check', {}); toast(serviceOffline(status) ? 'CV sync queued. It will start when the workspace owner resumes the sync service.' : 'CV sync requested. New files will appear when synchronization finishes.'); } catch (error) { toast(error.message); } };
$('#automatic').onchange = async () => { try { await api('/api/automatic', { enabled: $('#automatic').checked }); await refresh(); } catch (error) { $('#automatic').checked = status.automatic; toast(error.message); } };
$('#upload').onclick = () => { if (!status.drive_authorized) { toast('Ask the workspace owner to reconnect Drive before uploading.'); return; } $('#cv-file').click(); };
$('#cv-file').onchange = async () => {
  const file = $('#cv-file').files[0]; if (!file) return; const roleId = current; const form = new FormData(); form.append('file', file); $('#upload').disabled = true;
  try { const response = await fetch('/api/roles/' + encodeURIComponent(roleId) + '/upload', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: form }); const data = await response.json(); if (!response.ok) throw Error(data.error || 'Upload failed'); toast(data.filename + ' uploaded. It will be screened when Screen new CVs is enabled.'); await refresh(); }
  catch (error) { toast(error.message); } finally { $('#upload').disabled = !current || !!status.demo; $('#cv-file').value = ''; }
};
$('#add-criterion').onclick = () => { criteria = readCriteria(); criteria.push({ id: 'requirement_' + Date.now(), description: '', weight: 10, supported: 'Clear evidence of relevant work or a project.', partial: 'Relevant skill mentioned without work evidence.', not_demonstrated: 'No relevant evidence in the CV.' }); renderEditor(); };
$('#draft').onclick = async () => {
  if (editRole === current && JSON.stringify(readCriteria()) !== JSON.stringify(detail.rubric ? JSON.parse(detail.rubric.body).criteria : []) && !confirm('Replace your unsaved requirements with a new draft?')) return;
  const roleId = current; $('#draft').disabled = true;
  try { const data = await api('/api/roles/' + encodeURIComponent(roleId) + '/draft', {}); if (roleId !== current) return; criteria = data.rubric.criteria; draftHash = data.jd_hash; renderEditor(); toast('Draft requirements are ready. Review the evidence rules and points before applying.'); } catch (error) { toast(error.message); } finally { $('#draft').disabled = false; }
};
$('#approval-form').onsubmit = async event => { event.preventDefault(); const actor = $('#actor').value.trim(), rubric = { criteria: readCriteria() }; if (Math.abs(rubric.criteria.reduce((sum, item) => sum + item.weight, 0) - 100) > .00001) { toast('Requirement points must total 100.'); return; } if (!actor) { toast('Enter the approver’s name.'); return; } $('#approve').disabled = true; try { await api('/api/roles/' + encodeURIComponent(current) + '/approve', { rubric, actor, jd_hash: draftHash, plan: 'reassess_all', confirm_job_relevance: $('#relevance').checked }); editRole = null; $('#relevance').checked = false; toast('Criteria applied. Current applications will be reassessed.'); await refreshRole(); } catch (error) { toast(error.message); } finally { $('#approve').disabled = false; } };
$('#review-controls').onsubmit = async event => { event.preventDefault(); const actor = $('#review-actor').value.trim(), reason = $('#review-reason').value.trim(); if (!actor || !reason || !reviewId) { toast('Enter your name and a review reason.'); return; } $('#review-save').disabled = true; try { await api('/api/review/' + reviewId, { action: $('#review-action').value, actor, reason }); reviewId = null; $('#review-controls').hidden = true; toast('Review decision saved.'); await refreshRole(); } catch (error) { toast(error.message); } finally { $('#review-save').disabled = false; } };
async function ask(summarize = false) {
  const question = $('#question').value.trim(); if (!question) { $('#question').reportValidity(); return; } const roleId = current, box = $('#chat-answer'); box.textContent = summarize ? 'Preparing a summary with CV evidence…' : 'Looking through candidates for this job…'; $('#answer').disabled = true;
  try { const result = await api('/api/roles/' + encodeURIComponent(roleId) + (summarize ? '/answer' : '/chat'), { question }); if (roleId !== current) return; clear(box);
    if (summarize) { const generated = result.generated || {}, article = node('article', undefined, 'answer'); article.append(node('h3', 'Evidence summary')); if (!generated.claims?.length) article.append(node('p', 'A summary is unavailable. You can inspect any matching CV passages below.')); for (const claim of generated.claims || []) { article.append(node('p', claim.text)); for (const citation of claim.citations) article.append(node('blockquote', citation.quote), node('small', (citation.name || 'Candidate') + (citation.location ? ' · ' + citation.location : ''))); } box.append(article); }
    const candidates = summarize ? result.retrieval?.candidates || [] : result.candidates || [];
    if (!candidates.length) box.append(node('p', 'No supporting CV evidence found. Try a specific skill or an exact phrase in quotes.'));
    for (const candidate of candidates) { const article = node('article', undefined, 'answer'); article.append(node('h3', candidate.name + (candidate.rank ? ' · #' + candidate.rank + ' · ' + candidate.score + '/100' : '')), link('Original CV ↗', status.demo ? '/api/applications/' + candidate.application_id + '/source' : candidate.cv_url)); for (const citation of candidate.citations) article.append(node('blockquote', citation.quote), node('small', citation.location)); box.append(article); }
  } catch (error) { if (roleId === current) box.textContent = error.message; } finally { $('#answer').disabled = false; }
}
$('#chat-form').onsubmit = event => { event.preventDefault(); ask(); }; $('#answer').onclick = () => ask(true);
document.querySelectorAll('[data-question]').forEach(button => button.onclick = () => { $('#question').value = button.dataset.question; ask(); });
function renderProgress() {
  if (!status || !detail) return;
  const label = $('#progress-label'), info = $('#progress-detail'), elapsed = $('#progress-time'), progress = $('#progress-bar');
  if (Date.now() - lastRefresh > 20000) { label.textContent = 'Reconnecting to the workspace'; info.textContent = 'Screening status will update when the connection returns.'; progress.removeAttribute('value'); elapsed.textContent = ''; return; }
  const active = status.active_job, localJob = active && detail.applications.some(application => application.id === active.application_id);
  if (localJob) { label.textContent = 'Screening a CV'; elapsed.textContent = Math.max(0, Math.floor(Date.now() / 1000 - active.started)) + 's'; info.textContent = active.filename || 'New application'; progress.removeAttribute('value'); }
  else { const summary = detail.analytics; elapsed.textContent = ''; progress.max = summary.total || 1; progress.value = summary.total - summary.pipeline.pending - summary.pipeline.failed; label.textContent = detail.role.paused ? 'Job criteria need approval' : !summary.total ? 'Ready for the first CV' : status.automatic_pause ? 'Screening temporarily paused' : summary.pipeline.failed ? 'Some CVs need attention' : summary.pipeline.pending ? status.automatic ? 'CVs waiting for screening' : 'Screening paused' : 'Screening up to date'; info.textContent = summary.total ? number(summary.pipeline.completed) + ' assessed · ' + number(summary.pipeline.review) + ' to review · ' + number(summary.pipeline.pending) + ' awaiting screening' : 'Upload a CV or sync the job folder to get started.'; }
}
refresh(); setInterval(refresh, 5000); setInterval(renderProgress, 1000);
