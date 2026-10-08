/* Project wrap-up: HR screening application. */
'use strict';
const $ = selector => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
function node(tag, text, cls) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (cls) element.className = cls; return element; }
function clear(element) { element.replaceChildren(); }
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 7000); }
async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json(); if (!response.ok) throw Error(data.error || 'Request failed'); return data;
}
const date = value => value ? new Date(value * 1000).toLocaleString() : 'Not yet';
const number = value => Number(value || 0).toLocaleString();
function link(text, url) { const element = node('a', text); if (!/^https:\/\/drive\.google\.com\//.test(url) && !/^\/api\//.test(url)) return node('span', text); element.href = url; element.target = '_blank'; element.rel = 'noreferrer'; return element; }
function btn(text, action, cls) { const element = node('button', text, cls); element.type = 'button'; element.onclick = async () => { element.disabled = true; try { await action(); } catch (error) { toast(error.message); } finally { element.disabled = false; } }; return element; }
function stat(label, value, description) { const item = node('div', undefined, 'stat'); item.append(node('span', label), node('strong', value)); if (description) item.append(node('small', description)); return item; }
function svgElement(tag, attributes) { const element = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value); return element; }
function bar(parts, total, description) { const svg = svgElement('svg', { viewBox: '0 0 300 12', preserveAspectRatio: 'none', role: 'img', 'aria-label': description, class: 'bar-chart' }); let x = 0; svg.append(svgElement('rect', { x: 0, y: 0, width: 300, height: 12, rx: 4, class: 'bar-bg' })); for (const [value, cls] of parts) { const width = total ? value / total * 300 : 0; svg.append(svgElement('rect', { x, y: 0, width, height: 12, class: cls })); x += width; } return svg; }

function serviceOffline(status) { return !status.demo && (!status.service_heartbeat || Date.now() / 1000 - status.service_heartbeat > 650); }
