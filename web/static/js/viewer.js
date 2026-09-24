// Вьювер: одна половина экрана в обычном режиме и две в режиме сравнения.
// Всё, что относится к одному скану, живёт в SlideView; здесь — общие панели,
// активная половина, связанная навигация и ссылка на поле зрения.
import { isDefault } from './adjust.js';
import { AdjustPanel } from './adjust-panel.js';
import { AnnotationLayer } from './annotations.js';
import { api, logout } from './api.js';
import { initCellularity, isContour } from './cellularity.js';
import { chooseDialog, confirmDialog, formDialog, infoDialog } from './dialog.js';
import { applyIcons } from './icons.js';
import { applyI18n, formatDateTime, formatNumber, setLanguage, stainFull, t } from './i18n.js';
// Пересчёт точек и углов между половинами: link.forward, link.backward,
// link.rotationFor, link.fitBind — по имени модуля понятнее, чем сами по себе.
import * as link from './link.js';
import { bindFromViews, normalizeAngle } from './link.js';
import { OverlayLayer } from './overlay.js';
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
let linkBind = null; // взаимное положение половин, см. captureBind()
let adjustPanel = null;
let split = 50;
// Показ аннотаций — выбор пользователя, общий для всех сканов (А-11)
let annotationsShown = localStorage.getItem('viewer.annotations') !== 'off';
let activeTool = null;
let cellularity = null;  // панель клеточности (этап 11), см. cellularity.js

setLanguage('ru');
applyIcons();  // значки вставляются до подписей: подпись в кнопке остаётся своя (В-1)
applyI18n();
main();

async function main() {
  const query = new URLSearchParams(location.search);
  user = await api('/api/me');
  // Имя пользователя — кружок с инициалами, полное имя в подсказке: место в шапке
  // нужно увеличению (замечание патолога 2026-09-24)
  const name = user.name || user.login;
  $('userName').textContent = initials(name);
  $('userName').title = t('common.user.tip', { name });
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
  initAnnotations();
  cellularity = initCellularity({
    getActive: () => active,
    getPanes: () => panes,
    canDraw,
    setTool,
    activeTool: () => activeTool,
    isAdmin: () => user.role === 'admin',
    reloadAnnotations: loadAnnotations,
    focusAnnotation,
    removeAnnotation,
    showNote,
  });
  initZoomPanel();
  initRotatePanel();
  initRuler();
  initBinding();
  initShiftAdjust();
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

  // Ссылка на конкретную аннотацию (А-5): половины к этому моменту уже созданы,
  // но скан в них ещё открывается — ждём открытия
  const wantedAnnotation = query.get('annotation');
  if (wantedAnnotation) whenAllOpen(() => goToAnnotation(wantedAnnotation));
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

// У каждой половины свой слой: аннотации показываются в обеих независимо (А-7)
function attachAnnotations(pane) {
  pane.annotations = new AnnotationLayer({
    view: pane,
    // item === null — выделение сняли щелчком по препарату
    onSelect: (item) => {
      if (pane !== active) setActive(pane);
      renderAnnotationList();
      highlightRow(item?.id ?? null);
    },
    onSave: (item) => saveGeometry(pane, item),
    onFinishDraft: (kind, points) => createAnnotation(pane, kind, points),
    onDraftChange: updateAnnotationHint,
  });
  pane.annotations.setVisible(annotationsShown);
  loadAnnotations(pane);
  return pane;
}

async function loadAnnotations(pane) {
  try {
    pane.annotationItems = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/annotations`);
  } catch {
    pane.annotationItems = [];  // нет доступа или сеть: аннотации просто не показываем
  }
  pane.annotations.setItems(pane.annotationItems);
  if (pane === active) {
    renderAnnotationList();
    updateAnnotationCount();
  }
  cellularity?.onAnnotationsChanged(pane);
}

// Пометки для обучения — без контуров клеточности: те живут в своей панели
function learningItems(pane) {
  return (pane?.annotationItems ?? []).filter((item) => !isContour(item));
}

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
    onContextMenu: openStageMenu,
  });
  panes.push(view);
  attachAnnotations(view);
  cellularity?.onPaneAdded(view);
  view.overlay = new OverlayLayer({ view, onLandmark: updateBindBar });
  // Общий курсор: перекрестие в соседней половине идёт за мышью (С-8)
  element.addEventListener('pointermove', (event) => showCrosshairFrom(view, event));
  element.addEventListener('pointerleave', () => updateCrosshair(null));
  return view;
}

// Место, на которое указывает мышь в одной половине, отмечается перекрестием
// в другой. Точка пересчитывается связью, поэтому курсор точен настолько,
// насколько точна привязка (С-8). На сенсорном экране его нет.
function showCrosshairFrom(pane, event) {
  if (!linked || panes.length !== 2 || matchMedia('(pointer: coarse)').matches) return;
  if (!event.target.closest?.('.osd, .annot-layer, .overlay-layer')) return updateCrosshair(null);
  const at = pane.imageFromClient(event.clientX, event.clientY);
  const toSecond = pane === panes[0];
  const target = toSecond ? panes[1] : panes[0];
  const scale = pane.slide.mpp || 1;
  const microns = { x: at.x * scale, y: at.y * scale };
  const there = toSecond ? link.forward(linkBind, microns) : link.backward(linkBind, microns);
  const targetScale = target.slide.mpp || 1;
  updateCrosshair(target, [there.x / targetScale, there.y / targetScale]);
}

function updateCrosshair(target, point = null) {
  for (const pane of panes) pane.overlay?.setCrosshair(pane === target ? point : null);
}

function closePane(view) {
  if (panes.length < 2) return; // последнюю половину не закрыть
  panes = panes.filter((pane) => pane !== view);
  cellularity?.onPaneRemoved(view);
  view.annotations?.destroy();
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
  setTool(null);  // инструмент принадлежит половине, в которой его включили
  renderAnnotationList();
  updateAnnotationCount();
  cellularity?.onActiveChanged();
}

// ---------- связанная навигация (С-6, С-7) ----------

// bind задан — связь берётся готовой (привязка по ориентирам, С-10);
// без него запоминается то, как половины стоят сейчас (С-6).
function setLinked(value, bind = null) {
  // Связывать можно только открытые половины: у неоткрытой ещё нет координат
  linked = value && panes.length === 2 && panes.every((pane) => pane.viewer.isOpen());
  const button = $('btnLink2');
  button.classList.toggle('is-active', linked);
  // У кнопки только значок: подпись живёт в подсказке, в aria-label и в меню
  // «ещё». Записывать её в textContent нельзя — сотрётся значок (В-1).
  const label = t(linked ? 'compare.unlink' : 'compare.link');
  button.dataset.label = label;
  button.setAttribute('aria-label', label);
  button.title = t(linked ? 'compare.unlink.tip' : 'compare.link.tip');
  if (!linked) {
    linkBind = null;
    updateCrosshair(null);
    return;
  }
  const [a, b] = panes;
  if (bind) {
    linkBind = bind;
    onPaneChanged(a); // вторая половина сразу встаёт по привязке
    return;
  }
  captureBind();
  // Увеличение уравнивается: 10× слева это 10× справа (С-7)
  if (a.magnification && b.magnification) b.setMagnification(a.magnification, true);
}

// Связь запоминает, как половины стоят сейчас: пользователь сначала совмещает
// одинаковые участки вручную (С-6), затем связывает. Сама запись взаимного
// положения и все расчёты по нему живут в link.js.
function captureBind() {
  const [a, b] = panes.map((pane) => ({
    flipped: pane.flipped,
    rotation: pane.rotation,
    centerMicrons: pane.targetCenterMicrons,
  }));
  linkBind = bindFromViews(a, b);
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
  // Пока держат Shift, половины идут врозь: правится взаимное положение (С-11)
  if (!linked || syncing || shiftFrom || panes.length !== 2) return;
  const [a, b] = panes;
  const forward = view === a;
  const [source, target] = forward ? [a, b] : [b, a];

  syncing = true;
  try {
    // Без анимации: вторая половина следует сразу, иначе она догоняла бы первую
    const center = source.centerMicrons;
    // Одинаковое увеличение в микроскопических единицах, а не одинаковый зум:
    // у сканов 20× и 40× разный размер пикселя (С-7)
    if (source.magnification && target.magnification) target.setMagnification(source.magnification, true);
    target.panToMicrons(forward ? link.forward(linkBind, center) : link.backward(linkBind, center), true);
    const rotation = link.rotationFor(linkBind, source.rotation, forward);
    if (Math.abs(normalizeAngle(target.rotation - rotation)) > 0.01) target.setRotation(rotation, true);
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
  // Привязка по ориентирам есть только при сравнении и только если у обоих
  // сканов известен размер пикселя (С-10)
  const canBind = comparing && panes.every((pane) => pane.overlay?.available);
  $('btnBind').hidden = !canBind;
  if (!canBind) stopBinding();
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

  // Панель сворачивается, как мини-карта, и при каждом открытии страницы
  // снова развёрнута: увеличение нужно чаще, чем место под ним
  const toggle = $('zoomToggle');
  toggle.addEventListener('click', () => {
    const collapsed = toggle.closest('.zoom-panel').classList.toggle('is-collapsed');
    toggle.textContent = collapsed ? '▸' : '▾';
    toggle.setAttribute('aria-expanded', String(!collapsed));
  });
}

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
  // Кнопка неактивна, если ни одна половина такого увеличения не даёт
  const best = Math.max(...panes.map((pane) => pane.slide.objective || 0));
  for (const button of $('zoomButtons').querySelectorAll('[data-magnification]')) {
    const magnification = Number(button.dataset.magnification);
    button.disabled = !best || magnification > best;
    if (button.disabled) button.title = t('zoom.unavailable.tip');
  }
}

// ---------- поворот и отражение (Т-2, Т-3) ----------

function initRotatePanel() {
  const slider = $('rotateSlider');
  const number = $('rotateNumber');
  const flip = $('rotateFlip');
  bindPopover($('btnRotate'), $('rotatePopover'), { onOpen: refreshRotatePanel });

  slider.addEventListener('input', () => rotateTo(slider.valueAsNumber));
  number.addEventListener('change', () => rotateTo(number.valueAsNumber));
  $('rotateMinus90').addEventListener('click', () => rotateBy(-90));
  $('rotatePlus90').addEventListener('click', () => rotateBy(90));
  $('rotateZero').addEventListener('click', () => rotateTo(0));
  // Отражается только выбранная половина: две отражённые половины — это те же
  // два неотражённых среза (Т-3)
  flip.addEventListener('change', () => setFlip(active, flip.checked));
}

// Поворот задаётся для активной половины, вторая поворачивается на тот же угол:
// в режиме сравнения он синхронный, а взаимный угол половин задаёт привязка.
function rotateTo(degrees) {
  if (!active) return;
  rotateBy(normalizeAngle(degrees) - normalizeAngle(active.rotation));
}

function rotateBy(delta) {
  if (!active || !delta) return;
  // При связи хватает одной половины: вторая повернётся следом, сохранив
  // запомненный взаимный угол
  const targets = linked ? [active] : panes;
  for (const pane of targets) pane.setRotation(normalizeAngle(pane.rotation + delta), true);
  refreshRotatePanel();
}

function setFlip(pane, flipped) {
  if (!pane || pane.flipped === flipped) return;
  pane.setFlip(flipped);
  // Отражение меняет взаимное положение половин: связь запоминает его заново,
  // иначе она осталась бы с отражением, которого уже нет (С-9)
  if (linked) captureBind();
  refreshRotatePanel();
  updateStatusBar();
}

function refreshRotatePanel() {
  if (!active) return;
  const angle = Math.round(normalizeAngle(active.rotation));
  $('rotateSlider').value = angle;
  $('rotateNumber').value = angle;
  $('rotateFlip').checked = active.flipped;
  $('btnRotate').classList.toggle('is-active', angle !== 0 || active.flipped);
}

// Окошко у кнопки: открывается нажатием, закрывается щелчком мимо.
function bindPopover(button, popover, { onOpen } = {}) {
  const anchor = button.closest('.popover-anchor');
  const toggle = (open = popover.hidden) => {
    popover.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) onOpen?.();
  };
  button.addEventListener('click', () => toggle());
  document.addEventListener('pointerdown', (event) => {
    if (!popover.hidden && !anchor.contains(event.target)) toggle(false);
  });
  return toggle;
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
  refreshRotatePanel();
  updateRulerButton();
  // Увеличение и отражение — в шапке (нижней строки больше нет). Размеры и
  // микрометры ушли в «Сведения о скане» (ИН-16).
  $('statusMag').textContent = active.magnificationLabel();
  $('statusFlip').hidden = !active.flipped;  // отражение видно и на снимке экрана (Т-3)
  // Сбой загрузки тайлов показывается всплывашкой; обычная подгрузка — нет
  if (active.tilesFailed) showNote(active.tilesStatus);
}

function initials(name) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return letters.toUpperCase();
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
    [t('info.stain'), stainFull(slide)],
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
          ['1 … 4', t('help.fixed')],
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
          ['S', t('help.info')],
          ['?', t('help.help')],
        ],
      },
      {
        title: t('help.toolsGroup'),
        rows: [
          ['M', t('help.ruler')],
          ['A', t('help.annotations')],
          // Меню по правой кнопке и рисование контуров — только с мышью (Т-5, А-16)
          ...(phone() ? [] : [
            [t('help.rightClickKey'), t('help.rightClick')],
            ['Enter', t('help.closeShape')],
            ['Esc', t('help.escape')],
          ]),
        ],
      },
      // Сравнение объясняется не только клавишами: привязку по ориентирам и
      // общий курсор иначе неоткуда узнать (решение заказчика 2026-09-21).
      // На телефоне сравнения нет совсем (М-1), поэтому и раздела нет.
      ...(phone() ? [] : [{
        title: t('help.compareGroup'),
        rows: [
          ['C', t('help.compare')],
          ['Q', t('help.switchPane')],
          [t('compare.link'), t('help.link')],
          [t('bind.title'), t('help.bind')],
          [t('help.shiftKey'), t('help.shiftAdjust')],
          [t('help.crosshairName'), t('help.crosshair')],
        ],
      }]),
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

// ---------- рулетка и меню по правой кнопке (Т-4, Т-5) ----------

function initRuler() {
  $('btnRuler').addEventListener('click', () => setTool(activeTool === 'ruler' ? null : 'ruler'));

  const menu = $('stageMenu');
  $('menuRuler').addEventListener('click', () => withMenuPoint((pane, point) => {
    setTool('ruler');
    pane.overlay.startMeasure(point);
  }));
  $('menuPoint').addEventListener('click', () => withMenuPoint((pane, point) => {
    setTool(null);                                   // стрелка ставится сразу, режим не нужен
    createAnnotation(pane, 'point', [point]);
  }));
  $('menuPolygon').addEventListener('click', () => withMenuPoint((pane, point) => {
    setTool('polygon');
    pane.annotations.startDraft(point);
  }));
  document.addEventListener('pointerdown', (event) => {
    if (!menu.hidden && !menu.contains(event.target)) closeStageMenu();
  });
}

let menuTarget = null;

// Меню по правой кнопке на препарате (Т-5). На сенсорном экране его нет,
// поэтому у каждого пункта остаётся обычный путь: кнопка и клавиша.
function openStageMenu(pane, event, imagePoint) {
  if (matchMedia('(pointer: coarse)').matches) return;
  event.preventDefault();
  setActive(pane);
  menuTarget = { pane, point: [imagePoint.x, imagePoint.y] };

  const menu = $('stageMenu');
  const drawing = canDraw();
  $('menuPoint').hidden = !drawing;
  $('menuPolygon').hidden = !drawing;
  $('menuRuler').disabled = !pane.overlay.available;
  $('menuRuler').title = pane.overlay.available ? t('ruler.tip') : t('ruler.noMpp');
  menu.hidden = false;

  // Меню не должно вылезать за край области половин
  const box = menu.offsetParent.getBoundingClientRect();
  const left = Math.min(event.clientX - box.left, box.width - menu.offsetWidth - 4);
  const top = Math.min(event.clientY - box.top, box.height - menu.offsetHeight - 4);
  menu.style.left = `${Math.max(left, 4)}px`;
  menu.style.top = `${Math.max(top, 4)}px`;
}

function closeStageMenu() {
  if ($('stageMenu').hidden) return false;
  $('stageMenu').hidden = true;
  menuTarget = null;
  return true;
}

function withMenuPoint(action) {
  const target = menuTarget;
  closeStageMenu();
  if (target) action(target.pane, target.point);
}

// Кнопка неактивна, если в файле нет размера пикселя: мерить нечем (Т-4)
function updateRulerButton() {
  const button = $('btnRuler');
  const available = Boolean(active?.overlay?.available);
  button.disabled = !available;
  button.title = available ? t('ruler.tip') : t('ruler.noMpp');
  button.classList.toggle('is-active', activeTool === 'ruler');
}

// ---------- привязка по ориентирам (С-10) ----------
//
// Пользователь отмечает одну и ту же структуру в обеих половинах, и связь
// считается по этим парам. Двух пар хватает для неотражённых срезов; третья
// определяет отражение и показывает расхождение в микрометрах — по нему видно,
// насколько срезы растянуты. Масштаб по точкам не подбирается: щелчок мышью
// неточен, а микрометры у обеих половин и так общие.

let binding = false;

function initBinding() {
  $('btnBind').addEventListener('click', () => (binding ? stopBinding() : startBinding()));
  $('bindUndo').addEventListener('click', () => {
    for (const pane of [...panes].reverse()) {
      if (pane.overlay.landmarks.length) {
        pane.overlay.setLandmarks(pane.overlay.landmarks.slice(0, -1));
        break;
      }
    }
    updateBindBar();
  });
  $('bindCancel').addEventListener('click', stopBinding);
  $('bindApply').addEventListener('click', applyBinding);
}

function startBinding() {
  if (panes.length !== 2 || !panes.every((pane) => pane.overlay.available)) return;
  setTool(null);
  binding = true;
  for (const pane of panes) pane.overlay.setTool('landmarks');
  updateBindBar();
}

function stopBinding() {
  if (!binding) return;
  binding = false;
  for (const pane of panes) pane.overlay.setTool(null);
  updateBindBar();
}

function bindPairCount() {
  return Math.min(...panes.map((pane) => pane.overlay.landmarks.length));
}

function updateBindBar() {
  $('bindBar').hidden = !binding;
  $('btnBind').classList.toggle('is-active', binding);
  if (!binding) return;
  const counts = panes.map((pane) => pane.overlay.landmarks.length);
  const ready = counts[0] === counts[1] && counts[0] >= 2;
  $('bindHint').textContent = ready
    ? t('bind.ready', { n: counts[0] })
    : t('bind.hint', { left: counts[0], right: counts[1] });
  $('bindApply').disabled = !ready;
  $('bindUndo').disabled = !counts[0] && !counts[1];
}

function applyBinding() {
  const [a, b] = panes;
  const count = bindPairCount();
  if (count < 2) return;
  const first = a.overlay.landmarksInMicrons;
  const second = b.overlay.landmarksInMicrons;
  const pairs = Array.from({ length: count }, (_, index) => ({ first: first[index], second: second[index] }));

  // По двум парам зеркальный и незеркальный варианты подходят одинаково точно,
  // поэтому отражение берётся то, которое задал пользователь (Т-3). Третья пара
  // различает их сама.
  const fitted = count >= 3 ? link.fitBind(pairs) : link.fitBind(pairs, a.flipped !== b.flipped);
  if (fitted.mirror !== (a.flipped !== b.flipped)) {
    // Ориентиры говорят, что срезы зеркальны: вторую половину отражаем сами,
    // иначе такую связь не показать. Пометка «отражено» об этом скажет.
    setLinked(false);
    b.setFlip(!b.flipped);
  }
  stopBinding();
  setLinked(true, fitted);
  showNote(count >= 3
    ? t('bind.done', { miss: formatNumber(fitted.residual, fitted.residual < 10 ? 1 : 0) })
    : t('bind.donePair'), { error: false });
}

// ---------- поправка связи с Shift (С-11) ----------
//
// Пока Shift зажат, половины идут врозь: двигается и поворачивается только
// активная. При отпускании связь запоминает новое взаимное положение —
// разрывать и связывать заново не нужно.

let shiftFrom = null;

function initShiftAdjust() {
  addEventListener('keydown', (event) => {
    if (event.key !== 'Shift' || shiftFrom || !linked || !active) return;
    shiftFrom = { center: active.targetCenterMicrons, rotation: active.rotation };
  });
  const finish = () => {
    const before = shiftFrom;
    shiftFrom = null;
    if (!before || !linked || !active) return;
    const now = active.targetCenterMicrons;
    const moved = Math.hypot(now.x - before.center.x, now.y - before.center.y) > 0.5
      || Math.abs(normalizeAngle(active.rotation - before.rotation)) > 0.01;
    if (!moved) return;
    captureBind();
    showNote(t('compare.bindAdjusted'), { error: false });
  };
  addEventListener('keyup', (event) => {
    if (event.key === 'Shift') finish();
  });
  addEventListener('blur', finish); // Shift отпустили в другом окне
}

// ---------- аннотации (этап 9) ----------

function initAnnotations() {
  const panel = $('annotPanel');
  const toggle = (open = panel.hidden) => {
    panel.hidden = !open;
    $('btnAnnotations').setAttribute('aria-expanded', String(open));
    if (open) renderAnnotationList();
  };
  $('btnAnnotations').addEventListener('click', () => toggle());
  $('annotClose').addEventListener('click', () => toggle(false));

  const visible = $('annotVisible');
  visible.checked = annotationsShown;
  visible.addEventListener('change', () => setAnnotationsShown(visible.checked));

  $('toolPoint').addEventListener('click', () => setTool(activeTool === 'point' ? null : 'point'));
  $('toolPolygon').addEventListener('click', () => setTool(activeTool === 'polygon' ? null : 'polygon'));

}

// Инструменты есть только у патологов и только с мыши: пальцем по клетке
// не попасть, поэтому на телефоне их нет (А-3, А-16)
function canDraw() {
  return Boolean(active?.slide.can_annotate) && !phone();
}

// Рулетка доступна всем, инструменты разметки — только патологам (Т-4, А-3).
// Инструмент принадлежит активной половине: в соседней он выключен.
function setTool(tool) {
  if (tool === 'ruler') activeTool = active?.overlay?.available ? 'ruler' : null;
  else activeTool = canDraw() ? tool : null;
  if (activeTool) stopBinding();  // разметка ориентиров и инструменты не совмещаются
  for (const pane of panes) {
    const mine = pane === active;
    pane.annotations?.setTool(mine && activeTool !== 'ruler' ? activeTool : null);
    // Разметка ориентиров идёт сразу в обеих половинах и не сбивается при
    // переходе к соседней: именно им и отмечают пары точек (С-10)
    if (binding) pane.overlay?.setTool('landmarks');
    else pane.overlay?.setTool(mine && activeTool === 'ruler' ? 'ruler' : null);
  }
  $('toolPoint').classList.toggle('is-active', activeTool === 'point');
  $('toolPolygon').classList.toggle('is-active', activeTool === 'polygon');
  cellularity?.onToolChanged();
  updateRulerButton();
  updateAnnotationHint();
  if (activeTool === 'ruler') showNote(t('ruler.hint'), { error: false });
}

function updateAnnotationHint() {
  const hint = $('annotHint');
  if (!activeTool) {
    hint.textContent = active?.slide.can_annotate ? '' : t('annot.noTools');
    return;
  }
  hint.textContent = activeTool === 'point'
    ? t('annot.hint.point')
    : t('annot.hint.polygon', { n: active?.annotations.draft.length ?? 0 });
  // подсказка о контуре клеточности — в строке состояния: панель аннотаций может быть закрыта
  if (isContour({ kind: activeTool })) showNote(hint.textContent, { error: false });
}

function setAnnotationsShown(shown) {
  annotationsShown = shown;
  localStorage.setItem('viewer.annotations', shown ? 'on' : 'off');
  $('annotVisible').checked = shown;
  for (const pane of panes) pane.annotations?.setVisible(shown);
  if (!shown) setTool(null);  // рисовать при выключенном показе бессмысленно
  updateAnnotationCount();
  renderAnnotationList();
}

// При выключенном показе на кнопке видно число аннотаций скана (А-11)
function updateAnnotationCount() {
  const badge = $('annotCount');
  const count = learningItems(active).length;
  badge.hidden = annotationsShown || !count;
  badge.textContent = String(count);
  $('annotTools').hidden = !canDraw();
}

async function createAnnotation(pane, kind, points) {
  let comment = '';
  // Контур для клеточности (КЛ-2) сохраняется без окна: подпись ему не нужна
  if (!isContour({ kind })) {
    const values = await formDialog({
      title: t('annot.commentTitle'),
      submitLabel: t('annot.save'),
      fields: [{
        name: 'comment', label: t('annot.commentLabel'), type: 'textarea',
        hint: t('annot.commentHint'), maxLength: 1000,
      }],
    });
    if (values === null) return;  // окно закрыли — аннотация не создаётся
    comment = values.comment;
  }
  try {
    const created = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/annotations`, {
      method: 'POST', body: { kind, points, comment },
    });
    pane.annotationItems = [...(pane.annotationItems ?? []), created];
    pane.annotations.setItems(pane.annotationItems);
    pane.annotations.select(created.id);
    if (pane === active) {
      renderAnnotationList();
      updateAnnotationCount();
    }
    cellularity?.onAnnotationsChanged(pane);
  } catch (error) {
    showNote(error.message);
  }
}

async function saveGeometry(pane, item) {
  try {
    await api(`/api/annotations/${encodeURIComponent(item.id)}`, {
      method: 'PATCH', body: { points: item.points },
    });
  } catch (error) {
    showNote(error.message);
    loadAnnotations(pane);  // не сохранилось — возвращаем то, что на сервере
  }
}

async function editComment(item) {
  const values = await formDialog({
    title: t('annot.editTitle'),
    submitLabel: t('annot.save'),
    fields: [{
      name: 'comment', label: t('annot.commentLabel'), type: 'textarea',
      hint: t('annot.commentHint'), maxLength: 1000, value: item.comment,
    }, {
      name: 'color', label: t('annot.colorLabel'), type: 'select', value: item.color,
      hint: t('annot.colorHint'),
      options: [
        { value: 'green', label: t('annot.color.green') },
        { value: 'red', label: t('annot.color.red') },
      ],
    }],
  });
  if (values === null) return;
  try {
    await api(`/api/annotations/${encodeURIComponent(item.id)}`, {
      method: 'PATCH', body: { comment: values.comment, color: values.color },
    });
  } catch (error) {
    return showNote(error.message);
  }
  loadAnnotations(active);
}

async function removeAnnotation(item) {
  const ok = await confirmDialog({
    title: t('annot.deleteTitle'),
    text: isContour(item)
      ? t('annot.deleteContour', { kind: t(`annot.kind.${item.kind}`) })
      : t('annot.deleteText', {
        n: learningItems(active).indexOf(item) + 1,
        name: item.comment || t('annot.noComment'),
      }),
    submitLabel: t('common.delete'),
  });
  if (!ok) return;
  try {
    await api(`/api/annotations/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
  } catch (error) {
    return showNote(error.message);
  }
  loadAnnotations(active);
}

// Список аннотаций активной половины (А-4)
function renderAnnotationList() {
  const list = $('annotList');
  if (!list) return;
  const items = learningItems(active);
  const empty = $('annotEmpty');
  empty.hidden = Boolean(items.length);
  empty.textContent = annotationsShown ? t('annot.empty') : t('annot.emptyHidden');
  list.replaceChildren(...items.map((item, index) => annotationRow(item, index + 1)));
  updateAnnotationHint();
}

function annotationRow(item, number) {
  const row = document.createElement('li');
  row.className = 'annot-row';
  row.dataset.id = item.id;
  if (item.id === active?.annotations.selectedId) row.classList.add('is-selected');

  // Тот же номер стоит на препарате рядом со стрелкой или контуром
  const badge = document.createElement('span');
  badge.className = `annot-row-number${item.color === 'red' ? ' is-red' : ''}`;
  badge.textContent = String(number);
  const text = document.createElement('span');
  text.className = 'annot-row-text';
  text.append(badge, item.comment || t('annot.noComment'));
  const meta = document.createElement('span');
  meta.className = 'annot-row-meta';
  meta.textContent = `${t(`annot.kind.${item.kind}`)} · ${t('annot.byAuthor', {
    author: item.author, date: formatDateTime(item.created_at),
  })}`;
  row.append(text, meta);

  row.addEventListener('click', () => {
    active?.annotations.select(item.id);
    focusAnnotation(item);
    highlightRow(item.id);
  });

  const actions = document.createElement('span');
  actions.className = 'annot-row-actions';
  actions.append(rowButton(t('annot.copyLink'), () => copyAnnotationLink(item)));
  if (item.can_edit && !phone()) {
    actions.append(
      rowButton(t('annot.edit'), () => editComment(item)),
      rowButton(t('common.delete'), () => removeAnnotation(item), 'is-danger'),
    );
  }
  row.append(actions);
  return row;
}

function rowButton(text, handler, extraClass = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `link-btn ${extraClass}`;
  button.textContent = text;
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

function highlightRow(id) {
  for (const row of $('annotList').children) row.classList.toggle('is-selected', row.dataset.id === id);
}

// Перевести поле зрения к аннотации (А-4, А-5)
function focusAnnotation(item) {
  const pane = active;
  if (!pane) return;
  const xs = item.points.map((point) => point[0]);
  const ys = item.points.map((point) => point[1]);
  const centre = [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
  const { viewport } = pane.viewer;
  viewport.panTo(pane.image.imageToViewportCoordinates(centre[0], centre[1]));
  viewport.applyConstraints();
}

async function goToAnnotation(id) {
  if (!annotationsShown) setAnnotationsShown(true);
  const pane = panes[0];
  if (!pane) return;
  if (!pane.annotationItems) await loadAnnotations(pane);
  const item = (pane.annotationItems ?? []).find((row) => row.id === id);
  if (!item) return showNote(t('annot.notFound'));
  setActive(pane);
  pane.annotations.select(id);
  focusAnnotation(item);
  if (isContour(item)) return cellularity?.open();  // контур клеточности живёт в своей панели
  $('annotPanel').hidden = false;
  $('btnAnnotations').setAttribute('aria-expanded', 'true');
  renderAnnotationList();
  highlightRow(id);
}

async function copyAnnotationLink(item) {
  const url = `${location.origin}/viewer?slide=${encodeURIComponent(active.slide.id)}&annotation=${encodeURIComponent(item.id)}`;
  try {
    await navigator.clipboard.writeText(url);
    showNote(t('annot.linkCopied'));
  } catch {
    window.prompt(t('annot.copyLink'), url);  // буфер обмена недоступен — показываем адрес
  }
}

// Короткое сообщение в строке состояния. error: false — для подсказок вроде
// рулетки: на телефоне видны только сообщения об ошибках (М-4).
function showNote(text, { error = true } = {}) {
  const status = $('statusTiles');
  status.textContent = text;
  status.classList.toggle('is-error', error);
  status.hidden = false;
  setTimeout(() => {
    if (status.textContent === text) {
      status.textContent = '';
      status.classList.remove('is-error');
      status.hidden = true;
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
    const center = pane.image.viewportToImageCoordinates(pane.viewer.viewport.getCenter(true));
    query.set(`slide${suffix}`, pane.slide.id);
    query.set(`x${suffix}`, Math.round(center.x));
    query.set(`y${suffix}`, Math.round(center.y));
    if (pane.slide.objective) query.set(`m${suffix}`, pane.magnification.toFixed(2));
    else query.set(`z${suffix}`, pane.imageZoom.toFixed(4));
    // Угол теперь любой, а не кратный 90°: до десятых хватает (Т-2)
    const angle = normalizeAngle(pane.rotation);
    if (angle) query.set(`r${suffix}`, String(Math.round(angle * 10) / 10));
    if (pane.flipped) query.set(`f${suffix}`, '1'); // отражение тоже передаётся (Т-3)
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
  if (query.get(`f${suffix}`) === '1') pane.setFlip(true);
  if (Number.isFinite(r)) viewport.setRotation(r, true);
  const zoom = Number.isFinite(m) && pane.slide.objective ? m / pane.slide.objective : z;
  if (zoom > 0) viewport.zoomTo(pane.image.imageToViewportZoom(zoom), null, true);
  if (Number.isFinite(x) && Number.isFinite(y)) viewport.panTo(pane.image.imageToViewportCoordinates(x, y), true);
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
  cellularity?.onPaneRemoved(active);
  active.annotations?.destroy();
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
    KeyA: () => setAnnotationsShown(!annotationsShown),  // показ аннотаций (А-11)
    KeyM: () => setTool(activeTool === 'ruler' ? null : 'ruler'),  // рулетка (Т-4)
    Enter: () => active?.annotations.closeDraft(),       // замкнуть начатый контур (А-2)
    Escape: () => {
      // По порядку: меню, начатое измерение, начатый контур, сам инструмент
      if (closeStageMenu()) return;
      if (binding) return stopBinding();
      if (active?.overlay?.clearMeasure()) return;
      if (!active?.annotations.cancelDraft()) setTool(null);
    },
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
    // closest? — цель бывает и не элементом (например, сам документ).
    if (event.target.closest?.('input:not([type=range], [type=checkbox]), select, textarea')) return;
    if (event.target.matches?.('input[type=range]') && event.code.startsWith('Arrow')) return;
    const action = actions[event.code];
    if (!action) return;
    event.preventDefault();
    action();
  });
}
