// Тема ставится до первой отрисовки, иначе страница мигает светлым.
// Выбор пользователя лежит в localStorage; без него — настройка системы.
(function () {
  var theme = null;
  try { theme = localStorage.getItem('viewer.theme'); } catch (e) { /* хранилище закрыто */ }
  if (theme !== 'light' && theme !== 'dark') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.dataset.theme = theme;
  // Размер интерфейса вьювера (кнопка «Aa»): от 0,9 до 1,5
  try {
    var scale = Number(localStorage.getItem('viewer.uiScale'));
    if (scale >= 0.9 && scale <= 1.5) document.documentElement.style.setProperty('--ui-scale', String(scale));
  } catch (e) { /* хранилище закрыто */ }
})();
