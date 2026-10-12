'use strict';
const $ = s => document.querySelector(s);
let state = { bots: [], rules: [], chats: [], logs: [], inviteLink: '' };

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
async function call(method, url, body) {
  const res = await fetch(url, { method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.error || `Error ${res.status}`); throw new Error(data.error); }
  return data;
}
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  kids.forEach(k => e.append(k));
  return e;
};
const btn = (label, cls, fn) => el('button', { textContent: label, className: `sm ${cls || ''}`, onclick: fn });

function optionsFor(sel, items, keep) {
  const first = sel.options[0];
  sel.replaceChildren(first, ...items.map(([v, l]) => new Option(l, v)));
  sel.value = keep && items.some(i => i[0] === keep) ? keep : '';
}

function renderAccount() {
  const a = state.account || {};
  const msg = { idle: 'Not logged in', connecting: 'Connecting…', awaiting_code: 'Enter the code Telegram sent to your app/SMS', awaiting_password: 'Enter your 2-step verification password', ready: `Logged in as ${a.user}`, error: 'Error' }[a.state] || a.state;
  $('#accStatus').textContent = msg + (a.error ? ` — ${a.error}` : '') + (a.group ? ` · Group: ${a.group.title}` : '');
  $('#credsForm').hidden = a.credsFromEnv || a.state === 'ready';
  $('#phoneForm').hidden = !a.hasCreds || ['ready', 'awaiting_code', 'awaiting_password', 'connecting'].includes(a.state);
  $('#codeForm').hidden = a.state !== 'awaiting_code';
  $('#pwForm').hidden = a.state !== 'awaiting_password';
  $('#accActions').hidden = a.state !== 'ready';
}

function render() {
  renderAccount();
  const inv = $('#invite');
  inv.href = state.inviteLink || '#';
  inv.style.display = state.inviteLink ? '' : 'none';
  $('#invForm [name=inviteLink]').placeholder = state.inviteLink || 'Group invite link';

  $('#noBots').style.display = state.bots.length ? 'none' : '';
  $('#bots').replaceChildren(...state.bots.map(b => el('tr', {},
    el('td', { textContent: b.name }),
    el('td', { textContent: b.username ? '@' + b.username : '-' }),
    el('td', { textContent: b.tokenMasked }),
    el('td', {}, el('span', { className: `pill ${b.status}`, textContent: b.status }), b.error ? el('div', { className: 'hint', textContent: b.error }) : ''),
    el('td', { textContent: b.handled }),
    el('td', {},
      b.status === 'running'
        ? btn('Stop', '', () => act('POST', `/api/bots/${b.id}/stop`))
        : btn('Start', 'ok', () => act('POST', `/api/bots/${b.id}/start`)),
      btn('Remove', 'danger', () => confirm(`Remove ${b.name}?`) && act('DELETE', `/api/bots/${b.id}`))))));

  const cs = $('#cmdForm [name=botId]'), prev = cs.value;
  cs.replaceChildren(...state.bots.map(b => new Option(`${b.name}${b.username ? ' (@' + b.username + ')' : ''}${b.status === 'running' ? '' : ' [stopped]'}`, b.id)));
  if (state.bots.some(b => b.id === prev)) cs.value = prev;
  const botOpts = state.bots.map(b => [b.id, b.name]);
  document.querySelectorAll('select[name=botId]').forEach(s => optionsFor(s, botOpts, s.value));
  optionsFor($('#send [name=chatId]'), state.chats.map(c => [String(c.id), c.title]), $('#send [name=chatId]').value);

  $('#rules').replaceChildren(...state.rules.map(r => el('li', {},
    el('span', { textContent: `“${r.trigger}” (${r.match}) → ${r.reply}` }),
    btn('✕', 'danger', () => act('DELETE', `/api/rules/${r.id}`)))));
  $('#chats').replaceChildren(...state.chats.map(c => el('li', {},
    el('span', { textContent: `${c.title} · ${c.type} · ${c.id}` }),
    btn('✕', 'danger', () => act('DELETE', `/api/chats/${c.id}`)))));
  if (!state.chats.length) $('#chats').append(el('li', { className: 'hint', textContent: 'None yet — message a bot or run /id in the group.' }));
}

async function act(method, url, body) { await call(method, url, body); await refresh(); }
async function refresh() { state = await call('GET', '/api/state'); render(); }

function appendLog(e) {
  const pre = $('#log');
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.append(`${e.t.slice(11, 19)} ${e.level.toUpperCase().padEnd(5)} ${e.bot}: ${e.message}\n`);
  if (stick) pre.scrollTop = pre.scrollHeight;
}

const formData = f => Object.fromEntries(new FormData(f));

$('#addBot').onsubmit = async e => {
  e.preventDefault();
  await call('POST', '/api/bots', formData(e.target)); e.target.reset(); toast('Bot added'); refresh();
};
$('#send').onsubmit = async e => {
  e.preventDefault();
  const { botId, chatId, text } = formData(e.target);
  if (chatId) {
    const id = botId || state.bots[0]?.id;
    if (!id) return toast('Add a bot first');
    await call('POST', `/api/bots/${id}/send`, { chatId, text });
    toast('Sent');
  } else {
    const { results } = await call('POST', '/api/broadcast', { text, botId: botId || undefined });
    toast(`Delivered ${results.filter(r => r.ok).length}/${results.length}`);
  }
  e.target.text.value = '';
};
$('#addRule').onsubmit = async e => { e.preventDefault(); await call('POST', '/api/rules', formData(e.target)); e.target.reset(); refresh(); };
$('#invForm').onsubmit = async e => { e.preventDefault(); await call('POST', '/api/config', formData(e.target)); e.target.reset(); refresh(); };

$('#cmdForm').onsubmit = async e => {
  e.preventDefault();
  const { botId, text } = formData(e.target);
  const out = $('#console');
  const line = (cls, t) => { out.append(el('div', { className: cls, textContent: t })); out.scrollTop = out.scrollHeight; };
  line('u', `> ${text}`);
  e.target.text.value = '';
  try {
    const { reply, error } = await call('POST', `/api/bots/${botId}/command`, { text });
    line(error ? 'e' : 'b', reply ?? '(no reply)');
  } catch (err) { line('e', err.message); }
};

const post = (u, b) => act('POST', u, b);
$('#credsForm').onsubmit = async e => { e.preventDefault(); await post('/api/account/creds', formData(e.target)); e.target.reset(); toast('Saved'); };
$('#phoneForm').onsubmit = async e => { e.preventDefault(); await post('/api/account/login', formData(e.target)); };
$('#codeForm').onsubmit = async e => { e.preventDefault(); await post('/api/account/code', formData(e.target)); e.target.reset(); };
$('#pwForm').onsubmit = async e => { e.preventDefault(); await post('/api/account/password', formData(e.target)); e.target.reset(); };
$('#logoutBtn').onclick = () => confirm('Log out of the Telegram account?') && post('/api/account/logout');
$('#joinBtn').onclick = async () => { await call('POST', '/api/account/join'); toast('Joined group'); refresh(); };
$('#addBotsBtn').onclick = async () => {
  const { results } = await call('POST', '/api/account/add-bots');
  toast(results.map(r => `${r.bot}: ${r.ok ? 'ok' : r.error}`).join(' | ') || 'No bots to add'); refresh();
};

(async () => {
  await refresh();
  state.logs.forEach(appendLog);
  const es = new EventSource('/api/events');
  es.onmessage = ev => { appendLog(JSON.parse(ev.data)); };
  setInterval(refresh, 5000);
})();
