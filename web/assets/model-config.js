'use strict';
const $ = id => document.getElementById(id);
const titles = {overview: '运行概览', config: '模型配置', tests: '模型测试'};
let token = '', state = null, busy = false, page = 'overview', target = 'current';
let catalogIds = [], manualIds = [], draft = emptySelection(), confirmation = null, dialogOpener;
// 生效配置与草稿分别保存结果，并使用配置内容指纹防止旧测试被误认为有效。
const evidence = {current: {}, draft: {}};
let sidebarCollapsed = false, noticeTimer = null;
function emptySelection() { return {syncModel: '', asyncModel: '', fallbackModels: []}; }
function copy(value) { return {syncModel: value.syncModel, asyncModel: value.asyncModel, fallbackModels: [...value.fallbackModels]}; }
function fingerprint(value) { return JSON.stringify(copy(value)); }
function changed() { return state && fingerprint(draft) !== fingerprint(state.current); }
function allIds() { return [...new Set([...catalogIds, ...manualIds, draft.syncModel, draft.asyncModel, ...draft.fallbackModels].filter(Boolean))].sort(); }
function node(tag, text, classes = '') { const el = document.createElement(tag); el.textContent = text; el.className = classes; return el; }
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  const toast = $('notice');
  toast.textContent = message; toast.hidden = !message;
  toast.className = error ? 'notice notice-error' : 'notice';
  // 每条新消息重新计时，避免上一条提醒的定时器提前隐藏新消息。
  if (message) noticeTimer = setTimeout(() => { toast.hidden = true; }, 2000);
  // 登录失败仍在表单内展示，避免用户错过需要处理的认证错误。
  if (!state) $('loginNotice').textContent = message;
}
function drawer(open) {
  $('sidebar').classList.toggle('-translate-x-full', !open);
  $('drawerBackdrop').hidden = !open;
  $('menu').setAttribute('aria-expanded', String(open));
  // 移动抽屉打开时禁用背景区焦点；桌面侧栏始终可访问。
  const mobile = !matchMedia('(min-width: 1024px)').matches;
  $('sidebar').inert = mobile && !open;
  $('menu').closest('header').inert = mobile && open;
  document.querySelector('main').inert = mobile && open;
  if (open) $('sidebar').querySelector('[aria-current="page"]').focus();
}
function navigate(next, focus = true) {
  page = titles[next] ? next : 'overview';
  document.querySelectorAll('[data-view]').forEach(el => el.hidden = el.dataset.view !== page);
  document.querySelectorAll('nav [data-nav]').forEach(el => {
    if (el.dataset.nav === page) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('headerTitle').textContent = titles[page];
  document.querySelector('main').classList.toggle('config-workspace', page === 'config');
  document.querySelector('main').classList.toggle('tests-workspace', page === 'tests');
  history.replaceState(null, '', '#' + page);
  drawer(false);
  if (page === 'tests') renderTests();
  if (focus) { $('view-' + page).querySelector('h1').focus(); window.scrollTo({top: 0}); }
}
function controls() {
  document.querySelectorAll('button,input,select').forEach(el => el.disabled = busy);
  // 即使测试在进行，也允许切换模块查看，但不允许更改草稿或退出。
  document.querySelectorAll('[data-nav],#menu,#drawerBackdrop,#sidebarToggle,#explorerToggle').forEach(el => el.disabled = false);
  $('apply').disabled = busy || !changed(); $('discard').disabled = busy || !changed();
  $('rollback').disabled = busy || !state?.previous;
  $('dirty').textContent = changed() ? '草稿已修改 · 尚未应用到服务' : '与当前生效配置一致';
  $('draftDot').hidden = !changed();
  document.querySelectorAll('[data-order]').forEach(el => {
    el.disabled = busy || (el.dataset.order === 'up' ? Number(el.dataset.index) === 0 : Number(el.dataset.index) === draft.fallbackModels.length - 1);
  });
  document.querySelectorAll('#fallbackOptions input').forEach(el => el.disabled = busy || (!el.checked && draft.fallbackModels.length >= 8));
}
async function api(path, method = 'GET', body) {
  const response = await fetch('/api/admin/models/' + path, {method, cache: 'no-store', headers: {Authorization: 'Bearer ' + token, ...(body ? {'Content-Type': 'application/json'} : {})}, body: body ? JSON.stringify(body) : undefined});
  let data;
  try { data = await response.json(); } catch { throw Error('服务返回内容无法识别，请检查网络或代理配置。'); }
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '配置校验失败，请检查模型 ID 和备用模型数量。');
  return data;
}
async function action(fn) {
  if (busy) return;
  busy = true; controls();
  try { await fn(); } catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
}
function renderSelects() {
  const filtered = allIds().filter(id => id.toLowerCase().includes($('modelSearch').value.trim().toLowerCase()));
  for (const lane of ['sync', 'async']) {
    const selected = draft[lane + 'Model'];
    $(lane).replaceChildren(...[...new Set([selected, ...filtered].filter(Boolean))].map(id => new Option(id + (catalogIds.includes(id) ? '' : '（已配置 / 手动）'), id)));
    $(lane).value = selected;
  }
  $('filterCount').textContent = filtered.length + ' / ' + allIds().length + ' 个模型';
  renderExplorer(filtered);
}
function renderFallbackOptions() {
  const ids = allIds().filter(id => id.toLowerCase().includes($('fallbackSearch').value.trim().toLowerCase()));
  $('fallbackOptions').replaceChildren(...ids.map(id => {
    const label = node('label', '', 'flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2.5 hover:bg-slate-50');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = draft.fallbackModels.includes(id); checkbox.className = 'size-4 shrink-0 accent-blue-600';
    checkbox.onchange = () => {
      if (checkbox.checked && draft.fallbackModels.length < 8) draft.fallbackModels.push(id);
      else if (!checkbox.checked) draft.fallbackModels = draft.fallbackModels.filter(value => value !== id);
      draftChanged(false);
    };
    label.append(checkbox, node('span', id, 'break-all text-sm')); return label;
  }));
  if (!ids.length) $('fallbackOptions').append(node('p', '没有匹配的模型，可清空搜索或手动添加。', 'p-3 text-xs text-slate-500'));
}
function renderOrder() {
  $('fallbackCount').textContent = draft.fallbackModels.length + ' / 8 个';
  $('fallbackOrderSummary').textContent = '回退顺序 / 调整 · ' + draft.fallbackModels.length + ' 个';
  $('fallbackOrder').replaceChildren(...draft.fallbackModels.map((id, index) => {
    const li = node('li', '', 'flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-3');
    li.append(node('span', String(index + 1).padStart(2, '0'), 'text-xs font-medium text-slate-400'), node('span', id, 'model-name min-w-0 flex-1 break-all text-sm'));
    const buttons = node('div', '', 'ml-auto flex gap-1.5');
    for (const [kind, title] of [['up', '上移'], ['down', '下移'], ['remove', '移除']]) {
      const button = node('button', title, 'btn-small'); button.setAttribute('aria-label', title + ' ' + id);
      if (kind !== 'remove') { button.dataset.order = kind; button.dataset.index = index; }
      button.onclick = () => {
        if (kind === 'remove') draft.fallbackModels.splice(index, 1);
        else { const next = index + (kind === 'up' ? -1 : 1); [draft.fallbackModels[index], draft.fallbackModels[next]] = [draft.fallbackModels[next], draft.fallbackModels[index]]; }
        draftChanged();
      };
      buttons.append(button);
    }
    li.append(buttons); return li;
  }));
  $('fallbackFlow').textContent = draft.fallbackModels.length ? '主模型 → ' + draft.fallbackModels.join(' → ') : '未设置备用模型：主模型失败后不尝试其他模型。';
}
function draftChanged(rebuildOptions = true) {
  evidence.draft = {};
  renderSelects(); renderOrder(); if (rebuildOptions) renderFallbackOptions(); renderTests(); controls();
}
function render(value) {
  state = value; draft = copy(value.current); evidence.current = {}; evidence.draft = {};
  $('version').textContent = 'v' + value.current.version;
  $('currentSync').textContent = value.current.syncModel; $('currentAsync').textContent = value.current.asyncModel;
  $('backupCount').textContent = value.current.fallbackModels.length + ' 个';
  $('currentFallback').replaceChildren(...(value.current.fallbackModels.length ? value.current.fallbackModels.map((id, index) => node('li', (index + 1) + '. ' + id)) : [node('li', '未设置')]));
  $('updated').textContent = value.current.updatedAt ? new Date(value.current.updatedAt).toLocaleString() : '尚未通过页面应用';
  $('rollback').title = value.previous ? '查看上一版并确认回退' : '尚无上一版配置';
  draftChanged();
}
function rolesFor(selection, id) {
  const roles = [];
  if (selection.syncModel === id) roles.push('同步');
  if (selection.asyncModel === id) roles.push('异步');
  const index = selection.fallbackModels.indexOf(id);
  if (index >= 0) roles.push('备用第 ' + (index + 1) + ' 位');
  return roles;
}
function nodeStatus(id) {
  if (state && rolesFor(state.current, id).length) return 'current';
  return rolesFor(draft, id).length ? 'draft' : 'other';
}
function renderExplorer(ids) {
  if (!state) return;
  $('explorerCount').textContent = allIds().length + ' 个';
  $('modelList').replaceChildren(...ids.map(id => {
    const card = node('div', '', 'model-tile');
    card.dataset.model = id; card.dataset.status = nodeStatus(id);
    card.setAttribute('role', 'listitem');
    card.append(node('span', id, 'break-all text-sm font-medium'));
    const status = nodeStatus(id);
    if (status !== 'other') card.append(node('span', status === 'current' ? '生效' : '草稿', 'tile-state'));
    return card;
  }));
  if (!ids.length) $('modelList').append(node('p', '没有匹配模型，请修改搜索条件。', 'help'));
}
function testedSelection() { return target === 'current' ? copy(state.current) : copy(draft); }
function matchingResult(source, kind, selection) {
  const result = evidence[source][kind]; return result?.fingerprint === fingerprint(selection) ? result : null;
}
function allPassed(source, kind, selection) {
  const record = matchingResult(source, kind, selection);
  return record?.status === 'done' && record.results.length > 0 && record.results.every(item => item.passed);
}
function renderTests() {
  if (!state) return;
  const selection = testedSelection();
  document.querySelectorAll('[data-target]').forEach(el => {
    const active = el.dataset.target === target;
    el.setAttribute('aria-pressed', String(active)); el.classList.toggle('bg-white', active); el.classList.toggle('shadow-sm', active); el.classList.toggle('text-blue-700', active);
  });
  $('targetInfo').textContent = target === 'current' ? '生效版本 v' + state.current.version : (changed() ? '草稿 · 尚未应用' : '草稿 · 与生效配置相同');
  const ids = [...new Set([selection.syncModel, selection.asyncModel, ...selection.fallbackModels])];
  $('testSummary').textContent = ids.length + ' 个模型 · 重复项已去重';
  $('testRows').replaceChildren(...ids.map(id => {
    const tr = node('tr', '', 'border-b border-slate-100 align-top');
    const name = node('td', '', 'py-2 pr-3'); name.append(node('div', id, 'break-all text-sm font-medium text-slate-800'));
    const roles = []; if (selection.syncModel === id) roles.push('同步'); if (selection.asyncModel === id) roles.push('异步'); if (selection.fallbackModels.includes(id)) roles.push('备用');
    name.append(node('p', roles.join(' / '), 'mt-1 text-xs text-slate-400')); tr.append(name);
    for (const kind of ['text', 'vision']) {
      const record = matchingResult(target, kind, selection); const item = record?.results?.find(value => value.model === id);
      const td = node('td', '', 'px-2 py-2');
      const label = record?.status === 'running' ? '测试中' : record?.status === 'error' ? '请求失败' : item ? (item.passed ? '通过' : '未通过') : '未测试';
      td.append(node('span', label, 'badge ' + (item?.passed ? 'badge-green' : item || record?.status === 'error' ? 'badge-red' : '')));
      if (item) td.append(node('span', item.seconds + 's', 'ml-2 inline-block text-xs text-slate-400'));
      const errorMessage = record?.status === 'error' ? record.message : item && !item.passed ? item.message : '';
      if (errorMessage) {
        const details = node('details', '', 'mt-1 text-xs text-rose-600');
        details.append(node('summary', '失败原因'), node('p', errorMessage, 'mt-1 break-words leading-5'));
        td.append(details);
      }
      tr.append(td);
    }
    return tr;
  }));
}
async function loadCatalog() {
  $('catalog').textContent = '加载中…'; $('catalogStatus').textContent = '正在向网关请求模型列表…';
  try {
    catalogIds = [...new Set((await api('catalog')).filter(id => typeof id === 'string' && id))];
    $('catalogStatus').textContent = '已加载 ' + catalogIds.length + ' 个模型 · 能力需测试';
    renderSelects(); renderFallbackOptions(); $('catalog').textContent = '重新加载';
  } catch (error) { $('catalogStatus').textContent = '加载失败：' + error.message + '。可继续手动输入。'; $('catalog').textContent = '重试加载'; }
}
function openDialog(title, description, operation, destination = null, warning = '') {
  dialogOpener = document.activeElement; confirmation = operation;
  $('dialogTitle').textContent = title; $('dialogDescription').textContent = description;
  $('dialogDiff').replaceChildren();
  if (destination) for (const [key, label] of [['syncModel', '同步模型'], ['asyncModel', '异步模型'], ['fallbackModels', '备用顺序']]) {
    const format = value => Array.isArray(value) ? value.join(' → ') || '无' : value;
    const block = node('div', '', 'rounded-xl border border-slate-200 p-4');
    block.append(node('p', label, 'text-xs font-medium text-slate-400'), node('p', '当前：' + format(state.current[key]), 'mt-2 break-all text-xs text-slate-500'), node('p', '应用：' + format(destination[key]), 'mt-2 break-all text-sm font-medium text-blue-700'));
    $('dialogDiff').append(block);
  }
  $('dialogWarning').hidden = !warning; $('dialogWarning').textContent = warning;
  $('confirmDialog').showModal(); $('dialogCancel').focus();
}
function closeDialog() { $('confirmDialog').close(); confirmation = null; dialogOpener?.focus(); }
$('dialogCancel').onclick = closeDialog;
$('confirmDialog').addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
$('dialogConfirm').onclick = () => { const operation = confirmation; closeDialog(); if (operation) action(operation); };
$('loginForm').onsubmit = event => {
  event.preventDefault(); action(async () => {
    token = $('token').value; $('token').value = ''; $('loginNotice').textContent = '正在连接…';
    render(await api('config')); $('login').hidden = true; $('shell').hidden = false; notice(''); navigate(location.hash.slice(1) || 'overview', false); await loadCatalog();
  });
};
document.querySelectorAll('[data-nav]').forEach(el => el.onclick = () => navigate(el.dataset.nav));
document.querySelectorAll('[data-target]').forEach(el => el.onclick = () => { target = el.dataset.target; renderTests(); });
$('menu').onclick = () => drawer(true);
$('drawerBackdrop').onclick = () => { drawer(false); $('menu').focus(); };
document.addEventListener('keydown', event => {
  if ($('menu').getAttribute('aria-expanded') !== 'true') return;
  if (event.key === 'Escape') { drawer(false); $('menu').focus(); }
  if (event.key === 'Tab') {
    const focusable = [...$('sidebar').querySelectorAll('button:not(:disabled)')]; const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
matchMedia('(min-width: 1024px)').addEventListener('change', () => drawer(false));
$('testCurrent').onclick = () => { target = 'current'; navigate('tests'); };
$('testDraft').onclick = () => { target = 'draft'; navigate('tests'); };
$('refresh').onclick = () => {
  const perform = async () => { render(await api('config')); notice('已读取最新生效配置。'); };
  if (changed()) openDialog('重新读取配置？', '刷新将丢弃本页面尚未应用的草稿。', perform); else action(perform);
};
$('discard').onclick = () => openDialog('放弃草稿修改？', '恢复到当前已读取的生效配置，不会向服务器写入。', async () => { draft = copy(state.current); draftChanged(); notice('已放弃草稿修改。'); });
$('logout').onclick = () => {
  const perform = async () => {
    token = ''; state = null; draft = emptySelection(); catalogIds = []; manualIds = []; evidence.current = {}; evidence.draft = {};
    $('shell').hidden = true; $('login').hidden = false; $('loginNotice').textContent = ''; $('token').value = '';
    for (const id of ['syncManual', 'asyncManual', 'fallbackManual', 'modelSearch', 'fallbackSearch']) $(id).value = '';
    notice(''); drawer(false); $('token').focus();
  };
  if (changed()) openDialog('退出登录？', '未应用的草稿将丢失，服务器配置不会改变。', perform); else action(perform);
};
$('explorerToggle').onclick = () => {
  const collapsed = $('explorerToggle').getAttribute('aria-expanded') === 'true';
  $('explorerBody').hidden = collapsed;
  $('modelExplorer').classList.toggle('explorer-collapsed', collapsed);
  $('explorerToggle').setAttribute('aria-expanded', String(!collapsed));
  $('explorerToggle').textContent = collapsed ? '展开' : '收起';
};
$('catalog').onclick = () => action(loadCatalog);
$('modelSearch').oninput = renderSelects;
$('fallbackSearch').oninput = () => { renderFallbackOptions(); controls(); };
for (const lane of ['sync', 'async']) $(lane).onchange = () => { draft[lane + 'Model'] = $(lane).value; draftChanged(); };
function toggleFallback(open) { $('fallbackPanel').hidden = !open; $('fallbackToggle').setAttribute('aria-expanded', String(open)); if (open) $('fallbackSearch').focus(); }
$('fallbackToggle').onclick = () => toggleFallback($('fallbackPanel').hidden);
$('fallbackDone').onclick = () => { toggleFallback(false); $('fallbackToggle').focus(); };
$('fallbackPicker').addEventListener('keydown', event => { if (event.key === 'Escape') { toggleFallback(false); $('fallbackToggle').focus(); } });
for (const lane of ['sync', 'async', 'fallback']) $(lane + 'Add').onclick = () => {
  const id = $(lane + 'Manual').value.trim();
  if (!id || /[\s\x00-\x1f\x7f]/.test(id) || id.length > 200) { notice('模型 ID 应为 1～200 字符，不得包含空白或控制字符。', true); return; }
  if (lane === 'fallback') {
    if (draft.fallbackModels.includes(id) || draft.fallbackModels.length >= 8) { notice('备用模型不能重复，最多 8 个。', true); return; }
    draft.fallbackModels.push(id);
  } else draft[lane + 'Model'] = id;
  manualIds.push(id); $(lane + 'Manual').value = ''; draftChanged(); notice('已选入草稿，尚未应用。');
};
for (const kind of ['text', 'vision']) $(kind + 'Test').onclick = () => action(async () => {
  // 请求启动时捕获对象；切换模块不会改变该请求的归属。
  const source = target, selection = testedSelection();
  const record = {fingerprint: fingerprint(selection), status: 'running', results: []}; evidence[source][kind] = record; renderTests();
  notice('正在逐个测试所选模型，可能需要数分钟。可以切换模块查看，但暂不能修改配置。');
  try {
    const result = await api('test', 'POST', {...selection, kind}); record.results = result.results; record.status = 'done'; notice('测试完成，生效配置未更改。');
  } catch (error) { record.status = 'error'; record.message = error.message; throw error; }
  finally { renderTests(); }
});
$('apply').onclick = () => {
  const selection = copy(draft), version = state.current.version;
  const verified = allPassed('draft', 'text', selection) && allPassed('draft', 'vision', selection);
  openDialog('核对并应用配置', '应用后仅新任务使用新配置，已接收的任务不受影响。', async () => {
    render(await api('config', 'PUT', {...selection, expectedVersion: version})); notice('新配置已生效，无需重启服务。'); navigate('overview');
  }, selection, verified ? '' : '此草稿尚未通过完整的文本与视觉测试。仍可确认应用，但请先确认模型能力和业务风险。');
};
$('rollback').onclick = () => {
  const version = state.current.version;
  openDialog('回退到上一版配置', '恢复上一版内容并生成新版本号；未应用草稿将丢弃，仅影响后续任务。', async () => {
    render(await api('rollback', 'POST', {expectedVersion: version})); notice('已回退至上一版内容。'); navigate('overview');
  }, state.previous);
};
$('sidebarToggle').onclick = () => {
  sidebarCollapsed = !sidebarCollapsed;
  $('shell').classList.toggle('sidebar-collapsed', sidebarCollapsed);
  const label = sidebarCollapsed ? '展开侧边栏' : '收起侧边栏';
  $('sidebarToggle').setAttribute('aria-label', label);
  $('sidebarToggle').title = label;
  $('sidebarToggle').setAttribute('aria-expanded', String(!sidebarCollapsed));
};
window.addEventListener('beforeunload', event => { if (changed()) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('hashchange', () => { if (state) navigate(location.hash.slice(1)); });
controls();
