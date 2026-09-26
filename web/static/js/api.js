import { t } from './i18n.js';

// Настольная программа (docs/TOR-desktop.md): сервер ставит отметку на страницу.
// Один пользователь — нет входа, выхода, доступа и ссылок для пересылки (НП-3, НП-4).
export const isDesktop = document.documentElement.hasAttribute('data-desktop');

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
