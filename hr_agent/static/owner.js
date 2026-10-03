'use strict';
let status = null, usage = null, loading = false;
function historyTable(headers, rows) {
  const wrapper = node('div', undefined, 'table-wrap'), table = node('table'), head = node('thead'), heading = node('tr'), body = node('tbody');
  for (const label of headers) heading.append(node('th', label)); head.append(heading);
  for (const values of rows) { const row = node('tr'); for (const value of values) row.append(node('td', String(value))); body.append(row); }
  if (!rows.length) { const row = node('tr'), cell = node('td', 'No recorded usage yet.'); cell.colSpan = headers.length; row.append(cell); body.append(row); }
  table.append(head, body); wrapper.append(table); return wrapper;
}
function renderOwner() {
  const assessment = usage.assessment, embedding = usage.embedding, config = usage.configuration;
  clear($('#owner-stats'));
  for (const item of [['Completed assessments', number(assessment.total), number(assessment.last_24h) + ' in the last 24 hours'], ['Embedding runs', number(embedding.events), number(embedding.sections) + ' sections recorded'], ['Characters embedded', number(embedding.chars), 'Recorded document and query text'], ['Reported embedding tokens', embedding.tokens_reported === null ? '—' : number(embedding.tokens_reported), embedding.tokens_reported === null ? 'Provider has not reported tokens' : 'Only provider-reported usage']]) $('#owner-stats').append(stat(...item));
  const local = assessment.provider === 'ollama';
  $('#assessment-provider').textContent = local ? 'Local hosted · Ollama' : 'Hosted · Agent Router';
  $('#assessment-model').textContent = assessment.model || 'No model configured';
  clear($('#assessment-status')); $('#assessment-status').append(node('span', status.model_ready ? 'Validated' : 'Validation pending', 'status-pill status-' + (status.model_ready ? 'completed' : 'review')));
  $('#assessment-help').textContent = local ? 'Assessment runs on this machine. Model changes are managed in the project configuration.' : 'Candidate text is sent to Agent Router for assessment. Model changes are managed in the project configuration.';
  clear($('#assessment-budget'));
  if (assessment.daily_limit !== null) { $('#assessment-budget').append(node('p', (assessment.remaining === null ? 'Remaining requests unavailable' : number(assessment.remaining) + ' requests remaining') + ' · ' + number(assessment.daily_limit) + ' daily limit · resets at 00:00 UTC', 'budget')); }
  $('#search-provider').textContent = config.embedding_provider === 'voyage' ? 'Voyage · hosted API' : 'Local hosted · Ollama';
  $('#search-model').textContent = config.embedding_model || 'No model configured';
  $('#embedding-provider').value = status.embedding_provider;
  $('#embedding-provider').querySelector('option[value="voyage"]').disabled = !status.embedding_voyage_configured;
  $('#embedding-help').textContent = config.embedding_fallback ? 'Voyage is selected but its configuration is incomplete. Search currently falls back to local embeddings.' : config.embedding_provider === 'voyage' ? 'CV passages and search queries are sent to Voyage. Changing provider queues completed CVs for reindexing.' : 'CV search uses local embeddings. Changing provider queues completed CVs for reindexing.';
  if (!status.embedding_voyage_configured) $('#embedding-help').append(' Configure a Voyage key and hosted model to enable Voyage.');
  clear($('#assessment-history')); $('#assessment-history').append(historyTable(['Recorded model', 'Assessments'], assessment.by_model.map(row => [row.model, number(row.count)])));
  clear($('#embedding-history')); $('#embedding-history').append(historyTable(['Recorded model', 'Sections'], embedding.by_model.map(([model, count]) => [model, number(count)])));
  $('#ledger-note').textContent = usage.embedding_history_truncated ? 'Embedding history covers the most recent 5 MB of recorded events.' : 'Embedding history covers recorded events. Cached embeddings do not create new usage events.';
  clear($('#usage-chart')); const maximum = Math.max(1, ...usage.activity.flatMap(day => [day.assessments, day.embedding_events]));
  for (const day of usage.activity) { const row = node('div', undefined, 'usage-day'), bars = node('div', undefined, 'usage-bars'); bars.append(bar([[day.assessments, 'supported']], maximum, day.date + ': ' + day.assessments + ' assessments'), bar([[day.embedding_events, 'accent-fill']], maximum, day.date + ': ' + day.embedding_events + ' embedding runs')); row.append(node('span', new Date(day.date + 'T12:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })), bars, node('small', day.assessments + ' / ' + day.embedding_events)); $('#usage-chart').append(row); }
  clear($('#owner-health'));
  for (const item of [['CV synchronization', status.demo ? 'Demo' : serviceOffline(status) ? 'Service offline' : status.drive_authorized ? 'Connected' : 'Reconnect Drive'], ['Screening', status.automatic_pause ? 'Paused by quota' : status.automatic ? 'Enabled' : 'Paused'], ['Failed jobs', number(usage.failures.jobs_failed)], ['Failed search indexes', number(usage.failures.index_failed)]]) $('#owner-health').append(stat(...item));
  clear($('#owner-errors'));
  const health = [['Last successful sync', date(status.last_scan)], ['Next scheduled sync', date(status.next_scan)], ['Last assessment', date(assessment.last)], ['Last embedding', date(embedding.last)], ['Service heartbeat', date(status.service_heartbeat)]];
  for (const [name, value] of health) $('#owner-errors').append(node('div', name + ': ' + value, 'health-line'));
  if (status.scan_error) $('#owner-errors').append(node('p', 'Sync error: ' + status.scan_error.message + ' · ' + date(status.scan_error.at), 'error'));
  for (const failure of usage.failures.recent) $('#owner-errors').append(node('p', 'Application #' + failure.application_id + ' · ' + failure.step + ': ' + failure.error + ' · ' + date(failure.error_at), 'error'));
  $('#owner-notice').textContent = status.demo ? 'Demo workspace · Usage reflects synthetic test activity.' : 'Usage updated ' + new Date().toLocaleTimeString() + '. Token usage is shown only when reported; monetary costs are not estimated.';
  if (serviceOffline(status)) $('#owner-notice').textContent = 'The CV synchronization service has stopped checking in. Resume the scanner service to discover and screen new CVs. ' + $('#owner-notice').textContent;
  $('#owner-notice').classList.toggle('warning', serviceOffline(status));
}
async function loadOwner() {
  if (loading) return; loading = true; $('#refresh-owner').disabled = true;
  try { const result = await Promise.all([api('/api/status'), api('/api/usage')]); [status, usage] = result; renderOwner(); }
  catch (error) { $('#owner-notice').textContent = 'Usage could not be refreshed: ' + error.message; $('#owner-notice').classList.add('warning'); }
  finally { loading = false; $('#refresh-owner').disabled = false; }
}
$('#refresh-owner').onclick = loadOwner;
$('#embedding-provider').onchange = async () => {
  const select = $('#embedding-provider'), provider = select.value;
  if (provider === 'voyage' && !confirm('Send CV passages and search queries to Voyage’s hosted API and queue completed CVs for reindexing?')) { select.value = status.embedding_provider; return; }
  select.disabled = true;
  try { await api('/api/embedding-provider', { provider, confirm: provider === 'voyage' }); toast('Search provider updated. Enable Screen new CVs in the HR workspace to process pending search updates.'); await loadOwner(); }
  catch (error) { select.value = status.embedding_provider; toast(error.message); }
  finally { select.disabled = false; }
};
$('#reset').onclick = async () => {
  if (!confirm('Remove completed and failed local processing records, including associated local application records? Drive files will remain unchanged.')) return;
  $('#reset').disabled = true;
  try { const result = await api('/api/reset', { confirm: true }); toast('Reset ' + result.jobs + ' job records and ' + result.applications + ' application records.'); await loadOwner(); }
  catch (error) { toast(error.message); } finally { $('#reset').disabled = false; }
};
loadOwner(); setInterval(loadOwner, 15000);
