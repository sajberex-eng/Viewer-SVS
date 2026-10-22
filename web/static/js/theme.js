// Переключатель светлой и тёмной темы. Сама тема ставится в theme-init.js;
// здесь только кнопки [data-theme-toggle] и запоминание выбора.
import { t, applyI18n } from './i18n.js';
import { iconMarkup } from './icons.js';

function current() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

function paint(button) {
  const dark = current() === 'dark';
  // Значок показывает то, на что тема переключится
  button.innerHTML = iconMarkup(dark ? 'sun' : 'moon');
  const label = t(dark ? 'theme.light' : 'theme.dark');
  button.title = label;
  button.setAttribute('aria-label', label);
  button.dataset.label = label;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('viewer.theme', theme); } catch (e) { /* не записалось — не страшно */ }
  document.querySelectorAll('[data-theme-toggle]').forEach(paint);
  document.dispatchEvent(new CustomEvent('themechange', { detail: theme }));
}

export function initTheme() {
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    paint(button);
    button.addEventListener('click', () => setTheme(current() === 'dark' ? 'light' : 'dark'));
  });
}

initTheme();
