import { ImageAdjuster, isDefault } from './adjust.js';
import { AdjustPanel } from './adjust-panel.js';
import { api, logout } from './api.js';
import { applyI18n, formatNumber, setLanguage, t } from './i18n.js';

const FIXED_MAGNIFICATIONS = [2, 4, 10, 20, 40]; // горячие клавиши 1–5
const MAX_DIGITAL_ZOOM = 2; // зум не дальше 2× от увеличения сканирования (Н-2)
const SCALEBAR_MAX_PX = 180;
const PAN_STEP = 0.2; // доля видимой области на одно нажатие стрелки
const ZOOM_STEP = 1.3;
const SLIDER_STEPS = 1000;
const LINK_ROLES = ['teacher', 'admin'];

const $ = (id) => document.getElementById(id);
const { Point } = OpenSeadragon;

setLanguage('ru');
applyI18n();
main();

async function main() {
  const query = new URLSearchParams(location.search);
  const user = await api('/api/me');
  $('userName').textContent = user.name || user.login;
  $('btnLogout').addEventListener('click', logout);
  $('stageRetry').addEventListener('click', () => location.reload());

  const slideId = query.get('slide');
  if (!slideId) return showMessage(t('viewer.noSlide'), { retry: false });

  let slide;
  try {
    slide = await api(`/api/slides/${encodeURIComponent(slideId)}`);
  } catch (error) {
    return showMessage(error.status === 404 ? t('viewer.notFound') : error.message, { retry: error.status !== 404 });
  }

  document.title = `${slide.title} · ${t('app.title')}`;
  $('slideTitle').textContent = slide.title;
  initSlidesPanel(slide);
  initStatusBar(slide);

  const viewer = createViewer(slide);
  const adjuster = new ImageAdjuster(viewer);
  const adjustPanel = new AdjustPanel({
    panel: $('adjustPanel'),
    adjuster,
    viewer,
    storageKey: `svsviewer:adjust:${user.login}:${slide.id}`,
    onChange: (values) => { $('adjustSavedDot').hidden = isDefault(values); },
  });

  initZoomPanel(viewer, slide);
  initScalebar(viewer, slide);
  initCursorReadout(viewer, slide);
  initTileStatus(viewer);
  initTopbar(viewer, slide, adjustPanel, user);
  initMinimapToggle(viewer);
  initHotkeys(viewer, slide);

  viewer.addHandler('open', () => {
    restoreView(viewer, slide, query);
    adjustPanel.loadSaved();
    const shared = query.get('adj') && AdjustPanel.deserialize(query.get('adj'));
    if (shared) adjustPanel.setValues(shared, { save: false });
  });
  viewer.addHandler('open-failed', () => showMessage(t('viewer.storageDown')));
}

function createViewer(slide) {
  const viewer = OpenSeadragon({
    element: $('osd'),
    drawer: 'canvas', // 2D-canvas служит источником для шейдера коррекций (adjust.js)
    tileSources: {
      Image: {
        xmlns: 'http://schemas.microsoft.com/deepzoom/2008',
        Url: slide.tiles.url,
        Format: slide.tiles.format,
        Overlap: String(slide.tiles.overlap),
        TileSize: String(slide.tiles.tile_size),
        Size: { Width: String(slide.width), Height: String(slide.height) },
      },
    },
    showNavigationControl: false,
    showNavigator: true,
    navigatorId: 'navigator',
    navigatorDisplayRegionColor: '#d12f2f',
    maxZoomPixelRatio: MAX_DIGITAL_ZOOM,
    minZoomImageRatio: 1,
    preserveImageSizeOnResize: true,
    animationTime: 0.5,
    zoomPerScroll: ZOOM_STEP,
    timeout: 60000, // первый тайл большого слайда на медленном канале приходит не сразу
    gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
    gestureSettingsTouch: { clickToZoom: false, dblClickToZoom: true },
  });
  // Клавиатуру целиком обрабатывает initHotkeys, иначе действия выполнялись бы дважды.
  viewer.addHandler('canvas-key', (event) => { event.preventDefaultAction = true; });
  return viewer;
}

// ---------- увеличение ----------

const imageZoom = (viewer) => viewer.viewport.viewportToImageZoom(viewer.viewport.getZoom(true));

function magnificationLabel(viewer, slide) {
  // Без объектива и размера пикселя показывается процент от полного разрешения (Н-4).
  if (!slide.objective) return `${formatNumber(imageZoom(viewer) * 100)} %`;
  return `${formatNumber(slide.objective * imageZoom(viewer), 1)}×`;
}

function zoomToMagnification(viewer, slide, magnification) {
  viewer.viewport.zoomTo(viewer.viewport.imageToViewportZoom(magnification / slide.objective));
  viewer.viewport.applyConstraints();
}

function initZoomPanel(viewer, slide) {
  const buttons = $('zoomButtons');
  const fit = document.createElement('button');
  fit.className = 'zoom-btn';
  fit.textContent = t('zoom.fit');
  fit.title = t('zoom.fit.tip');
  fit.addEventListener('click', () => viewer.viewport.goHome());
  buttons.append(fit);

  FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
    const button = document.createElement('button');
    button.className = 'zoom-btn';
    button.textContent = `${magnification}×`;
    // Кнопки выше увеличения сканирования неактивны: скан 20× не предлагает 40× (Н-1).
    button.disabled = !slide.objective || magnification > slide.objective;
    button.title = button.disabled ? t('zoom.unavailable.tip') : t('zoom.fixed.tip', { mag: magnification, key: index + 1 });
    button.addEventListener('click', () => zoomToMagnification(viewer, slide, magnification));
    buttons.append(button);
  });

  // Ползунок логарифмический: одинаковый ход даёт одинаковую кратность зума.
  const slider = $('zoomSlider');
  const range = () => [viewer.viewport.getMinZoom(), viewer.viewport.getMaxZoom()];
  let dragging = false;
  slider.addEventListener('input', () => {
    dragging = true;
    const [min, max] = range();
    viewer.viewport.zoomTo(min * (max / min) ** (slider.value / SLIDER_STEPS), null, true);
  });
  slider.addEventListener('change', () => { dragging = false; });

  const update = () => {
    const label = magnificationLabel(viewer, slide);
    $('zoomCurrent').textContent = label;
    $('statusMag').textContent = t('status.mag', { mag: label });
    if (!dragging) {
      const [min, max] = range();
      slider.value = max > min ? (SLIDER_STEPS * Math.log(viewer.viewport.getZoom(true) / min)) / Math.log(max / min) : 0;
    }
  };
  for (const event of ['open', 'animation', 'resize']) viewer.addHandler(event, update);

  const rotate = (degrees) => viewer.viewport.setRotation(viewer.viewport.getRotation() + degrees);
  $('rotateLeft').addEventListener('click', () => rotate(-90));
  $('rotateRight').addEventListener('click', () => rotate(90));
}

// ---------- масштабная линейка ----------

function initScalebar(viewer, slide) {
  if (!slide.mpp) return;
  $('scalebar').hidden = false;
  const update = () => {
    const micronsPerPixel = slide.mpp / imageZoom(viewer);
    const maxLength = micronsPerPixel * SCALEBAR_MAX_PX;
    const magnitude = 10 ** Math.floor(Math.log10(maxLength));
    const length = [5, 2, 1].find((n) => n * magnitude <= maxLength) * magnitude;
    $('scalebarLine').style.width = `${length / micronsPerPixel}px`;
    $('scalebarLabel').textContent = length >= 1000
      ? `${formatNumber(length / 1000, length % 1000 ? 1 : 0)} ${t('unit.mm')}`
      : `${formatNumber(length, length < 1 ? 1 : 0)} ${t('unit.um')}`;
  };
  for (const event of ['open', 'animation', 'resize']) viewer.addHandler(event, update);
}

// ---------- строка состояния ----------

function initStatusBar(slide) {
  $('statusSize').textContent = t('status.size', { w: formatNumber(slide.width), h: formatNumber(slide.height) });
  $('statusMpp').textContent = slide.mpp ? t('status.mpp', { mpp: formatNumber(slide.mpp, 4) }) : t('status.mpp.unknown');
}

function initCursorReadout(viewer, slide) {
  const readout = $('statusCursor');
  viewer.element.addEventListener('pointermove', (event) => {
    if (!viewer.isOpen()) return;
    const rect = viewer.element.getBoundingClientRect();
    const point = viewer.viewport.viewerElementToImageCoordinates(new Point(event.clientX - rect.left, event.clientY - rect.top));
    const inside = point.x >= 0 && point.y >= 0 && point.x < slide.width && point.y < slide.height;
    readout.textContent = inside ? t('status.cursor', { x: formatNumber(Math.floor(point.x)), y: formatNumber(Math.floor(point.y)) }) : '';
  }, true);
  viewer.element.addEventListener('pointerleave', () => { readout.textContent = ''; });
}

function initTileStatus(viewer) {
  const status = $('statusTiles');
  let failed = false;
  let checking = false;
  viewer.addHandler('open', () => {
    viewer.world.getItemAt(0).addHandler('fully-loaded-change', (event) => {
      if (event.fullyLoaded) failed = false;
      status.classList.toggle('is-error', failed);
      status.textContent = event.fullyLoaded ? '' : t(failed ? 'status.tilesError' : 'status.tilesLoading');
    });
  });
  viewer.addHandler('tile-load-failed', async () => {
    failed = true;
    status.classList.add('is-error');
    status.textContent = t('status.tilesError');
    if (checking) return;
    checking = true;
    try {
      await api('/api/me'); // истёкшая сессия уводит на страницу входа
    } catch {
      // сеть недоступна: сообщение об ошибке уже показано
    }
    setTimeout(() => { checking = false; }, 10000);
  });
}

// ---------- верхняя панель ----------

function initTopbar(viewer, slide, adjustPanel, user) {
  const panel = $('adjustPanel');
  const toggleAdjust = (open = panel.hidden) => {
    panel.hidden = !open;
    $('btnAdjust').setAttribute('aria-expanded', String(open));
  };
  $('btnAdjust').addEventListener('click', () => toggleAdjust());
  $('adjustClose').addEventListener('click', () => toggleAdjust(false));
  $('btnReset').addEventListener('click', () => resetView(viewer));
  $('btnFullscreen').addEventListener('click', toggleFullscreen);

  if (!LINK_ROLES.includes(user.role)) return;
  const button = $('btnLink');
  const popover = $('linkPopover');
  const refresh = () => { $('linkUrl').value = buildLink(viewer, slide, $('linkWithAdjust').checked ? adjustPanel : null); };
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

function resetView(viewer) {
  viewer.viewport.setRotation(0);
  viewer.viewport.goHome();
}

function toggleFullscreen() {
  const root = document.documentElement;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else {
    (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
  }
}

// ---------- ссылка на поле зрения (Н-9) ----------

function buildLink(viewer, slide, adjustPanel) {
  const center = viewer.viewport.viewportToImageCoordinates(viewer.viewport.getCenter(true));
  const query = new URLSearchParams({ slide: slide.id, x: Math.round(center.x), y: Math.round(center.y) });
  if (slide.objective) query.set('m', (slide.objective * imageZoom(viewer)).toFixed(2));
  else query.set('z', imageZoom(viewer).toFixed(4));
  const rotation = viewer.viewport.getRotation();
  if (rotation) query.set('r', rotation);
  if (adjustPanel && !isDefault(adjustPanel.values)) query.set('adj', adjustPanel.serialize());
  return `${location.origin}/viewer?${query}`;
}

function restoreView(viewer, slide, query) {
  const number = (name) => (query.has(name) ? Number(query.get(name)) : NaN);
  const { viewport } = viewer;
  const [x, y, m, z, r] = ['x', 'y', 'm', 'z', 'r'].map(number);
  if (Number.isFinite(r)) viewport.setRotation(r, true);
  const zoom = Number.isFinite(m) && slide.objective ? m / slide.objective : z;
  if (zoom > 0) viewport.zoomTo(viewport.imageToViewportZoom(zoom), null, true);
  if (Number.isFinite(x) && Number.isFinite(y)) viewport.panTo(viewport.imageToViewportCoordinates(x, y), true);
  viewport.applyConstraints(true);
}

// ---------- панели ----------

function initSlidesPanel(slide) {
  const list = $('slidesList');
  for (const sibling of slide.siblings) {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `/viewer?slide=${encodeURIComponent(sibling.id)}`;
    link.className = 'slide-thumb';
    if (sibling.id === slide.id) link.setAttribute('aria-current', 'true');
    const image = document.createElement('img');
    image.src = `/api/slides/${encodeURIComponent(sibling.id)}/thumbnail.jpg`;
    image.alt = '';
    image.loading = 'lazy';
    const caption = document.createElement('span');
    caption.textContent = sibling.title;
    link.append(image, caption);
    item.append(link);
    list.append(item);
  }
  list.querySelector('[aria-current]')?.scrollIntoView({ block: 'nearest' });

  // На планшете панель свёрнута по умолчанию.
  const panel = $('slidesPanel');
  const toggle = $('slidesToggle');
  const setCollapsed = (collapsed) => {
    panel.classList.toggle('is-collapsed', collapsed);
    toggle.textContent = collapsed ? '›' : '‹';
    toggle.setAttribute('aria-expanded', String(!collapsed));
  };
  setCollapsed(matchMedia('(pointer: coarse), (max-width: 1100px)').matches);
  toggle.addEventListener('click', () => setCollapsed(!panel.classList.contains('is-collapsed')));
}

function initMinimapToggle(viewer) {
  const minimap = $('minimap');
  const toggle = $('minimapToggle');
  toggle.addEventListener('click', () => {
    const collapsed = minimap.classList.toggle('is-collapsed');
    toggle.textContent = collapsed ? '▸' : '▾';
    toggle.setAttribute('aria-expanded', String(!collapsed));
    if (!collapsed) {
      viewer.navigator.updateSize();
      viewer.navigator.update(viewer.viewport);
    }
  });
}

// ---------- горячие клавиши (Н-8) ----------

function initHotkeys(viewer, slide) {
  const pan = (dx, dy) => {
    const { viewport } = viewer;
    const bounds = viewport.getBounds();
    const step = Math.min(bounds.width, bounds.height) * PAN_STEP;
    viewport.panBy(new Point(dx * step, dy * step).rotate(-viewport.getRotation()));
    viewport.applyConstraints();
  };
  const zoomBy = (factor) => {
    viewer.viewport.zoomBy(factor);
    viewer.viewport.applyConstraints();
  };
  // Коды клавиш не зависят от раскладки: F и R работают и в русской.
  const actions = {
    Digit0: () => viewer.viewport.goHome(),
    Numpad0: () => viewer.viewport.goHome(),
    Equal: () => zoomBy(ZOOM_STEP),
    NumpadAdd: () => zoomBy(ZOOM_STEP),
    Minus: () => zoomBy(1 / ZOOM_STEP),
    NumpadSubtract: () => zoomBy(1 / ZOOM_STEP),
    ArrowLeft: () => pan(-1, 0),
    ArrowRight: () => pan(1, 0),
    ArrowUp: () => pan(0, -1),
    ArrowDown: () => pan(0, 1),
    KeyF: toggleFullscreen,
    KeyR: () => resetView(viewer),
  };
  FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
    const action = () => {
      if (slide.objective && magnification <= slide.objective) zoomToMagnification(viewer, slide, magnification);
    };
    actions[`Digit${index + 1}`] = action;
    actions[`Numpad${index + 1}`] = action;
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey || !viewer.isOpen()) return;
    // В полях ввода клавиши принадлежат полю; у ползунков остаются только стрелки.
    if (event.target.closest('input:not([type=range], [type=checkbox]), select, textarea')) return;
    if (event.target.matches('input[type=range]') && event.code.startsWith('Arrow')) return;
    const action = actions[event.code];
    if (!action) return;
    event.preventDefault();
    action();
  });
}

function showMessage(text, { retry = true } = {}) {
  $('stageMessageText').textContent = text;
  $('stageRetry').hidden = !retry;
  $('stageMessage').hidden = false;
}
