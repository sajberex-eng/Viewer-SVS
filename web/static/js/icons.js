// Значки верхней панели вьювера (В-1).
//
// Рисуются линиями в один цвет (currentColor), поэтому наследуют цвет кнопки и
// одинаково смотрятся в обычном и в выключенном состоянии. Шрифтов и внешних
// библиотек нет: разметка вставляется прямо в кнопку.
//
// Название кнопки остаётся во всплывающей подсказке и в aria-label (В-5):
// значок сам по себе программе чтения с экрана ничего не говорит.

const SHAPES = {
  // Каталог: карточки сканов
  catalog: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/>'
    + '<rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
  // Сравнить: два скана рядом
  compare: '<rect x="3" y="4.5" width="8" height="15" rx="1.5"/><rect x="13" y="4.5" width="8" height="15" rx="1.5"/>',
  // Один экран: обратно к одному скану
  single: '<rect x="4" y="4.5" width="16" height="15" rx="1.5"/>',
  // Связать половины: две области, соединённые звеном
  linkPanes: '<rect x="2.5" y="5.5" width="7" height="13" rx="1.5"/><rect x="14.5" y="5.5" width="7" height="13" rx="1.5"/>'
    + '<path d="M9.5 12h5"/><circle cx="12" cy="12" r="1.6"/>',
  // Поменять местами
  swap: '<path d="M7 8h11l-3.2-3.2"/><path d="M17 16H6l3.2 3.2"/>',
  // Этикетка стекла
  label: '<path d="M12.4 3H5.6A2.6 2.6 0 0 0 3 5.6v6.8c0 .7.3 1.4.8 1.9l6.9 6.9a2.6 2.6 0 0 0 3.7 0l6.2-6.2a2.6 2.6 0 0 0 0-3.7l-6.9-6.9c-.5-.5-1.2-.8-1.9-.8z"/>'
    + '<circle cx="7.8" cy="7.8" r="1.4"/>',
  // Настройка изображения: привычный знак яркости и контраста
  adjust: '<circle cx="12" cy="12" r="8.6"/><path d="M12 3.4v17.2a8.6 8.6 0 0 0 0-17.2z" fill="currentColor" stroke="none"/>',
  // Сброс вида: вернуть как было
  reset: '<path d="M3.5 12a8.5 8.5 0 1 0 2.9-6.4"/><path d="M3 3.5V9h5.5"/>',
  // Во весь экран
  fullscreen: '<path d="M9 3.5H5.5A2 2 0 0 0 3.5 5.5V9M15 3.5h3.5a2 2 0 0 1 2 2V9M9 20.5H5.5a2 2 0 0 1-2-2V15M15 20.5h3.5a2 2 0 0 0 2-2V15"/>',
  // Ссылка на поле зрения: звено цепи
  share: '<path d="M10.5 13.5a4.5 4.5 0 0 0 6.8.5l2-2a4.5 4.5 0 0 0-6.4-6.4l-1.1 1.1"/>'
    + '<path d="M13.5 10.5a4.5 4.5 0 0 0-6.8-.5l-2 2a4.5 4.5 0 0 0 6.4 6.4l1.1-1.1"/>',
  // Сведения о скане
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.6" r="1.1" fill="currentColor" stroke="none"/>',
  // Горячие клавиши
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.4 9.2a2.7 2.7 0 1 1 3.4 2.6c-.6.2-1 .8-1 1.4v.5"/>'
    + '<circle cx="12" cy="16.8" r="1.1" fill="currentColor" stroke="none"/>',
  // Аннотации: маркер-булавка на препарате
  annotations: '<path d="M12 21.5s7-6.1 7-11.2A7 7 0 0 0 5 10.3c0 5.1 7 11.2 7 11.2z"/><circle cx="12" cy="10" r="2.6"/>',
  // Поворот и отражение (Т-2, Т-3): стрелка по дуге вокруг препарата
  rotate: '<path d="M20.5 12a8.5 8.5 0 1 1-2.9-6.4"/><path d="M21 4v5.5h-5.5"/>',
  // Рулетка (Т-4): линейка с делениями
  ruler: '<rect x="1.8" y="7.5" width="20.4" height="9" rx="1.5" transform="rotate(-20 12 12)"/>'
    + '<path d="M6.6 9.6l1.2 2.6M10.4 8.2l1.2 2.6M14.2 6.8l1.2 2.6M18 5.4l1.2 2.6"/>',
  // Привязка по ориентирам (С-10): две отмеченные точки, соединённые линией
  bind: '<circle cx="6" cy="17.5" r="2.6"/><circle cx="18" cy="6.5" r="2.6"/><path d="M8 15.6l8-7.2"/>'
    + '<path d="M3 8.5V3.5h5M21 15.5v5h-5"/>',
  // Клеточность (КЛ-1, значок выбран заказчиком 2026-09-22): знак процента,
  // верхний кружок пустой (жир), нижний закрашен (клетка)
  cellularity: '<circle cx="7" cy="7" r="3"/><circle cx="17" cy="17" r="3" fill="currentColor" stroke="none"/><path d="M19 5L5 19"/>',
  // Выход: дверь со стрелкой
  logout: '<path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><path d="M14 8l4 4-4 4M18 12H9"/>',
  // Ещё: кнопки, которые не поместились (В-4)
  more: '<circle cx="5.5" cy="12" r="1.7" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none"/>'
    + '<circle cx="18.5" cy="12" r="1.7" fill="currentColor" stroke="none"/>',
};

export function iconMarkup(name) {
  const shape = SHAPES[name];
  if (!shape) return '';
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${shape}</svg>`;
}

// Расставляет значки по атрибуту data-icon. Подпись, если она нужна рядом со
// значком, лежит в самой кнопке и не затирается (В-3).
export function applyIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((element) => {
    if (element.querySelector('svg.icon')) return;
    element.insertAdjacentHTML('afterbegin', iconMarkup(element.dataset.icon));
  });
}
