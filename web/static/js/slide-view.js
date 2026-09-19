// Одна половина экрана: изображение, панель увеличений, мини-карта, линейка,
// этикетка. В обычном режиме половина одна, в режиме сравнения их две, поэтому
// элементы ищутся внутри своего контейнера, а не по идентификаторам страницы.
import { ImageAdjuster } from './adjust.js';
import { loadSavedValues, saveValues } from './adjust-panel.js';
import { applyI18n, formatNumber, t } from './i18n.js';

const FIXED_MAGNIFICATIONS = [2, 4, 10, 20, 40]; // горячие клавиши 1–5
const MAX_DIGITAL_ZOOM = 2; // зум не дальше 2× от увеличения сканирования (Н-2)
const SCALEBAR_MAX_PX = 180;
const ZOOM_STEP = 1.3;
const SLIDER_STEPS = 1000;

const { Point } = OpenSeadragon;

export { FIXED_MAGNIFICATIONS, ZOOM_STEP };

export class SlideView {
  constructor({ slide, container, storageKey, onActivate, onViewChange, onClose, onPickWhite }) {
    this.slide = slide;
    this.element = container;
    this.storageKey = storageKey;
    this.onViewChange = onViewChange;
    this.onPickWhite = onPickWhite;
    this.parts = {};
    for (const node of container.querySelectorAll('[data-role]')) {
      this.parts[node.dataset.role] = node;
    }
    applyI18n(container);

    this.parts.title.textContent = slide.title;
    this.parts.stageRetry.addEventListener('click', () => location.reload());
    container.addEventListener('pointerdown', () => onActivate?.(this), true);
    if (onClose) {
      this.parts.paneClose.hidden = false;
      this.parts.paneClose.addEventListener('click', () => onClose(this));
    }

    this.viewer = this.#createViewer();
    this.adjuster = new ImageAdjuster(this.viewer);
    this.#initZoomPanel();
    this.#initScalebar();
    this.#initMinimap();
    this.#initLabel();
    this.#initTiles();
  }

  #createViewer() {
    const { slide } = this;
    const viewer = OpenSeadragon({
      element: this.parts.osd,
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
      navigatorElement: this.parts.navigator,
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
    // Клавиатуру целиком обрабатывает страница, иначе действия шли бы дважды.
    viewer.addHandler('canvas-key', (event) => { event.preventDefaultAction = true; });
    viewer.addHandler('canvas-click', (event) => {
      if (event.quick && this.onPickWhite?.(this, event.position)) event.preventDefaultAction = true;
    });
    viewer.addHandler('open-failed', () => this.showMessage(t('viewer.storageDown')));
    for (const event of ['pan', 'zoom', 'rotate']) {
      viewer.addHandler(event, () => this.onViewChange?.(this));
    }
    return viewer;
  }

  // ---------- увеличение ----------

  get imageZoom() {
    return this.viewer.viewport.viewportToImageZoom(this.viewer.viewport.getZoom(true));
  }

  get magnification() {
    return this.slide.objective ? this.slide.objective * this.imageZoom : null;
  }

  magnificationLabel() {
    // Без объектива и размера пикселя показывается процент от полного разрешения (Н-4).
    if (!this.slide.objective) return `${formatNumber(this.imageZoom * 100)} %`;
    return `${formatNumber(this.magnification, 1)}×`;
  }

  zoomToMagnification(magnification) {
    const { viewport } = this.viewer;
    viewport.zoomTo(viewport.imageToViewportZoom(magnification / this.slide.objective));
    viewport.applyConstraints();
  }

  zoomBy(factor) {
    this.viewer.viewport.zoomBy(factor);
    this.viewer.viewport.applyConstraints();
  }

  panByFraction(dx, dy, fraction) {
    const { viewport } = this.viewer;
    const bounds = viewport.getBounds();
    const step = Math.min(bounds.width, bounds.height) * fraction;
    viewport.panBy(new Point(dx * step, dy * step).rotate(-viewport.getRotation()));
    viewport.applyConstraints();
  }

  resetView() {
    this.viewer.viewport.setRotation(0);
    this.viewer.viewport.goHome();
  }

  #initZoomPanel() {
    const buttons = this.parts.zoomButtons;
    const fit = document.createElement('button');
    fit.className = 'zoom-btn';
    fit.textContent = t('zoom.fit');
    fit.title = t('zoom.fit.tip');
    fit.addEventListener('click', () => this.viewer.viewport.goHome());
    buttons.append(fit);

    FIXED_MAGNIFICATIONS.forEach((magnification, index) => {
      const button = document.createElement('button');
      button.className = 'zoom-btn';
      button.textContent = `${magnification}×`;
      // Кнопки выше увеличения сканирования неактивны: скан 20× не предлагает 40× (Н-1).
      button.disabled = !this.slide.objective || magnification > this.slide.objective;
      button.title = button.disabled
        ? t('zoom.unavailable.tip')
        : t('zoom.fixed.tip', { mag: magnification, key: index + 1 });
      button.addEventListener('click', () => this.zoomToMagnification(magnification));
      buttons.append(button);
    });

    // Ползунок логарифмический: одинаковый ход даёт одинаковую кратность зума.
    const slider = this.parts.zoomSlider;
    const range = () => [this.viewer.viewport.getMinZoom(), this.viewer.viewport.getMaxZoom()];
    let dragging = false;
    slider.addEventListener('input', () => {
      dragging = true;
      const [min, max] = range();
      this.viewer.viewport.zoomTo(min * (max / min) ** (slider.value / SLIDER_STEPS), null, true);
    });
    slider.addEventListener('change', () => { dragging = false; });

    this.updateZoomReadout = () => {
      this.parts.zoomCurrent.textContent = this.magnificationLabel();
      if (!dragging) {
        const [min, max] = range();
        slider.value = max > min
          ? (SLIDER_STEPS * Math.log(this.viewer.viewport.getZoom(true) / min)) / Math.log(max / min)
          : 0;
      }
    };
    for (const event of ['open', 'animation', 'resize']) {
      this.viewer.addHandler(event, this.updateZoomReadout);
    }

    const rotate = (degrees) => {
      const { viewport } = this.viewer;
      viewport.setRotation(viewport.getRotation() + degrees);
    };
    this.parts.rotateLeft.addEventListener('click', () => rotate(-90));
    this.parts.rotateRight.addEventListener('click', () => rotate(90));
  }

  #initScalebar() {
    if (!this.slide.mpp) return;
    this.parts.scalebar.hidden = false;
    const update = () => {
      const micronsPerPixel = this.slide.mpp / this.imageZoom;
      const maxLength = micronsPerPixel * SCALEBAR_MAX_PX;
      const magnitude = 10 ** Math.floor(Math.log10(maxLength));
      const length = [5, 2, 1].find((n) => n * magnitude <= maxLength) * magnitude;
      this.parts.scalebarLine.style.width = `${length / micronsPerPixel}px`;
      this.parts.scalebarLabel.textContent = length >= 1000
        ? `${formatNumber(length / 1000, length % 1000 ? 1 : 0)} ${t('unit.mm')}`
        : `${formatNumber(length, length < 1 ? 1 : 0)} ${t('unit.um')}`;
    };
    for (const event of ['open', 'animation', 'resize']) this.viewer.addHandler(event, update);
  }

  #initMinimap() {
    const { minimap, minimapToggle } = this.parts;
    minimapToggle.addEventListener('click', () => {
      const collapsed = minimap.classList.toggle('is-collapsed');
      minimapToggle.textContent = collapsed ? '▸' : '▾';
      minimapToggle.setAttribute('aria-expanded', String(!collapsed));
      if (!collapsed) {
        this.viewer.navigator.updateSize();
        this.viewer.navigator.update(this.viewer.viewport);
      }
    });
  }

  // На этикетке бывают персональные данные, поэтому панель закрыта по умолчанию,
  // изображение запрашивается только по нажатию, а сервер отдаёт его без кэша.
  #initLabel() {
    const { paneLabel, labelPanel, labelImage, labelError, labelClose, labelRotate } = this.parts;
    paneLabel.hidden = false;
    if (!this.slide.has_label) {
      paneLabel.disabled = true;
      paneLabel.title = t('top.label.none');
      return;
    }
    let rotation = 0;
    let loaded = false;
    this.toggleLabel = (open = labelPanel.hidden) => {
      labelPanel.hidden = !open;
      paneLabel.classList.toggle('is-active', open);
      if (open && !loaded) {
        loaded = true;
        labelImage.src = `/api/slides/${encodeURIComponent(this.slide.id)}/label.jpg`;
      }
    };
    labelImage.addEventListener('error', () => {
      labelImage.hidden = true;
      labelError.hidden = false;
      labelError.textContent = t('label.failed');
    });
    paneLabel.addEventListener('click', () => this.toggleLabel());
    labelClose.addEventListener('click', () => this.toggleLabel(false));
    labelRotate.addEventListener('click', () => {
      rotation = (rotation + 90) % 360;
      labelImage.style.transform = `rotate(${rotation}deg)`;
      labelImage.classList.toggle('is-sideways', rotation % 180 !== 0);
    });
  }

  #initTiles() {
    this.tilesStatus = '';
    this.tilesFailed = false;
    this.viewer.addHandler('open', () => {
      this.viewer.world.getItemAt(0).addHandler('fully-loaded-change', (event) => {
        if (event.fullyLoaded) this.tilesFailed = false;
        this.tilesStatus = event.fullyLoaded ? '' : t(this.tilesFailed ? 'status.tilesError' : 'status.tilesLoading');
        this.onViewChange?.(this);
      });
    });
    this.viewer.addHandler('tile-load-failed', () => {
      this.tilesFailed = true;
      this.tilesStatus = t('status.tilesError');
      this.onViewChange?.(this);
    });
  }

  // ---------- координаты для связанной навигации ----------

  // Центр поля зрения в микрометрах от левого верхнего угла препарата.
  // Микрометры, а не пиксели: у сканов 20× и 40× разный размер пикселя (С-7).
  get centerMicrons() {
    const center = this.viewer.viewport.viewportToImageCoordinates(this.viewer.viewport.getCenter(true));
    const scale = this.slide.mpp || 1;
    return { x: center.x * scale, y: center.y * scale };
  }

  panToMicrons({ x, y }, immediately = false) {
    const scale = this.slide.mpp || 1;
    const { viewport } = this.viewer;
    viewport.panTo(viewport.imageToViewportCoordinates(x / scale, y / scale), immediately);
    viewport.applyConstraints(immediately);
  }

  setMagnification(magnification, immediately = false) {
    if (!this.slide.objective) return;
    const { viewport } = this.viewer;
    const zoom = viewport.imageToViewportZoom(magnification / this.slide.objective);
    const limited = Math.min(Math.max(zoom, viewport.getMinZoom()), viewport.getMaxZoom());
    viewport.zoomTo(limited, null, immediately);
  }

  get rotation() {
    return this.viewer.viewport.getRotation();
  }

  setRotation(degrees, immediately = false) {
    this.viewer.viewport.setRotation(degrees, immediately);
  }

  setActive(active) {
    this.element.classList.toggle('is-active', active);
  }

  // Ширина половины изменилась. Размер контейнера OpenSeadragon отслеживает
  // сам (autoResize), поэтому достаточно перерисовать на следующем кадре,
  // когда браузер уже применил новую разметку.
  resize() {
    requestAnimationFrame(() => {
      if (!this.viewer.isOpen()) return;
      this.viewer.forceRedraw();
      this.updateZoomReadout?.();
    });
  }

  // Сохранённые настройки этой половины: применяются при открытии слайда.
  applySavedAdjust() {
    const saved = loadSavedValues(this.storageKey);
    if (saved) this.adjuster.setValues(saved);
  }

  // «Применить к обеим» (С-9): значения соседней половины ставятся и сохраняются.
  applyAdjust(values) {
    this.adjuster.setValues(values);
    saveValues(this.storageKey, values);
  }

  showMessage(text, { retry = true } = {}) {
    this.parts.stageMessageText.textContent = text;
    this.parts.stageRetry.hidden = !retry;
    this.parts.stageMessage.hidden = false;
  }

  destroy() {
    this.adjuster.destroy?.();
    this.viewer.destroy();
    this.element.remove();
  }
}
