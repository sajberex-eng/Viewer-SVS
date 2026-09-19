// Вьювер: одна половина экрана в обычном режиме и две в режиме сравнения.
// Всё, что относится к одному скану, живёт в SlideView; здесь — общие панели,
// активная половина, связанная навигация и ссылка на поле зрения.
import { isDefault } from './adjust.js';
import { AdjustPanel } from './adjust-panel.js';
import { api, logout } from './api.js';
import { pickerDialog } from './dialog.js';
import { applyI18n, formatNumber, setLanguage, t } from './i18n.js';
import { FIXED_MAGNIFICATIONS, SlideView, ZOOM_STEP } from './slide-view.js';

const PAN_STEP = 0.2; // доля видимой области на одно нажатие стрелки
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
applyI18n();
main();

async function main() {
  const query = new URLSearchParams(location.search);
  user = await api('/api/me');
  $('userName').textContent = user.name || user.login;
  $('btnLogout').addEventListener('click', logout);

  const slideId = query.get('slide');
  if (!slideId) return showPageMessage(t('viewer.noSlide'));

  const slide = await loadSlide(slideId);
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
  first.viewer.addHandler('open', () => {
    restoreView(first, query, '');
    const shared = query.get('adj') && AdjustPanel.deserialize(query.get('adj'));
    if (shared) adjustPanel.setValues(shared, { save: false });
  });

  // Ссылка на два скана открывает сразу режим сравнения (С-11)
  const secondId = query.get('slide2');
  if (secondId) {
    const second = await loadSlide(secondId, { silent: true });
    if (!second) {
      showNote(t('compare.noAccess'));
    } else {
      const pane = addPane(second);
      pane.viewer.addHandler('open', () => {
        restoreView(pane, query, '2');
        // Связь включается, когда открылись обе половины: порядок не гарантирован
        if (query.get('link') === '1') whenAllOpen(() => setLinked(true));
      });
      applyCompareLayout();
    }
  }
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
    onClose: panes.length ? closePane : null,
    onPickWhite: (pane, position) => (active === pane ? adjustPanel.pickWhite(position) : false),
  });
  view.viewer.addHandler('open', () => {
    if (active !== view) view.applySavedAdjust(); // активную настроит панель
  });
  panes.push(view);
  return view;
}

function closePane(view) {
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
  document.title = `${view.slide.title} · ${t('app.title')}`;
  adjustPanel.attachTo({ adjuster: view.adjuster, viewer: view.viewer, storageKey: view.storageKey });
  $('adjustTarget').textContent = t('adjust.target', { title: view.slide.title });
  $('adjustTarget').hidden = panes.length < 2;
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
  $('btnCompare').hidden = comparing;
  if (comparing) ensureSplitter();
  else $('splitter')?.remove();
  for (const pane of panes) {
    pane.setActive(comparing && pane === active);
    pane.parts.paneClose.hidden = !comparing;
    pane.resize(); // у ещё не открытого слайда области просмотра нет
  }
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
  const chosen = await pickerDialog({
    title: t('compare.pick'),
    submitLabel: t('compare.open'),
    sections: [{
      name: 'slide',
      items: options.map((row) => ({
        id: row.id,
        label: row.title,
        hint: folders.get(row.folder_id) ?? '',
        checked: false,
      })),
    }],
    onSubmit: (values) => {
      if (values.slide.length !== 1) throw new Error(t('compare.pickOne'));
      return values;
    },
  });
  if (!chosen) return;

  const slide = await loadSlide(String(chosen.slide[0]), { silent: true });
  if (!slide) return showNote(t('compare.noAccess'));
  const pane = addPane(slide);
  // Новая половина открывается на «вписать» уже в своей ширине: иначе масштаб
  // считается по прежнему размеру и половины стартуют с разного увеличения.
  pane.viewer.addOnceHandler('open', () => requestAnimationFrame(() => pane.viewer.viewport.goHome(true)));
  applyCompareLayout();
  setActive(pane);
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
  panes[0].parts.paneClose.hidden = true;
  panes[1].parts.paneClose.hidden = false;
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
  $('btnReset').addEventListener('click', () => active?.resetView());
  $('btnFullscreen').addEventListener('click', toggleFullscreen);
  $('btnCompare').addEventListener('click', startCompare);
  $('btnSingle').addEventListener('click', leaveCompare);
  $('btnSwap').addEventListener('click', swapPanes);
  $('btnLink2').addEventListener('click', () => setLinked(!linked));
  $('adjustBoth').addEventListener('click', () => {
    for (const pane of panes) {
      if (pane !== active) pane.adjustPanel.setValues(adjustPanel.values);
    }
  });
  addEventListener('resize', () => {
    if (panes.length > 1) for (const pane of panes) pane.resize();
  });

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

function updateStatusBar() {
  if (!active) return;
  updateZoomPanel();
  const { slide } = active;
  $('statusSize').textContent = t('status.size', { w: formatNumber(slide.width), h: formatNumber(slide.height) });
  $('statusMpp').textContent = slide.mpp ? t('status.mpp', { mpp: formatNumber(slide.mpp, 4) }) : t('status.mpp.unknown');
  $('statusMag').textContent = t('status.mag', { mag: active.magnificationLabel() });
  const status = $('statusTiles');
  status.textContent = active.tilesStatus;
  status.classList.toggle('is-error', active.tilesFailed);
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
    if (withAdjust && !isDefault(pane.adjustPanel.values)) {
      query.set(`adj${suffix}`, pane.adjustPanel.serialize());
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
  const shared = query.get(`adj${suffix}`) && AdjustPanel.deserialize(query.get(`adj${suffix}`));
  if (shared) pane.adjustPanel.setValues(shared, { save: false });
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
    // В режиме сравнения щелчок заменяет скан в активной половине (С-10)
    link.addEventListener('click', async (event) => {
      if (panes.length < 2) return;
      event.preventDefault();
      await replaceActive(sibling.id);
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
  const neighbour = panes[index ? 0 : 1];
  active.destroy();
  panes.splice(index, 1);
  const pane = addPane(slide);
  // Новая половина встаёт на своё место: слева или справа от разделителя
  if (index === 0) $('panes').insertBefore(pane.element, $('splitter'));
  setLinked(false);
  panes = index === 0 ? [pane, neighbour] : [neighbour, pane];
  setActive(pane);
  applyCompareLayout();
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
    Tab: () => {
      if (panes.length > 1) setActive(panes[(panes.indexOf(active) + 1) % panes.length]);
    },
  };
  FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
    const action = () => zoomAllTo(magnification);
    actions[`Digit${index + 1}`] = action;
    actions[`Numpad${index + 1}`] = action;
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey || !active?.viewer.isOpen()) return;
    // В полях ввода клавиши принадлежат полю; у ползунков остаются только стрелки.
    if (event.target.closest('input:not([type=range], [type=checkbox]), select, textarea')) return;
    if (event.target.matches('input[type=range]') && event.code.startsWith('Arrow')) return;
    const action = actions[event.code];
    if (!action) return;
    event.preventDefault();
    action();
  });
}
