'use strict';

/* =======================================================================
   Token 消耗可视化 · 前端
   纯原生 JS / 内联 SVG 自绘图表 / 零外部依赖
   数据来源：同源 REST API，统一解包 { ok, data }
   ======================================================================= */

/* ---------- agent 配色（样式常量，非统计数字） ---------- */
const AGENT_COLORS = { claude: '#e08a5f', codex: '#3fb6a8', pi: '#7c9cff' };
const EXTRA_COLORS = ['#b48ead', '#e5b567', '#6fbf73', '#d16b8c', '#5aa9e6', '#c08fd6'];
const _extraCache = {};
let _extraIdx = 0;
function agentColor(a) {
  if (AGENT_COLORS[a]) return AGENT_COLORS[a];
  if (!_extraCache[a]) {
    _extraCache[a] = EXTRA_COLORS[_extraIdx % EXTRA_COLORS.length];
    _extraIdx++;
  }
  return _extraCache[a];
}
/* 把 #rrggbb 提亮到与白色混合的比例 t（用于渐变填充末端色） */
function lighten(hex, t) {
  const m = /^#([0-9a-f]{6})$/i.exec(String(hex || ''));
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const ch = [n >> 16, (n >> 8) & 255, n & 255].map(v => Math.round(v + (255 - v) * t));
  return '#' + ch.map(v => v.toString(16).padStart(2, '0')).join('');
}

/* ---------- 数字格式化 ---------- */
function num(v, d) { const x = Number(v); return isFinite(x) ? x : (d || 0); }
function fmt(n) { return (Number(n) || 0).toLocaleString('en-US'); }
function compact(n) {
  n = Number(n) || 0;
  const abs = Math.abs(n);
  const units = [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
  for (const [base, suf] of units) {
    if (abs >= base) {
      const x = n / base;
      const d = Math.abs(x) >= 100 ? 0 : Math.abs(x) >= 10 ? 1 : 2;
      const s = x.toFixed(d).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
      return s + suf;
    }
  }
  return String(n);
}
function money(n) {
  n = Number(n) || 0;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}
function pct(part, total) { return total > 0 ? (part / total) * 100 : 0; }
function pctLabel(pc) {
  // 返回可直接插入 SVG <text> 的字符串；<0.1% 的 < 用实体转义，避免破坏标记
  return (pc > 0 && pc < 0.1) ? '&lt;0.1%' : (pc.toFixed(1) + '%');
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}
function truncate(s, n) {
  s = String(s == null ? '' : s);
  return s.length > n ? s.slice(0, n - 1) + '\u2026' : s;
}

/* ---------- 状态 ---------- */
const state = {
  overview: null, byAgent: [], timeseries: [], byModel: [], sessions: [], meta: null,
  days: 30, daysLabel: '近 30 天'
};
const HOST_IDS = ['agent-bars', 'donut', 'timeseries', 'models', 'sessions'];

/* ---------- DOM 小工具 ---------- */
function measure(id) {
  const el = document.getElementById(id);
  const w = el ? Math.round(el.clientWidth) : 0;
  return Math.max(300, w || 720);
}
function paint(hostId, svg, emptyMsg) {
  const el = document.getElementById(hostId);
  if (!el) return;
  el.innerHTML = svg || ('<div class="empty">' + esc(emptyMsg || 'No results.') + '</div>');
}
const SKELETON = '<div class="skeleton" aria-hidden="true">' +
  '<div class="skeleton-block"></div><div class="skeleton-block"></div><div class="skeleton-block"></div>' +
  '</div>';
function setHostsLoading() {
  HOST_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = SKELETON;
  });
  const tsub = document.getElementById('ts-sub');
  if (tsub) tsub.textContent = '';
  const cnt = document.getElementById('sessions-count');
  if (cnt) cnt.textContent = '';
}

/* ---------- 状态条 ---------- */
function setStatus(type, msg) {
  const el = document.getElementById('status');
  if (!el) return;
  el.className = 'status status-' + type;
  const icon = type === 'loading' ? '<span class="spinner"></span>'
    : type === 'error' ? '<span class="dot dot-err"></span>'
    : type === 'ok' ? '<span class="dot dot-ok"></span>'
    : '';
  el.innerHTML = icon + '<span>' + esc(msg || '') + '</span>';
}
function setLastUpdated() {
  const el = document.getElementById('last-updated');
  if (el) el.textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
}
function lastImportText(li) {
  if (li == null) return '未知';
  if (typeof li === 'string') return li;
  if (typeof li === 'object') return li.finished_at || li.started_at || JSON.stringify(li);
  return String(li);
}

/* ---------- 登录联动：401 统一跳转 / logout / status ---------- */
const LOGIN_PATH = '/login.html';
let _authRedirecting = false;
function toLogin() {
  if (_authRedirecting) return;
  _authRedirecting = true;
  window.location.href = LOGIN_PATH;
}
function authFail() {
  const e = new Error('unauthorized');
  e.authRedirect = true;
  return e;
}
/* 任何 API 响应 401（或静态 302 被 fetch 跟到非 API 页）→ 登录页 */
function guard(res) {
  if (res.status === 401 || (res.redirected && String(res.url || '').indexOf('/api/') === -1)) {
    toLogin();
    throw authFail();
  }
}
/* 可选：显示当前登录用户；任何失败静默降级，不阻塞主流程 */
async function loadAuthStatus() {
  try {
    const res = await fetch('/api/auth/status', { cache: 'no-store' });
    guard(res);
    if (!res.ok) return;
    const j = await res.json();
    const d = (j && j.ok === true && j.data) ? j.data : {};
    const el = document.getElementById('auth-user');
    if (!el) return;
    const name = d.username ? String(d.username) : '';
    el.textContent = name || '本机';
    if (name) el.title = '当前登录用户：' + name;
  } catch (e) { /* 静默降级 */ }
}

/* ---------- 网络：统一解包 { ok, data } ---------- */
async function getJSON(path) {
  const res = await fetch(path, { cache: 'no-store' });
  guard(res);
  if (!res.ok) throw new Error('HTTP ' + res.status + ' · ' + path);
  let j;
  try { j = await res.json(); } catch (e) { throw new Error('响应不是合法 JSON · ' + path); }
  if (!j || j.ok !== true) throw new Error((j && j.error) ? String(j.error) : ('接口返回异常 · ' + path));
  return j.data;
}

async function load(note) {
  setStatus('loading', '正在读取本机数据\u2026');
  const q = 'days=' + encodeURIComponent(state.days);
  const metaPromise = getJSON('/api/meta').catch(() => ({}));
  try {
    const [overview, byAgent, timeseries, byModel, sessions, meta] = await Promise.all([
      getJSON('/api/overview?' + q),
      getJSON('/api/by-agent?' + q),
      getJSON('/api/timeseries?' + q),
      getJSON('/api/by-model?' + q),
      getJSON('/api/sessions?' + q + '&limit=50'),
      metaPromise
    ]);
    state.overview = overview || {};
    state.byAgent = Array.isArray(byAgent) ? byAgent : [];
    state.timeseries = Array.isArray(timeseries) ? timeseries : [];
    state.byModel = Array.isArray(byModel) ? byModel : [];
    state.sessions = Array.isArray(sessions) ? sessions : [];
    state.meta = meta || {};
    renderAll();
    // 首次加载完成触发一次克制的淡入（刷新/切范围不重放）
    const grid = document.querySelector('.grid');
    if (grid && !grid.classList.contains('ready')) grid.classList.add('ready');
    let msg = '数据已同步 · ' + fmt(num(state.overview.event_count)) + ' 条事件 · 最近导入 ' + lastImportText(state.meta.last_import);
    if (note) msg = note + ' · ' + msg;
    setStatus('ok', msg);
    setLastUpdated();
  } catch (err) {
    if (err && err.authRedirect) return; // 正在跳转登录页，不覆盖为错误态
    renderError(err);
  }
}
function renderError(err) {
  const msg = (err && err.message) ? err.message : String(err);
  setStatus('error', '数据加载失败：' + msg + '（请确认 server.py 已启动，然后点击「刷新数据」重试）');
  HOST_IDS.forEach(id => paint(id, null, 'No results. · 数据加载失败，请刷新重试'));
  const kp = document.getElementById('kpis');
  if (kp) kp.innerHTML = '<div class="empty span-full">No results. · 无法加载 KPI 数据</div>';
  const tsub = document.getElementById('ts-sub');
  if (tsub) tsub.textContent = '';
  const cnt = document.getElementById('sessions-count');
  if (cnt) cnt.textContent = '';
  // 阻止 resize 用上一次的旧数据重绘覆盖错误态；下次 load 成功会重新填充
  state.overview = null;
}

/* ---------- 总渲染 ---------- */
function renderAll() {
  renderKPIs();
  renderAgentBars();
  renderDonut();
  renderTimeseries();
  renderModels();
  renderSessions();
}

/* ---------- KPI 卡片 ---------- */
function renderKPIs() {
  const ov = state.overview;
  const coverage = state.timeseries.length;
  const cards = [
    { label: '总 tokens', value: compact(ov.total_tokens), full: fmt(ov.total_tokens), sub: '各次调用上报总量之和', accent: '#7c9cff', icon: 'Σ', raw: num(ov.total_tokens), primary: true },
    { label: '输入 tokens', value: compact(ov.input_tokens), full: fmt(ov.input_tokens), sub: 'input_tokens 累计', accent: '#3fb6a8', icon: '↓', raw: num(ov.input_tokens) },
    { label: '输出 tokens', value: compact(ov.output_tokens), full: fmt(ov.output_tokens), sub: 'output_tokens 累计', accent: '#e08a5f', icon: '↑', raw: num(ov.output_tokens) },
    { label: '覆盖天数', value: fmt(coverage), full: '', sub: '有消耗记录的天数', accent: '#e5b567', icon: '▦', raw: coverage },
    { label: '活跃 Agent', value: fmt(ov.agent_count), full: '', sub: '产生消耗的来源数', accent: '#b48ead', icon: '◇', raw: num(ov.agent_count) },
    { label: '事件数', value: compact(ov.event_count), full: fmt(ov.event_count), sub: 'token 记录条数', accent: '#6fbf73', icon: '#', raw: num(ov.event_count) },
    { label: '成本 (USD)', value: money(ov.total_cost_usd), full: '', sub: '仅含提供定价的来源', accent: '#d16b8c', icon: '$', raw: num(ov.total_cost_usd) }
  ];
  // 微型趋势条：按「该指标 / 选中范围总 tokens」或自身比例映射，纯视觉，不是统计口径
  const denom = Math.max(1, num(ov.total_tokens));
  const html = cards.map(c => {
    let fill;
    if (c.label === '总 tokens') fill = 100;
    else if (c.label === '输入 tokens' || c.label === '输出 tokens') fill = Math.min(100, (c.raw / denom) * 100);
    else if (c.label === '覆盖天数') fill = Math.min(100, (c.raw / Math.max(1, state.days || 30)) * 100);
    else if (c.label === '活跃 Agent') fill = Math.min(100, (c.raw / 3) * 100);
    else if (c.label === '事件数') fill = Math.min(100, (c.raw / Math.max(1, denom / 1000)) * 100);
    else fill = c.raw > 0 ? 100 : 0;
    fill = Math.max(0, Math.min(100, Number(fill) || 0)).toFixed(1);
    return '<div class="kpi-card' + (c.primary ? ' kpi-primary' : '') + '" style="--accent:' + c.accent + '">' +
      '<div class="kpi-top">' +
        '<div class="kpi-label">' + esc(c.label) + '</div>' +
        '<span class="kpi-icon" aria-hidden="true">' + esc(c.icon) + '</span>' +
      '</div>' +
      '<div class="kpi-value" title="' + esc(c.full || c.value) + '">' + esc(c.value) + '</div>' +
      '<div class="kpi-sub">' + esc(c.sub) + '</div>' +
      '<span class="kpi-spark" style="--fill:' + fill + '%" aria-hidden="true"></span>' +
    '</div>';
  }).join('');
  const kp = document.getElementById('kpis');
  if (kp) kp.innerHTML = html;
}

/* ---------- 通用横向条形图（内联 SVG），主视图与模型分布共用 ---------- */
function hbarSVG(rows, W, opts) {
  opts = opts || {};
  const rowH = opts.rowH || 52;
  const padL = 16, padR = 16, padB = 12;
  const labelW = opts.labelW || 150;
  const rightW = opts.rightW || 190;
  const topH = opts.headers ? 34 : 12;
  const rightStart = W - padR - rightW;
  const barX = padL + labelW;
  const barMaxW = Math.max(40, rightStart - barX - 20);
  const grand = (typeof opts.total === 'number' && opts.total > 0)
    ? opts.total : (rows.reduce((s, r) => s + r.value, 0) || 1);
  let maxVal = 0;
  rows.forEach(r => { if (r.value > maxVal) maxVal = r.value; });
  if (maxVal <= 0) maxVal = 1;
  const H = topH + rows.length * rowH + padB;
  const p = [];
  p.push('<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMinYMin meet" role="img">');
  if (opts.headers) {
    p.push('<text x="' + padL + '" y="20" class="hb-head">' + esc(opts.headers.label) + '</text>');
    p.push('<text x="' + barX + '" y="20" class="hb-head">' + esc(opts.headers.bar) + '</text>');
    p.push('<text x="' + rightStart + '" y="20" class="hb-head">' + esc(opts.headers.right) + '</text>');
  }
  rows.forEach((r, i) => {
    const cy = topH + i * rowH + rowH / 2;
    const bh = Math.min(28, rowH - 18);
    const by = cy - bh / 2;
    const fillW = Math.max(r.value > 0 ? 3 : 0, (r.value / maxVal) * barMaxW);
    const pc = pct(r.value, grand);
    p.push('<rect x="' + padL + '" y="' + (cy - 7) + '" width="12" height="12" rx="3" fill="' + r.color + '"/>');
    p.push('<text x="' + (padL + 20) + '" y="' + (cy + (r.meta ? -2 : 5)) + '" class="hb-label">' + esc(truncate(r.label, opts.labelChars || 18)) + '</text>');
    if (r.meta) p.push('<text x="' + (padL + 20) + '" y="' + (cy + 14) + '" class="hb-meta">' + esc(r.meta) + '</text>');
    p.push('<rect x="' + barX + '" y="' + by + '" width="' + barMaxW + '" height="' + bh + '" rx="7" class="hb-track"/>');
    p.push('<rect x="' + barX + '" y="' + by + '" width="' + fillW.toFixed(1) + '" height="' + bh + '" rx="2" class="hb-bar" fill="' + r.color + '"/>');
    p.push('<text x="' + rightStart + '" y="' + (cy - 2) + '" class="hb-value">' + fmt(r.value) + '</text>');
    if (opts.showPercent) {
      p.push('<text x="' + rightStart + '" y="' + (cy + 15) + '" class="hb-pct">' + pctLabel(pc) + ' · ' + compact(r.value) + '</text>');
    } else if (opts.rightMeta) {
      p.push('<text x="' + rightStart + '" y="' + (cy + 15) + '" class="hb-pct">' + esc(opts.rightMeta(r)) + '</text>');
    }
  });
  p.push('</svg>');
  return p.join('');
}

/* ---------- 主视图：按 agent 横向条形图 ---------- */
function renderAgentBars() {
  const rows = state.byAgent.map(d => ({
    label: d.agent, value: num(d.total_tokens), color: agentColor(d.agent), meta: null
  }));
  if (!rows.length) { paint('agent-bars', null, 'No results. · 所选范围内无 Agent 消耗数据'); return; }
  const W = measure('agent-bars');
  const svg = hbarSVG(rows, W, {
    rowH: 58, labelW: 150, rightW: 200, labelChars: 16, showPercent: true,
    headers: { label: 'AGENT', bar: 'TOKENS 消耗分布', right: '数值 / 占比' }
  });
  paint('agent-bars', svg);
}

/* ---------- 占比环形图（donut） ---------- */
function polar(cx, cy, rad, a) { return [cx + rad * Math.cos(a), cy + rad * Math.sin(a)]; }
function donutSVG(segments, W) {
  const total = segments.reduce((s, d) => s + d.value, 0) || 1;
  const H = 250;
  const cx = Math.min(140, W * 0.30);
  const cy = H / 2;
  const R = Math.min(92, W * 0.24);
  const r = R * 0.62;
  const mid = (R + r) / 2;
  const band = R - r;
  const legendX = cx + R + 30;
  const p = [];
  p.push('<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMinYMin meet" role="img">');
  p.push('<circle class="donut-base" cx="' + cx + '" cy="' + cy + '" r="' + mid + '" fill="none" stroke-width="' + band + '"/>');
  let ang = -Math.PI / 2;
  const gap = segments.length > 1 ? 0.016 : 0; // 扇区间隙（弧度）
  segments.forEach((seg, si) => {
    const frac = seg.value / total;
    const sweep = frac * Math.PI * 2;
    if (sweep >= Math.PI * 2 - 1e-6) {
      p.push('<circle class="seg-path" cx="' + cx + '" cy="' + cy + '" r="' + mid + '" fill="none" stroke="' + seg.color + '" stroke-width="' + band + '"/>');
    } else if (sweep > 1e-6) {
      const a0 = ang + gap / 2;
      const a1 = ang + sweep - gap / 2;
      const large = (a1 - a0) > Math.PI ? 1 : 0;
      const [ox0, oy0] = polar(cx, cy, R, a0);
      const [ox1, oy1] = polar(cx, cy, R, a1);
      const [ix1, iy1] = polar(cx, cy, r, a1);
      const [ix0, iy0] = polar(cx, cy, r, a0);
      const d = 'M ' + ox0 + ' ' + oy0 +
        ' A ' + R + ' ' + R + ' 0 ' + large + ' 1 ' + ox1 + ' ' + oy1 +
        ' L ' + ix1 + ' ' + iy1 +
        ' A ' + r + ' ' + r + ' 0 ' + large + ' 0 ' + ix0 + ' ' + iy0 + ' Z';
      p.push('<path class="seg-path" data-seg="' + si + '" d="' + d + '" fill="' + seg.color + '"/>');
    }
    ang += sweep;
  });
  p.push('<text x="' + cx + '" y="' + (cy - 2) + '" text-anchor="middle" class="donut-big">' + compact(total) + '</text>');
  p.push('<text x="' + cx + '" y="' + (cy + 18) + '" text-anchor="middle" class="donut-sub">总 tokens</text>');
  const n = segments.length;
  const rowH = Math.min(38, (H - 36) / Math.max(1, n));
  let ly = (H - n * rowH) / 2 + rowH / 2 + 2;
  segments.forEach(seg => {
    const pc = pct(seg.value, total);
    p.push('<rect x="' + legendX + '" y="' + (ly - 6) + '" width="12" height="12" rx="3" fill="' + seg.color + '"/>');
    p.push('<text x="' + (legendX + 20) + '" y="' + (ly + 4) + '" class="donut-legend">' + esc(seg.label) + '</text>');
    p.push('<text x="' + (W - 14) + '" y="' + (ly + 4) + '" text-anchor="end" class="donut-legend-val">' + pctLabel(pc) + ' · ' + compact(seg.value) + '</text>');
    ly += rowH;
  });
  p.push('</svg>');
  return p.join('');
}
function renderDonut() {
  const segs = state.byAgent.map(d => ({ label: d.agent, value: num(d.total_tokens), color: agentColor(d.agent) }))
    .filter(s => s.value > 0);
  if (!segs.length) { paint('donut', null, 'No results. · 所选范围内无占比数据'); return; }
  const W = measure('donut');
  paint('donut', donutSVG(segs, W));
}

/* ---------- 每日堆叠面积图 ---------- */
function orderedAgents() {
  if (state.byAgent && state.byAgent.length) return state.byAgent.map(d => d.agent);
  const totals = {};
  state.timeseries.forEach(row => {
    const ba = row.by_agent || {};
    Object.keys(ba).forEach(a => { totals[a] = (totals[a] || 0) + num(ba[a]); });
  });
  return Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
}
function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const norm = v / mag;
  const steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  let s = 10;
  for (const st of steps) { if (norm <= st) { s = st; break; } }
  return s * mag;
}
function timeseriesSVG(rows, agents, W) {
  const H = 280;
  const padL = 56, padR = 16, padT = 36, padB = 30;
  const plotW = Math.max(60, W - padL - padR);
  const plotH = H - padT - padB;
  const N = rows.length;
  const stacks = rows.map(row => {
    const ba = row.by_agent || {};
    let cum = 0;
    const lower = [], upper = [];
    agents.forEach(a => { lower.push(cum); cum += num(ba[a]); upper.push(cum); });
    return { lower: lower, upper: upper, top: cum };
  });
  let maxTop = 0;
  stacks.forEach(s => { if (s.top > maxTop) maxTop = s.top; });
  const maxY = niceMax(maxTop * 1.05);
  const x = i => N > 1 ? padL + (i * plotW / (N - 1)) : padL + plotW / 2;
  const y = v => padT + plotH * (1 - v / maxY);
  const p = [];
  p.push('<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMinYMin meet" role="img">');
  let lx = padL;
  agents.forEach(a => {
    const c = agentColor(a);
    p.push('<rect x="' + lx + '" y="10" width="11" height="11" rx="3" fill="' + c + '"/>');
    p.push('<text x="' + (lx + 16) + '" y="20" class="ts-legend">' + esc(a) + '</text>');
    lx += 26 + String(a).length * 8;
  });
  for (let k = 0; k <= 4; k++) {
    const val = maxY * k / 4;
    const yy = y(val);
    p.push('<line x1="' + padL + '" y1="' + yy + '" x2="' + (W - padR) + '" y2="' + yy + '" class="grid-line"/>');
    p.push('<text x="' + (padL - 8) + '" y="' + (yy + 4) + '" text-anchor="end" class="axis">' + compact(val) + '</text>');
  }
  if (N === 1) {
    const bw = 60;
    const bx = x(0) - bw / 2;
    agents.forEach((a, j) => {
      const yTop = y(stacks[0].upper[j]);
      const yBot = y(stacks[0].lower[j]);
      const h = Math.max(0, yBot - yTop);
      if (h > 0) p.push('<rect x="' + bx + '" y="' + yTop + '" width="' + bw + '" height="' + h + '" rx="2" fill="' + agentColor(a) + '" fill-opacity="0.25"/>');
    });
  } else {
    agents.forEach((a, j) => {
      const up = [], lo = [], topLine = [];
      for (let i = 0; i < N; i++) {
        up.push([x(i), y(stacks[i].upper[j])]);
        lo.push([x(i), y(stacks[i].lower[j])]);
        topLine.push(x(i) + ',' + y(stacks[i].upper[j]));
      }
      const poly = [];
      up.forEach(pt => poly.push(pt[0] + ',' + pt[1]));
      for (let i = N - 1; i >= 0; i--) poly.push(lo[i][0] + ',' + lo[i][1]);
      p.push('<polygon points="' + poly.join(' ') + '" fill="' + agentColor(a) + '" fill-opacity="0.22"/>');
      p.push('<polyline points="' + topLine.join(' ') + '" fill="none" stroke="' + agentColor(a) + '" stroke-width="2" stroke-linejoin="round" opacity="0.95"/>');
    });
  }
  // 峰值标注：取堆叠总消耗最高的一天
  let peakI = 0;
  stacks.forEach((s, i) => { if (s.top > stacks[peakI].top) peakI = i; });
  if (stacks[peakI] && stacks[peakI].top > 0) {
    const px = x(peakI), py = y(stacks[peakI].top);
    p.push('<circle class="ts-peak-dot" cx="' + px + '" cy="' + py + '" r="4" fill="' + lighten(agentColor(agents[agents.length - 1]), 0.5) + '"/>');
    const anchor = peakI > N / 2 ? 'end' : 'start';
    const dx = peakI > N / 2 ? -8 : 8;
    p.push('<text x="' + (px + dx) + '" y="' + (py - 9) + '" text-anchor="' + anchor + '" class="ts-peak">峰值 ' + compact(stacks[peakI].top) + '</text>');
  }
  const step = Math.max(1, Math.ceil(N / 8));
  for (let i = 0; i < N; i++) {
    if (i % step === 0 || i === N - 1) {
      const dd = String(rows[i].day == null ? '' : rows[i].day).slice(5);
      p.push('<text x="' + x(i) + '" y="' + (H - 9) + '" text-anchor="middle" class="axis">' + esc(dd) + '</text>');
    }
  }
  p.push('</svg>');
  return p.join('');
}
function renderTimeseries() {
  const rows = state.timeseries;
  const agents = orderedAgents();
  const sub = document.getElementById('ts-sub');
  if (!rows.length || !agents.length) {
    paint('timeseries', null, 'No results. · 所选范围内无趋势数据');
    if (sub) sub.textContent = '';
    return;
  }
  const W = measure('timeseries');
  paint('timeseries', timeseriesSVG(rows, agents, W));
  if (sub) sub.textContent = state.daysLabel + ' · ' + rows.length + ' 天';
}

/* ---------- 模型分布（横向条形） ---------- */
function renderModels() {
  const rows = state.byModel.slice(0, 12).map(d => ({
    label: d.model, value: num(d.total_tokens), color: agentColor(d.agent),
    meta: d.agent, event_count: num(d.event_count)
  }));
  if (!rows.length) { paint('models', null, 'No results. · 所选范围内无模型数据'); return; }
  const W = measure('models');
  const svg = hbarSVG(rows, W, {
    rowH: 46, labelW: 230, rightW: 165, labelChars: 26,
    showPercent: false,
    rightMeta: r => fmt(r.event_count) + ' 次调用',
    headers: { label: '模型 / 来源', bar: 'TOKENS 消耗分布', right: '数值 / 调用' }
  });
  paint('models', svg);
}

/* ---------- 会话明细表 ---------- */
function agentBadge(a) {
  const c = agentColor(a);
  return '<span class="badge" style="color:' + c + ';border-color:' + c + '55;background:' + c + '1f">' + esc(a) + '</span>';
}
function renderSessions() {
  const rows = state.sessions;
  const host = document.getElementById('sessions');
  if (!host) return;
  const cnt = document.getElementById('sessions-count');
  if (cnt) cnt.textContent = rows.length + ' 行';
  if (!rows.length) { host.innerHTML = '<div class="empty">No results. · 所选范围内无会话数据</div>'; return; }
  let h = '<div class="table-scroll"><table class="tbl"><thead><tr>' +
    '<th class="c-rank">#</th><th>Session ID</th><th>Agent</th><th>Model</th><th>Day</th><th class="c-num">Tokens</th><th class="c-num">事件</th>' +
    '</tr></thead><tbody>';
  rows.forEach((r, i) => {
    h += '<tr>' +
      '<td class="c-rank">' + (i + 1) + '</td>' +
      '<td class="mono" title="' + esc(r.session_id) + '">' + esc(truncate(r.session_id, 24)) + '</td>' +
      '<td>' + agentBadge(r.agent) + '</td>' +
      '<td class="model-cell" title="' + esc(r.model) + '">' + esc(truncate(r.model, 34)) + '</td>' +
      '<td class="mono">' + esc(r.day) + '</td>' +
      '<td class="c-num strong">' + fmt(r.total_tokens) + '</td>' +
      '<td class="c-num">' + fmt(r.event_count) + '</td>' +
      '</tr>';
  });
  h += '</tbody></table></div>';
  host.innerHTML = h;
}

/* ---------- 控件 ---------- */
const DAYS_OPTIONS = [
  { v: 7, label: '近 7 天' }, { v: 30, label: '近 30 天' },
  { v: 90, label: '近 90 天' }, { v: 0, label: '全部' }
];
/* ---------- 侧栏导航：纯前端锚点 / 平滑滚动 / active 态 ---------- */
function initSidebarNav() {
  const items = Array.prototype.slice.call(document.querySelectorAll('.nav-item'));
  const crumb = document.getElementById('crumb-current');
  const shell = document.querySelector('.app-shell');
  items.forEach(item => {
    item.addEventListener('click', e => {
      const id = item.getAttribute('data-target');
      const target = id && document.getElementById(id);
      if (!target) return;
      e.preventDefault();
      const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
      target.scrollIntoView({ behavior: behavior, block: 'start' });
      items.forEach(x => x.classList.remove('active'));
      item.classList.add('active');
      if (crumb) crumb.textContent = (item.querySelector('.nav-text') || {}).textContent || '总览';
    });
  });
  const toggle = document.getElementById('rail-toggle');
  if (toggle && shell) {
    toggle.addEventListener('click', () => {
      const collapsed = shell.classList.toggle('rail');
      toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      toggle.setAttribute('title', collapsed ? '展开侧栏' : '折叠侧栏');
      toggle.setAttribute('aria-label', collapsed ? '展开侧栏' : '折叠侧栏');
      const ico = toggle.querySelector('span');
      if (ico) ico.textContent = collapsed ? '\u276F' : '\u276E';
    });
  }
}
function initControls() {
  const sel = document.getElementById('days-select');
  if (sel) {
    sel.addEventListener('change', () => {
      const v = parseInt(sel.value, 10);
      state.days = isNaN(v) ? 30 : v;
      const opt = DAYS_OPTIONS.find(o => o.v === v);
      state.daysLabel = opt ? opt.label : (state.days + ' 天');
      setHostsLoading();
      load();
    });
  }
  const btn = document.getElementById('refresh-btn');
  if (btn) btn.addEventListener('click', doRefresh);
  const lo = document.getElementById('logout-btn');
  if (lo) lo.addEventListener('click', doLogout);
}
async function doLogout() {
  const btn = document.getElementById('logout-btn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/auth/logout', { method: 'POST' });
    guard(res); // 401 视为会话已失效，直接回登录页
    if (!res.ok) throw new Error('HTTP ' + res.status);
    let j;
    try { j = await res.json(); } catch (e) { throw new Error('响应不是合法 JSON'); }
    if (!j || j.ok !== true) throw new Error((j && j.error) || '退出失败');
    toLogin();
  } catch (err) {
    if (err && err.authRedirect) return;
    setStatus('error', '退出登录失败：' + (err && err.message ? err.message : String(err)) + '，请重试');
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function doRefresh() {
  const btn = document.getElementById('refresh-btn');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  setStatus('loading', '正在重新扫描本机日志并重建索引\u2026');
  try {
    const res = await fetch('/api/refresh', { method: 'POST' });
    guard(res); // 401 与静态 302 都走登录页，避免刷新按钮漏拦截
    if (!res.ok) throw new Error('HTTP ' + res.status);
    let j;
    try { j = await res.json(); } catch (e) { throw new Error('响应不是合法 JSON'); }
    if (!j || j.ok !== true) throw new Error((j && j.error) || '刷新失败');
    const d = j.data || {};
    await load('刷新完成 · 扫描 ' + fmt(num(d.files_scanned)) + ' 文件 · 新增 ' + fmt(num(d.records_inserted)) + ' 条');
  } catch (err) {
    if (err && err.authRedirect) return; // 登录跳转中
    setStatus('error', '刷新失败：' + (err && err.message ? err.message : String(err)));
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/* ---------- 尺寸变化：仅重绘 SVG 图表（1:1 像素），KPI / 表格靠 CSS 自适应 ---------- */
let _resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    if (!state.overview) return;
    renderAgentBars();
    renderDonut();
    renderTimeseries();
    renderModels();
  }, 180);
});

/* ---------- 启动 ---------- */
document.addEventListener('DOMContentLoaded', () => {
  initSidebarNav();
  initControls();
  setHostsLoading();
  loadAuthStatus(); // 失败静默降级，不阻塞主流程
  load();
});
