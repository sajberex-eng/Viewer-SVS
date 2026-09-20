// Вьювер: одна половина экрана в обычном режиме и две в режиме сравнения.
// Всё, что относится к одному скану, живёт в SlideView; здесь — общие панели,
// активная половина, связанная навигация и ссылка на поле зрения.
import { isDefault } from './adjust.js';
import { AdjustPanel } from './adjust-panel.js';
import { api, logout } from './api.js';
import { chooseDialog, infoDialog } from './dialog.js';
import { applyIcons } from './icons.js';
import { applyI18n, formatDateTime, formatNumber, setLanguage, t } from './i18n.js';
import { FIXED_MAGNIFICATIONS, SlideView, ZOOM_STEP } from './slide-view.js';

const PAN_STEP = 0.2; // доля видимой области на одно нажатие стрелки
// Телефон: тот же порог, что в стилях. Список сканов лежит поверх изображения,
// поэтому после выбора его нужно закрыть (М-8).
const phone = () => matchMedia('(max-width: 700px)').matches;
// Минимальная ширина области двух половин. Считается именно она, а не ширина
// окна: при масштабировании Windows (150–200 %) экран 2256 точек даёт браузеру
// около 1128, и проверка по окну запрещала бы сравнение без причины.
const MIN_PANES_WIDTH = 820;
const SPLIT_LIMITS = [30, 70]; // проценты, ТЗ С-2

const $ = (id) => document.getElementById(id);

let user = null;
let panes = []; // одна или две половины
let active = null;
let linked = false;
let linkOffset = null; // взаимное положение половин в момент связывания
let adjustPanel = null;
let split = 50;

setLanguage('ru');
applyIcons();  // значки вставляются до подписей: подпись в кнопке остаётся своя (В-1)
applyI18n();
main();

async function main() {
  const query = new URLSearchParams(location.search);
  user = await api('/api/me');
  $('userName').textContent = user.name || user.login;
  $('btnLogout').addEventListener('click', logout);

  const slideId = query.get('slide');
  if (!slideId) return showPageMessage(t('viewer.noSlide'));

  // Ссылка на два скана открывает сразу режим сравнения (С-11). Сведения о
  // втором скане нужны до создания половин: иначе первая успевает открыться
  // на весь экран и после разделения остаётся с вдвое большим увеличением.
  const secondId = query.get('slide2');
  const [slide, second] = await Promise.all([
    loadSlide(slideId),
    secondId ? loadSlide(secondId, { silent: true }) : null,
  ]);
  if (!slide) return;

  document.title = `${slide.title} · ${t('app.title')}`;
  adjustPanel = new AdjustPanel({
    panel: $('adjustPanel'),
    onChange: (values) => { $('adjustSavedDot').hidden = isDefault(values); },
  });
  initSlidesPanel(slide);
  initTopbar();
  initZoomPanel();
  initHotkeys();

  const first = addPane(slide);
  setActive(first);
  first.viewer.addHandler('open', () => restoreView(first, query, ''));

  if (secondId && !second) {
    showNote(t('compare.noAccess'));
  } else if (second) {
    const pane = addPane(second);
    pane.viewer.addHandler('open', () => {
      restoreView(pane, query, '2');
      // Связь включается, когда открылись обе половины: порядок не гарантирован
      if (query.get('link') === '1') whenAllOpen(() => setLinked(true));
    });
  }
  // Раскладка считается всегда, а не только при двух сканах: от неё зависит и
  // видимость кнопки «Сравнить», которой на телефоне быть не должно (М-1)
  applyCompareLayout();
}

async function loadSlide(id, { silent = false } = {}) {
  try {
    return await api(`/api/slides/${encodeURIComponent(id)}`);
  } catch (error) {
    if (silent) return null;
    showPageMessage(error.status === 404 ? t('viewer.notFound') : error.message);
    return null;
  }
}

// ---------- половины ----------

function addPane(slide) {
  const fragment = $('paneTemplate').content.cloneNode(true);
  const element = fragment.querySelector('.pane');
  $('panes').append(element);

  const view = new SlideView({
    slide,
    container: element,
    storageKey: `svsviewer:adjust:${user.login}:${slide.id}`,
    onActivate: setActive,
    onViewChange: onPaneChanged,
    onClose: closePane,
    onPickWhite: (pane, position) => (active === pane ? adjustPanel.pickWhite(position) : false),
  });
  panes.push(view);
  return view;
}

function closePane(view) {
  if (panes.length < 2) return; // последнюю половину не закрыть
  panes = panes.filter((pane) => pane !== view);
  view.destroy();
  setLinked(false);
  setActive(panes[0]);
  applyCompareLayout();
}

function setActive(view) {
  if (!view || active === view) return;
  active = view;
  for (const pane of panes) pane.setActive(panes.length > 1 && pane === view);
  $('slideTitle').textContent = view.slide.title;
  showPath($('slidePath'), view.slide);  // путь в шапке — от активной половины (ИН-3)
  document.title = `${view.slide.title} · ${t('app.title')}`;
  adjustPanel.attachTo(view);
  $('adjustTarget').textContent = t('adjust.target', { title: view.slide.title });
  $('adjustTarget').hidden = panes.length < 2;
  updateLabelButton();
  updateStatusBar();
  markCurrentThumb();
}

// ---------- связанная навигация (С-6, С-7) ----------

function setLinked(value) {
  // Связывать можно только открытые половины: у неоткрытой ещё нет координат
  linked = value && panes.length === 2 && panes.every((pane) => pane.viewer.isOpen());
  $('btnLink2').classList.toggle('is-active', linked);
  $('btnLink2').textContent = linked ? t('compare.unlink') : t('compare.link');
  if (!linked) {
    linkOffset = null;
    return;
  }
  // Связь относительная по положению: запоминаем, как половины стоят сейчас,
  // и держим это смещение — пользователь сначала совмещает участки вручную (С-6).
  // Увеличение, наоборот, уравнивается: 10× слева это 10× справа (С-7).
  const [a, b] = panes;
  linkOffset = {
    dx: b.centerMicrons.x - a.centerMicrons.x,
    dy: b.centerMicrons.y - a.centerMicrons.y,
    rotation: b.rotation - a.rotation,
  };
  if (a.magnification && b.magnification) b.setMagnification(a.magnification, true);
}

function whenAllOpen(action) {
  const pending = panes.filter((pane) => !pane.viewer.isOpen());
  if (!pending.length) return action();
  let left = pending.length;
  for (const pane of pending) {
    pane.viewer.addOnceHandler('open', () => {
      if (--left === 0) action();
    });
  }
}

let syncing = false;

function onPaneChanged(view) {
  if (active === view) updateStatusBar();
  if (!linked || syncing || panes.length !== 2) return;
  const [a, b] = panes;
  const [source, target] = view === a ? [a, b] : [b, a];
  const sign = view === a ? 1 : -1;

  syncing = true;
  try {
    // Без анимации: вторая половина следует сразу, иначе она догоняла бы первую
    const center = source.centerMicrons;
    // Одинаковое увеличение в микроскопических единицах, а не одинаковый зум:
    // у сканов 20× и 40× разный размер пикселя (С-7)
    if (source.magnification && target.magnification) target.setMagnification(source.magnification, true);
    target.panToMicrons({ x: center.x + sign * linkOffset.dx, y: center.y + sign * linkOffset.dy }, true);
    const rotation = source.rotation + sign * linkOffset.rotation;
    if (Math.abs(target.rotation - rotation) > 0.01) target.setRotation(rotation, true);
  } finally {
    syncing = false;
  }
}

// ---------- режим сравнения ----------

function applyCompareLayout() {
  const comparing = panes.length > 1;
  document.body.classList.toggle('is-comparing', comparing);
  $('panes').style.gridTemplateColumns = comparing ? `${split}fr 6px ${100 - split}fr` : '1fr';
  for (const id of ['btnSwap', 'btnSingle', 'btnLink2']) $(id).hidden = !comparing;
  $('adjustBoth').hidden = !comparing;
  $('adjustTarget').hidden = !comparing;
  updateCompareButton(comparing);
  updateLabelButton();
  if (comparing) ensureSplitter();
  else $('splitter')?.remove();
  for (const pane of panes) {
    pane.setActive(comparing && pane === active);
    pane.parts.paneClose.hidden = !comparing;
    pane.resize(); // у ещё не открытого скана области просмотра нет
  }
  // Набор видимых кнопок изменился — раскладку панели надо пересчитать (В-4)
  layoutTopbar();
}

function ensureSplitter() {
  if ($('splitter')) return;
  const splitter = document.createElement('div');
  splitter.className = 'splitter';
  splitter.id = 'splitter';
  splitter.title = t('compare.splitter.tip');
  $('panes').insertBefore(splitter, panes[1].element);

  const move = (event) => {
    const rect = $('panes').getBoundingClientRect();
    const percent = ((event.clientX - rect.left) / rect.width) * 100;
    split = Math.min(Math.max(percent, SPLIT_LIMITS[0]), SPLIT_LIMITS[1]);
    $('panes').style.gridTemplateColumns = `${split}fr 6px ${100 - split}fr`;
  };
  const stop = () => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', stop);
    for (const pane of panes) pane.resize();
  };
  splitter.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop);
  });
  splitter.addEventListener('dblclick', () => {
    split = 50;
    $('panes').style.gridTemplateColumns = '50fr 6px 50fr';
    for (const pane of panes) pane.resize();
  });
}

// Панель стёкол в режиме сравнения сворачивается (С-10) и освобождает место,
// поэтому при проверке места её ширина не учитывается.
function panesWidthIfCompared() {
  const panel = $('slidesPanel');
  const freed = panel.classList.contains('is-collapsed') ? 0 : panel.offsetWidth - 42;
  return $('panes').offsetWidth + Math.max(freed, 0);
}

async function startCompare() {
  if (panesWidthIfCompared() < MIN_PANES_WIDTH) return showNote(t('compare.tooNarrow'));
  collapseSlidesPanel(true);
  const catalog = await api('/api/catalog');
  const current = new Set(panes.map((pane) => pane.slide.id));
  const options = catalog.slides.filter((row) => !current.has(row.id));
  if (!options.length) return showNote(t('compare.nothingToCompare'));

  const folders = new Map(catalog.folders.map((row) => [row.id, row.name]));
  // Щелчок по строке сразу открывает скан рядом: отмечать и подтверждать нечего (ИН-17)
  const chosen = await chooseDialog({
    title: t('compare.pick'),
    hint: t('compare.pickHint'),
    items: options.map((row) => ({
      id: row.id,
      label: row.title,
      hint: folders.get(row.folder_id) ?? '',
    })),
  });
  if (!chosen) return;

  const slide = await loadSlide(String(chosen), { silent: true });
  if (!slide) return showNote(t('compare.noAccess'));
  const pane = addPane(slide);
  applyCompareLayout();
  setActive(pane);
}

// На телефоне сравнения нет совсем: две половины по 180 точек бесполезны
// (раздел 13.5 ТЗ), поэтому кнопки там нет. На узком окне компьютера кнопка
// остаётся, но неактивна и объясняет причину подсказкой.
function updateCompareButton(comparing) {
  const button = $('btnCompare');
  button.hidden = comparing || phone();
  const narrow = panesWidthIfCompared() < MIN_PANES_WIDTH;
  button.disabled = narrow;
  button.title = narrow ? t('compare.tooNarrow') : t('top.compare.tip');
}

function leaveCompare() {
  if (panes.length < 2) return;
  const keep = active ?? panes[0];
  for (const pane of panes.filter((item) => item !== keep)) pane.destroy();
  panes = [keep];
  setLinked(false);
  setActive(keep);
  applyCompareLayout();
}

function swapPanes() {
  if (panes.length !== 2) return;
  panes.reverse();
  const container = $('panes');
  container.insertBefore(panes[0].element, $('splitter'));
  container.append(panes[1].element);
  if (linked) setLinked(true); // взаимное положение пересчитывается заново
  for (const pane of panes) pane.resize();
}

// ---------- панель увеличений: одна на обе половины ----------

// Кнопки и ползунок действуют сразу на все открытые половины: в режиме
// сравнения масштаб должен быть одинаковым, иначе сканы не сопоставить.
function initZoomPanel() {
  const buttons = $('zoomButtons');
  const fit = document.createElement('button');
  fit.className = 'zoom-btn';
  fit.textContent = t('zoom.fit');
  fit.title = t('zoom.fit.tip');
  fit.addEventListener('click', () => forEachPane((pane) => pane.viewer.viewport.goHome(true)));
  buttons.append(fit);

  FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
    const button = document.createElement('button');
    button.className = 'zoom-btn';
    button.textContent = `${magnification}×`;
    button.dataset.magnification = magnification;
    button.title = t('zoom.fixed.tip', { mag: magnification, key: index + 1 });
    button.addEventListener('click', () => zoomAllTo(magnification));
    buttons.append(button);
  });

  const slider = $('zoomSlider');
  slider.addEventListener('input', () => {
    sliderDragging = true;
    forEachPane((pane) => pane.zoomToSliderPosition(slider.value));
  });
  slider.addEventListener('change', () => { sliderDragging = false; });
  $('rotateLeft').addEventListener('click', () => forEachPane((pane) => pane.rotateBy(-90)));
  $('rotateRight').addEventListener('click', () => forEachPane((pane) => pane.rotateBy(90)));
}

let sliderDragging = false;

// Действие применяется ко всем половинам; при связанной навигации хватает
// первой, остальные подтянутся сами.
function forEachPane(action) {
  for (const pane of linked ? panes.slice(0, 1) : panes) action(pane);
}

function zoomAllTo(magnification) {
  forEachPane((pane) => {
    // Скан 20× не может показать 40×: он останется на своём пределе (Н-1)
    if (pane.slide.objective) pane.zoomToMagnification(Math.min(magnification, pane.slide.objective * 2));
  });
}

function updateZoomPanel() {
  if (!active) return;
  $('zoomCurrent').textContent = active.magnificationLabel();
  if (!sliderDragging) $('zoomSlider').value = active.sliderPosition;
  // Кнопка неактивна, если ни одна половина такого увеличения не даёт
  const best = Math.max(...panes.map((pane) => pane.slide.objective || 0));
  for (const button of $('zoomButtons').querySelectorAll('[data-magnification]')) {
    const magnification = Number(button.dataset.magnification);
    button.disabled = !best || magnification > best;
    if (button.disabled) button.title = t('zoom.unavailable.tip');
  }
}

// ---------- верхняя панель и строка состояния ----------

function initTopbar() {
  const panel = $('adjustPanel');
  const toggleAdjust = (open = panel.hidden) => {
    panel.hidden = !open;
    $('btnAdjust').setAttribute('aria-expanded', String(open));
  };
  $('btnAdjust').addEventListener('click', () => toggleAdjust());
  $('adjustClose').addEventListener('click', () => toggleAdjust(false));
  $('btnLabel').addEventListener('click', () => active?.toggleLabel?.());
  $('btnReset').addEventListener('click', () => active?.resetView());
  $('btnFullscreen').addEventListener('click', toggleFullscreen);
  $('btnCompare').addEventListener('click', startCompare);
  $('btnSingle').addEventListener('click', leaveCompare);
  $('btnSwap').addEventListener('click', swapPanes);
  $('btnLink2').addEventListener('click', () => setLinked(!linked));
  $('btnInfo').addEventListener('click', showSlideInfo);
  $('btnHelp').addEventListener('click', showHelp);

  // Меню «ещё» (В-4)
  const more = $('btnMore');
  const moreMenu = $('morePopover');
  more.addEventListener('click', () => {
    moreMenu.hidden = !moreMenu.hidden;
    more.setAttribute('aria-expanded', String(!moreMenu.hidden));
  });
  moreMenu.addEventListener('click', (event) => {
    if (event.target.closest('button, a')) {
      moreMenu.hidden = true;
      more.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('pointerdown', (event) => {
    if (!moreMenu.hidden && !event.target.closest('#moreAnchor')) {
      moreMenu.hidden = true;
      more.setAttribute('aria-expanded', 'false');
    }
  });
  // «Применить к обеим» (С-9): настройки активной половины копируются
  // во вторую и сохраняются для её скана.
  $('adjustBoth').addEventListener('click', () => {
    for (const pane of panes) {
      if (pane !== active) pane.setAdjust(active.adjustValues);
    }
  });
  addEventListener('resize', () => {
    if (panes.length > 1) for (const pane of panes) pane.resize();
    updateCompareButton(panes.length > 1); // поворот телефона меняет ширину
    layoutTopbar();
  });
  layoutTopbar();

  // Ссылка доступна всем: открыть её сможет только тот, у кого есть доступ
  // к этим сканам, это проверяет сервер (Д-5).
  const button = $('btnLink');
  const popover = $('linkPopover');
  const refresh = () => { $('linkUrl').value = buildLink($('linkWithAdjust').checked); };
  button.hidden = false;
  button.addEventListener('click', () => {
    popover.hidden = !popover.hidden;
    button.setAttribute('aria-expanded', String(!popover.hidden));
    if (!popover.hidden) {
      refresh();
      $('linkUrl').select();
    }
  });
  $('linkWithAdjust').addEventListener('change', refresh);
  $('linkCopy').addEventListener('click', async () => {
    refresh();
    try {
      await navigator.clipboard.writeText($('linkUrl').value);
    } catch {
      $('linkUrl').select();
      document.execCommand('copy'); // запасной путь для страниц без доступа к буферу обмена
    }
    $('linkCopy').textContent = t('link.copied');
    setTimeout(() => { $('linkCopy').textContent = t('link.copy'); }, 1500);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!popover.hidden && !event.target.closest('.popover-anchor')) {
      popover.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    }
  });
}

// Заголовок половины с кнопкой этикетки виден только в режиме сравнения,
// поэтому при одном скане этикетку открывает кнопка верхней панели.
function updateLabelButton() {
  const button = $('btnLabel');
  button.hidden = panes.length > 1;
  const hasLabel = Boolean(active?.slide.has_label);
  button.disabled = !hasLabel;
  button.title = t(hasLabel ? 'top.label.tip' : 'top.label.none');
}

function updateStatusBar() {
  if (!active) return;
  updateZoomPanel();
  // В строке состояния остаётся то, что меняется на ходу: увеличение и
  // состояние загрузки. Размеры и микрометры ушли в «Сведения о скане» (ИН-16).
  $('statusMag').textContent = t('status.mag', { mag: active.magnificationLabel() });
  const status = $('statusTiles');
  status.textContent = active.tilesStatus;
  status.classList.toggle('is-error', active.tilesFailed);
}

// Путь «2026-09-19 › Случай 01» рядом с названием скана. Щелчок открывает
// эту папку в каталоге (ИН-3).
function showPath(node, slide) {
  const path = slide.path ?? [];
  node.hidden = !path.length;
  if (!path.length) return;
  node.textContent = path.map((folder) => folder.name).join(' › ');
  node.href = `/?folder=${path[path.length - 1].id}`;
}

// Сведения о скане (ИН-16): то, что нужно редко и не должно занимать строку
// состояния. Размер файла и исходное имя показываются только администратору.
function showSlideInfo() {
  if (!active) return;
  const { slide } = active;
  const rows = [
    [t('info.name'), slide.title],
    [t('info.folder'), (slide.path ?? []).map((folder) => folder.name).join(' › ') || t('info.unknown')],
    [t('info.size'), t('info.sizePx', { w: formatNumber(slide.width), h: formatNumber(slide.height) })],
    [t('info.mpp'), slide.mpp ? t('info.mppValue', { mpp: formatNumber(slide.mpp, 4) }) : t('info.unknown')],
    [t('info.objective'), slide.objective ? `${formatNumber(slide.objective, 1)}×` : t('info.unknown')],
    [t('info.format'), slide.format || t('info.unknown')],
    [t('info.added'), slide.added_at ? formatDateTime(slide.added_at) : t('info.unknown')],
  ];
  if (slide.size_bytes) rows.push([t('info.fileSize'), sizeText(slide.size_bytes)]);
  if (slide.original_name) rows.push([t('info.fileName'), slide.original_name]);
  infoDialog({ title: t('info.title'), sections: [{ rows }] });
}

function sizeText(bytes) {
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${formatNumber(value, unit >= 2 ? 1 : 0)} ${units[unit]}`;
}

// Окно справки по горячим клавишам (ИН-9): открывается кнопкой и клавишей «?».
function showHelp() {
  infoDialog({
    title: t('help.title'),
    sections: [
      {
        title: t('help.zoomGroup'),
        rows: [
          ['0', t('help.fit')],
          ['1 … 5', t('help.fixed')],
          ['+', t('help.zoomIn')],
          ['−', t('help.zoomOut')],
        ],
      },
      {
        title: t('help.moveGroup'),
        rows: [
          ['← ↑ → ↓', t('help.arrows')],
          ['Page Down / Page Up', t('help.pageKeys')],
        ],
      },
      {
        title: t('help.viewGroup'),
        rows: [
          ['R', t('help.reset')],
          ['F', t('help.fullscreen')],
          ['L', t('help.label')],
          ['I', t('help.adjust')],
          ['C', t('help.compare')],
          ['Q', t('help.switchPane')],
          ['S', t('help.info')],
          ['?', t('help.help')],
        ],
      },
    ],
  });
}

// Кнопки, которым не хватило ширины, переезжают в меню «ещё» (В-4). Порядок
// сохраняется: из панели уходит последняя видимая, возвращается первая в меню.
function layoutTopbar() {
  const actions = $('topbarActions');
  const anchor = $('moreAnchor');
  const menu = $('morePopover');
  while (menu.firstElementChild) actions.insertBefore(menu.firstElementChild, anchor);
  anchor.hidden = true;

  const movable = () => [...actions.children].filter(
    (node) => node !== anchor && !node.hidden && !(node.id === 'linkAnchor' && $('btnLink').hidden),
  );
  let guard = 0;
  while (actions.scrollWidth > actions.clientWidth + 1 && guard < 20) {
    const items = movable();
    if (items.length <= 1) break;
    anchor.hidden = false;
    menu.prepend(items[items.length - 1]);
    guard += 1;
  }
  if (!menu.firstElementChild) {
    anchor.hidden = true;
    menu.hidden = true;
    $('btnMore').setAttribute('aria-expanded', 'false');
  }
}

function showNote(text) {
  const status = $('statusTiles');
  status.textContent = text;
  status.classList.add('is-error');
  setTimeout(() => {
    if (status.textContent === text) {
      status.textContent = '';
      status.classList.remove('is-error');
    }
  }, 6000);
}

function showPageMessage(text) {
  const message = document.createElement('p');
  message.className = 'page-message';
  message.textContent = text;
  $('panes').append(message);
}

function toggleFullscreen() {
  const root = document.documentElement;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else {
    (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
  }
}

// ---------- ссылка на поле зрения (Н-9, С-11) ----------

function buildLink(withAdjust) {
  const query = new URLSearchParams();
  panes.forEach((pane, index) => {
    const suffix = index ? '2' : '';
    const center = pane.viewer.viewport.viewportToImageCoordinates(pane.viewer.viewport.getCenter(true));
    query.set(`slide${suffix}`, pane.slide.id);
    query.set(`x${suffix}`, Math.round(center.x));
    query.set(`y${suffix}`, Math.round(center.y));
    if (pane.slide.objective) query.set(`m${suffix}`, pane.magnification.toFixed(2));
    else query.set(`z${suffix}`, pane.imageZoom.toFixed(4));
    if (pane.rotation) query.set(`r${suffix}`, pane.rotation);
    if (withAdjust && !isDefault(pane.adjustValues)) {
      query.set(`adj${suffix}`, AdjustPanel.serialize(pane.adjustValues));
    }
  });
  if (linked) query.set('link', '1');
  return `${location.origin}/viewer?${query}`;
}

function restoreView(pane, query, suffix) {
  const number = (name) => (query.has(name) ? Number(query.get(name)) : NaN);
  const { viewport } = pane.viewer;
  const [x, y, m, z, r] = ['x', 'y', 'm', 'z', 'r'].map((key) => number(key + suffix));
  if (Number.isFinite(r)) viewport.setRotation(r, true);
  const zoom = Number.isFinite(m) && pane.slide.objective ? m / pane.slide.objective : z;
  if (zoom > 0) viewport.zoomTo(viewport.imageToViewportZoom(zoom), null, true);
  if (Number.isFinite(x) && Number.isFinite(y)) viewport.panTo(viewport.imageToViewportCoordinates(x, y), true);
  viewport.applyConstraints(true);
  // Настройки из ссылки показываются, но не заменяют сохранённые у получателя
  const shared = query.get(`adj${suffix}`) && AdjustPanel.deserialize(query.get(`adj${suffix}`));
  if (!shared) return;
  if (pane === active) adjustPanel.setValues(shared, { save: false }); // ползунки тоже
  else pane.setAdjust(shared, { save: false });
}

// ---------- панель стёкол ----------

function initSlidesPanel(slide) {
  const list = $('slidesList');
  for (const sibling of slide.siblings) {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `/viewer?slide=${encodeURIComponent(sibling.id)}`;
    link.className = 'slide-thumb';
    link.dataset.slide = sibling.id;
    const image = document.createElement('img');
    image.src = `/api/slides/${encodeURIComponent(sibling.id)}/thumbnail.jpg`;
    image.alt = '';
    image.loading = 'lazy';
    const caption = document.createElement('span');
    caption.textContent = sibling.title;
    link.append(image, caption);
    // Щелчок заменяет скан в активной половине: страница не перезагружается,
    // настройки панелей и свёрнутый список остаются как были (С-10)
    link.addEventListener('click', (event) => {
      event.preventDefault();
      openInActive(sibling.id);
      if (phone()) collapseSlidesPanel(true); // панель лежит поверх препарата (М-8)
    });
    item.append(link);
    list.append(item);
  }
  markCurrentThumb();

  // На планшете панель свёрнута по умолчанию; в режиме сравнения её сворачивает
  // collapseSlidesPanel, освобождая место половинам (С-10).
  const panel = $('slidesPanel');
  const toggle = $('slidesToggle');
  collapseSlidesPanel(matchMedia('(pointer: coarse), (max-width: 1100px)').matches);
  toggle.addEventListener('click', () => collapseSlidesPanel(!panel.classList.contains('is-collapsed')));
}

function collapseSlidesPanel(collapsed) {
  const panel = $('slidesPanel');
  if (panel.classList.contains('is-collapsed') === collapsed) return;
  panel.classList.toggle('is-collapsed', collapsed);
  const toggle = $('slidesToggle');
  toggle.textContent = collapsed ? '›' : '‹';
  toggle.setAttribute('aria-expanded', String(!collapsed));
  for (const pane of panes) pane.resize();
}

async function replaceActive(slideId) {
  const slide = await loadSlide(slideId, { silent: true });
  if (!slide) return showNote(t('compare.noAccess'));
  const index = panes.indexOf(active);
  const others = panes.filter((pane) => pane !== active); // при сравнении — соседняя половина
  active.destroy();
  const pane = addPane(slide);
  // Новая половина встаёт на своё место: слева или справа от разделителя
  if (index === 0 && others.length) $('panes').insertBefore(pane.element, $('splitter'));
  setLinked(false);
  panes = index === 0 ? [pane, ...others] : [...others, pane];
  setActive(pane);
  applyCompareLayout();
}

// Следующий и предыдущий скан папки (ИН-10). В режиме сравнения меняется
// только активная половина.
function stepSlide(step) {
  const list = active?.slide.siblings ?? [];
  if (list.length < 2) return;
  const index = list.findIndex((row) => row.id === active.slide.id);
  if (index < 0) return;
  const next = list[(index + step + list.length) % list.length];
  if (next.id !== active.slide.id) replaceActive(next.id);
}

// Щелчок по скану в списке случая: в обычном режиме половина одна, и её
// содержимое меняется так же, как в режиме сравнения (С-10).
function openInActive(slideId) {
  if (active?.slide.id !== slideId) replaceActive(slideId);
}

function markCurrentThumb() {
  const shown = new Set(panes.map((pane) => pane.slide.id));
  for (const link of $('slidesList').querySelectorAll('.slide-thumb')) {
    link.toggleAttribute('aria-current', shown.has(link.dataset.slide));
  }
}

// ---------- горячие клавиши (Н-8) ----------

function initHotkeys() {
  // Коды клавиш не зависят от раскладки: F и R работают и в русской.
  // Увеличение меняется у обеих половин, как и кнопками панели; движение
  // стрелками остаётся у активной, чтобы можно было совместить участки.
  const actions = {
    Digit0: () => forEachPane((pane) => pane.viewer.viewport.goHome(true)),
    Numpad0: () => forEachPane((pane) => pane.viewer.viewport.goHome(true)),
    Equal: () => forEachPane((pane) => pane.zoomBy(ZOOM_STEP)),
    NumpadAdd: () => forEachPane((pane) => pane.zoomBy(ZOOM_STEP)),
    Minus: () => forEachPane((pane) => pane.zoomBy(1 / ZOOM_STEP)),
    NumpadSubtract: () => forEachPane((pane) => pane.zoomBy(1 / ZOOM_STEP)),
    ArrowLeft: () => active?.panByFraction(-1, 0, PAN_STEP),
    ArrowRight: () => active?.panByFraction(1, 0, PAN_STEP),
    ArrowUp: () => active?.panByFraction(0, -1, PAN_STEP),
    ArrowDown: () => active?.panByFraction(0, 1, PAN_STEP),
    KeyF: toggleFullscreen,
    KeyR: () => forEachPane((pane) => pane.resetView()),
    // Tab возвращён браузеру: им переходят по элементам страницы (ИН-15)
    KeyQ: () => {
      if (panes.length > 1) setActive(panes[(panes.indexOf(active) + 1) % panes.length]);
    },
    KeyL: () => $('btnLabel').hidden ? active?.toggleLabel?.() : $('btnLabel').click(),
    KeyI: () => $('btnAdjust').click(),
    KeyC: () => (panes.length > 1 ? leaveCompare() : startCompare()),
    KeyS: showSlideInfo,
    Slash: showHelp, // Shift+/ это «?»
    PageDown: () => stepSlide(1),
    PageUp: () => stepSlide(-1),
  };
  FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
    const action = () => zoomAllTo(magnification);
    actions[`Digit${index + 1}`] = action;
    actions[`Numpad${index + 1}`] = action;
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey || !active?.viewer.isOpen()) return;
    if (document.querySelector('dialog[open]')) return; // в открытом окне клавиши принадлежат ему
    // В полях ввода клавиши принадлежат полю; у ползунков остаются только стрелки.
    if (event.target.closest('input:not([type=range], [type=checkbox]), select, textarea')) return;
    if (event.target.matches('input[type=range]') && event.code.startsWith('Arrow')) return;
    const action = actions[event.code];
    if (!action) return;
    event.preventDefault();
    action();
  });
}
