const $ = selector => document.querySelector(selector);
const state = {accounts: [], tasks: [], settings: {}, overview: {}, page: 0, accountFilter: 'enabled', browserAccount: null, profileAccount: null, webAccount: null};
const taskLimits = [20, 50, 100, 200, 500];
function preference(key, choices, fallback) { try { const value = localStorage.getItem('art2api.' + key); return value !== null && choices.includes(Number(value)) ? Number(value) : fallback; } catch { return fallback; } }
function savePreference(key, value) { try { localStorage.setItem('art2api.' + key, String(value)); return true; } catch { return false; } }
state.taskLimit = preference('taskLimit', taskLimits, 50);
state.refreshSeconds = preference('refreshSeconds', [0, 5, 15, 30, 60], 15);
let toastTimer, browserTimer, browserBlob, refreshTimer, refreshSequence = 0, refreshActive = 0, busyCount = 0;
const labels = {ready:'可用',unauthorized:'等待授权',oauth_client_required:'需要 OAuth 客户端',unchecked:'待检测',error:'连接异常',proxy_conflict:'出口重复',queued:'排队',preparing:'准备中',submitting:'提交中',running:'生成中',succeeded:'已完成',failed:'失败',submission_unknown:'结果未知'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '—';
const safeUrl = value => { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; } catch { return ''; } };
const iconPaths = {
  sparkles:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/><path d="M20 2v4m-2-2h4"/>',
  'user-plus':'<circle cx="9" cy="7" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2m3-15v6m-3-3h6"/>',
  users:'<circle cx="9" cy="7" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2M17 3a4 4 0 0 1 0 8m2 4a6 6 0 0 1 3 5"/>',
  settings:'<path d="m9 3-1 3-3 1v4l-2 1 2 2v3l3 1 1 3h6l1-3 3-1v-3l2-2-2-1V7l-3-1-1-3Z"/><circle cx="12" cy="12" r="3"/>',
  book:'<path d="M12 5v16M12 5C8 2 4 3 2 4v16c3-2 7-1 10 1 3-2 7-3 10-1V4c-2-1-6-2-10 1Z"/>',
  refresh:'<path d="M21 10a9 9 0 0 0-15-6L3 7m0-5v5h5M3 14a9 9 0 0 0 15 6l3-3m0 5v-5h-5"/>',
  logout:'<path d="M9 3H4v18h5m5-14 5 5-5 5M8 12h13"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8Zm-10 9 10 5 10-5M2 17l10 5 10-5"/>',
  left:'<path d="m14 6-6 6 6 6"/>',right:'<path d="m10 6 6 6-6 6"/>',x:'<path d="m6 6 12 12M6 18 18 6"/>',
  save:'<path d="M4 3h13l4 4v14H3V3Zm3 0v6h10V3M7 21v-8h10v8"/>',
  shield:'<path d="m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7Zm-5 9 3 3 7-7"/>',
  edit:'<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14Z"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>'
};
function icon(name) { return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || ''}</svg>`; }
document.querySelectorAll('[data-icon]').forEach(node => node.outerHTML = icon(node.dataset.icon));
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false; toastTimer = setTimeout(() => $('#toast').hidden = true, 7000); }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json','X-Requested-With':'art2api',...options.headers}});
  if (response.status === 401) { location.href = '/login'; throw new Error('请重新登录'); }
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务暂不可用（HTTP ${response.status}）`); }
  if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) || '请求失败');
  return data;
}
async function busy(button, action) {
  if (button.disabled) return;
  const markup = button.innerHTML;
  button.disabled = true; button.setAttribute('aria-busy', 'true'); busyCount++;
  try { await action(); } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.removeAttribute('aria-busy'); button.innerHTML = markup; busyCount--; }
}
function badge(status) { return `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`; }
function accountRow(account) {
  return `<tr data-id="${esc(account.id)}"><td><strong class="account-name">${esc(account.name)}</strong><small>${account.backend === 'web' ? 'Artlist 网页协议' : 'Artlist MCP'} · ${esc(account.id.slice(0, 8))}</small></td>
    <td class="status-stack">${badge(account.status)}<small>${account.authorized ? '已授权' : '未授权'}${account.duplicate_egress ? ' · 出口重复' : ''}</small>${account.last_error ? `<small class="error-note">${esc(account.last_error)}</small>` : ''}</td>
    <td class="mono">${account.active_tasks} / ${account.max_concurrency}</td><td class="mono">${Object.keys(account.profiles).length}</td>
    <td><code>${esc(account.proxy_url_masked)}</code><small class="mono">${esc(account.egress_ip || '出口未检测')} · v${account.proxy_version}</small></td><td>${esc(stamp(account.checked_at))}</td>
    <td><div class="actions"><button data-action="edit" class="icon-button" title="编辑账号与代理" aria-label="编辑账号与代理">${icon('edit')}</button><button data-action="connect">${account.authorized ? '重新授权' : '连接账号'}</button><button data-action="check">检测</button><button data-action="profiles">模型</button>${account.backend === 'web' ? '<button data-action="web-session">网页登录设置</button><button data-action="web-query">查询网页任务</button>' : ''}<button data-action="toggle">${account.enabled ? '停用' : '启用'}</button><button data-action="delete" class="icon-button danger" title="删除账号" aria-label="删除账号">${icon('trash')}</button></div></td></tr>`;
}
function renderAccounts() {
  const enabled = state.accounts.filter(a => a.enabled).length;
  $('#enabledCount').textContent = enabled; $('#disabledCount').textContent = state.accounts.length - enabled;
  const accounts = state.accounts.filter(a => a.enabled === (state.accountFilter === 'enabled'));
  $('#accounts').innerHTML = accounts.map(accountRow).join('');
  $('#accountsEmpty').hidden = accounts.length > 0;
  $('#accountsEmptyText').textContent = state.accountFilter === 'enabled' ? '暂无启用账号，可在停用列表中配置并启用' : '暂无停用账号';
  $('#accountTabs').querySelectorAll('button').forEach(b => { const active = b.dataset.filter === state.accountFilter; b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active)); });
}
function durationText(task) {
  const end = ['succeeded','failed'].includes(task.internal_status) ? task.updated_at : Date.now()/1000;
  const seconds = Math.max(0, Math.floor(end - task.created_at));
  return seconds < 60 ? `${seconds}秒` : `${Math.floor(seconds/60)}分${seconds%60}秒`;
}
function mediaFromTask(task) {
  const request = task.request || {};
  const inputs = [['image_urls','image','图'],['video_urls','video','视'],['audio_urls','audio','音']].flatMap(([field,kind,label]) => (request[field] || []).map((url,index) => ({kind, label:label+(index+1), url})));
  for (const [field,label] of [['first_frame','首帧'],['last_frame','尾帧']]) if (request[field]) inputs.push({kind:'image', label, url:request[field]});
  return inputs;
}
function mediaButtons(task) {
  return mediaFromTask(task).map((item,index) => `<button type="button" data-media-task="${esc(task.id)}" data-media-index="${index}" title="预览${esc(item.label)}" aria-label="预览${esc(item.label)}"${safeUrl(item.url) ? '' : ' disabled'}>${esc(item.label)}</button>`).join('');
}
function taskRow(task) {
  const url = safeUrl(task.content?.video_url), request = task.request || {};
  const terminal = ['succeeded','failed'].includes(task.internal_status);
  const progress = Math.max(0, Math.min(Number(task.progress) || 0, 100));
  const account = state.accounts.find(a => a.id === task.account_id)?.name || task.account_id || '待分配';
  const spec = [task.model, request.duration ? `${request.duration}S` : '', request.resolution, request.aspect_ratio].filter(Boolean).join(' · ');
  const result = task.error ? `<span class="error-stack"><span class="error-public" title="${esc(task.error.message)}">响应：${esc(task.error.message)}</span><span class="error-upstream" title="${esc(task.error.code)}">代码：${esc(task.error.code)}</span></span>` : `<span class="result-links">${url ? `<a href="${esc(url)}" data-result-task="${esc(task.id)}" target="_blank" rel="noopener noreferrer" title="预览生成视频">视</a>` : ''}</span>`;
  return `<tr data-task-id="${esc(task.id)}"><td><button class="cell-title link-button mono" data-detail="${esc(task.id)}" title="${esc(task.id)}">${esc(task.id.slice(0,20))}</button><span class="cell-sub mono" title="ARTAPI · ${esc(account)} · 代理 v${task.proxy_version}">ARTAPI · ${esc(account)}</span></td>
    <td class="prompt-cell"><span class="cell-title" title="${esc(request.prompt)}">${esc(request.prompt || '—')}</span><span class="task-meta-line"><span class="task-spec" title="${esc(spec)}">${esc(spec)}</span><span class="media-text">${mediaButtons(task)}</span></span></td>
    <td>${badge(task.internal_status)}</td><td><div class="progress-stack"><span class="progress-value" title="按任务状态展示的阶段进度"><b class="mono">${progress}%</b><span class="progress-track"><i style="width:${progress}%"></i></span></span><span class="elapsed">${terminal ? '耗时' : '已用'} ${durationText(task)}</span></div></td>
    <td><span class="time-stack"><span>创建 ${esc(stamp(task.created_at))}</span><span>更新 ${esc(stamp(task.updated_at))}</span></span></td><td>${result}${task.internal_status === 'submission_unknown' ? `<button data-recover="${esc(task.id)}">恢复查询</button>` : ''}</td></tr>`;
}
function renderTasks() {
  $('#tasks').innerHTML = state.tasks.map(taskRow).join(''); $('#tasksEmpty').hidden = state.tasks.length > 0;
  const total = state.overview.total || 0, pages = Math.max(1, Math.ceil(total/state.taskLimit));
  $('#taskCount').textContent = `共 ${total} 条`;
  $('#pageInfo').textContent = `第 ${state.page+1} / ${pages} 页`;
  $('#previousPage').disabled = state.page === 0; $('#nextPage').disabled = (state.page+1)*state.taskLimit >= total;
  $('#taskLimit').value = String(state.taskLimit);
}
async function refresh() {
  const sequence = ++refreshSequence, page = state.page, limit = state.taskLimit;
  refreshActive++;
  try {
    const [accounts,tasks,settings,events,overview] = await Promise.all(['/api/accounts',`/api/tasks?limit=${limit}&offset=${page*limit}`,'/api/settings','/api/events','/api/overview'].map(path => api(path)));
    if (sequence !== refreshSequence) return;
    Object.assign(state, {accounts,tasks,settings,overview});
    if (page > 0 && !tasks.length) { state.page = Math.max(0, Math.ceil(overview.total/limit)-1); return await refresh(); }
    renderAccounts(); renderTasks();
    const ready = accounts.filter(a => a.enabled && a.status === 'ready' && !a.duplicate_egress);
    $('#readyCount').textContent = `${ready.length} / ${accounts.length}`;
    $('#proxyCount').textContent = new Set(accounts.map(a => a.egress_ip).filter(Boolean)).size;
    $('#runningCount').textContent = overview.active; $('#runningCount').title = `包含 ${overview.unknown} 个提交结果未知的任务`;
    $('#completeCount').textContent = overview.succeeded; $('#failedCount').textContent = overview.failed;
    const models = [...new Set(ready.flatMap(a => Object.keys(a.profiles)))], selected = $('#taskModel').value;
    $('#modelCount').textContent = models.length;
    if (JSON.stringify([...$('#taskModel').options].map(o => o.value)) !== JSON.stringify(models)) {
      $('#taskModel').innerHTML = models.map(m => `<option>${esc(m)}</option>`).join('');
      if (models.includes(selected)) $('#taskModel').value = selected;
    }
    $('#events').innerHTML = events.slice(0, 30).map(e => `<div class="event"><span>${esc(stamp(e.created_at))}</span><span>${esc(e.kind)}</span><span>${esc(e.detail)}</span></div>`).join('') || '<div class="empty">暂无事件</div>';
    $('#updated').textContent = '已更新 ' + new Date().toLocaleTimeString(); $('#updated').classList.remove('refresh-error');
    $('#appVersion').textContent = 'v' + settings.version;
  } catch (error) {
    if (sequence === refreshSequence) { $('#updated').textContent = '刷新失败，显示上次数据'; $('#updated').classList.add('refresh-error'); }
    throw error;
  } finally { refreshActive--; }
}
function autoRefresh() { if (!document.hidden && !refreshActive && !busyCount) refresh().catch(() => {}); }
function configureRefresh() { clearInterval(refreshTimer); if (state.refreshSeconds) refreshTimer = setInterval(autoRefresh, state.refreshSeconds*1000); }
document.addEventListener('visibilitychange', () => { if (state.refreshSeconds) autoRefresh(); });
$('#accountTabs').onclick = event => { const button = event.target.closest('[data-filter]'); if (button) { state.accountFilter = button.dataset.filter; renderAccounts(); } };
$('#taskLimit').value = String(state.taskLimit);
$('#taskLimit').onchange = () => { state.taskLimit = Number($('#taskLimit').value); state.page = 0; savePreference('taskLimit', state.taskLimit); refresh().catch(error => toast(error.message)); };
$('#previousPage').onclick = () => { if (state.page > 0) { state.page--; refresh().catch(error => toast(error.message)); } };
$('#nextPage').onclick = () => { if ((state.page+1)*state.taskLimit < state.overview.total) { state.page++; refresh().catch(error => toast(error.message)); } };
$('#tasksRefresh').onclick = event => busy(event.currentTarget, refresh);
const runtimeFields = ['queue_limit','task_timeout_seconds','poll_interval_seconds','request_timeout_seconds','browser_timeout_seconds','browser_headless','sd25_video_policy'];
let settingsBaseline = {};
$('#settingsButton').onclick = event => busy(event.currentTarget, async () => {
  const settings = await api('/api/settings'); state.settings = settings; settingsBaseline = {...settings};
  const form = $('#settingsForm');
  for (const key of runtimeFields) { const input = form.elements[key]; if (input.type === 'checkbox') input.checked = settings[key]; else input.value = settings[key]; }
  form.elements.task_limit.value = state.taskLimit; form.elements.refresh_seconds.value = state.refreshSeconds;
  $('#chromeExecutable').value = settings.chrome_executable || '自动检测 Chromium / Chrome';
  $('#publicBaseUrl').value = settings.public_base_url; $('#mcpUrl').value = settings.mcp_url;
  $('#settingsModels').innerHTML = settings.model_ids.map(model => `<code>${esc(model)}</code>`).join('');
  $('#settingsHint').textContent = '服务端设置重启后保留；未保存的修改不会生效。'; $('#settingsHint').classList.remove('error-note');
  $('#settingsDialog').showModal();
});
$('#settingsForm').onsubmit = event => {
  event.preventDefault(); const form = event.target;
  busy(form.querySelector('[type=submit]'), async () => {
    const values = {};
    for (const key of runtimeFields) { const input = form.elements[key], value = input.type === 'checkbox' ? input.checked : input.tagName === 'SELECT' ? input.value : Number(input.value); if (value !== settingsBaseline[key]) values[key] = value; }
    try { if (Object.keys(values).length) state.settings = await api('/api/settings', {method:'PATCH', body:JSON.stringify(values)}); }
    catch (error) { $('#settingsHint').textContent = error.message; $('#settingsHint').classList.add('error-note'); throw error; }
    const limit = Number(form.elements.task_limit.value); if (limit !== state.taskLimit) state.page = 0;
    state.taskLimit = limit; state.refreshSeconds = Number(form.elements.refresh_seconds.value);
    const persistedLimit = savePreference('taskLimit', limit), persistedRefresh = savePreference('refreshSeconds', state.refreshSeconds);
    configureRefresh(); $('#settingsDialog').close(); toast(persistedLimit && persistedRefresh ? '设置已保存' : '运行设置已保存；当前浏览器不允许持久保存显示偏好');
    await refresh();
  });
};
function openTaskDetail(task) {
  if (!task) return;
  const fields = [['本地任务', task.id],['上游任务', task.upstream_id || '—'],['账号', state.accounts.find(a => a.id === task.account_id)?.name || task.account_id],['模型', task.model],['生成参数', `${task.request.duration} 秒 · ${task.request.resolution} · ${task.request.aspect_ratio}`],['状态', labels[task.internal_status] || task.internal_status],['创建 / 更新', `${stamp(task.created_at)} / ${stamp(task.updated_at)}`]];
  if (task.error) fields.push(['错误代码', task.error.code],['错误详情', task.error.message]);
  if (task.prompt_processing) fields.push(['音频引用兼容处理', `${task.prompt_processing.action}（${task.prompt_processing.tags.join('、')}）`]);
  for (const record of task.media_processing || []) {
    const audio = record.field === 'audio_urls';
    const spec = value => audio ? `${(value.durationMs/1000).toFixed(3)} 秒` : `${value.width}×${value.height} / ${(value.durationMs/1000).toFixed(3)} 秒 / ${value.fps}fps`;
    fields.push([`参考${audio ? '音频' : '视频'} ${record.index} ${audio ? '补齐' : '适配'}`, `${spec(record.before)} → ${spec(record.after)}；${record.actions.join('；')}`]);
  }
  $('#taskDetail').innerHTML = `<dl class="detail-grid">${fields.map(([key,value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl><p class="field-title">完整提示词</p><div class="detail-prompt">${esc(task.request.prompt)}</div><div class="detail-links media-text">${mediaButtons(task)}${safeUrl(task.content?.video_url) ? `<a href="${esc(safeUrl(task.content.video_url))}" data-result-task="${esc(task.id)}" target="_blank" rel="noopener noreferrer">生成视频 ↗</a>` : ''}</div>`;
  state.detailTask = task;
  $('#taskDetailDialog').showModal();
}
let previewAnchor;
function closeMediaPreview(restoreFocus = false) {
  const popover = $('#mediaPopover');
  popover.querySelectorAll('video,audio').forEach(media => { media.pause(); media.removeAttribute('src'); media.load(); });
  if (popover.matches(':popover-open')) popover.hidePopover();
  popover.hidden = true; popover.replaceChildren();
  if (restoreFocus && previewAnchor?.isConnected) previewAnchor.focus();
  previewAnchor = null;
}
function previewMedia(item, anchor) {
  if (!item || !safeUrl(item.url)) return;
  closeMediaPreview(); previewAnchor = anchor;
  const popover = $('#mediaPopover'), url = safeUrl(item.url);
  popover.innerHTML = `<header><strong>${esc(item.label)}</strong><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开原素材 ↗</a><button type="button" class="icon-button" data-close-preview aria-label="关闭素材预览">${icon('x')}</button></header><div class="media-preview-body"></div><p class="preview-error" hidden>素材暂时无法预览，可打开原素材查看。</p>`;
  const media = document.createElement(item.kind === 'image' ? 'img' : item.kind);
  if (item.kind === 'image') { media.alt = item.label; media.referrerPolicy = 'no-referrer'; }
  else { media.controls = true; media.preload = 'metadata'; media.autoplay = true; media.muted = item.kind === 'video'; media.playsInline = true; }
  media.addEventListener('error', () => { if (media.isConnected) popover.querySelector('.preview-error').hidden = false; });
  media.src = url; popover.querySelector('.media-preview-body').append(media);
  popover.hidden = false; popover.showPopover();
  const rect = anchor.getBoundingClientRect(), box = popover.getBoundingClientRect();
  popover.style.left = `${Math.max(10, Math.min(rect.left, innerWidth-box.width-10))}px`;
  popover.style.top = `${Math.max(10, Math.min(rect.bottom+6, innerHeight-box.height-10))}px`;
}
document.addEventListener('click', event => {
  const button = event.target.closest('[data-media-task],[data-result-task]');
  if (button) {
    if (button.tagName === 'A' && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
    event.preventDefault();
    const id = button.dataset.mediaTask || button.dataset.resultTask;
    const task = state.tasks.find(t => t.id === id) || (state.detailTask?.id === id ? state.detailTask : null);
    if (task) previewMedia(button.dataset.resultTask ? {kind:'video',label:'生成视频',url:task.content?.video_url} : mediaFromTask(task)[Number(button.dataset.mediaIndex)], button);
    return;
  }
  if (event.target.closest('[data-close-preview]')) closeMediaPreview(true);
  else if (!event.target.closest('#mediaPopover')) closeMediaPreview();
});
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#mediaPopover').hidden) { event.preventDefault(); event.stopPropagation(); closeMediaPreview(true); } }, true);
document.addEventListener('scroll', event => { if (!event.target.closest?.('#mediaPopover')) closeMediaPreview(); }, true);
window.addEventListener('resize', () => closeMediaPreview());
document.addEventListener('close', () => closeMediaPreview(), true);
let pendingSubmission = null;
function submissionKey(body) {
  if (pendingSubmission?.body !== body) {
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    pendingSubmission = {body, key:'console-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')};
  }
  return pendingSubmission.key;
}

function editAccount(account){const form=$('#accountForm');form.reset();form.elements.id.value=account?.id||'';form.elements.name.value=account?.name||'';form.elements.max_concurrency.value=account?.max_concurrency||1;form.elements.backend.value=account?.backend||'web';form.elements.proxy_url.required=!account;$('#accountTitle').textContent=account?'编辑账号':'添加账号';$('#proxyHelp').textContent=account?`当前：${account.proxy_url_masked}。留空保留；更换后需重新检测。`:'必填。每个账号使用一个独立固定出口。';$('#accountDialog').showModal();}
$('#addAccount').onclick=$('#emptyAdd').onclick=()=>editAccount();
document.querySelectorAll('.close').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('#accountForm').onsubmit=event=>{event.preventDefault();const form=event.target,values=Object.fromEntries(new FormData(form)),id=values.id;delete values.id;values.max_concurrency=Number(values.max_concurrency);if(!values.proxy_url)delete values.proxy_url;busy(form.querySelector('[type=submit]'),async()=>{await api('/api/accounts'+(id?'/'+id:''),{method:id?'PATCH':'POST',body:JSON.stringify(values)});$('#accountDialog').close();state.accountFilter=id?(state.accounts.find(a=>a.id===id)?.enabled?'enabled':'disabled'):'disabled';await refresh();toast('账号已保存');});};
$('#accounts').onclick=event=>{const button=event.target.closest('button[data-action]');if(!button)return;const account=state.accounts.find(a=>a.id===button.closest('[data-id]').dataset.id);busy(button,async()=>{switch(button.dataset.action){case 'edit':editAccount(account);return;case 'check':await api(`/api/accounts/${account.id}/check`,{method:'POST'});break;case 'toggle':await api(`/api/accounts/${account.id}`,{method:'PATCH',body:JSON.stringify({enabled:!account.enabled})});break;case 'delete':if(!confirm('删除这个已停用且没有关联任务的账号？'))return;await api(`/api/accounts/${account.id}`,{method:'DELETE'});break;case 'web-session':state.webAccount=account.id;$('#webSessionForm').reset();$('#webSessionDialog').showModal();return;case 'web-query':state.webAccount=account.id;$('#queryResult').textContent='';$('#queryDialog').showModal();return;case 'profiles':openProfiles(account);return;case 'connect':toast('正在通过账号代理启动授权浏览器…');await api(`/api/accounts/${account.id}/connect`,{method:'POST'});state.browserAccount=account.id;$('#saveWebLogin').hidden=account.backend!=='web';$('#browserDialog').showModal();await browserSnapshot();browserTimer=setInterval(browserSnapshot,1800);return;}await refresh();});};
function openProfiles(account){state.profileAccount=account.id;const web=account.backend==='web';$('#profilesForm').elements.profiles.readOnly=web;$('#profilesForm [type=submit]').hidden=web;$('#insertTemplate').hidden=web;$('#fetchCatalog').disabled=web;$('#profilesForm').elements.profiles.value=JSON.stringify(account.profiles,null,2);$('#toolsJson').textContent=JSON.stringify(account.tools,null,2);$('#catalogJson').textContent=JSON.stringify(account.catalog,null,2);$('#catalogTool').innerHTML=account.tools.filter(t=>/(list|get|search|available)/i.test(t.name)&&/(model|capabilit|setting)/i.test(t.name)).map(t=>`<option>${esc(t.name)}</option>`).join('');$('#profilesDialog').showModal();}
$('#profilesForm').onsubmit=event=>{event.preventDefault();busy(event.target.querySelector('[type=submit]'),async()=>{const value=JSON.parse(event.target.elements.profiles.value);await api(`/api/accounts/${state.profileAccount}/profiles`,{method:'PUT',body:JSON.stringify(value)});await refresh();$('#profilesDialog').close();toast('模型配置已保存');});};
$('#insertTemplate').onclick=()=>{const input=$('#profilesForm').elements.profiles;try{const value=JSON.parse(input.value||'{}');value['doubao-seedance-2-0-260128']??=state.settings.profile_template;input.value=JSON.stringify(value,null,2);}catch(error){toast(error.message);}};
$('#fetchCatalog').onclick=event=>busy(event.currentTarget,async()=>{$('#catalogJson').textContent=JSON.stringify(await api(`/api/accounts/${state.profileAccount}/catalog`,{method:'POST',body:JSON.stringify({tool:$('#catalogTool').value,arguments:JSON.parse($('#catalogArguments').value)})}),null,2);});
let snapshotBusy=false;
async function browserSnapshot(){if(!state.browserAccount||snapshotBusy)return;snapshotBusy=true;try{const response=await fetch(`/api/accounts/${state.browserAccount}/browser`);if(!response.ok)throw new Error('授权窗口暂不可用');const blob=URL.createObjectURL(await response.blob());$('#browserImage').src=blob;if(browserBlob)URL.revokeObjectURL(browserBlob);browserBlob=blob;$('#browserStatus').textContent='画面更新 '+new Date().toLocaleTimeString();}catch(error){$('#browserStatus').textContent=error.message;}finally{snapshotBusy=false;}}
async function browserAction(value){try{await api(`/api/accounts/${state.browserAccount}/browser`,{method:'POST',body:JSON.stringify(value)});setTimeout(browserSnapshot,350);}catch(error){toast(error.message);}}
$('#browserImage').onclick=event=>{const box=event.target.getBoundingClientRect();browserAction({kind:'click',x:(event.clientX-box.left)*1100/box.width,y:(event.clientY-box.top)*760/box.height});};
$('#browserInput').onsubmit=event=>{event.preventDefault();const input=event.target.elements.text;browserAction({kind:'type',text:input.value});input.value='';};
document.querySelectorAll('[data-key]').forEach(button=>button.onclick=()=>browserAction({kind:'key',key:button.dataset.key}));
document.querySelectorAll('[data-scroll]').forEach(button=>button.onclick=()=>browserAction({kind:'scroll',y:Number(button.dataset.scroll)}));
async function closeBrowser(){clearInterval(browserTimer);const id=state.browserAccount;state.browserAccount=null;$('#browserDialog').close();if(id)await api(`/api/accounts/${id}/browser`,{method:'DELETE'});await refresh();}
$('#closeBrowser').onclick=()=>closeBrowser().catch(error=>toast(error.message));$('#browserDialog').addEventListener('cancel',event=>{event.preventDefault();closeBrowser().catch(error=>toast(error.message));});
$('#newTask').onclick=()=>{if(!$('#taskModel').options.length){toast('请先授权、检测并启用已配置模型的账号');return;}$('#taskDialog').showModal();};
$('#taskForm').onsubmit=event=>{event.preventDefault();busy(event.target.querySelector('[type=submit]'),async()=>{const values=Object.fromEntries(new FormData(event.target));values.duration=Number(values.duration);for(const key of ['image_urls','video_urls','audio_urls'])values[key]=values[key].split(/\r?\n/).map(v=>v.trim()).filter(Boolean);const body=JSON.stringify(values);await api('/api/tasks',{method:'POST',headers:{'Idempotency-Key':submissionKey(body)},body});pendingSubmission=null;state.page=0;$('#taskDialog').close();await refresh();toast('生成任务已提交');});};
$('#tasks').onclick=event=>{const detail=event.target.closest('[data-detail]');if(detail){openTaskDetail(state.tasks.find(t=>t.id===detail.dataset.detail));return;}const button=event.target.closest('[data-recover]');if(!button)return;const id=prompt('输入该账号对应的真实 Artlist generation ID，仅恢复查询，不重新生成：');if(id)busy(button,async()=>{await api(`/api/tasks/${button.dataset.recover}/recover`,{method:'POST',body:JSON.stringify({upstream_id:id})});await refresh();});};
$('#docsButton').onclick=()=>$('#docsDialog').showModal();$('#refreshButton').onclick=event=>busy(event.currentTarget,refresh);$('#logout').onclick=()=>api('/logout',{method:'POST'}).then(()=>location.href='/login').catch(error=>toast(error.message));
refresh().catch(error=>toast(error.message));configureRefresh();

$('#saveWebLogin').onclick=event=>busy(event.currentTarget,async()=>{await api(`/api/accounts/${state.browserAccount}/browser/save-session`,{method:'POST'});await refresh();toast('网页登录已保存，模型配置已更新');});
$('#webSessionForm').onsubmit=event=>{event.preventDefault();busy(event.target.querySelector('[type=submit]'),async()=>{const values=Object.fromEntries(new FormData(event.target));if(values.cookie)await api(`/api/accounts/${state.webAccount}/web-session`,{method:'PUT',body:JSON.stringify({cookie:values.cookie,user_agent:values.user_agent,team_id:values.team_id})});if(values.token)await api(`/api/accounts/${state.webAccount}/web-verification`,{method:'POST',body:JSON.stringify({token:values.token})});event.target.reset();$('#webSessionDialog').close();await refresh();toast('网页登录设置已保存');});};
$('#queryForm').onsubmit=event=>{event.preventDefault();busy(event.target.querySelector('[type=submit]'),async()=>{const id=event.target.elements.generation_id.value.trim();const result=await api(`/api/accounts/${state.webAccount}/web-tasks/${encodeURIComponent(id)}`);$('#queryResult').textContent=JSON.stringify(result,null,2);});};
