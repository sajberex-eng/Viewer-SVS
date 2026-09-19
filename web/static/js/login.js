import { api } from './api.js';
import { applyI18n, setLanguage, t } from './i18n.js';

setLanguage('ru');
applyI18n();

// Возврат только на адрес внутри сайта: «//host» и «/\host» браузер счёл бы внешними.
function nextUrl() {
  const next = new URLSearchParams(location.search).get('next') || '/';
  return /^\/(?![/\\])/.test(next) ? next : '/';
}

document.getElementById('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.getElementById('loginError');
  const submit = form.querySelector('[type="submit"]');
  error.textContent = '';
  submit.disabled = true;
  try {
    await api('/api/login', {
      method: 'POST',
      body: { login: form.login.value, password: form.password.value },
      redirectOn401: false,
    });
    location.href = nextUrl();
  } catch (failure) {
    error.textContent = failure.message || t('login.failed');
    submit.disabled = false;
  }
});
