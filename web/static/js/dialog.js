// Модальные окна на <dialog>: ввод, подтверждение и выбор в списке.
// Подтверждение обязательно для необратимых действий (ТЗ, раздел 5).
import { applyI18n, t } from './i18n.js';

function build(title, bodyNodes, { submitLabel, danger = false, cancelLabel } = {}) {
  const dialog = document.createElement('dialog');
  dialog.className = 'modal';

  const form = document.createElement('form');
  form.method = 'dialog';

  const heading = document.createElement('h2');
  heading.textContent = title;

  const body = document.createElement('div');
  body.className = 'modal-body';
  body.append(...bodyNodes);

  const actions = document.createElement('div');
  actions.className = 'modal-actions';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn';
  cancel.textContent = cancelLabel ?? t('common.cancel');
  cancel.addEventListener('click', () => dialog.close(''));
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  submit.textContent = submitLabel ?? t('common.save');
  submit.value = 'ok';
  actions.append(cancel, submit);

  const error = document.createElement('p');
  error.className = 'modal-error';

  form.append(heading, body, error, actions);
  dialog.append(form);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  return { dialog, form, submit, error };
}

function show(dialog, focusTarget) {
  dialog.showModal();
  focusTarget?.focus();
  focusTarget?.select?.();
}

// Окно с полями. Возвращает объект значений или null, если отменили.
// onSubmit может бросить ошибку: она покажется в окне, окно останется открытым.
export function formDialog({ title, fields, submitLabel, danger, onSubmit }) {
  const inputs = new Map();
  const nodes = fields.map((field) => {
    const label = document.createElement('label');
    label.className = 'modal-field';
    const caption = document.createElement('span');
    caption.textContent = field.label;
    let input;
    if (field.type === 'textarea') {
      input = document.createElement('textarea');
      input.rows = field.rows ?? 3;
    } else {
      input = document.createElement('input');
      input.type = field.type ?? 'text';
    }
    input.value = field.value ?? '';
    if (field.placeholder) input.placeholder = field.placeholder;
    if (field.required) input.required = true;
    if (field.maxLength) input.maxLength = field.maxLength;
    inputs.set(field.name, input);
    label.append(caption, input);
    if (field.hint) {
      const hint = document.createElement('span');
      hint.className = 'muted';
      hint.textContent = field.hint;
      label.append(hint);
    }
    return label;
  });

  const { dialog, form, submit, error } = build(title, nodes, { submitLabel, danger });
  return new Promise((resolve) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries([...inputs].map(([name, input]) => [name, input.value]));
      submit.disabled = true;
      error.textContent = '';
      try {
        const result = onSubmit ? await onSubmit(values) : values;
        dialog.close('ok');
        resolve(result ?? values);
      } catch (failure) {
        error.textContent = failure.message;
        submit.disabled = false;
      }
    });
    dialog.addEventListener('close', () => {
      if (dialog.returnValue !== 'ok') resolve(null);
    });
    show(dialog, inputs.values().next().value);
  });
}

export function confirmDialog({ title, text, submitLabel, danger = true }) {
  const paragraph = document.createElement('p');
  paragraph.className = 'modal-text';
  paragraph.textContent = text;
  const { dialog, form } = build(title, [paragraph], { submitLabel, danger });
  return new Promise((resolve) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      dialog.close('ok');
      resolve(true);
    });
    dialog.addEventListener('close', () => {
      if (dialog.returnValue !== 'ok') resolve(false);
    });
    show(dialog, dialog.querySelector('.btn-primary, .btn-danger'));
  });
}

// Окно со списком отметок: используется для доступа и состава группы.
export function pickerDialog({ title, sections, submitLabel, onSubmit, onReady, extraNodes = [] }) {
  const boxes = new Map();
  const nodes = [...extraNodes];
  for (const section of sections) {
    const group = document.createElement('div');
    group.className = 'modal-section';
    if (section.title) {
      const caption = document.createElement('p');
      caption.className = 'modal-section-title';
      caption.textContent = section.title;
      group.append(caption);
    }
    if (!section.items.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = section.emptyText ?? t('common.nothing');
      group.append(empty);
    }
    for (const item of section.items) {
      const label = document.createElement('label');
      label.className = 'check';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = item.checked;
      box.disabled = item.disabled ?? false;
      boxes.set(`${section.name}:${item.id}`, box);
      const caption = document.createElement('span');
      caption.textContent = item.label;
      label.append(box, caption);
      if (item.hint) {
        const hint = document.createElement('span');
        hint.className = 'muted';
        hint.textContent = item.hint;
        label.append(hint);
      }
      group.append(label);
    }
    nodes.push(group);
  }

  const { dialog, form, submit, error } = build(title, nodes, { submitLabel });
  const selection = () => {
    const result = {};
    for (const section of sections) result[section.name] = [];
    for (const [key, box] of boxes) {
      const [name, id] = key.split(':');
      if (box.checked) result[name].push(Number.isNaN(Number(id)) ? id : Number(id));
    }
    return result;
  };

  applyI18n(dialog);
  onReady?.(dialog);
  return new Promise((resolve) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      error.textContent = '';
      try {
        const values = selection();
        const result = onSubmit ? await onSubmit(values, dialog) : values;
        dialog.close('ok');
        resolve(result ?? values);
      } catch (failure) {
        error.textContent = failure.message;
        submit.disabled = false;
      }
    });
    dialog.addEventListener('close', () => {
      if (dialog.returnValue !== 'ok') resolve(null);
    });
    show(dialog, dialog.querySelector('input, button'));
  });
}

// Показ одноразового пароля: он больше нигде не хранится и не повторяется.
export function passwordDialog({ login, password }) {
  const text = document.createElement('p');
  text.className = 'modal-text';
  text.textContent = t('users.passwordOnce', { login });

  const row = document.createElement('div');
  row.className = 'password-row';
  const field = document.createElement('input');
  field.type = 'text';
  field.readOnly = true;
  field.value = password;
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'btn';
  copy.textContent = t('common.copy');
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(password);
      copy.textContent = t('common.copied');
    } catch {
      field.select(); // буфер обмена доступен не всегда
    }
  });
  row.append(field, copy);

  const { dialog, form } = build(t('users.passwordTitle'), [text, row], {
    submitLabel: t('common.close'),
    cancelLabel: null,
  });
  dialog.querySelector('.modal-actions .btn:not(.btn-primary)')?.remove();
  return new Promise((resolve) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      dialog.close('ok');
      resolve();
    });
    dialog.addEventListener('close', () => resolve());
    show(dialog, field);
  });
}
