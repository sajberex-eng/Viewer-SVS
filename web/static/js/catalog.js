// Каталог: дерево папок, карточки сканов, загрузка и права доступа.
import { api, logout } from './api.js';
import { confirmDialog, formDialog, pickerDialog } from './dialog.js';
import { applyI18n, formatNumber, setLanguage, t } from './i18n.js';
import { SLIDE_EXTENSIONS, UploadQueue, isSlideFile } from './upload.js';

const $ = (id) => document.getElementById(id);
const MODES = ['admins', 'all', 'selected'];

let user = null;
let folders = [];
let slides = [];
let space = null;
let currentFolder = null; // null означает «все папки»
const expanded = new Set();

setLanguage('ru');
applyI18n();
main();

async function main() {
  user = await api('/api/me');
  $('userName').textContent = user.name || user.login;
  $('btnLogout').addEventListener('click', logout);
  $('btnProfile').addEventListener('click', changePassword);
  $('search').addEventListener('input', renderSlides);

  if (user.role === 'admin') {
    for (const id of ['linkUsers', 'linkJournal', 'btnNewFolder', 'btnUpload', 'btnAccess']) $(id).hidden = false;
    setUpAdmin();
  }
  await load();
  // После каталога: прерванным загрузкам нужны названия папок
  if (user.role === 'admin') await loadPending();
}

// silent: обновление в фоне, после загрузки скана. Надпись «Загрузка…» в этом
// случае только мигает поверх каталога, ничего не сообщая.
async function load({ silent = false } = {}) {
  if (!silent) setNote(t('common.loading'));
  try {
    const data = await api('/api/catalog');
    folders = data.folders;
    slides = data.slides;
    space = data.space;
    if (currentFolder !== null && !folders.some((f) => f.id === currentFolder)) currentFolder = null;
    renderTree();
    renderSlides();
    renderSpace();
  } catch (error) {
    setNote(error.message, true); // в том числе «хранилище недоступно»
  }
}

// ---------- дерево папок ----------

const childrenOf = (parentId) => folders.filter((f) => f.parent_id === parentId).sort(byName);
const byName = (a, b) => a.name.localeCompare(b.name, 'ru');
const folderById = (id) => folders.find((f) => f.id === id) ?? null;

function pathOf(folderId) {
  const chain = [];
  let current = folderById(folderId);
  while (current) {
    chain.unshift(current);
    current = current.parent_id === null ? null : folderById(current.parent_id);
  }
  return chain;
}

function renderTree() {
  const root = document.createElement('li');
  root.append(folderRow({ id: null, name: t('catalog.root') }, 0));
  const list = [root, ...childrenOf(null).flatMap((folder) => branch(folder, 1))];
  $('tree').replaceChildren(...list);
  if (user.role === 'admin') updateFolderButtons();
}

// В корне («Все папки») загружать и настраивать доступ некуда: кнопки
// неактивны, подсказка говорит, что сделать (ИН-4).
function updateFolderButtons() {
  const inRoot = currentFolder === null;
  $('btnUpload').disabled = inRoot;
  $('btnUpload').title = t(inRoot ? 'catalog.selectFolder' : 'catalog.upload.tip');
  $('btnAccess').disabled = inRoot;
  $('btnAccess').title = t(inRoot ? 'catalog.selectFolderAccess' : 'catalog.access.tip');
}

function branch(folder, depth) {
  const item = document.createElement('li');
  item.append(folderRow(folder, depth));
  const kids = childrenOf(folder.id);
  if (!kids.length || !expanded.has(folder.id)) return [item];
  return [item, ...kids.flatMap((kid) => branch(kid, depth + 1))];
}

function folderRow(folder, depth) {
  const row = document.createElement('div');
  row.className = 'tree-row';
  row.style.paddingLeft = `${6 + depth * 14}px`;
  if (folder.id === currentFolder) row.classList.add('is-current');

  const kids = folder.id === null ? [] : childrenOf(folder.id);
  const toggle = document.createElement('button');
  toggle.className = 'tree-toggle';
  toggle.type = 'button';
  toggle.textContent = kids.length ? (expanded.has(folder.id) ? '▾' : '▸') : '';
  toggle.disabled = !kids.length;
  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    if (expanded.has(folder.id)) expanded.delete(folder.id);
    else expanded.add(folder.id);
    renderTree();
  });

  const name = document.createElement('button');
  name.className = 'tree-name';
  name.type = 'button';
  name.textContent = folder.name;
  name.addEventListener('click', () => {
    currentFolder = folder.id;
    if (folder.id !== null) expanded.add(folder.id);
    renderTree();
    renderSlides();
  });

  row.append(toggle, name);
  if (user.role === 'admin' && folder.id !== null) {
    row.append(accessBadge(folder), folderActions(folder));
  }
  return row;
}

function folderActions(folder) {
  const actions = document.createElement('span');
  actions.className = 'tree-actions';
  actions.append(
    iconAction('✎', t('catalog.rename'), () => renameFolder(folder)),
    iconAction('🔒', t('catalog.access'), () => editAccess({ folder })),
    iconAction('×', t('catalog.deleteFolder'), () => deleteFolder(folder), 'is-danger'),
  );
  return actions;
}

function iconAction(glyph, tip, handler, extraClass = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `icon-btn ${extraClass}`;
  button.textContent = glyph;
  button.title = tip;
  button.setAttribute('aria-label', tip);
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

function accessBadge(item) {
  const badge = document.createElement('span');
  badge.className = 'badge';
  const mode = item.access_mode;
  if (!mode) {
    badge.textContent = '';
    return badge;
  }
  // Короткая подпись помещается в строку дерева, полная — в подсказке (ИН-5)
  badge.classList.add(`badge-${mode}`);
  badge.textContent = t(`access.badge.${mode}`);
  badge.title = t('access.badge.tip', { mode: t(`access.${mode}`) });
  return badge;
}

// ---------- сканы ----------

function visibleSlides() {
  const needle = $('search').value.trim().toLowerCase();
  const inFolder = currentFolder === null
    ? slides
    : slides.filter((slide) => descendants(currentFolder).has(slide.folder_id));
  return inFolder.filter((slide) => slide.title.toLowerCase().includes(needle));
}

function descendants(folderId) {
  const result = new Set([folderId]);
  const stack = [folderId];
  while (stack.length) {
    for (const kid of childrenOf(stack.pop())) {
      result.add(kid.id);
      stack.push(kid.id);
    }
  }
  return result;
}

let shownSignature = '';

function renderSlides() {
  const list = visibleSlides();
  // Карточки пересоздаются, только если список действительно изменился: иначе
  // миниатюры перезапрашиваются и заметно мигают при каждом обновлении.
  const signature = list.map((slide) => `${slide.id}:${slide.title}:${slide.stain ?? ''}`).join('|');
  if (signature !== shownSignature) {
    shownSignature = signature;
    $('grid').replaceChildren(...list.map(card));
  }
  renderCrumbs();
  if (list.length) setNote('');
  else if ($('search').value.trim()) setNote(t('catalog.nothingFound'));
  else if (!slides.length) setNote(user.role === 'admin' ? t('catalog.emptyAdmin') : t('catalog.empty'));
  else setNote(t('catalog.emptyFolder'));
}

function renderCrumbs() {
  const chain = currentFolder === null ? [] : pathOf(currentFolder);
  const nodes = [crumb(t('catalog.root'), null)];
  for (const folder of chain) nodes.push(separator(), crumb(folder.name, folder.id));
  $('crumbs').replaceChildren(...nodes);
}

function crumb(text, folderId) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'crumb';
  button.textContent = text;
  if (folderId === currentFolder) button.setAttribute('aria-current', 'true');
  button.addEventListener('click', () => {
    currentFolder = folderId;
    renderTree();
    renderSlides();
  });
  return button;
}

function separator() {
  const span = document.createElement('span');
  span.className = 'crumb-sep';
  span.textContent = '›';
  return span;
}

function sizeText(bytes) {
  if (bytes >= 1e9) return `${formatNumber(bytes / 1e9, 2)} ГБ`;
  if (bytes >= 1e6) return `${formatNumber(bytes / 1e6)} МБ`;
  return `${formatNumber(bytes / 1e3)} КБ`;
}

function card(slide) {
  const item = document.createElement('li');
  item.className = 'slide-item';

  const link = document.createElement('a');
  link.className = 'slide-card';
  link.href = `/viewer?slide=${encodeURIComponent(slide.id)}`;

  const image = document.createElement('img');
  image.src = `/api/slides/${encodeURIComponent(slide.id)}/thumbnail.jpg`;
  image.alt = '';
  image.loading = 'lazy';

  const title = document.createElement('span');
  title.className = 'slide-card-title';
  title.textContent = slide.title;

  const details = document.createElement('span');
  details.className = 'muted';
  const scan = slide.objective ? t('catalog.scan', { objective: `${formatNumber(slide.objective)}×` }) : '';
  details.textContent = [slide.stain, scan, sizeText(slide.size_bytes)].filter(Boolean).join(' · ');

  link.append(image, title, details);
  item.append(link);
  if (user.role === 'admin') item.append(slideMenu(slide));
  return item;
}

function slideMenu(slide) {
  const menu = document.createElement('div');
  menu.className = 'slide-actions';
  menu.append(
    actionButton(t('catalog.edit'), () => editSlide(slide)),
    actionButton(t('catalog.access'), () => editAccess({ slide })),
    actionButton(t('catalog.move'), () => moveSlide(slide)),
    actionButton(t('common.delete'), () => removeSlide(slide), 'is-danger'),
  );
  return menu;
}

function actionButton(text, handler, extraClass = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `link-btn ${extraClass}`;
  button.textContent = text;
  button.addEventListener('click', handler);
  return button;
}

function setNote(text, isError = false) {
  $('note').textContent = text;
  $('note').classList.toggle('is-error', isError);
}

function renderSpace() {
  if (!space) return;
  const node = $('space');
  node.hidden = false;
  const free = sizeText(space.free_bytes);
  const low = space.free_bytes < space.warn_below_bytes;
  node.textContent = low
    ? t('catalog.spaceLow', { free })
    : t('catalog.space', { free, total: sizeText(space.total_bytes) });
  node.classList.toggle('is-warning', low);
}

// ---------- действия администратора ----------

function setUpAdmin() {
  $('btnNewFolder').addEventListener('click', createFolder);
  $('btnAccess').addEventListener('click', () => {
    const folder = folderById(currentFolder);
    if (folder) editAccess({ folder });
  });
  $('fileInput').accept = SLIDE_EXTENSIONS.join(',');
  $('btnUpload').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', (event) => {
    enqueue([...event.target.files]);
    event.target.value = '';
  });
  setUpDropZone();
  setUpUploads();
}

async function createFolder() {
  const parent = folderById(currentFolder);
  const values = await formDialog({
    title: parent ? t('folder.createIn', { folder: parent.name }) : t('folder.createTitle'),
    submitLabel: t('common.create'),
    fields: [{
      name: 'name', label: t('folder.nameLabel'), hint: t('folder.nameHint'), required: true, maxLength: 100,
    }],
    onSubmit: (data) => api('/api/folders', { method: 'POST', body: { name: data.name, parent_id: currentFolder } }),
  });
  if (!values) return;
  if (currentFolder !== null) expanded.add(currentFolder);
  currentFolder = values.id;
  await load();
}

async function renameFolder(folder) {
  const done = await formDialog({
    title: t('folder.renameTitle'),
    fields: [{ name: 'name', label: t('folder.nameLabel'), value: folder.name, required: true, maxLength: 100 }],
    onSubmit: (data) => api(`/api/folders/${folder.id}`, { method: 'PATCH', body: { name: data.name } }),
  });
  if (done) await load();
}

async function deleteFolder(folder) {
  const counts = await api(`/api/folders/${folder.id}/contents`);
  const ok = await confirmDialog({
    title: t('folder.deleteTitle', { folder: folder.name }),
    text: t('folder.deleteText', counts),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  await api(`/api/folders/${folder.id}`, { method: 'DELETE' });
  currentFolder = folder.parent_id;
  await load();
}

async function editSlide(slide) {
  const done = await formDialog({
    title: t('slide.editTitle'),
    fields: [
      { name: 'title', label: t('slide.title'), value: slide.title, required: true, maxLength: 100 },
      { name: 'stain', label: t('slide.stain'), value: slide.stain ?? '' },
      {
        name: 'note', label: t('slide.note'), type: 'textarea', value: slide.note ?? '',
        hint: slide.original_name ? t('slide.originalName', { name: slide.original_name }) : '',
      },
    ],
    onSubmit: (data) => api(`/api/slides/${slide.id}`, { method: 'PATCH', body: data }),
  });
  if (done) await load();
}

async function moveSlide(slide) {
  const target = await pickFolder(t('slide.moveTitle'), t('slide.moveHint'), slide.folder_id);
  if (target === null) return;
  await api(`/api/slides/${slide.id}`, { method: 'PATCH', body: { folder_id: target } });
  await load();
}

async function removeSlide(slide) {
  const ok = await confirmDialog({
    title: t('slide.deleteTitle', { title: slide.title }),
    text: t('slide.deleteText'),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  await api(`/api/slides/${slide.id}`, { method: 'DELETE' });
  await load();
}

// Выбор папки одним списком: дерево здесь мельче, чем польза от простоты.
async function pickFolder(title, hint, exclude = null) {
  const options = folders.filter((folder) => folder.id !== exclude);
  const result = await pickerDialog({
    title,
    submitLabel: t('common.save'),
    sections: [{
      name: 'folder',
      title: t('folder.moveTo'),
      items: options.map((folder) => ({
        id: folder.id,
        label: pathOf(folder.id).map((f) => f.name).join(' › '),
        checked: false,
      })),
    }],
    extraNodes: hint ? [hintNode(hint)] : [],
    onSubmit: (values) => {
      if (values.folder.length !== 1) throw new Error(t('folder.moveTo'));
      return values;
    },
  });
  return result ? result.folder[0] : null;
}

function hintNode(text) {
  const node = document.createElement('p');
  node.className = 'muted';
  node.textContent = text;
  return node;
}

async function editAccess({ folder, slide }) {
  const target = folder ?? slide;
  const query = folder ? `folder_id=${folder.id}` : `slide_id=${encodeURIComponent(slide.id)}`;
  const [current, groupsData, users] = await Promise.all([
    api(`/api/access?${query}`),
    api('/api/groups'),
    api('/api/users'),
  ]);

  const modeRow = document.createElement('div');
  modeRow.className = 'modal-field';
  const caption = document.createElement('span');
  caption.textContent = t('access.mode');
  const select = document.createElement('select');
  const canInherit = folder ? folder.parent_id !== null : true;
  if (canInherit) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = t('access.inherit', { mode: t(`access.${current.inherited_mode ?? 'admins'}`) });
    select.append(option);
  }
  for (const mode of MODES) {
    const option = document.createElement('option');
    option.value = mode;
    option.textContent = t(`access.${mode}`);
    select.append(option);
  }
  select.value = current.mode ?? '';
  modeRow.append(caption, select);

  const note = hintNode(t('access.adminsAlways'));
  // Списки нужны только в режиме «Выбранные»: в остальных они сбивают с толку
  const sectionsWrap = () => select.value === 'selected';

  const done = await pickerDialog({
    title: t('access.title', { name: folder ? folder.name : slide.title }),
    extraNodes: [modeRow, note],
    sections: [
      {
        name: 'groups',
        title: t('access.groups'),
        items: groupsData.groups.map((group) => ({
          id: group.id,
          label: group.name,
          checked: current.group_ids.includes(group.id),
        })),
      },
      {
        name: 'users',
        title: t('access.users'),
        items: users.filter((row) => row.role !== 'admin').map((row) => ({
          id: row.id,
          label: row.name || row.login,
          checked: current.user_ids.includes(row.id),
        })),
      },
    ],
    onReady: (dialog) => {
      const toggle = () => {
        for (const node of dialog.querySelectorAll('.modal-section')) node.hidden = !sectionsWrap();
      };
      select.addEventListener('change', toggle);
      toggle();
    },
    onSubmit: (values) => api('/api/access', {
      method: 'POST',
      body: {
        folder_id: folder ? folder.id : null,
        slide_id: slide ? slide.id : null,
        mode: select.value || null,
        group_ids: sectionsWrap() ? values.groups : [],
        user_ids: sectionsWrap() ? values.users : [],
      },
    }),
  });
  if (done) await load();
}

// ---------- загрузка ----------

const queue = new UploadQueue(renderUploads);

function enqueue(files) {
  const accepted = files.filter((file) => isSlideFile(file.name));
  const rejected = files.filter((file) => !accepted.includes(file));
  if (rejected.length) {
    setNote(t('upload.wrongFormat', { names: rejected.map((f) => f.name).join(', '), formats: SLIDE_EXTENSIONS.join(' ') }), true);
  }
  if (!accepted.length) return;
  if (currentFolder === null) {
    setNote(t('catalog.selectFolder'), true);
    return;
  }
  queue.add(accepted, currentFolder);
}

function setUpDropZone() {
  const zone = $('dropZone');
  let depth = 0;
  zone.addEventListener('dragenter', (event) => {
    if (!event.dataTransfer?.types.includes('Files')) return;
    depth += 1;
    const folder = folderById(currentFolder);
    $('dropHint').textContent = folder
      ? t('catalog.dropHere', { folder: folder.name })
      : t('catalog.selectFolder');
    $('dropHint').hidden = false;
  });
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    // В корне курсор показывает «сюда нельзя», подсказка уже видна
    if (currentFolder === null) event.dataTransfer.dropEffect = 'none';
  });
  zone.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) $('dropHint').hidden = true;
  });
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    depth = 0;
    $('dropHint').hidden = true;
    enqueue([...event.dataTransfer.files]);
  });
}

function setUpUploads() {
  $('btnClearUploads').addEventListener('click', () => queue.clearFinished());
}

// Прерванные загрузки прошлого сеанса: сервер помнит принятые части, но файл
// заново выбирает человек — браузер не может открыть его с диска сам.
let pending = [];

async function loadPending() {
  try {
    pending = await api('/api/uploads');
  } catch {
    pending = [];
  }
  renderUploads(queue.tasks);
}

function resumeUpload(state) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = SLIDE_EXTENSIONS.join(',');
  input.addEventListener('change', () => {
    const file = input.files[0];
    if (!file) return;
    if (file.name !== state.original_name || file.size !== state.size) {
      setNote(t('upload.wrongFile', { name: state.original_name }), true);
      return;
    }
    setNote('');
    pending = pending.filter((row) => row.id !== state.id);
    queue.add([file], state.folder_id, state.received);
  });
  input.click();
}

async function dropPending(state) {
  const ok = await confirmDialog({
    title: t('upload.dropTitle'),
    text: t('upload.dropText', { done: Math.round((state.received / state.size) * 100) }),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  await api(`/api/uploads/${state.id}`, { method: 'DELETE' });
  pending = pending.filter((row) => row.id !== state.id);
  renderUploads(queue.tasks);
  await load();
}

let reloadTimer = null;
const uploadRows = new Map(); // ключ задачи или прерванной загрузки -> её строка

// Строки не пересоздаются на каждую принятую часть: иначе панель дёргается,
// меняет высоту и весь каталог над ней подпрыгивает.
function renderUploads(tasks) {
  const items = [
    ...pending.map((state) => ({ key: `p:${state.id}`, build: () => pendingRow(state), fill: (row) => fillPending(row, state) })),
    ...tasks.map((task) => ({ key: `t:${task.id}`, build: () => uploadRow(task), fill: (row) => fillUpload(row, task) })),
  ];
  const list = $('uploadList');
  $('uploadPanel').hidden = !items.length;

  for (const [key, row] of uploadRows) {
    if (!items.some((item) => item.key === key)) {
      row.remove();
      uploadRows.delete(key);
    }
  }
  items.forEach((item, index) => {
    let row = uploadRows.get(item.key);
    if (!row) {
      row = item.build();
      uploadRows.set(item.key, row);
      list.append(row);
    }
    item.fill(row);
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] ?? null);
  });

  $('btnClearUploads').hidden = !tasks.some((task) => task.status !== 'running' && task.status !== 'waiting');
  if (tasks.some((task) => task.status === 'done') && !reloadTimer) {
    reloadTimer = setTimeout(() => {
      reloadTimer = null;
      loadPending();
      load({ silent: true });
    }, 400);
  }
}

// Пустая строка панели: содержимое проставляется отдельно, чтобы обновлять
// её потом без пересоздания узлов.
function emptyRow() {
  const item = document.createElement('li');
  item.className = 'upload-row';
  const name = document.createElement('span');
  name.className = 'upload-name';
  const bar = document.createElement('div');
  bar.className = 'upload-bar';
  const fill = document.createElement('div');
  fill.className = 'upload-fill';
  bar.append(fill);
  const label = document.createElement('span');
  label.className = 'upload-state';
  const actions = document.createElement('span');
  actions.className = 'upload-actions';
  item.append(name, bar, label, actions);
  return item;
}

function pendingRow(state) {
  const item = emptyRow();
  item.classList.add('is-paused');
  item.querySelector('.upload-actions').append(
    actionButton(t('upload.resume'), () => resumeUpload(state)),
    actionButton(t('common.delete'), () => dropPending(state), 'is-danger'),
  );
  return item;
}

function fillPending(row, state) {
  const folder = folderById(state.folder_id);
  const done = Math.round((state.received / state.size) * 100);
  row.querySelector('.upload-name').textContent = folder
    ? `${state.original_name} → ${folder.name}`
    : state.original_name;
  row.querySelector('.upload-fill').style.width = `${done}%`;
  row.querySelector('.upload-state').textContent = t('upload.paused', {
    done,
    left: sizeText(state.size - state.received),
  });
}

function uploadRow(task) {
  const item = emptyRow();
  // Имя файла видит только администратор; на сервер оно уходит отдельно
  item.querySelector('.upload-name').textContent = task.file.name;
  const cancel = document.createElement('button');
  cancel.className = 'icon-btn';
  cancel.type = 'button';
  cancel.textContent = '×';
  cancel.title = t('upload.cancel.tip');
  cancel.addEventListener('click', () => task.cancel());
  // Файл ещё открыт на странице: повтор без выбора файла (ЗГ-2)
  const retry = actionButton(t('upload.retry'), () => task.retry(), 'upload-retry');
  item.querySelector('.upload-actions').append(retry, cancel);
  return item;
}

function fillUpload(row, task) {
  row.className = `upload-row is-${task.status}`;
  row.querySelector('.upload-fill').style.width = `${Math.round(task.progress * 100)}%`;
  const label = row.querySelector('.upload-state');
  if (task.status === 'done') label.textContent = t('upload.done');
  else if (task.status === 'canceled') label.textContent = t('upload.canceled');
  else if (task.status === 'error') label.textContent = task.detail;
  else if (task.status === 'waiting') label.textContent = t('upload.waiting');
  else label.textContent = task.detail || `${Math.round(task.progress * 100)}%`;
  label.classList.toggle('is-error', task.status === 'error');
  const running = task.status === 'running' || task.status === 'waiting';
  row.querySelector('.upload-actions .icon-btn').hidden = !running;
  row.querySelector('.upload-retry').hidden = task.status !== 'error';
}

// ---------- профиль ----------

async function changePassword() {
  const done = await formDialog({
    title: t('profile.title'),
    fields: [
      { name: 'current_password', label: t('profile.current'), type: 'password', required: true },
      { name: 'new_password', label: t('profile.new'), type: 'password', required: true, hint: t('profile.hint') },
    ],
    onSubmit: (values) => api('/api/password', { method: 'POST', body: values }),
  });
  if (done) setNote(t('profile.done'));
}
