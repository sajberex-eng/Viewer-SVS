import { t } from './i18n.js';

// Настольная программа (docs/TOR-desktop.md): сервер ставит отметку на страницу.
// Один пользователь — нет входа, выхода, доступа и ссылок для пересылки (НП-3, НП-4).
export const isDesktop = document.documentElement.hasAttribute('data-desktop');

// В программе у страницы нет консоли: ошибки уходят в журнал программы (logs/app.log),
// иначе сбой на компьютере заказчика не разобрать (замечания по macOS 2026-10-24).
if (isDesktop) {
  const report = (text) => {
    fetch('/api/desktop/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: location.pathname + location.search, text: String(text).slice(0, 4000) }),
      credentials: 'same-origin',
    }).catch(() => {});
  };
  window.addEventListener('error', (event) => {
    report(`${event.message} (${event.filename}:${event.lineno})`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    report(event.reason?.stack || event.reason?.message || event.reason);
  });
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function goToLogin() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/login?next=${next}`;
}

// Запрос к API. Истёкшая сессия отправляет на страницу входа с возвратом на текущий адрес.
export async function api(path, { method = 'GET', body, redirectOn401 = true } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError(0, t('common.networkError'));
  }
  if (response.status === 401 && redirectOn401) {
    goToLogin();
    return new Promise(() => {}); // страница уходит, продолжать нечего
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, typeof data.detail === 'string' ? data.detail : t('common.error'));
  }
  return data;
}

export async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}
