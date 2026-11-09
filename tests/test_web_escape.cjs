// Offline regression test: node tests/test_web_escape.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
function part(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, `missing ${start}`);
  return html.slice(a, b);
}
const attack = `</span><img src=x onerror="alert('x')">&`;
const box = { innerHTML: '', querySelector: () => null };
const trace = {innerHTML: ''};
const ctx = {
  console, URL, URLSearchParams, Headers,
  $: id => id === 'l-range' ? {value:'today'} : trace,
  document: {querySelector: () => box},
  t: (key) => key, fmt: Number, fmtCost: () => '$1', fmtCost6: () => '$1',
  logsState: {lastData: {items: [{ts: 1, alias:attack, source:attack, model:attack, client_key:attack,
    requested_model:attack, account:attack, result:attack, error_stage:attack, status:200,
    billing_mode:attack, trace_id:attack, breakdown:null}]}},
  traceCache: new Map(), copyTraceId: () => {},
  apiFetch: async url => { ctx.fetched = url; return {json: async () => ({total_ms:1, spans:[{name:attack, error:attack, duration_ms:1}]})}; },
};
vm.createContext(ctx);
vm.runInContext(part('function escapeHtml(', 'async function loadSessions(skipIfFetching = false)') +
  part('function logRowHtml(', 'function copyTraceId(') +
  part('async function fillLogDetail(', 'async function exportLogs()'), ctx);
(async () => {
  const originalTimezone = process.env.TZ;
  process.env.TZ = 'Asia/Shanghai';
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-11-08T04:00:00+08:00'])); }
  }
  const heatmap = {innerHTML:''};
  const heatContext = {Date:FixedDate, $:() => heatmap, LANG:'en',
    cssv:() => '#000', escapeHtml:ctx.escapeHtml};
  vm.createContext(heatContext);
  vm.runInContext(part('function renderHeatmap(', 'let trendSerial = 0;'), heatContext);
  heatContext.renderHeatmap([{date:'2026-11-08', tokens:123}]);
  assert(heatmap.innerHTML.includes('title="2026-11-08  123 tokens"'));
  assert(!heatmap.innerHTML.includes('title="2026-11-07  123 tokens"'));
  heatContext.renderHeatmap([
    {date:'2026-11-08', tokens:100000},
    {date:'2026-11-07', tokens:123},
  ], '2026-11-07');
  assert(heatmap.innerHTML.includes('class="l4" title="2026-11-07  123 tokens"'));
  assert(!heatmap.innerHTML.includes('2026-11-08  100,000 tokens'));
  if (originalTimezone === undefined) delete process.env.TZ;
  else process.env.TZ = originalTimezone;
  const row = ctx.logRowHtml(ctx.logsState.lastData.items[0], 0);
  assert(!row.includes(attack));
  assert(row.includes('&lt;img'));
  await ctx.fillLogDetail(0);
  assert(!box.innerHTML.includes(attack));
  assert(!box.innerHTML.includes('onclick='));
  assert(box.innerHTML.includes('&lt;img'));
  assert(!trace.innerHTML.includes(attack));
  assert(trace.innerHTML.includes('&quot;'));
  assert.equal(ctx.fetched, '/api/traces/' + encodeURIComponent(attack));
  const sources = {escapeHtml:ctx.escapeHtml, fmtExact:n=>String(n),
    t:(key,...args)=>key+':'+args.join('|')};
  vm.createContext(sources);
  vm.runInContext(part('function modelSourcesMarkup(', 'function renderLinePicker('), sources);
  const sourceMarkup = sources.modelSourcesMarkup({grp:'same'}, [
    {model:'same', source:attack, requests:3, tokens:120},
    {model:'same', source:'second', requests:2, tokens:80},
    {model:'different', source:'ignored', requests:99, tokens:99},
  ]);
  assert(sourceMarkup.includes('model.sources:2'));
  assert(sourceMarkup.includes('model.source_counts:3|120'));
  assert(sourceMarkup.includes('model.source_counts:2|80'));
  assert(sourceMarkup.includes('data-model="same"'));
  assert(!sourceMarkup.includes('" open>'));
  const expandedMarkup = sources.modelSourcesMarkup({grp:attack}, [
    {model:attack, source:'one', requests:1, tokens:1},
    {model:attack, source:'two', requests:2, tokens:2},
  ], true);
  assert(expandedMarkup.includes('data-model="&lt;/span&gt;&lt;img'));
  assert(expandedMarkup.includes('" open>'));
  assert(!expandedMarkup.includes(attack));
  assert(!sourceMarkup.includes(attack));
  assert(!sourceMarkup.includes('ignored'));
  const singleSource = sources.modelSourcesMarkup({grp:'same'}, [{model:'same',source:'one',requests:1,tokens:42}]);
  assert(singleSource.includes('model.source_one:1'));
  assert(singleSource.includes('one'));
  assert(singleSource.includes('42'));
  assert.equal(sources.modelSourcesMarkup({grp:'same'}, undefined), '');
  const missingMarkup = sources.modelSourcesMarkup({grp:'(unknown)'}, [
    {model:null,source:'one',requests:1,tokens:10},
    {model:'(unknown)',source:'one',requests:2,tokens:20},
    {model:null,source:'two',requests:3,tokens:30},
    {model:'',source:'ignored',requests:9,tokens:90},
  ]);
  assert(missingMarkup.includes('model.source_models:3'));
  assert(missingMarkup.includes('one · model.missing: · model.source_counts_one:1|10'));
  assert(missingMarkup.includes('one · model.named:(unknown) · model.source_counts:2|20'));
  assert(missingMarkup.includes('two · model.missing: · model.source_counts:3|30'));
  assert(!missingMarkup.includes('ignored'));
  const sessionRender = part('function renderSessions()', "$('sessions-prev').onclick");
  assert(sessionRender.includes('button.textContent'));
  assert(!sessionRender.includes('innerHTML'));
  const auth = {URL, Headers, location:{href:'http://localhost/', origin:'http://localhost'},
    $: () => ({style: auth.style}), style: {}, calls: [],
    fetch: async (url, options) => { auth.calls.push({url, options}); const status = auth.status || 200; return {status, ok: status >= 200 && status < 300}; }};
  vm.createContext(auth);
  vm.runInContext(part("let statsKey = '';", 'function fmt(n)'), auth);
  await auth.apiFetch('/api/logs');
  assert.equal(auth.calls[0].options.headers.has('X-Stats-Key'), false);
  vm.runInContext("statsKey = 'private-key'", auth);
  await auth.apiFetch('/api/logs');
  assert.equal(auth.calls[1].options.headers.get('X-Stats-Key-Encoded'), 'private-key');
  vm.runInContext("statsKey = '统计 é+%'", auth);
  await auth.apiFetch('/api/logs');
  assert.equal(auth.calls[2].options.headers.get('X-Stats-Key-Encoded'), encodeURIComponent('统计 é+%'));
  assert(!auth.calls[1].url.includes('private-key'));
  auth.status = 401;
  await assert.rejects(auth.apiFetch('/api/logs'));
  assert.equal(auth.style.display, '');
  await assert.rejects(auth.apiFetch('https://example.com/api/logs'));
  for (const status of [400, 403, 404, 500]) {
    auth.status = status;
    await assert.rejects(auth.apiFetch('/api/logs'), new RegExp(String(status)));
  }

  // Filter options are replaced atomically, preserving valid selections and ignoring stale requests.
  const select = () => ({value:'', options:[{value:'', textContent:'all'}],
    replaceChildren(...options) { this.options = options; }});
  const filterNodes = {'l-source':select(), 'l-key':select(), 'export-state':{style:{display:''}}};
  const pendingFilters = [];
  const filters = {console, $: id => filterNodes[id], fmt: String, currentTab:'logs',
    logsState:{page:3}, sessionState:{page:2}, reloads:0,
    loadLogs: () => { filters.reloads++; },
    document:{createElement: () => ({})},
    apiFetch: url => new Promise((resolve, reject) => pendingFilters.push({url, resolve, reject}))};
  vm.createContext(filters);
  vm.runInContext(part('let logFiltersSerial = 0;', "$('l-range').onchange"), filters);
  const respond = (n, data) => pendingFilters[n].resolve({json:async () => data});
  let filterLoad = filters.populateLogFilters();
  respond(0, [{name:'a', alias:'A'}]); respond(1, [{grp:'k', requests:2}]);
  await filterLoad;
  filterNodes['l-source'].value = 'a'; filterNodes['l-key'].value = 'k';
  const staleFilters = filters.populateLogFilters();
  filterLoad = filters.populateLogFilters();
  respond(4, [{name:'a', alias:'A'}]); respond(5, [{grp:'k', requests:3}]);
  await filterLoad;
  respond(2, [{name:'old'}]); respond(3, [{grp:'old', requests:1}]);
  await staleFilters;
  assert.deepEqual(filterNodes['l-source'].options.map(x => x.value), ['', 'a']);
  assert.deepEqual(filterNodes['l-key'].options.map(x => x.value), ['', 'k']);
  assert.equal(filterNodes['l-source'].value, 'a');
  assert.equal(filterNodes['l-key'].value, 'k');
  assert.equal(filters.reloads, 0);
  filterLoad = filters.populateLogFilters();
  respond(6, []); respond(7, [{grp:'k', requests:3}]);
  await filterLoad;
  assert.equal(filterNodes['l-source'].value, '');
  assert.equal(filters.logsState.page, 1);
  assert.equal(filters.sessionState.page, 1);
  assert.equal(filters.reloads, 1);
  assert.equal(filterNodes['export-state'].style.display, 'none');
  filterLoad = filters.populateLogFilters();
  pendingFilters[8].reject(new Error('offline')); respond(9, []);
  await filterLoad;
  assert.deepEqual(filterNodes['l-key'].options.map(x => x.value), ['', 'k']);

  filterLoad = filters.populateLogFilters();
  respond(10, []); respond(11, [{grp:'(unknown)', is_collector:1, requests:4},
    {grp:'(unknown)', is_collector:0, requests:2}, {grp:'k', is_collector:0, requests:3}]);
  await filterLoad;
  assert.deepEqual(filterNodes['l-key'].options.map(x => x.value), ['', '(unknown)', 'k']);
  assert.equal(filterNodes['l-key'].options[1].textContent, '(unknown) · 2');
  filterNodes['l-key'].value = '(unknown)';
  filterLoad = filters.populateLogFilters();
  respond(12, []); respond(13, [{grp:'(unknown)', is_collector:0, requests:2}]);
  await filterLoad;
  assert.equal(filterNodes['l-key'].value, '(unknown)');
  filterLoad = filters.populateLogFilters();
  respond(14, []); respond(15, [{grp:'(unknown)', requests:6}, {grp:'legacy', requests:1}]);
  await filterLoad;
  assert.deepEqual(filterNodes['l-key'].options.map(x => x.value), ['', 'legacy']);
  assert.equal(filterNodes['l-key'].value, '');

  // Overview and detail start with comparable filters and share the time range.
  const overviewSelect = part('<select id="range"', '</select>');
  const detailSelect = part('<select id="l-range"', '</select>');
  const originSelect = part('<select id="l-origin"', '</select>');
  for (const markup of [overviewSelect, detailSelect]) {
    assert.match(markup, /<option value="1d" selected/);
  }
  assert.match(originSelect, /<option value="all" selected/);
  const rangeNodes = {'range':{value:'1d'}, 'l-range':{value:'1d'}, 'export-state':{style:{display:''}}};
  const ranges = {$: id => rangeNodes[id], currentTab:'logs', overviewSerial:0, trendSerial:0, htlSerial:0,
    logsState:{page:4}, sessionState:{page:3}, logLoads:0, overviewLoads:0,
    loadLogs:()=>ranges.logLoads++, load:()=>ranges.overviewLoads++, syncHealthRange:range=>ranges.healthRange=range};
  vm.createContext(ranges);
  vm.runInContext(part("$('l-range').onchange = () => {", "$('l-source').onchange") +
    part("$('range').onchange = () => {", '// 健康时间线的时间范围切换'), ranges);
  rangeNodes.range.value = '3d'; rangeNodes.range.onchange();
  assert.equal(rangeNodes['l-range'].value, '3d');
  assert.equal(ranges.healthRange, '3d');
  assert.equal(ranges.logLoads, 1); assert.equal(ranges.overviewLoads, 0);
  assert.deepEqual([ranges.overviewSerial,ranges.trendSerial,ranges.htlSerial],[1,1,1]);
  assert.equal(ranges.logsState.page, 1); assert.equal(ranges.sessionState.page, 1);
  rangeNodes['l-range'].value = '7d'; rangeNodes['l-range'].onchange();
  assert.equal(rangeNodes.range.value, '7d'); assert.equal(ranges.logLoads, 2);
  assert.equal(ranges.healthRange, '7d');
  assert.deepEqual([ranges.overviewSerial,ranges.trendSerial,ranges.htlSerial],[2,2,2]);
  ranges.currentTab = 'overview'; rangeNodes.range.value = '1d'; rangeNodes.range.onchange();
  assert.equal(rangeNodes['l-range'].value, '1d'); assert.equal(ranges.overviewLoads, 1);
  assert.equal(rangeNodes['export-state'].style.display, 'none');

  const healthButtons = ['1440','4320','10080'].map(min=>({dataset:{min},active:false,
    setAttribute(name,value){this[name]=value;},
    classList:{toggle(_name,on){this.owner.active=on;}}}));
  for (const button of healthButtons) button.classList.owner=button;
  const healthRangeCtx={document:{querySelectorAll:()=>healthButtons}, $:id=>rangeNodes[id]};
  vm.createContext(healthRangeCtx);
  vm.runInContext('let htlMinutes=1440;\n'+part('function syncHealthRange(', 'function renderHealthTimeline(')+
    part('// 健康时间线的时间范围切换', '\n\nload();'),healthRangeCtx);
  rangeNodes.range.onchange=()=>healthRangeCtx.syncHealthRange(rangeNodes.range.value);
  healthButtons[2].onclick();
  assert.equal(rangeNodes.range.value,'7d');
  assert.deepEqual(healthButtons.map(b=>b.active),[false,false,true]);
  assert.deepEqual(healthButtons.map(b=>b['aria-pressed']),['false','false','true']);
  healthRangeCtx.syncHealthRange('all');
  assert.deepEqual(healthButtons.map(b=>b.active),[false,false,true]);

  const pickerNode = {innerHTML:'', querySelectorAll:()=>[]};
  const pickerCount = {};
  const picker = {$:id=>id==='line-picker'?pickerNode:pickerCount,
    activeModels:[{source:'one',model:'a',requests:1},{source:'two',model:'b',requests:2}],
    selectedModels:['one/a'], modelKey:m=>`${m.source}/${m.model}`, modelLabel:m=>m.model,
    MODEL_COLORS:['#112233'], escapeHtml:ctx.escapeHtml, fmtExact:n=>String(n), t:(key,...args)=>key+args.join(',')};
  vm.createContext(picker);
  vm.runInContext(part('function renderLinePicker()', '// 请求健康时间线'),picker);
  picker.renderLinePicker();
  assert.match(pickerNode.innerHTML,/aria-pressed="true" data-key="one\/a"/);
  assert.match(pickerNode.innerHTML,/aria-pressed="false" data-key="two\/b"/);
  assert.match(pickerNode.innerHTML,/lp\.request_one1/);
  assert.match(pickerNode.innerHTML,/lp\.requests2/);
  assert.match(pickerNode.innerHTML,/<span class="line-label">a<\/span>/);
  const localizedPicker = {innerHTML:'', querySelectorAll:()=>[]};
  const localized = {document:{querySelectorAll:()=>[]}, localStorage:{getItem:()=>null},
    escapeHtml:ctx.escapeHtml, fmtExact:String, $:id=>id==='line-picker'?localizedPicker:{},
    activeModels:picker.activeModels, selectedModels:[], modelKey:picker.modelKey,
    modelLabel:picker.modelLabel, MODEL_COLORS:picker.MODEL_COLORS};
  vm.createContext(localized);
  vm.runInContext(part('const I18N_ZH = {};', 'function applyStaticText()') +
    part('function modelSourcesMarkup(', '// 请求健康时间线'), localized);
  for (const [language, one, many, oneDetail, manyDetail] of [
    ['zh','1 次','2 次','1 次请求 · 10 Token','2 次请求 · 20 Token'],
    ['en','1 request','2 requests','1 request · 10 tokens','2 requests · 20 tokens'],
  ]) {
    vm.runInContext(`LANG = '${language}'`, localized);
    localized.renderLinePicker();
    assert(localizedPicker.innerHTML.includes(`>${one}</span>`));
    assert(localizedPicker.innerHTML.includes(`>${many}</span>`));
    const detail = localized.modelSourcesMarkup({grp:'a'}, [
      {model:'a',source:'one',requests:1,tokens:10},
      {model:'a',source:'two',requests:2,tokens:20},
    ]);
    assert(detail.includes(oneDetail));
    assert(detail.includes(manyDetail));
  }
  assert(html.includes('.line-item .line-label{min-width:0;overflow-wrap:anywhere}'));
  assert.equal(pickerCount.textContent,'lp.selected1,2');
  picker.selectedModels=['two/b']; picker.renderLinePicker();
  assert.match(pickerNode.innerHTML,/aria-pressed="false" data-key="one\/a"/);
  assert.match(pickerNode.innerHTML,/aria-pressed="true" data-key="two\/b"/);
  const firstPickerButton={dataset:{key:'one/a'}};
  const replacementPickerButton={dataset:{key:'one/a'},focus(){this.focused=true;}};
  let pickerQueries=0;
  pickerNode.querySelectorAll=()=>++pickerQueries===1?[firstPickerButton]:[replacementPickerButton];
  picker.loadTrendCharts=()=>{};
  picker.renderLinePicker(); firstPickerButton.onclick();
  assert.equal(replacementPickerButton.focused,true);

  const rendered = {innerHTML: ''};
  const quota = {quota: [
    {source: attack, account: attack, window: attack, currency: attack, remaining_pct: null, remaining_amt: 3, level:'warn', burn_per_hour: 2, sample_count: attack, sample_span_hours: attack},
    {source: '__proto__', account: '__proto__', window: attack, remaining_pct: '1; color:red', remaining_amt: attack},
    {source: attack, account: attack, window: 'valid', remaining_pct: 25, burn_per_hour: 1},
  ], t: (key, ...args) => key + args.join(','), fmtTime: () => 'time', $: () => rendered};
  vm.createContext(quota);
  const quotaRender = part('    const groups = Object.create(null);', '\n    });\n    // 额度告警横幅');
  vm.runInContext(part('function escapeHtml(', 'async function loadSessions(skipIfFetching = false)') + part('function finiteNumber(', 'function fmt(n)') + quotaRender, quota);
  assert(!rendered.innerHTML.includes(attack));
  assert(rendered.innerHTML.includes('&lt;img'));
  assert(!rendered.innerHTML.includes('width:1; color:red'));
  assert(rendered.innerHTML.includes('width:25%'));
  assert(rendered.innerHTML.includes('__proto__'));
  assert(!rendered.innerHTML.includes('NaN'));
  quota.quota = null;
  vm.runInContext(`{${quotaRender}}`, quota);
  assert(rendered.innerHTML.includes('state.error'));
  quota.quota = [];
  vm.runInContext(`{${quotaRender}}`, quota);
  assert(rendered.innerHTML.includes('w.noquota'));
  const alertNodes = {alertbar:{classList:{add(){}, remove(){}, toggle(){}}},
    'alertbar-cnt':{}, 'alertbar-rows':{}, 'alertbar-state':{}};
  const alerts = {$: id => alertNodes[id], t: x => x, fmtReset: () => '',
    data: {items:[{level:'warn', source:attack, account:attack, window:attack,
      currency:attack, remaining_pct:attack, remaining_amt:4}]}};
  vm.createContext(alerts);
  vm.runInContext(part('function escapeHtml(', 'async function loadSessions(skipIfFetching = false)') + part('function finiteNumber(', 'function fmt(n)') + part('function renderAlerts(', 'let sparkReq') + 'renderAlerts(data);', alerts);
  assert(!alertNodes['alertbar-rows'].innerHTML.includes(attack));
  assert(alertNodes['alertbar-rows'].innerHTML.includes('&lt;img'));
  assert(alertNodes['alertbar-rows'].innerHTML.includes('—'));

  const pages = {URLSearchParams, AbortController, setTimeout, clearTimeout, sessionState: {page: 5, serial: 0}, requests: [],
    lParams: () => new URLSearchParams(), $: () => ({style:{}}), t: x => x,
    renderSessions: () => { pages.rendered = true; },
    apiFetch: async url => { pages.requests.push(url); return {ok:true, json: async () => ({pages: 2})}; }};
  vm.createContext(pages);
  vm.runInContext(part('async function loadSessions(skipIfFetching = false)', 'function renderSessions()'), pages);
  await pages.loadSessions();
  assert.equal(pages.sessionState.page, 2);
  assert.equal(pages.requests.length, 2);
  assert(pages.requests[1].includes('page=2'));
  assert(pages.rendered);
  pages.sessionState.page = 3;
  pages.apiFetch = async () => ({ok:true, json: async () => ({pages:0})});
  await pages.loadSessions();
  assert.equal(pages.sessionState.page, 1);
  // Exercise overview renderers with hostile API values and failed optional endpoints.
  const nodes = new Map();
  const node = id => { if (!nodes.has(id)) nodes.set(id, {innerHTML:'', textContent:'', style:{}, classList:{add(){},remove(){},toggle(){}}}); return nodes.get(id); };
  const overview = {$: node, t: key => key, fmt: Number, escapeHtml: ctx.escapeHtml, Date, Math,
    health:[{name:attack, alias:attack, last_error:attack, success_rate:attack, cooldown_remaining_sec:attack, in_cooldown:true, total_requests:attack}],
    speed:{history:[{ts:1, source:attack, model:attack, latency_ms:1, tok_per_sec:1, gen_tok_per_sec:1}]},
    traces:[{ts:1, source:attack, model:attack, status:attack, total_ms:1, spans:[{name:attack,error:attack,duration_ms:1}]}],
    routes:[{alias:attack,name:attack,models:[attack]}]};
  vm.createContext(overview);
  const overviewRender = part('    // 上游健康状态', '    // 额度：按来源分组')
    .replace(/    const (?:health|speed|traces|routes)Render = renderAuxiliary\([^\n]+\n/g, '')
    .replace(/    \}\);\n/g, '');
  vm.runInContext(`{${overviewRender}}`, overview);
  for (const id of ['tb-health','tb-speed','tb-traces','tb-routes']) {
    assert(!node(id).innerHTML.includes(attack), id);
    assert(node(id).innerHTML.includes('&lt;img'), id);
  }
  overview.traces=[{ts:1,status:500,total_ms:3,spans:[
    {name:'upstream',duration_ms:1,has_error:true},
    {name:'legacy',duration_ms:1,error:'private-error'},
    {name:'parse',duration_ms:1,has_error:false}]}];
  vm.runInContext(`{${overviewRender}}`, overview);
  assert.equal((node('tb-traces').innerHTML.match(/background:#ef4444/g)||[]).length,2);
  assert(node('tb-traces').innerHTML.includes('w.failed'));
  assert(!node('tb-traces').innerHTML.includes('private-error'));
  assert(node('tb-traces').innerHTML.includes('background:#8b5cf6'));
  overview.health = overview.speed = overview.traces = overview.routes = null;
  vm.runInContext(`{${overviewRender}}`, overview);
  for (const id of ['tb-health','tb-speed','tb-traces','tb-routes']) assert(node(id).innerHTML.includes('state.error'), id);
  overview.routes = [];
  vm.runInContext(`{${overviewRender}}`, overview);
  assert(node('tb-routes').innerHTML.includes('w.noroutes'));
  assert.equal(node('speed-avg').textContent, 'state.error');
  alerts.data = null;
  const oldAlertRows=alertNodes['alertbar-rows'].innerHTML;
  vm.runInContext('renderAlerts(data)', alerts);
  assert.equal(alertNodes['alertbar-state'].textContent, 'state.stale');
  assert.equal(alertNodes['alertbar-rows'].innerHTML,oldAlertRows);
  alerts.data = {items:[]};
  vm.runInContext('renderAlerts(data)', alerts);
  assert.equal(alertNodes['alertbar-cnt'].textContent,'');
  assert.equal(alertNodes['alertbar-state'].textContent,'');
  alerts.data = null;
  vm.runInContext('renderAlerts(data)', alerts);
  assert.equal(alertNodes['alertbar-state'].textContent,'state.error');

  // A failed refresh preserves previous data; a superseded response cannot replace it.
  const logNodes = new Map();
  const logNode = id => { if (!logNodes.has(id)) logNodes.set(id, {innerHTML:'old',textContent:'',style:{},classList:{add(){},toggle(){}}}); return logNodes.get(id); };
  let resolveLog;
  const logs = {console:{error(){}}, $:logNode, t:x=>x, lParams:()=>'', loadSessions:()=>{}, AbortController, setTimeout, clearTimeout,
    logsState:{fetching:false, serial:0, page:1, pages:4, lastData:{items:[1]}},
    apiFetch:()=>new Promise(resolve => { resolveLog=resolve; }), renderLogs:()=>{logs.rendered=true;}};
  vm.createContext(logs);
  vm.runInContext(part('async function loadLogs(isPolling = false)', 'function renderLogs(data)'), logs);
  const first = logs.loadLogs();
  assert.deepEqual(logs.logsState.lastData, {items:[1]});
  assert.equal(logNode('tb-logs').innerHTML, 'old');
  assert.equal(logNode('logs-state').textContent, 'state.updating');
  assert.equal(logNode('logs-state').style.display, '');
  logs.apiFetch = async () => {throw new Error('offline');};
  await logs.loadLogs();
  resolveLog({json:async()=>({items:[1]})});
  await first;
  await new Promise(resolve => setImmediate(resolve));
  assert(!logs.rendered);
  assert.deepEqual(logs.logsState.lastData, {items:[1]});
  assert.equal(logNode('logs-state').textContent,'state.stale');
  assert.equal(logNode('tb-logs').innerHTML, 'old');
  const pageCalls = [];
  const logPages = {console:{error(){}}, $:logNode, t:x=>x, loadSessions:()=>{}, AbortController, setTimeout, clearTimeout,
    logsState:{fetching:false, serial:0, page:4, pages:4, lastData:null}, rendered:[],
    lParams:()=>`page=${logPages.logsState.page}`,
    apiFetch:async url=>{pageCalls.push(url); return {json:async()=>({page:pageCalls.length===1?4:2, pages:2, total:55, items:[]})};},
    renderLogs:data=>logPages.rendered.push(data.page)};
  vm.createContext(logPages);
  vm.runInContext(part('async function loadLogs(isPolling = false)', 'function renderLogs(data)'), logPages);
  logNode('export-state').style.display = '';
  await logPages.loadLogs();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(pageCalls, ['/api/logs?page=4', '/api/logs?page=2']);
  assert.equal(logPages.logsState.page, 2);
  assert.deepEqual(logPages.rendered, [2]);
  assert.equal(logNode('export-state').style.display, '');

  let fireTimeout, cleared = 0, slowCalls = 0, filter = 'old';
  const slowUrls = [], slowRendered = [];
  const slow = {console:{error(){}}, $:logNode, t:x=>x, loadSessions:()=>{}, AbortController,
    setTimeout:(fn, ms)=>{assert.equal(ms,30000); fireTimeout=fn; return 7;},
    clearTimeout:id=>{assert.equal(id,7); cleared++;},
    logsState:{fetching:false, serial:0, page:1, lastData:{items:['previous']}},
    lParams:()=>filter, renderLogs:data=>slowRendered.push(data),
    apiFetch:async (url, options)=>{
      slowUrls.push(url);
      if (++slowCalls === 1) return {json:()=>new Promise((resolve,reject)=>{
        options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
      })};
      return {json:async()=>({page:1,pages:1,total:1,items:['new']})};
    }};
  vm.createContext(slow);
  vm.runInContext(part('async function loadLogs(isPolling = false)', 'function renderLogs(data)'), slow);
  const slowRequest = slow.loadLogs();
  await new Promise(resolve=>setImmediate(resolve));
  const oldTimeout = fireTimeout;
  filter = 'new'; await slow.loadLogs();
  await slowRequest;
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(slowUrls, ['/api/logs?old', '/api/logs?new']);
  assert.deepEqual(slowRendered[0].items, ['new']);
  assert.equal(slow.logsState.fetching, false);
  assert.equal(cleared, 2);
  oldTimeout();
  slowCalls = 0; slowRendered.length = 0;
  const timedOutRequest = slow.loadLogs();
  await new Promise(resolve=>setImmediate(resolve));
  fireTimeout(); await timedOutRequest;
  assert.equal(logNode('logs-state').textContent, 'state.stale');
  assert.deepEqual(slow.logsState.lastData.items, ['new']);
  assert.equal(slow.logsState.fetching, false);
  await slow.loadLogs();
  assert.deepEqual(slowRendered[0].items, ['new']);

  const overlapCalls = [], overlapRendered = [];
  const overlap = {console:{error(){}}, $:logNode, t:x=>x, loadSessions:()=>{}, AbortController, setTimeout, clearTimeout,
    logsState:{fetching:false, serial:0, page:1, lastData:{items:['previous']}},
    lParams:()=>'', renderLogs:data=>overlapRendered.push(data),
    apiFetch:(url, options)=>new Promise(resolve=>overlapCalls.push({resolve, signal:options.signal}))};
  vm.createContext(overlap);
  vm.runInContext(part('async function loadLogs(isPolling = false)', 'function renderLogs(data)'), overlap);
  const overlapOld = overlap.loadLogs();
  const overlapNew = overlap.loadLogs();
  assert.equal(overlapCalls[0].signal.aborted, true);
  overlapCalls[0].resolve({json:async()=>({items:['old'], pages:1})});
  await overlapOld;
  assert.equal(overlap.logsState.fetching, true);
  assert.equal(overlap.logsState.controller.signal, overlapCalls[1].signal);
  await overlap.loadLogs(true);
  assert.equal(overlapCalls.length, 2);
  overlapCalls[1].resolve({json:async()=>({items:['new'], pages:1})});
  await overlapNew;
  assert.deepEqual(overlapRendered.map(data=>data.items[0]), ['new']);
  assert.equal(overlap.logsState.fetching, false);
  assert.equal(logNode('logs-state').style.display, 'none');

  // Periodic refresh must not continually supersede a slow in-flight request.
  let tick, timerLoads = 0, clearedTimer;
  const autoSelect = {value:'5'};
  const auto = {$:()=>autoSelect, currentTab:'logs', document:{hidden:false},
    logsState:{timer:null, fetching:false}, loadLogs:()=>{timerLoads++;},
    setInterval:(fn,ms)=>{assert.equal(ms,5000); tick=fn; return 42;},
    clearInterval:id=>{clearedTimer=id;}};
  vm.createContext(auto);
  vm.runInContext(part("$('l-auto').onchange =", '\n\napplyStaticText();'), auto);
  autoSelect.onchange();
  auto.logsState.fetching = true; tick(); tick();
  assert.equal(timerLoads, 0);
  auto.logsState.fetching = false; tick();
  assert.equal(timerLoads, 1);
  auto.document.hidden = true; tick();
  auto.document.hidden = false; auto.currentTab = 'overview'; tick();
  assert.equal(timerLoads, 1);
  autoSelect.value = '0'; autoSelect.onchange();
  assert.equal(clearedTimer, 42);
  assert.equal(auto.logsState.timer, null);

  const summaryNodes = new Map();
  const summaryNode = id => {
    if (!summaryNodes.has(id)) summaryNodes.set(id, {innerHTML:'', textContent:'', classList:{toggle(){}}});
    return summaryNodes.get(id);
  };
  const summaryView = {$:summaryNode, t:x=>x, fmt:x=>String(x), fmtCost:x=>'$'+x,
    escapeHtml:x=>String(x), document:{querySelectorAll:()=>[]}, logsState:{expanded:new Set(),pages:1}};
  vm.createContext(summaryView);
  vm.runInContext(part('function renderLogs(data)', 'function logRowHtml('), summaryView);
  const renderSummary = summary => summaryView.renderLogs({summary, items:[], page:1, total:0});
  renderSummary({requests:1, priced_requests:1, cache_saved_usd:0, cache_saved_priced_requests:0, cache_saved_unpriced_requests:1});
  assert(summaryNode('l-summary').innerHTML.includes('l.saved_total <b style="color:var(--green)">—</b>'));
  renderSummary({requests:2, priced_requests:2, cache_saved_usd:1.2, cache_saved_priced_requests:1, cache_saved_unpriced_requests:1});
  assert(summaryNode('l-summary').innerHTML.includes('l.saved_partial <b style="color:var(--green)">$1.2</b>'));
  renderSummary({requests:0, cache_saved_usd:0, cache_saved_priced_requests:0, cache_saved_unpriced_requests:0});
  assert(summaryNode('l-summary').innerHTML.includes('l.saved_total <b style="color:var(--green)">$0</b>'));
  renderSummary({requests:1, priced_requests:1, cache_saved_usd:0});
  assert(summaryNode('l-summary').innerHTML.includes('l.saved_total <b style="color:var(--green)">—</b>'));

  const csvCalls = [];
  let selectedRange = '1d', csvBlob;
  const csvButton = {textContent:'export'};
  const csvState = {style:{}};
  const csv = {URLSearchParams, Blob, Date, console, statsKeyVersion:0, $:id=>id === 'l-export' ? csvButton : csvState, t:(x,...args)=>x === 'btn.export' ? 'export' : [x,...args].join(':'),
    URL:{createObjectURL:blob=>{csvBlob=blob;return 'blob:test';}, revokeObjectURL(){}},
    document:{createElement:()=>({click(){}})},
    lParams:()=>new URLSearchParams({range:selectedRange,page:'3',page_size:'50'}),
    apiFetch:async url=>{csvCalls.push(url); selectedRange='7d'; return {json:async()=>
      ({total:2, items:[{ts:1,source:'=1+1',alias:' a,b',model:'"quoted"\r\nline',client_key:'\t@cmd'},
        {ts:2,source:'normal',alias:'',model:'m'}]})};}};
  vm.createContext(csv);
  vm.runInContext(part('function csvCell(', 'let logFiltersSerial = 0;'), csv);
  assert.equal(csv.csvCell('  +SUM(A1)'), "'  +SUM(A1)");
  assert.equal(csv.csvCell(-12), '-12');
  await csv.exportLogs();
  assert.equal(csvCalls.length, 1);
  assert(csvCalls.every(url=>new URLSearchParams(url.split('?')[1]).get('range') === '1d'));
  assert(csvCalls[0].startsWith('/api/logs/export?'));
  assert(!new URLSearchParams(csvCalls[0].split('?')[1]).has('page'));
  const csvText = await csvBlob.text();
  assert(csvText.includes("'=1+1"));
  assert(csvText.includes('" a,b"'));
  assert(csvText.includes('"""quoted""\r\nline"'));
  assert(csvText.includes("'\t@cmd"));
  assert.equal(csvButton.textContent, 'export');
  assert.equal(csvState.style.display, 'none');
  assert.equal(csvButton.disabled, false);
  csv.apiFetch = async()=>({json:async()=>({total:2,items:[
    {ts:1,status:200,result:'stream_interrupted',error_stage:'stream',
      error_kind:'=untrusted',error_status:502,cost_usd:0.1,cost_source:'computed'},
    {ts:2,status:200,cost_usd:0,cost_source:'recorded'},
  ]})});
  await csv.exportLogs();
  const outcomeLines = (await csvBlob.text()).replace(/^\uFEFF/, '').split('\n').map(line=>line.split(','));
  const columns = outcomeLines[0];
  const outcome = Object.fromEntries(columns.map((column,i)=>[column,outcomeLines[1][i]]));
  const legacyOutcome = Object.fromEntries(columns.map((column,i)=>[column,outcomeLines[2][i]]));
  assert.equal(outcome.status, '200');
  assert.equal(outcome.result, 'stream_interrupted');
  assert.equal(outcome.error_stage, 'stream');
  assert.equal(outcome.error_kind, "'=untrusted");
  assert.equal(outcome.error_status, '502');
  assert.equal(outcome.cost_source, 'computed');
  assert.equal(legacyOutcome.result, '');
  assert.equal(legacyOutcome.error_stage, '');
  assert.equal(legacyOutcome.cost_source, 'recorded');
  let count = 0;
  csv.apiFetch = async()=>({json:async()=>({total:0,items:[]})});
  await csv.exportLogs();
  assert.equal(csvState.textContent, 'l.export_done:0');
  assert.equal(csvState.style.display, '');
  csv.apiFetch = async()=>({json:async()=>{count++; return {total:5001, items:Array.from({length:5000},()=>({ts:1}))};}});
  await csv.exportLogs();
  assert.equal(count, 1);
  assert.equal(csvState.textContent, 'l.export_limit:5000:5001');
  csv.apiFetch = async()=>({json:async()=>({total:3,items:[]})});
  await csv.exportLogs();
  assert.equal(csvState.textContent, 'l.export_error');
  csv.apiFetch = async()=>{throw new Error('offline');};
  await csv.exportLogs();
  assert.equal(csvState.textContent, 'l.export_error');
  assert.equal(csvButton.disabled, false);
  let finishExport, downloads = 0;
  csv.document.createElement = () => ({click(){downloads++;}});
  csv.apiFetch = () => new Promise(resolve => {finishExport = resolve;});
  const oldKeyExport = csv.exportLogs();
  csv.statsKeyVersion++;
  finishExport({json:async()=>({total:1,items:[{ts:1}]})});
  await oldKeyExport;
  assert.equal(downloads, 0);
  assert.equal(csvState.style.display, 'none');
  assert.equal(csvButton.disabled, false);
  let failExport;
  csv.apiFetch = () => new Promise((resolve, reject) => {failExport = reject;});
  const oldKeyFailure = csv.exportLogs();
  csv.statsKeyVersion++;
  failExport(new Error('old key failed'));
  await oldKeyFailure;
  assert.equal(csvState.style.display, 'none');
  assert.equal(csvButton.disabled, false);
  csv.apiFetch = () => new Promise(resolve => {finishExport = resolve;});
  const languageExport = csv.exportLogs();
  csv.t = x => x === 'btn.export' ? '导出 CSV' : x;
  finishExport({json:async()=>({total:0,items:[]})});
  await languageExport;
  assert.equal(csvButton.textContent, '导出 CSV');
  csvButton.disabled = true;
  csvState.textContent = 'unchanged';
  await csv.exportLogs();
  assert.equal(csvState.textContent, 'unchanged');
  const sessionNodes = new Map();
  const sessionNode = id => { if (!sessionNodes.has(id)) sessionNodes.set(id,{textContent:'',style:{},disabled:false,replaceChildren(){this.textContent='';},appendChild(){}}); return sessionNodes.get(id); };
  const sessions = {URLSearchParams, AbortController, setTimeout, clearTimeout, $:sessionNode, t:x=>x, fmt:Number, fmtCost6:()=>'', document:{createElement:()=>({})},
    sessionState:{page:1,pages:3,serial:0,data:{items:[1]}}, lParams:()=>new URLSearchParams(),
    apiFetch:async()=>{throw new Error('offline');}};
  vm.createContext(sessions);
  vm.runInContext(part('async function loadSessions(skipIfFetching = false)', "$('sessions-prev').onclick"), sessions);
  await sessions.loadSessions();
  assert.deepEqual(sessions.sessionState.data,{items:[1]});
  assert.equal(sessionNode('sessions-state').textContent,'state.stale');
  sessions.apiFetch = async()=>({ok:true,json:async()=>({items:[],page:1,pages:1,total:0})});
  await sessions.loadSessions();
  assert.equal(sessionNode('sessions-list').textContent,'state.empty');
  const sessionTimers = [], sessionCalls = [], sessionResults = [];
  const delayedSessions = {URLSearchParams, AbortController,
    setTimeout:fn=>{sessionTimers.push(fn); return sessionTimers.length;}, clearTimeout:()=>{},
    $:sessionNode, t:x=>x, lParams:()=>new URLSearchParams(),
    sessionState:{page:1,serial:0,data:{items:['previous']}},
    renderSessions:()=>sessionResults.push(delayedSessions.sessionState.data),
    apiFetch:async (_url, options)=>{
      sessionCalls.push(options.signal);
      if (sessionCalls.length !== 2) return {ok:true,json:()=>new Promise((_resolve,reject)=>{
        options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
      })};
      return {ok:true,json:async()=>({items:['new'],pages:1})};
    }};
  vm.createContext(delayedSessions);
  vm.runInContext(part('async function loadSessions(skipIfFetching = false)', 'function renderSessions()'), delayedSessions);
  const oldSessionRequest = delayedSessions.loadSessions();
  assert.equal(sessionNode('sessions-state').textContent, 'state.updating');
  assert.equal(sessionNode('sessions-state').style.display, '');
  await new Promise(resolve=>setImmediate(resolve));
  await delayedSessions.loadSessions(true);
  assert.equal(sessionCalls.length, 1);
  await delayedSessions.loadSessions();
  await oldSessionRequest;
  assert.equal(sessionCalls[0].aborted, true);
  assert.deepEqual(sessionResults[0].items, ['new']);
  assert.equal(sessionNode('sessions-state').style.display, 'none');
  const timedOutSession = delayedSessions.loadSessions();
  await new Promise(resolve=>setImmediate(resolve));
  sessionTimers[2]();
  await timedOutSession;
  assert.equal(sessionNode('sessions-state').textContent, 'state.stale');
  assert.deepEqual(delayedSessions.sessionState.data.items, ['new']);
  assert.equal(delayedSessions.sessionState.controller, null);
  const superseded = delayedSessions.loadSessions();
  await new Promise(resolve=>setImmediate(resolve));
  const latest = delayedSessions.loadSessions();
  await superseded;
  assert.equal(delayedSessions.sessionState.controller.signal, sessionCalls[4]);
  sessionTimers[4]();
  await latest;
  assert.equal(delayedSessions.sessionState.controller, null);
  // Older timeline failures and successes must not overwrite newer selections.
  const deferred = () => { let resolve, reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
  const timelinePending = [];
  const timeline = {console:{error(){}}, document:{querySelector:()=>null}, $:node, t:x=>x, rendered:[], urls:[],
    renderHealthTimeline:data=>timeline.rendered.push(data),
    apiFetch:url=>{timeline.urls.push(url); const d=deferred(); timelinePending.push(d); return d.promise;}};
  vm.createContext(timeline);
  vm.runInContext('let htlMinutes=1440, htlSerial=0, htlHasData=false, htlDisplayedMinutes=null;\n'+part('function setHealthTimelineState(', 'function makeSpark('), timeline);
  node('htl-grid').innerHTML='old'; node('htl-stats').textContent='old'; node('htl-axis').textContent='old';
  const oldTimeline=timeline.loadHealthTimeline();
  assert.equal(node('htl-grid').textContent,'state.loading');
  assert.equal(node('htl-stats').textContent,'');
  assert.equal(node('htl-axis').textContent,'');
  vm.runInContext('htlMinutes=4320',timeline);
  const newTimeline=timeline.loadHealthTimeline();
  assert(timeline.urls[1].includes('minutes=4320&bucket_sec=1200'));
  timelinePending[1].resolve({json:async()=>({id:'3d'})}); await newTimeline;
  timelinePending[0].reject(new Error('old')); await oldTimeline;
  assert.equal(timeline.rendered.length,1);
  assert.equal(timeline.rendered[0].id,'3d');
  assert.notEqual(node('htl-grid').textContent,'state.error');
  const failedTimeline=timeline.loadHealthTimeline(); timelinePending[2].reject(new Error('new')); await failedTimeline;
  assert.notEqual(node('htl-grid').textContent,'state.error');
  assert.equal(node('htl-state').textContent,'state.stale');
  const snapshotTimeline={id:'snapshot'};
  await timeline.loadHealthTimeline(snapshotTimeline);
  assert.equal(timeline.rendered.at(-1),snapshotTimeline);
  assert.equal(timelinePending.length,3);
  vm.runInContext('htlMinutes=10080',timeline);
  const rangeTimeline=timeline.loadHealthTimeline();
  assert.equal(node('htl-state').textContent,'state.updating_range');
  timelinePending[3].reject(new Error('offline')); await rangeTimeline;
  assert.equal(node('htl-state').textContent,'state.stale_range');
  const retryTimeline=timeline.loadHealthTimeline();
  timelinePending[4].resolve({json:async()=>({minutes:10080})}); await retryTimeline;
  assert.equal(node('htl-state').style.display,'none');
  vm.runInContext('htlHasData=false',timeline);
  node('htl-grid').textContent='state.loading';
  timeline.setHealthTimelineError();
  assert.equal(node('htl-grid').textContent,'state.error');
  assert.equal(node('htl-axis').textContent,'');
  assert.equal(node('htl-stats').textContent,'');
  assert.equal(node('htl-state').textContent,'state.error');

  // An empty timeline clears previously rendered statistics and tooltip.
  const timelineDesc={title:'old tooltip'};
  const emptyTimeline={$:node,t:x=>x,document:{querySelector:()=>({closest:()=>({querySelector:()=>timelineDesc})})}};
  vm.createContext(emptyTimeline);
  vm.runInContext(part('function renderHealthTimeline(', 'async function loadHealthTimeline('),emptyTimeline);
  for (const id of ['htl-stats','htl-minutes','htl-bucket']) node(id).textContent='old';
  emptyTimeline.renderHealthTimeline({buckets:[]});
  for (const id of ['htl-stats','htl-minutes','htl-bucket']) assert.equal(node(id).textContent,'');
  assert.equal(timelineDesc.title,'');
  assert(node('htl-grid').innerHTML.includes('w.nodata'));

  // Unresolved outcomes use a neutral cell and an unknown success rate.
  const outcomeTimeline={$:node, t:(key,...args)=>key+':'+args.join('|'),
    document:{querySelector:()=>({closest:()=>({querySelector:()=>timelineDesc})})}};
  vm.createContext(outcomeTimeline);
  vm.runInContext('const HTL_COLS=60;\n'+part('function escapeHtml(', 'async function loadSessions(skipIfFetching = false)')+
    part('function renderHealthTimeline(', 'async function loadHealthTimeline('), outcomeTimeline);
  outcomeTimeline.renderHealthTimeline({bucket_sec:120,total_requests:2,success_rate:null,active_buckets:1,
    buckets:[{time:'12:00',total:2,ok:0,failed:0,cancelled:1,unknown:1,tokens:0,level:4}]});
  assert(node('htl-grid').innerHTML.includes('background:#8b909a'));
  assert(node('htl-grid').innerHTML.includes('l.cancelled: 1'));
  assert(node('htl-grid').innerHTML.includes('status.unknown: 1'));
  assert(node('htl-stats').textContent.includes('—'));
  assert(node('htl-stats').title.includes('w.rate_tip'));
  outcomeTimeline.renderHealthTimeline({bucket_sec:120,minutes:10,total_requests:1,success_rate:100,active_buckets:1,
    buckets:[{time:'12:00',total:0,level:0,partial:true},
      {time:'12:02',total:1,ok:1,failed:0,tokens:3,level:3,partial:true},
      {time:'12:04',total:0,level:0,partial:false}]});
  assert(node('htl-grid').innerHTML.includes('12:00 · trend.partial:  htl.none:'));
  assert(node('htl-grid').innerHTML.includes('htl.tip:12:02 · trend.partial:|1|1|0|3'));
  assert(!node('htl-grid').innerHTML.includes('12:04 · trend.partial'));
  outcomeTimeline.renderHealthTimeline({bucket_sec:1800,minutes:180,total_requests:0,success_rate:null,active_buckets:0,
    buckets:[{time:'01:30',bucket_offset:'-0400',total:0,level:0},
      {time:'01:30',bucket_offset:'-0500',total:0,level:0}]});
  assert(node('htl-grid').innerHTML.includes('01:30 -0400'));
  assert(node('htl-grid').innerHTML.includes('01:30 -0500'));
  outcomeTimeline.renderHealthTimeline({bucket_sec:1800,minutes:1440,total_requests:0,success_rate:null,active_buckets:0,
    buckets:[{time:'01:30',bucket_offset:'-0500',total:0,level:0},
      {time:'01:30',bucket_offset:'-0500',total:0,level:0}]});
  assert(!node('htl-grid').innerHTML.includes('01:30 -0500'));
  assert(html.includes('value="unknown" data-i18n="status.unknown"'));
  for (const outcome of [{result:'unknown',status:500},{result:'client_cancelled',status:500},{result:null,status:null}]) {
    assert(ctx.logRowHtml({ts:1,...outcome},0).includes('color:var(--muted)'));
  }

  // A stalled timeline cannot delay the model or mini-chart work.
  const split=part('    // 时间线与趋势互不阻塞', '    // 迷你图（顶部卡片）');
  const splitCalls=[];
  const splitCtx={splitCalls,range:'today',serial:1,usage:{active_models:[]},loadHealthTimeline:()=>{splitCalls.push('timeline');return new Promise(()=>{});},
    loadOverviewTrends:()=>{splitCalls.push('trends');return new Promise(()=>{});}};
  vm.createContext(splitCtx);
  await vm.runInContext(`(async()=>{${split} splitCalls.push('spark');})()`,splitCtx);
  assert.deepEqual(splitCalls,['timeline','trends','spark']);

  // An old overview model request cannot fail or restart a newer trend request.
  const modelsPending=[]; const trendEvents=[];
  const cross={console:{error(){}},apiFetch:()=>{const d=deferred();modelsPending.push(d);return d.promise;},
    setTrendState:value=>trendEvents.push(value),loadTrendCharts:()=>{trendEvents.push('charts');},
    renderLinePicker:()=>trendEvents.push('picker'), modelKey:x=>x, activeModels:[], selectedModels:[]};
  vm.createContext(cross);
  vm.runInContext('let trendSerial=0;\n'+part('let overviewSerial = 0;', 'async function load(){'),cross);
  const oldModels=cross.loadOverviewTrends('today',0);
  vm.runInContext('++trendSerial',cross);
  modelsPending[0].reject(new Error('old')); await oldModels;
  assert.deepEqual(trendEvents,['state.loading']);
  const obsoleteSuccess=cross.loadOverviewTrends('today',0);
  vm.runInContext('++trendSerial',cross);
  modelsPending[1].resolve({json:async()=>['old']}); await obsoleteSuccess;
  assert(!trendEvents.includes('picker'));
  const freshModels=cross.loadOverviewTrends('today',0);
  modelsPending[2].resolve({json:async()=>[]}); await freshModels;
  assert(trendEvents.includes('charts'));

  // Older running backends can still serve the page until they are restarted.
  const legacyCalls=[];
  const legacy={apiFetch:async (url, options)=>{legacyCalls.push({url,options}); if (url.includes('overview-usage')) {const e=new Error('missing');e.status=404;throw e;} return {json:async()=>[]};}};
  vm.createContext(legacy);
  vm.runInContext(part('async function fetchOverviewUsage(', 'async function loadOverviewTrends('), legacy);
  const legacySignal=new AbortController().signal;
  const legacyData=await legacy.fetchOverviewUsage('7d', {signal:legacySignal});
  assert.equal(legacyCalls.length,6);
  assert(legacyCalls.some(call=>call.url.includes('bucket=hour')));
  assert(legacyCalls.every(call=>call.options.signal===legacySignal));
  assert(Array.isArray(legacyData.models));

  // Statistics and each auxiliary panel finish independently, even across range switches.
  const splitNodes=new Map(), splitRequests=[], splitTimers=[];
  const splitNode=id=>{
    if (!splitNodes.has(id)) splitNodes.set(id,{style:{},textContent:'',innerHTML:'',value:'1d',
      classList:{add(){},remove(){},toggle(){}},querySelectorAll:()=>[]});
    return splitNodes.get(id);
  };
  const splitOverview={$:splitNode,t:x=>x,fmt:Number,fmtExact:Number,fmtCost:Number,escapeHtml:String,
    modelSourcesMarkup:()=>'',AbortController,console:{error(){}},
    setTimeout:fn=>{splitTimers.push(fn);return splitTimers.length;},clearTimeout:()=>{},
    setHealthTimelineState(){},setHealthTimelineError(){},renderHeatmap(){},loadHealthTimeline(){},loadTrendCharts(){},
    renderAlerts:data=>{splitNode('test-alert').textContent=data === null ? 'error' : 'ready';
      if (data && data.items?.length) splitNode('alertbar-rows').innerHTML='critical alert';},
    makeSpark:()=>({data:{datasets:[{}]},update(){},destroy(){}}),sparkReq:null,sparkTok:null,
    apiFetch:(url,{signal})=>{const d=deferred();splitRequests.push({url,signal,...d});return d.promise;}};
  vm.createContext(splitOverview);
  vm.runInContext('let trendSerial=0, htlSerial=0, overviewTrendData=null;\n'+part('let overviewSerial = 0;', '// ====== 多语言'),splitOverview);
  splitOverview.loadOverviewTrends=()=>{};
  const snapshot=count=>({summary:{total_requests:count,failed_requests:0,by_source:[],by_client_key:[]},
    timeseries:[],models:[],costs:[],active_models:[],health_timeline:{}});
  const respondSplit=(request,data)=>request.resolve({json:async()=>data});
  const flushSplit=async()=>{for(let i=0;i<8;i++) await Promise.resolve();};
  const firstSplit=splitOverview.load();
  respondSplit(splitRequests[0],snapshot(10)); await flushSplit();
  assert.equal(splitNode('v-requests').textContent,10);
  assert.equal(splitNode('overview-content').style.display,'');
  assert(splitNode('tb-routes').innerHTML.includes('state.loading'));
  respondSplit(splitRequests[2],[{alias:'ready',name:'route',models:[]}]); await flushSplit();
  assert(splitNode('tb-routes').innerHTML.includes('ready'));
  assert(splitNode('quota-groups').innerHTML.includes('state.loading'));
  splitNode('range').value='7d';
  const secondSplit=splitOverview.load();
  assert(splitRequests[1].signal.aborted);
  respondSplit(splitRequests[7],snapshot(20)); await flushSplit();
  assert.equal(splitNode('v-requests').textContent,20);
  respondSplit(splitRequests[1],[{source:'obsolete',window:'week'}]);
  for (const index of [3,4,5,6]) respondSplit(splitRequests[index],[]);
  await firstSplit; await flushSplit();
  assert(splitNode('quota-groups').innerHTML.includes('state.loading'));
  splitRequests[8].reject(new Error('quota offline'));
  respondSplit(splitRequests[9],[]);
  respondSplit(splitRequests[10],[]);
  respondSplit(splitRequests[11],[]);
  respondSplit(splitRequests[12],{history:[]});
  respondSplit(splitRequests[13],{items:[]});
  await secondSplit;
  assert(splitNode('quota-groups').innerHTML.includes('state.error'));
  assert(splitNode('tb-routes').innerHTML.includes('w.noroutes'));
  assert.equal(splitNode('v-requests').textContent,20);
  const thirdSplit=splitOverview.load();
  respondSplit(splitRequests[14],snapshot(30)); await flushSplit();
  assert.equal(splitNode('v-requests').textContent,30);
  respondSplit(splitRequests[15],[]);
  for (const index of [16,17,18,19,20]) {
    splitRequests[index].signal.addEventListener('abort',()=>splitRequests[index].reject(new Error('timeout')),{once:true});
  }
  splitTimers[2](); await thirdSplit;
  assert(splitNode('quota-groups').innerHTML.includes('w.noquota'));
  assert(splitNode('tb-routes').innerHTML.includes('state.error'));
  assert.equal(splitNode('v-requests').textContent,30);
  const fourthSplit=splitOverview.load();
  respondSplit(splitRequests[21],snapshot(40)); await flushSplit();
  respondSplit(splitRequests[27],{items:[{level:'critical'}]}); await flushSplit();
  assert.equal(splitNode('test-alert').textContent,'ready');
  assert(splitNode('quota-groups').innerHTML.includes('state.loading'));
  const fifthSplit=splitOverview.load();
  assert(splitNode('quota-groups').innerHTML.includes('state.error'));
  splitRequests[28].reject(new Error('statistics offline'));
  await fifthSplit;
  assert.equal(splitNode('overview-state').textContent,'state.stale');
  assert.equal(splitNode('test-alert').textContent,'error');
  assert.equal(splitNode('alertbar-rows').innerHTML,'critical alert');
  respondSplit(splitRequests[22],[{source:'obsolete',window:'week'}]);
  for (const index of [23,24,25,26,27]) respondSplit(splitRequests[index],[]);
  await fourthSplit;
  assert(splitNode('quota-groups').innerHTML.includes('state.error'));
  assert.equal(splitNode('v-requests').textContent,40);

  // Initial overview errors hide contents; refresh errors preserve already visible data.
  const overPending=[];
  const overNodes=new Map();
  const overNode=id=>{if(!overNodes.has(id)) overNodes.set(id,{style:{},textContent:'old',classList:{remove(){}}});return overNodes.get(id);};
  let restoredTrends=0, timelineErrors=0, alertErrors=0;
  const overCtx={$:overNode,t:x=>x,console:{error(){}},AbortController,setTimeout,clearTimeout,loadTrendCharts:()=>{restoredTrends++;},
    setHealthTimelineState:()=>{},setHealthTimelineError:()=>{timelineErrors++;},renderAlerts:()=>{alertErrors++;},
    apiFetch:()=>{const d=deferred();overPending.push(d);return d.promise;}};
  vm.createContext(overCtx);
  vm.runInContext('let trendSerial=0, htlSerial=0, overviewTrendData=null;\n'+part('let overviewSerial = 0;', '// ====== 多语言'),overCtx);
  const oldOverview=overCtx.load(), newOverview=overCtx.load();
  assert.equal(overNode('overview-content').style.display,'none');
  overPending[0].reject(new Error('old')); await oldOverview;
  assert.equal(overNode('overview-state').textContent,'state.loading');
  overPending[7].reject(new Error('new')); await newOverview;
  assert.equal(overNode('overview-state').textContent,'state.error');
  assert.equal(overNode('overview-content').style.display,'none');
  assert.equal(timelineErrors,1);
  assert.equal(alertErrors,0);
  vm.runInContext('overviewHasData=true',overCtx);
  overNode('overview-content').style.display='';
  overNode('updated').textContent='previous';
  const refreshOverview=overCtx.load();
  assert.equal(overNode('overview-content').style.display,'');
  assert.equal(overNode('overview-state').textContent,'state.updating');
  overPending[14].reject(new Error('offline')); await refreshOverview;
  assert.equal(overNode('overview-content').style.display,'');
  assert.equal(overNode('overview-state').textContent,'state.stale');
  assert.equal(overNode('updated').textContent,'previous');
  assert.equal(restoredTrends,1);
  assert.equal(timelineErrors,2);
  vm.runInContext("overviewTrendData={range:'1d'}",overCtx);
  overNode('range').value='7d';
  const changedRange=overCtx.load();
  assert.equal(overNode('overview-state').textContent,'state.updating_range');
  overPending[21].reject(new Error('offline')); await changedRange;
  assert.equal(overNode('overview-state').textContent,'state.stale_range');
  assert.equal(restoredTrends,2);
  assert.equal(timelineErrors,3);
  const interacting=overCtx.load();
  vm.runInContext('++trendSerial; ++htlSerial',overCtx);
  overPending[28].reject(new Error('offline')); await interacting;
  assert.equal(restoredTrends,2);
  assert.equal(timelineErrors,3);

  // The timeout also covers a response body that stalls after receiving headers.
  const timers=[]; const timeoutSignals=[]; const timeoutNodes=new Map();
  const timeoutNode=id=>{if(!timeoutNodes.has(id)) timeoutNodes.set(id,{style:{},textContent:'',classList:{remove(){}}});return timeoutNodes.get(id);};
  const timeoutCtx={$:timeoutNode,t:x=>x,console:{error(){}},AbortController,
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},
    setHealthTimelineState:()=>{},setHealthTimelineError:()=>{},loadTrendCharts:()=>{},
    apiFetch:async (url,{signal})=>{
      timeoutSignals.push(signal);
      return {json:()=>new Promise((resolve,reject)=>{
        if (signal.aborted) reject(new Error('aborted'));
        else signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
      })};
    }};
  vm.createContext(timeoutCtx);
  vm.runInContext('let trendSerial=0, htlSerial=0, overviewTrendData=null;\n'+part('let overviewSerial = 0;', '// ====== 多语言'),timeoutCtx);
  vm.runInContext('overviewHasData=true',timeoutCtx);
  timeoutNode('overview-content').style.display='';
  const stalled=timeoutCtx.load();
  await Promise.resolve(); await Promise.resolve();
  timers[0](); await stalled;
  assert.equal(timeoutNode('overview-state').textContent,'state.stale');
  assert.equal(timeoutNode('overview-content').style.display,'');
  const obsolete=timeoutCtx.load();
  await Promise.resolve(); await Promise.resolve();
  const obsoleteSignal=timeoutSignals[7];
  const current=timeoutCtx.load();
  const currentSignal=timeoutSignals[14];
  assert(obsoleteSignal.aborted);
  assert(!currentSignal.aborted);
  await obsolete;
  assert(!currentSignal.aborted);
  assert.equal(timeoutNode('overview-state').textContent,'state.updating');
  timers[2](); await current;
  assert(currentSignal.aborted);
  assert.equal(timeoutNode('overview-state').textContent,'state.stale');
  assert(!html.includes('setInterval(load, 30000)'));
  assert(html.includes('${fmtExact(m.tokens)}'));

  const priceNodes = new Map();
  const priceNode = id => { if (!priceNodes.has(id)) priceNodes.set(id,{textContent:'',innerHTML:''}); return priceNodes.get(id); };
  const priceCtx = {$:priceNode, t:x=>x, fmt:Number, fmtExact:Number};
  vm.createContext(priceCtx);
  vm.runInContext(part('function fmtCost(v)', 'function lParams()'), priceCtx);
  const priceBlock = part('    // 总成本（原价）+ 平均单价', '    // 按来源');
  const renderPrice = sum => { priceCtx.sum=sum; vm.runInContext(`{${priceBlock}}`, priceCtx); };
  renderPrice({total_requests:2,priced_requests:0,unpriced_requests:2,total_cost_usd:0,total_tokens:100});
  assert.equal(priceNode('v-cost').textContent,'—');
  assert.equal(priceNode('v-unit').textContent,'—');
  renderPrice({total_requests:2,priced_requests:1,unpriced_requests:1,total_cost_usd:1,total_tokens:100});
  assert.equal(priceNode('v-cost').textContent,'$1.0000');
  assert(priceNode('v-cost-sub').textContent.includes('w.partial'));
  assert.equal(priceNode('v-unit').textContent,'—');
  renderPrice({total_requests:1,priced_requests:1,unpriced_requests:0,total_cost_usd:0,total_tokens:100});
  assert.equal(priceNode('v-cost').textContent,'$0');
  assert.equal(priceNode('v-unit').textContent,'$0.00');
  renderPrice({total_requests:1,priced_requests:1,unpriced_requests:0,total_cost_usd:0.000001,total_tokens:100});
  assert.equal(priceNode('v-cost').textContent,'$0.000001');
  renderPrice({total_requests:1,priced_requests:1,unpriced_requests:0,total_cost_usd:0.0000004,total_tokens:1000000});
  assert.equal(priceNode('v-cost').textContent,'$4.00e-7');
  assert.equal(priceNode('v-unit').textContent,'$4.00e-7');
  renderPrice({total_requests:1,priced_requests:0,unpriced_requests:1,total_cost_usd:0,total_tokens:100});
  assert.equal(priceNode('v-cost').textContent,'—');
  assert.equal(priceNode('v-unit').textContent,'—');

  // Overview request totals and outcome counts remain exact for reconciliation.
  const countNodes=new Map();
  const countNode=id=>{if(!countNodes.has(id)) countNodes.set(id,{textContent:''});return countNodes.get(id);};
  const countCtx={$:countNode,t:x=>x,fmt:n=>`compact:${n}`,fmtExact:n=>`exact:${n}`,
    sum:{total_requests:13915,successful_requests:12001,failed_requests:1002,cancelled_requests:900,unknown_requests:12,success_rate:92}};
  vm.createContext(countCtx);
  vm.runInContext(`{${part("    $('v-requests').textContent", "    $('v-tokens').textContent")}}`,countCtx);
  assert.equal(countNode('v-requests').textContent,'exact:13915');
  for (const n of [12001,1002,900,12]) assert(countNode('v-requests-sub').textContent.includes(`exact:${n}`));
  assert(!countNode('v-requests-sub').textContent.includes('compact:'));
  renderPrice({total_requests:13915,priced_requests:1,unpriced_requests:0,total_cost_usd:1,total_tokens:100});
  assert(priceNode('v-cost-sub').textContent.includes('13915 w.requests'));

  // Small recorded costs remain visible in every overview breakdown.
  const breakdownNodes = new Map();
  const breakdownNode = id => { if (!breakdownNodes.has(id)) breakdownNodes.set(id,{innerHTML:'',querySelectorAll:()=>[]}); return breakdownNodes.get(id); };
  const breakdown = {$:breakdownNode,t:(x,...args)=>`${x}${args.join('')}`,fmt:n=>`compact:${n}`,fmtExact:n=>`exact:${n}`,escapeHtml:String,modelSourcesMarkup:(_model,_items,expanded)=>expanded?'EXPANDED':'',
    sum:{by_source:[{source:'a',billing_mode:'payg',requests:10001,input:10002,output:10003,cache_read:10004,cache_write:1,billable_requests:1,priced_requests:1,unpriced_requests:0,cost_usd:0.000001},
        {source:'zero-failure',billing_mode:'payg',requests:2,input:0,output:0,cache_read:0,cache_write:0,billable_requests:0,priced_requests:0,unpriced_requests:0,cost_usd:0}],
      by_client_key:[{client_key:'(direct)',is_collector:1,requests:10001,tokens:13915,billable_requests:1,priced_requests:1,unpriced_requests:0,cost:0.000001},
        {client_key:'(direct)',is_collector:0,requests:1,billable_requests:1,priced_requests:1,unpriced_requests:0,cost:0.000001},
        {client_key:'legacy',requests:1,billable_requests:1,priced_requests:1,unpriced_requests:0,cost:0.000001}]},
    costs:[{source:'a',model:'m',requests:10001,input:10002,output:10003,billable_requests:1,priced:1,unpriced_requests:0,cost:0.000001,avg_cost_per_request:0.000001}],
    byModel:[{grp:'m',requests:10001,input:0,output:0,tokens:0,reasoning:0}],usage:{}};
  vm.createContext(breakdown);
  vm.runInContext(part('function fmtCost(v)', 'function lParams()') +
    `{${part('    // 按来源', '    for (const [id, columns]')}}`,breakdown);
  for (const id of ['tb-source','tb-cost','tb-key']) assert(breakdownNode(id).innerHTML.includes('$0.000001'),id);
  assert.equal(breakdown.fmtCost(0.0000004),'$4.00e-7');
  assert.equal(breakdown.fmtCost6(0.0000004),'$4.00e-7');
  assert.equal(breakdown.fmtCost6(0),'$0.000000');
  for (const id of ['tb-source','tb-cost','tb-key']) {
    assert(breakdownNode(id).innerHTML.includes('exact:10001'),id);
    assert(!breakdownNode(id).innerHTML.includes('compact:10001'),id);
  }
  assert(breakdownNode('tb-key').innerHTML.includes('exact:13915'));
  assert(breakdownNode('tb-source').innerHTML.includes('exact:10005'));
  const zeroFailureRow = breakdownNode('tb-source').innerHTML.split('<tr>')
    .find(row => row.includes('zero-failure'));
  assert(zeroFailureRow && zeroFailureRow.includes('$0</td>'));
  assert(!zeroFailureRow.includes('*'));
  assert(breakdownNode('tb-model').innerHTML.includes('exact:10001'));
  assert(!breakdownNode('tb-model').innerHTML.includes('compact:10001'));
  breakdownNode('tb-model').querySelectorAll=()=>[{dataset:{model:'m'}},{dataset:{model:'removed'}}];
  vm.runInContext(`{${part('    // 按来源', '    for (const [id, columns]')}}`,breakdown);
  assert(breakdownNode('tb-model').innerHTML.includes('EXPANDED'));
  breakdown.byModel=[{grp:'other',requests:1,input:0,output:0,tokens:0,reasoning:0}];
  vm.runInContext(`{${part('    // 按来源', '    for (const [id, columns]')}}`,breakdown);
  assert(!breakdownNode('tb-model').innerHTML.includes('EXPANDED'));
  assert(breakdownNode('tb-key').innerHTML.includes('key.collector'));
  assert(breakdownNode('tb-key').innerHTML.includes('key.named(direct)'));
  assert(breakdownNode('tb-key').innerHTML.includes('legacy'));

  // Trend errors are visible and stale model/timeseries requests cannot restore charts.
  const trendPending=[]; const trendNodes=new Map();
  const trendNode=id=>{if(!trendNodes.has(id)) trendNodes.set(id,{style:{},value:'today',setAttribute(){}});return trendNodes.get(id);};
  const trends={$:trendNode,t:x=>x,console:{error(){}},Chart:{defaults:{}},cssv:()=>'',
    reqChart:null,tokChart:null,
    apiFetch:()=>{const d=deferred();trendPending.push(d);return d.promise;}};
  vm.createContext(trends);
  vm.runInContext(part('let trendSerial = 0;', 'let overviewSerial = 0;'),trends);
  const oldTrend=trends.loadTrendCharts(), newTrend=trends.loadTrendCharts();
  assert.equal(trendNode('chart-requests').style.visibility,'hidden');
  assert.equal(trendNode('chart-tokens').style.visibility,'hidden');
  trendPending[0].reject(new Error('old')); await oldTrend;
  assert.equal(trendNode('trend-state').textContent,'state.loading');
  trendPending[1].reject(new Error('new')); await newTrend;
  assert.equal(trendNode('trend-state').textContent,'state.error');
  assert.notEqual(trendNode('chart-cost').style.visibility,'hidden');

  // Model comparison is drawn from the same snapshot, with zero-filled buckets.
  const charts=[]; let modelFetches=0;
  function FakeChart(node, config){ const chart={node,config,destroyed:false}; charts.push(chart); this.destroy=()=>{chart.destroyed=true;}; }
  FakeChart.defaults={};
  const compareNodes=new Map();
  const compareNode=id=>{if(!compareNodes.has(id)) compareNodes.set(id,{id,style:{},value:'7d',setAttribute(){}});return compareNodes.get(id);};
  const keyCtx={t:x=>x};
  vm.createContext(keyCtx);
  vm.runInContext(part('function modelKey(m)', 'function modelSourcesMarkup('),keyCtx);
  const modelKey=keyCtx.modelKey, modelLabel=keyCtx.modelLabel;
  assert.notEqual(modelKey({source:'a/b',model:'c'}),modelKey({source:'a',model:'b/c'}));
  assert.equal(modelLabel({source:'a',model:null}),'a · model.missing');
  assert.equal(modelLabel({source:'a',model:''}),'a · model.empty');
  assert.equal(modelLabel({source:'',model:'a'}),'model.empty_source · a');
  const compare={$:compareNode,t:(key,...args)=>args.length ? `${key}:${args.join(',')}` : key,cssv:()=>'',Chart:FakeChart,console,fmt:Number,fmtExact:Number,
    modelKey,modelLabel,MODEL_COLORS:['#123456','#654321'],
    activeModels:[{source:'a',model:'same'},{source:'b',model:'same'}],
    selectedModels:[modelKey({source:'a',model:'same'}),modelKey({source:'b',model:'same'})],
    trendChart:null,costChart:null,reqChart:null,tokChart:null,renderLinePicker:()=>{},
    apiFetch:()=>{modelFetches++;throw new Error('unexpected independent request');}};
  vm.createContext(compare);
  vm.runInContext(part('let trendSerial = 0;', 'let overviewSerial = 0;'),compare);
  vm.runInContext(`overviewTrendData={range:'7d',timeseries:[
    {bucket:'2026-11-07 10:00',input:5,output:0,cache_read:0,requests:1,priced_requests:0,unpriced_requests:1,cost:0},
    {bucket:'2026-11-07 11:00',input:7,output:0,cache_read:0,requests:1,priced_requests:1,unpriced_requests:0,cost:0}],
    modelTimeseries:[{source:'a',model:'same',bucket:'2026-11-07 10:00',requests:1,tokens:5},
    {source:'b',model:'same',bucket:'2026-11-07 11:00',requests:1,tokens:7}]}`,compare);
  await compare.loadTrendCharts();
  const composition=charts.find(c=>c.node.id==='chart-trend');
  assert.deepEqual(Array.from(composition.config.data.datasets,d=>d.fill),['origin','-1','-1','-1']);
  assert(composition.config.data.datasets.every(d=>d.tension===0));
  const costData=charts.find(c=>c.config.data.datasets[0]?.label === 'tok.cost');
  assert.equal(costData.config.data.datasets[0].tension,0);
  assert.deepEqual(Array.from(costData.config.data.datasets[0].data),[null,0]);
  assert.equal(costData.config.options.plugins.tooltip.callbacks.label({parsed:{y:0.000001}}),'$0.000001');
  assert.equal(costData.config.options.scales.y.ticks.callback(0.000001),'$0.000001');
  assert(html.includes('const billable = s.billable_requests ?? s.priced_requests ?? 0;'));
  assert.equal(modelFetches,0);
  const compareChart=charts.find(c=>c.node.id==='chart-tokens');
  assert.equal(compareNode('chart-tokens').style.visibility,'');
  assert.deepEqual(Array.from(compareChart.config.data.datasets,d=>d.label),['a · same','b · same']);
  const requestChart=charts.find(c=>c.node.id==='chart-requests');
  assert(compareChart.config.data.datasets.every(d=>d.fill===false && d.tension===0));
  assert(requestChart.config.data.datasets.every(d=>d.fill===false && d.tension===0));
  assert.deepEqual(Array.from(requestChart.config.data.datasets,d=>d.label),['a · same','b · same']);
  assert.deepEqual(Array.from(compareChart.config.data.datasets[0].data),[5,0]);
  assert.deepEqual(Array.from(compareChart.config.data.datasets[1].data),[0,7]);
  assert.equal(compareNode('requests-total').textContent,'trend.selected_total:2');
  assert.equal(compareNode('tokens-total').textContent,'trend.selected_total:12');
  assert.equal(requestChart.config.options.plugins.tooltip.callbacks.label({dataset:{label:'a · same'},parsed:{y:1}}),'a · same: 1');
  await requestChart.config.options.plugins.legend.onClick(null,{datasetIndex:0});
  assert.deepEqual(Array.from(compare.selectedModels),[modelKey({source:'b',model:'same'})]);
  assert.equal(compareNode('requests-total').textContent,'trend.selected_total:1');
  assert.equal(compareNode('tokens-total').textContent,'trend.selected_total:7');
  assert.deepEqual(Array.from(charts.at(-2).config.data.datasets,d=>d.label),['b · same']);
  assert.deepEqual(Array.from(charts.at(-1).config.data.datasets,d=>d.label),['b · same']);
  await requestChart.config.options.plugins.legend.onClick(null,{datasetIndex:0});
  assert.equal(compareNode('tokens-total').textContent,'trend.selected_total:7');
  compare.selectedModels=[];
  await compare.loadTrendCharts();
  assert.equal(compareNode('requests-total').textContent,'trend.all_total:2');
  assert.equal(compareNode('tokens-total').textContent,'trend.all_total:12');
  compare.selectedModels=compare.activeModels.map(modelKey);
  for (const id of ['chart-trend','chart-cost','chart-requests','chart-tokens']) {
    const chart=charts.find(c=>c.node.id===id);
    const title=chart.config.options.plugins.tooltip.callbacks.title;
    assert.equal(title([{dataIndex:0}]),'11-07 10:00');
    assert.equal(title([]),'');
  }
  vm.runInContext('overviewTrendData.timeseries[0].partial=true',compare);
  charts.length=0;
  await compare.loadTrendCharts();
  for (const id of ['chart-trend','chart-cost','chart-requests','chart-tokens']) {
    const title=charts.find(c=>c.node.id===id).config.options.plugins.tooltip.callbacks.title;
    assert.equal(title([{dataIndex:0}]),'11-07 10:00 · trend.partial');
    assert.equal(title([{dataIndex:1}]),'11-07 11:00');
  }

  charts.length=0;
  vm.runInContext('overviewTrendData.timeseries[1].cost=0.0000004',compare);
  await compare.loadTrendCharts();
  const tinyCost=charts.find(c=>c.node.id==='chart-cost');
  assert.deepEqual(Array.from(tinyCost.config.data.datasets[0].data),[null,0.0000004]);
  assert.equal(tinyCost.config.options.plugins.tooltip.callbacks.label({parsed:{y:0.0000004}}),'$0.000000400');
  assert.equal(tinyCost.config.options.scales.y.ticks.callback(0.0000004),'$0.000000400');
  assert.equal(tinyCost.config.options.plugins.tooltip.callbacks.label({parsed:{y:1e-15}}),'$1.00e-15');

  // Slashes cannot merge distinct source/model pairs, including on older backends.
  charts.length=0;
  compare.activeModels=[{source:'a/b',model:'c'},{source:'a',model:'b/c'}];
  compare.selectedModels=compare.activeModels.map(modelKey);
  vm.runInContext(`overviewTrendData={range:'7d',timeseries:[
    {bucket:'2026-11-07 10:00',input:12,output:0,cache_read:0,requests:2,cost:0},
    {bucket:'2026-11-07 11:00',input:0,output:0,cache_read:0,requests:0,cost:0}],
    modelTimeseries:[{source:'a/b',model:'c',bucket:'2026-11-07 10:00',requests:1,tokens:5},
    {source:'a',model:'b/c',bucket:'2026-11-07 10:00',requests:1,tokens:7}]}`,compare);
  await compare.loadTrendCharts();
  const slashChart=charts.find(c=>c.node.id==='chart-tokens');
  assert.deepEqual(Array.from(slashChart.config.data.datasets,d=>d.label),['a/b · c','a · b/c']);
  assert.deepEqual(Array.from(slashChart.config.data.datasets,d=>Array.from(d.data)),[[5,0],[7,0]]);
  const urls=[];
  compare.apiFetch=async url=>{
    urls.push(url);
    const params=new URL(url,'http://localhost').searchParams;
    const tokens=params.get('source')==='a/b'?5:7;
    return {json:async()=>[{bucket:'2026-11-07 10:00',input:tokens,output:0,cache_read:0,requests:1}]};
  };
  vm.runInContext('delete overviewTrendData.modelTimeseries',compare);
  charts.length=0;
  await compare.loadTrendCharts();
  assert(urls.some(url=>url.includes('source=a%2Fb&model=c')));
  assert(urls.some(url=>url.includes('source=a&model=b%2Fc')));
  const fallbackChart=charts.find(c=>c.node.id==='chart-tokens');
  assert.deepEqual(Array.from(fallbackChart.config.data.datasets,d=>Array.from(d.data)),[[5,0],[7,0]]);
  for (const selected of [{source:'a',model:null},{source:'a',model:''},{source:'',model:'a'}]) {
    compare.activeModels=[selected]; compare.selectedModels=[modelKey(selected)];
    urls.length=0;
    await compare.loadTrendCharts();
    assert.equal(compareNode('trend-state').textContent,'state.model_filter_unsupported');
    assert.equal(compareNode('chart-tokens').style.visibility,'hidden');
    assert.equal(compareNode('requests-total').textContent,'');
    assert.equal(compareNode('tokens-total').textContent,'');
    assert.equal(urls.length,0);
  }
  assert(fallbackChart.destroyed);
  compare.activeModels=[{source:'a',model:'same'},{source:'b',model:'same'}];
  compare.selectedModels=compare.activeModels.map(modelKey);

  // Repeated local hours must remain separate in both labels and model values.
  charts.length=0;
  vm.runInContext(`overviewTrendData={range:'7d',timeseries:[
    {bucket:'2026-11-01 01:00',bucket_start:100,bucket_offset:'-0400',input:5,output:0,cache_read:0,requests:1,cost:0},
    {bucket:'2026-11-01 01:00',bucket_start:3700,bucket_offset:'-0500',input:7,output:0,cache_read:0,requests:1,cost:0}],
    modelTimeseries:[{source:'a',model:'same',bucket:'2026-11-01 01:00',bucket_start:100,requests:1,tokens:5},
    {source:'a',model:'same',bucket:'2026-11-01 01:00',bucket_start:3700,requests:1,tokens:7}]}`,compare);
  await compare.loadTrendCharts();
  const repeatedChart=charts.find(c=>c.node.id==='chart-tokens');
  assert.deepEqual(Array.from(repeatedChart.config.data.labels),['11-01 01:00 -0400','11-01 01:00 -0500']);
  assert.deepEqual(Array.from(repeatedChart.config.data.datasets[0].data),[5,7]);

  // A range switch in flight or failed cannot mix a new trend with the old overview.
  charts.length=0;
  compareNode('range').value='3d';
  await compare.loadTrendCharts();
  assert.equal(modelFetches,0);
  const staleRangeChart=charts.find(c=>c.node.id==='chart-tokens');
  assert.deepEqual(Array.from(staleRangeChart.config.data.datasets[0].data),[5,7]);

  // A single bucket is valid data; an empty snapshot clears every old chart.
  charts.length=0;
  vm.runInContext(`overviewTrendData={range:'3d',timeseries:[
    {bucket:'2026-11-08 10:00',input:5,output:2,cache_read:0,requests:1,priced_requests:0,unpriced_requests:1,cost:0}],
    modelTimeseries:[{source:'a',model:'same',bucket:'2026-11-08 10:00',requests:1,tokens:7}]}`,compare);
  await compare.loadTrendCharts();
  assert(staleRangeChart.destroyed);
  assert.equal(charts.length,3);
  assert.equal(compareNode('cost-box').style.display,'none');
  assert.equal(compareNode('cost-empty').style.display,'block');
  assert.equal(compareNode('cost-empty').textContent,'state.cost_unpriced');
  assert.deepEqual(Array.from(charts.find(c=>c.node.id==='chart-tokens').config.data.datasets[0].data),[7]);
  for (const id of ['trend','requests','tokens']) {
    assert.equal(compareNode(`${id}-box`).style.display,'');
    assert.equal(compareNode(`${id}-empty`).style.display,'none');
  }
  vm.runInContext(`overviewTrendData={range:'3d',timeseries:[
    {bucket:'2026-11-08 09:00',input:0,output:0,cache_read:0,requests:0,priced_requests:0,unpriced_requests:0,cost:0},
    {bucket:'2026-11-08 10:00',input:5,output:2,cache_read:0,requests:1,priced_requests:0,unpriced_requests:1,cost:0},
    {bucket:'2026-11-08 11:00',input:0,output:0,cache_read:0,requests:0,priced_requests:0,unpriced_requests:0,cost:0}],modelTimeseries:[]}`,compare);
  await compare.loadTrendCharts();
  assert.equal(compareNode('cost-box').style.display,'none');
  assert.equal(compareNode('cost-empty').textContent,'state.cost_unpriced');
  vm.runInContext(`overviewTrendData.timeseries[1].priced_requests=1; overviewTrendData.timeseries[1].unpriced_requests=0`,compare);
  await compare.loadTrendCharts();
  assert.equal(compareNode('cost-box').style.display,'');
  assert.equal(compareNode('cost-empty').style.display,'none');
  vm.runInContext(`overviewTrendData.timeseries[1].requests=0; overviewTrendData.timeseries[1].priced_requests=0`,compare);
  await compare.loadTrendCharts();
  assert.equal(compareNode('cost-box').style.display,'');
  const singleCharts=charts.slice();
  vm.runInContext(`overviewTrendData={range:'3d',timeseries:[],modelTimeseries:[]}`,compare);
  await compare.loadTrendCharts();
  assert(singleCharts.every(c=>c.destroyed));
  assert.equal(charts.length,14);
  for (const id of ['trend','cost','requests','tokens']) {
    assert.equal(compareNode(`${id}-box`).style.display,'none');
    assert.equal(compareNode(`${id}-empty`).style.display,'block');
  }
  assert.equal(compareNode('trend-state').style.display,'none');
  assert.equal(compareNode('requests-total').textContent,'');
  assert.equal(compareNode('tokens-total').textContent,'');
  assert.equal(compareNode('cost-empty').textContent,'state.empty');

  // Failed trace requests are evicted, show retry, and never write into a recycled row.
  let traceRequest=deferred(), traceCalls=0, activeBox;
  const traceElement={textContent:'',innerHTML:'',appendChild(child){this.child=child;}};
  activeBox={innerHTML:'',querySelector:()=>null};
  const detail={console, $:()=>traceElement, document:{querySelector:()=>activeBox,createElement:()=>({})},
    t:x=>x,fmt:Number,fmtCost:()=>'',fmtCost6:()=>'',escapeHtml:ctx.escapeHtml,copyTraceId(){},
    logsState:{lastData:{items:[{id:1,trace_id:'one'}]}},traceCache:new Map(),
    apiFetch:()=>{traceCalls++;return traceRequest.promise;}};
  vm.createContext(detail);
  vm.runInContext(part('async function fillLogDetail(', 'async function exportLogs()'),detail);
  const failDetail=detail.fillLogDetail(0); traceRequest.reject(new Error('offline')); await failDetail;
  assert.equal(detail.traceCache.size,0);
  assert.equal(traceElement.child.textContent,'state.retry');
  traceRequest=deferred(); traceElement.child.onclick();
  assert.equal(traceCalls,2);
  detail.logsState.lastData={items:[{id:2,trace_id:'two'}]};
  activeBox={innerHTML:'',querySelector:()=>null};
  traceRequest.resolve({json:async()=>({total_ms:1,spans:[{name:'old',duration_ms:1}]})});
  await new Promise(resolve=>setImmediate(resolve));
  assert(!traceElement.innerHTML.includes('old'));
  detail.logsState.lastData={items:[{id:3,trace_id:'three'}]};
  traceRequest=deferred();
  const successDetail=detail.fillLogDetail(0);
  traceRequest.resolve({json:async()=>({total_ms:2,spans:[{name:'fresh',duration_ms:1,has_error:true},
    {name:'legacy',duration_ms:1,error:'private-error'}]})});
  await successDetail;
  assert(traceElement.innerHTML.includes('fresh'));
  assert.equal((traceElement.innerHTML.match(/background:#ef4444/g)||[]).length,2);
  assert(traceElement.innerHTML.includes('w.failed'));
  assert(!traceElement.innerHTML.includes('private-error'));
  console.log('web escaping, quota, sessions and API regression passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
