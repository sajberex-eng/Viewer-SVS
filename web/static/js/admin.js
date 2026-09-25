// Экраны администратора: учётные записи, группы и журнал действий.
import { api, logout } from './api.js';
import { confirmDialog, formDialog, passwordDialog, pickerDialog } from './dialog.js';
import { applyI18n, formatDateTime, setLanguage, t } from './i18n.js';

const $ = (id) => document.getElementById(id);
const JOURNAL_PAGE = 200;

const TABS = {
  users: { button: 'tabUsers', section: 'usersSection', load: () => loadUsers() },
  groups: { button: 'tabGroups', section: 'groupsSection', load: () => loadGroups() },
  journal: { button: 'tabJournal', section: 'journalSection', load: () => loadJournal(true) },
  settings: { button: 'tabSettings', section: 'settingsSection', load: () => loadSettings() },
};

let me = null;
let users = [];
let groups = [];
let administrators = null;
let journalOffset = 0;

setLanguage('ru');
applyI18n();
main();

async function main() {
  me = await api('/api/me');
  if (me.role !== 'admin') {
    location.href = '/';
    return;
  }
  $('userName').textContent = me.name || me.login;
  $('btnLogout').addEventListener('click', logout);
  $('btnAddUser').addEventListener('click', addUser);
  $('btnAddGroup').addEventListener('click', addGroup);
  $('btnMore').addEventListener('click', () => loadJournal(false));
  $('btnExport').addEventListener('click', exportJournal);
  $('journalAction').addEventListener('change', () => loadJournal(true));
  $('aiSave').addEventListener('click', () => saveAiKey({ key: $('aiKey').value }));
  $('aiRemove').addEventListener('click', () => saveAiKey({ key: '' }));
  $('geminiSave').addEventListener('click', () => saveAiKey({ gemini_key: $('geminiKey').value }));
  $('geminiRemove').addEventListener('click', () => saveAiKey({ gemini_key: '' }));
  for (const [name, tab] of Object.entries(TABS)) {
    $(tab.button).addEventListener('click', () => openTab(name));
  }
  openTab(location.hash === '#journal' ? 'journal' : 'users');
}

function openTab(name) {
  for (const [key, tab] of Object.entries(TABS)) {
    $(tab.section).hidden = key !== name;
    $(tab.button).classList.toggle('is-active', key === name);
  }
  setNote('');
  TABS[name].load();
}

// Ключ ИИ вводится здесь, а не пересылается разработчику: хранится только на сервере
async function loadSettings() {
  try {
    showAiStatus(await api('/api/settings/ai'));
  } catch (error) {
    setNote(error.message, true);
  }
}

function showAiStatus(state) {
  $('aiStatus').textContent = state.from_env ? t('settings.ai.fromEnv')
    : state.configured ? t('settings.ai.configured', { model: state.model }) : t('settings.ai.missing');
  $('aiRemove').hidden = !state.configured || state.from_env;
  $('geminiStatus').textContent = state.gemini_from_env ? t('settings.ai.fromEnv')
    : state.gemini_configured ? t('settings.gemini.configured', { model: state.gemini_model }) : t('settings.gemini.missing');
  $('geminiRemove').hidden = !state.gemini_configured || state.gemini_from_env;
}

// body — { key } для Anthropic или { gemini_key } для запасного Gemini; пустая строка удаляет ключ
async function saveAiKey(body) {
  const value = body.key ?? body.gemini_key ?? '';
  try {
    const state = await api('/api/settings/ai', { method: 'PUT', body });
    $('aiKey').value = '';
    $('geminiKey').value = '';
    showAiStatus(state);
    setNote(t(value.trim() ? 'settings.ai.saved' : 'settings.ai.removed'));
  } catch (error) {
    setNote(error.message, true);
  }
}

function setNote(text, isError = false) {
  $('note').textContent = text;
  $('note').classList.toggle('is-error', isError);
}

function cell(text, className = '') {
  const td = document.createElement('td');
  td.textContent = text ?? '';
  if (className) td.className = className;
  return td;
}

function actionButton(text, handler, extraClass = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `link-btn ${extraClass}`;
  button.textContent = text;
  button.addEventListener('click', handler);
  return button;
}

// ---------- пользователи ----------

async function loadUsers() {
  [users, { groups, administrators }] = await Promise.all([api('/api/users'), api('/api/groups')]);
  renderUsers();
}

function groupNames(ids) {
  return ids.map((id) => groups.find((group) => group.id === id)?.name).filter(Boolean).join(', ');
}

function renderUsers() {
  const rows = users.map((row) => {
    const tr = document.createElement('tr');
    if (row.status === 'blocked') tr.classList.add('is-muted');
    const expired = row.expires_at && row.expires_at < new Date().toISOString().slice(0, 10);
    tr.append(
      cell(row.login),
      cell(row.name),
      cell(t(`users.role.${row.role}`)),
      cell(row.role === 'admin' ? '—' : groupNames(row.group_ids)),
      cell(t(`users.status.${row.status}`)),
      cell(row.expires_at || t('common.unlimited'), expired ? 'is-error' : ''),
      cell(row.last_login_at ? formatDateTime(row.last_login_at) : t('common.never')),
    );
    const actions = document.createElement('td');
    actions.className = 'row-actions';
    actions.append(
      actionButton(t('catalog.edit'), () => editUser(row)),
      actionButton(t('users.resetPassword'), () => resetPassword(row)),
      actionButton(t('users.preview'), () => previewAccess(row)),
      actionButton(
        row.status === 'active' ? t('users.block') : t('users.unblock'),
        () => toggleBlock(row),
      ),
      actionButton(t('common.delete'), () => removeUser(row), 'is-danger'),
    );
    tr.append(actions);
    return tr;
  });
  $('usersBody').replaceChildren(...rows);
}

function groupPickerItems(selected) {
  return groups.map((group) => ({ id: group.id, label: group.name, checked: selected.includes(group.id) }));
}

async function addUser() {
  const created = await formDialog({
    title: t('users.add'),
    submitLabel: t('common.create'),
    fields: [
      { name: 'login', label: t('users.login'), required: true },
      { name: 'name', label: t('users.name') },
      { name: 'password', label: t('users.password'), hint: t('users.passwordHint') },
      { name: 'expires_at', label: t('users.expires'), placeholder: '2026-12-31', hint: t('users.expiresHint') },
    ],
    onSubmit: (values) => api('/api/users', {
      method: 'POST',
      body: {
        login: values.login,
        name: values.name,
        role: 'user',
        expires_at: values.expires_at || null,
        password: values.password || null,
      },
    }),
  });
  if (!created) return;
  await passwordDialog({ login: created.login, password: created.password });
  await loadUsers();
}

async function editUser(row) {
  const isSelf = row.login === me.login;
  const values = await pickerDialog({
    title: `${row.login} · ${t('catalog.edit')}`,
    sections: row.role === 'admin' ? [] : [{ name: 'groups', title: t('users.groupsOf'), items: groupPickerItems(row.group_ids) }],
    extraNodes: [buildUserFields(row, isSelf)],
    onSubmit: (picked) => api(`/api/users/${row.id}`, {
      method: 'PATCH',
      body: {
        name: $('userNameField').value,
        role: $('userRoleField').value,
        expires_at: $('userExpiresField').value || null,
        clear_expiry: !$('userExpiresField').value,
        group_ids: picked.groups ?? [],
      },
    }),
  });
  if (values) await loadUsers();
}

function buildUserFields(row, isSelf) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-body';

  const nameField = document.createElement('label');
  nameField.className = 'modal-field';
  const nameCaption = document.createElement('span');
  nameCaption.textContent = t('users.name');
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.id = 'userNameField';
  nameInput.value = row.name;
  nameField.append(nameCaption, nameInput);

  const roleField = document.createElement('label');
  roleField.className = 'modal-field';
  const roleCaption = document.createElement('span');
  roleCaption.textContent = t('users.role');
  const roleSelect = document.createElement('select');
  roleSelect.id = 'userRoleField';
  for (const role of ['user', 'admin']) {
    const option = document.createElement('option');
    option.value = role;
    option.textContent = t(`users.role.${role}`);
    roleSelect.append(option);
  }
  roleSelect.value = row.role;
  roleSelect.disabled = isSelf; // себя не понижаем: иначе можно остаться без администратора
  roleField.append(roleCaption, roleSelect);

  const expiresField = document.createElement('label');
  expiresField.className = 'modal-field';
  const expiresCaption = document.createElement('span');
  expiresCaption.textContent = t('users.expires');
  const expiresInput = document.createElement('input');
  expiresInput.type = 'text';
  expiresInput.id = 'userExpiresField';
  expiresInput.placeholder = '2026-12-31';
  expiresInput.value = row.expires_at ?? '';
  const expiresHint = document.createElement('span');
  expiresHint.className = 'muted';
  expiresHint.textContent = t('users.expiresHint');
  expiresField.append(expiresCaption, expiresInput, expiresHint);

  wrap.append(nameField, roleField, expiresField);
  return wrap;
}

async function toggleBlock(row) {
  const status = row.status === 'active' ? 'blocked' : 'active';
  try {
    await api(`/api/users/${row.id}`, { method: 'PATCH', body: { status } });
    await loadUsers();
  } catch (error) {
    setNote(error.message, true);
  }
}

async function resetPassword(row) {
  const result = await formDialog({
    title: `${t('users.resetPassword')}: ${row.login}`,
    submitLabel: t('users.resetPassword'),
    fields: [{ name: 'password', label: t('users.password'), hint: t('users.passwordHint') }],
    onSubmit: (values) => api(`/api/users/${row.id}/password`, {
      method: 'POST',
      body: { password: values.password || null },
    }),
  });
  if (!result) return;
  await passwordDialog(result);
  await loadUsers();
}

async function removeUser(row) {
  const ok = await confirmDialog({
    title: t('users.deleteTitle', { login: row.login }),
    text: t('users.deleteText'),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  try {
    await api(`/api/users/${row.id}`, { method: 'DELETE' });
    await loadUsers();
  } catch (error) {
    setNote(error.message, true);
  }
}

async function previewAccess(row) {
  const data = await api(`/api/access/preview/${row.id}`);
  const lines = [];
  if (!data.folders.length && !data.slides.length) lines.push(t('users.previewNothing'));
  else {
    lines.push(t('users.previewFolders', { list: data.folders.map((f) => f.name).join(', ') || '—' }));
    lines.push(t('users.previewSlides', { list: data.slides.map((s) => s.title).join(', ') || '—' }));
  }
  await confirmDialog({
    title: t('users.previewTitle', { login: row.login }),
    text: lines.join('\n'),
    submitLabel: t('common.close'),
    danger: false,
  });
}

// ---------- группы ----------

async function loadGroups() {
  [{ groups, administrators }, users] = await Promise.all([api('/api/groups'), api('/api/users')]);
  renderGroups();
}

function renderGroups() {
  const items = groups.map((group) => {
    const item = document.createElement('li');
    item.className = 'group-row';
    const name = document.createElement('span');
    name.className = 'group-name';
    name.textContent = group.name;
    const count = document.createElement('span');
    count.className = 'muted';
    count.textContent = t('groups.members', { n: group.member_ids.length });
    const actions = document.createElement('span');
    actions.className = 'row-actions';
    actions.append(
      actionButton(t('groups.editMembers'), () => editMembers(group)),
      actionButton(t('catalog.rename'), () => renameGroup(group)),
      actionButton(t('common.delete'), () => removeGroup(group), 'is-danger'),
    );
    item.append(name, count, actions);
    return item;
  });

  const builtin = document.createElement('li');
  builtin.className = 'group-row is-builtin';
  const name = document.createElement('span');
  name.className = 'group-name';
  name.textContent = administrators.name;
  const hint = document.createElement('span');
  hint.className = 'muted';
  hint.textContent = `${t('groups.members', { n: administrators.member_ids.length })} · ${t('groups.builtin')}`;
  builtin.append(name, hint);
  $('groupsList').replaceChildren(...items, builtin);
}

async function addGroup() {
  const done = await formDialog({
    title: t('groups.add'),
    submitLabel: t('common.create'),
    fields: [{ name: 'name', label: t('groups.nameLabel'), required: true, maxLength: 100 }],
    onSubmit: (values) => api('/api/groups', { method: 'POST', body: values }),
  });
  if (done) await loadGroups();
}

async function renameGroup(group) {
  const done = await formDialog({
    title: t('groups.renameTitle'),
    fields: [{ name: 'name', label: t('groups.nameLabel'), value: group.name, required: true, maxLength: 100 }],
    onSubmit: (values) => api(`/api/groups/${group.id}`, { method: 'PATCH', body: values }),
  });
  if (done) await loadGroups();
}

async function editMembers(group) {
  const done = await pickerDialog({
    title: t('groups.membersTitle', { name: group.name }),
    sections: [{
      name: 'users',
      title: t('users.heading'),
      emptyText: t('users.empty'),
      items: users.filter((row) => row.role !== 'admin').map((row) => ({
        id: row.id,
        label: row.name || row.login,
        checked: group.member_ids.includes(row.id),
      })),
    }],
    onSubmit: (values) => api(`/api/groups/${group.id}/members`, { method: 'PUT', body: { user_ids: values.users } }),
  });
  if (done) await loadGroups();
}

async function removeGroup(group) {
  const ok = await confirmDialog({
    title: t('groups.deleteTitle', { name: group.name }),
    text: t('groups.deleteText'),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  await api(`/api/groups/${group.id}`, { method: 'DELETE' });
  await loadGroups();
}

// ---------- журнал ----------

const ACTIONS = [
  'login.ok', 'login.failed', 'logout', 'password.changed',
  'slide.open', 'slide.label', 'slide.upload', 'slide.update', 'slide.move', 'slide.delete',
  'folder.create', 'folder.rename', 'folder.move', 'folder.delete', 'access.change',
  'user.create', 'user.update', 'user.delete', 'user.reset_password',
  'group.create', 'group.rename', 'group.members', 'group.delete', 'upload.start',
];

let journalRows = [];

function fillActionFilter() {
  const select = $('journalAction');
  if (select.options.length) return;
  const all = document.createElement('option');
  all.value = '';
  all.textContent = t('journal.all');
  select.append(all);
  for (const action of ACTIONS) {
    const option = document.createElement('option');
    option.value = action;
    option.textContent = t(`action.${action}`);
    select.append(option);
  }
}

async function loadJournal(reset) {
  fillActionFilter();
  if (reset) {
    journalOffset = 0;
    journalRows = [];
  }
  const action = $('journalAction').value;
  const query = new URLSearchParams({ limit: JOURNAL_PAGE, offset: journalOffset });
  if (action) query.set('action', action);
  const batch = await api(`/api/journal?${query}`);
  journalRows = journalRows.concat(batch);
  journalOffset += batch.length;
  $('btnMore').hidden = batch.length < JOURNAL_PAGE;
  renderJournal();
}

function renderJournal() {
  const rows = journalRows.map((row) => {
    const tr = document.createElement('tr');
    tr.append(
      cell(formatDateTime(row.at)),
      cell(row.actor),
      cell(t(`action.${row.action}`)),
      cell(row.object_type ? `${row.object_type} ${row.object_id ?? ''}`.trim() : ''),
      cell(row.detail),
      cell(row.ip),
    );
    return tr;
  });
  $('journalBody').replaceChildren(...rows);
  if (!journalRows.length) setNote(t('journal.empty'));
}

// Время в CSV местное, в сортируемом виде «2026-09-19 19:42:05»;
// пояс один на весь файл и указан в заголовке столбца (ИН-1).
function localTimestamp(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso ?? '';
  const pad = (number) => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function utcOffsetLabel() {
  const minutes = -new Date().getTimezoneOffset();
  const abs = Math.abs(minutes);
  const pad = (number) => String(number).padStart(2, '0');
  return `UTC${minutes < 0 ? '-' : '+'}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

function exportJournal() {
  const header = ['at', 'actor', 'action', 'object_type', 'object_id', 'detail', 'ip'];
  const escape = (value) => `"${String(value ?? '').replaceAll('"', '""')}"`;
  const titles = header.map((key) => (key === 'at' ? `at (${utcOffsetLabel()})` : key));
  const lines = [titles.map(escape).join(';')];
  for (const row of journalRows) {
    lines.push(header.map((key) => escape(key === 'at' ? localTimestamp(row.at) : row[key])).join(';'));
  }
  // BOM: иначе Excel читает кириллицу как кракозябры
  const blob = new Blob([`﻿${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `journal-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}
