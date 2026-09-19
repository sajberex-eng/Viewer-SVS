// Строки интерфейса. Чтобы добавить язык, добавьте словарь (kk, en) с теми же ключами.
const STRINGS = {
  ru: {
    'app.title': 'Вьювер гистосканов',
    'app.brand': 'Центр гематологии',
    'login.heading': 'Вход в систему',
    'login.login': 'Логин',
    'login.password': 'Пароль',
    'login.submit': 'Войти',
    'login.note': 'Учётные записи создаёт администратор.',
    'login.failed': 'Не удалось войти',
    'common.logout': 'Выход',
    'common.loading': 'Загрузка…',
    'common.ready': 'Готово',
    'common.error': 'Ошибка',
    'common.networkError': 'Нет связи с сервером. Проверьте подключение и повторите.',
    'catalog.heading': 'Слайды',
    'catalog.search': 'Поиск по названию',
    'catalog.rescan': 'Обновить список',
    'catalog.rescan.tip': 'Найти новые файлы в хранилище',
    'catalog.rescan.done': 'Найдено файлов: {total}, обновлено записей: {updated}',
    'catalog.empty': 'Слайдов нет. Загрузите сканы в хранилище и обновите список.',
    'catalog.nothingFound': 'По запросу ничего не найдено.',
    'catalog.scan': 'скан {objective}',
    'top.catalog': 'Каталог',
    'top.catalog.tip': 'Вернуться к списку слайдов',
    'top.adjust': 'Настройка изображения',
    'top.adjust.tip': 'Яркость, контраст, гамма и цвет',
    'top.adjust.saved': 'Для этого слайда сохранены настройки изображения',
    'top.reset': 'Сброс вида',
    'top.reset.tip': 'Показать весь препарат без поворота (R)',
    'top.fullscreen': 'Во весь экран',
    'top.fullscreen.tip': 'Полноэкранный режим (F)',
    'top.link': 'Ссылка на поле зрения',
    'top.link.tip': 'Ссылка, которая откроет слайд в этом месте и на этом увеличении',
    'link.withAdjust': 'Включить настройки изображения',
    'link.copy': 'Копировать',
    'link.copied': 'Скопировано',
    'link.note': 'Ссылка работает только для пользователей, вошедших в систему.',
    'slides.heading': 'Стёкла',
    'slides.toggle.tip': 'Свернуть или развернуть панель слайдов',
    'zoom.fit': 'Fit',
    'zoom.fit.tip': 'Весь препарат (0)',
    'zoom.fixed.tip': 'Увеличение {mag}× ({key})',
    'zoom.unavailable.tip': 'Выше увеличения сканирования',
    'zoom.slider.tip': 'Плавный зум',
    'zoom.current.tip': 'Текущее увеличение',
    'zoom.rotateLeft.tip': 'Повернуть на 90° против часовой стрелки',
    'zoom.rotateRight.tip': 'Повернуть на 90° по часовой стрелке',
    'minimap.toggle.tip': 'Свернуть или развернуть мини-карту',
    'unit.um': 'мкм',
    'unit.mm': 'мм',
    'status.size': 'Размер: {w} × {h} px',
    'status.cursor': 'X: {x}  Y: {y}',
    'status.mag': 'Увеличение: {mag}',
    'status.mpp': 'Пиксель: {mpp} мкм',
    'status.mpp.unknown': 'Пиксель: нет данных',
    'status.tilesLoading': 'Загрузка тайлов…',
    'status.tilesError': 'Часть изображения не загрузилась',
    'viewer.noSlide': 'Слайд не выбран. Откройте его из каталога.',
    'viewer.notFound': 'Слайд не найден. Возможно, он удалён из хранилища.',
    'viewer.storageDown': 'Хранилище слайдов временно недоступно. Попробуйте позже или сообщите администратору.',
    'viewer.retry': 'Повторить',
    'adjust.heading': 'Настройка изображения',
    'adjust.close.tip': 'Закрыть панель',
    'adjust.preset': 'Пресет',
    'adjust.preset.custom': 'Свои значения',
    'adjust.preset.original': 'Исходное',
    'adjust.preset.pale': 'Бледная окраска',
    'adjust.preset.overstained': 'Перекрашенный препарат',
    'adjust.brightness': 'Яркость',
    'adjust.contrast': 'Контраст',
    'adjust.gamma': 'Гамма',
    'adjust.saturation': 'Насыщенность',
    'adjust.hue': 'Оттенок, °',
    'adjust.red': 'Баланс красного',
    'adjust.green': 'Баланс зелёного',
    'adjust.blue': 'Баланс синего',
    'adjust.slider.tip': 'Двойной клик возвращает значение по умолчанию',
    'adjust.resetAll': 'Сбросить всё',
    'adjust.resetAll.tip': 'Вернуть все параметры к значениям по умолчанию',
    'adjust.compare': 'До/После',
    'adjust.compare.tip': 'Удерживайте, чтобы увидеть исходное изображение',
    'adjust.pipette': 'Пипетка белого',
    'adjust.pipette.tip': 'Кликните по пустому фону стекла: баланс каналов сделает его нейтрально-белым',
    'adjust.pipette.hint': 'Кликните по пустому фону стекла. Esc отменяет.',
    'adjust.pipette.dark': 'Это не фон: выберите светлый пустой участок стекла.',
    'adjust.order': 'Порядок применения: баланс каналов → яркость и контраст → гамма → насыщенность и оттенок.',
    'adjust.noWebgl': 'WebGL недоступен в этом браузере, настройка изображения отключена.',
  },
};

let current = 'ru';

export function setLanguage(lang) {
  if (STRINGS[lang]) current = lang;
  document.documentElement.lang = current;
}

export function t(key, params = {}) {
  const template = STRINGS[current][key] ?? STRINGS.ru[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (_, name) => params[name] ?? '');
}

// data-i18n задаёт текст, data-i18n-tip задаёт всплывающую подсказку и aria-label.
export function applyI18n(root = document) {
  root.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-tip]').forEach((el) => {
    const tip = t(el.dataset.i18nTip);
    el.title = tip;
    if (!el.textContent.trim()) el.setAttribute('aria-label', tip);
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
}

export function formatNumber(value, digits = 0) {
  return value.toLocaleString(current, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
