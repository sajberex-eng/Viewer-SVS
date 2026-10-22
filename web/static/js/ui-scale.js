// Размер интерфейса вьювера: кнопки, подписи и панели. Препарат не масштабируется.
// Сам коэффициент ставит theme-init.js до отрисовки; здесь кнопка «Aa» и меню выбора.
import { t } from './i18n.js';

const STEPS = [0.9, 1, 1.1, 1.25, 1.5];

function percent(value) {
  return `${Math.round(value * 100)}%`;
}

function current() {
  const value = Number(getComputedStyle(document.documentElement).getPropertyValue('--ui-scale'));
  return STEPS.includes(value) ? value : 1;
}

function set(value) {
  document.documentElement.style.setProperty('--ui-scale', String(value));
  try { localStorage.setItem('viewer.uiScale', String(value)); } catch (e) { /* не записалось */ }
  // Размеры панелей поменялись: OpenSeadragon пересчитает половины сам, но ему нужен кадр
  window.dispatchEvent(new Event('resize'));
}

export function initUiScale() {
  const button = document.getElementById('btnScale');
  const menu = document.getElementById('scaleMenu');
  if (!button || !menu) return;
  const paint = () => {
    button.textContent = `Aa ${percent(current())}`;
    for (const item of menu.querySelectorAll('button')) {
      item.classList.toggle('is-active', Number(item.dataset.scale) === current());
    }
  };
  for (const step of STEPS) {
    const item = document.createElement('button');
    item.className = 'btn';
    item.type = 'button';
    item.dataset.scale = String(step);
    item.textContent = percent(step);
    item.addEventListener('click', () => {
      set(step);
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      paint();
    });
    menu.append(item);
  }
  button.title = t('ui.scale.tip');
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    button.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', () => {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
  });
  paint();
}

initUiScale();
