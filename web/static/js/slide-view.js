// Одна половина экрана: изображение, панель увеличений, мини-карта, линейка,
// этикетка. В обычном режиме половина одна, в режиме сравнения их две, поэтому
// элементы ищутся внутри своего контейнера, а не по идентификаторам страницы.
import { ImageAdjuster } from './adjust.js';
import { clampValues, loadSavedValues, saveValues } from './adjust-panel.js';
import { applyI18n, formatNumber, t } from './i18n.js';

// Горячие клавиши 1–4. С 2× ряд начинался зря: на микроскопе увеличения
// начинаются с 4× (решение заказчика 2026-09-21).
const FIXED_MAGNIFICATIONS = [4, 10, 20, 40];
const MAX_DIGITAL_ZOOM = 2; // зум не дальше 2× от увеличения сканирования (Н-2)
const SCALEBAR_MAX_PX = 180;
const ZOOM_STEP = 1.3;

const { Point } = OpenSeadragon;

export { FIXED_MAGNIFICATIONS, ZOOM_STEP };

export class SlideView {
  constructor({ slide, container, storageKey, onActivate, onViewChange, onClose, onPickWhite, onContextMenu }) {
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
    // Путь до папки скана: виден у каждой половины и открывает её в каталоге (ИН-3)
    const path = slide.path ?? [];
    if (path.length) {
      this.parts.path.hidden = false;
      this.parts.path.textContent = path.map((folder) => folder.name).join(' › ');
      this.parts.path.href = `/?folder=${path[path.length - 1].id}`;
    }
    this.parts.stageRetry.addEventListener('click', () => location.reload());
    container.addEventListener('pointerdown', () => onActivate?.(this), true);
    // Меню браузера подавляется только над самим препаратом: на мини-карте,
    // этикетке и линейке правая кнопка работает как обычно (Т-5)
    this.parts.stage.addEventListener('contextmenu', (event) => {
      if (!event.target.closest('.osd, .annot-layer, .overlay-layer')) return;
      onContextMenu?.(this, event, this.imageFromClient(event.clientX, event.clientY));
    });
    // Крестик виден только в режиме сравнения: это решает viewer.js
    this.parts.paneClose.addEventListener('click', () => onClose(this));

    this.viewer = this.#createViewer();
    // Размер области просмотра запомнен при создании половины, а экран мог
    // с тех пор разделиться (ссылка на два скана): «вписать» считается заново.
    // Обработчик добавлен первым и срабатывает раньше восстановления вида из ссылки.
    this.viewer.addHandler('open', () => {
      this.#syncViewportSize();
      this.viewer.viewport.goHome(true);
    });
    this.adjuster = new ImageAdjuster(this.viewer);
    const saved = loadSavedValues(storageKey);
    if (saved) this.adjuster.setValues(saved);
    this.#initZoomReadout();
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
      // Размер задаём сами: иначе мини-карта растёт от ширины окна и в режиме
      // сравнения вылезает за свою половину
      navigatorMaintainSizeRatio: false,
      navigatorAutoResize: false,
      navigatorWidth: 220,
      navigatorHeight: 150,
      maxZoomPixelRatio: MAX_DIGITAL_ZOOM,
      minZoomImageRatio: 1,
      preserveImageSizeOnResize: true,
      animationTime: 0.5,
      zoomPerScroll: ZOOM_STEP,
      timeout: 60000, // первый тайл большого скана на медленном канале приходит не сразу
      // Не больше восьми тайлов в работе на половину экрана (СК-6): при быстром
      // перемещении сервер с двумя ядрами иначе дорабатывает тайлы, которые уже
      // ушли с экрана, и нужные ждут очереди
      imageLoaderLimit: 8,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
      gestureSettingsTouch: { clickToZoom: false, dblClickToZoom: true },
    });
    // Клавиатуру целиком обрабатывает страница, иначе действия шли бы дважды.
    viewer.addHandler('canvas-key', (event) => { event.preventDefaultAction = true; });
    viewer.addHandler('canvas-click', (event) => {
      // Пипетка берёт цвет с холста библиотеки, а он при отражении нарисован
      // зеркально: положение щелчка приходится вернуть обратно (Т-3)
      if (event.quick && this.onPickWhite?.(this, this.canvasPoint(event.position))) {
        event.preventDefaultAction = true;
      }
    });
    viewer.addHandler('open-failed', () => this.showMessage(t('viewer.storageDown')));
    for (const event of ['pan', 'zoom', 'rotate', 'flip']) {
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

  // Кнопки фиксированных увеличений переключают сразу, без плавного перехода:
  // так быстрее и предсказуемее, а пустых областей не возникает — до подхода
  // чётких тайлов OpenSeadragon показывает растянутые с предыдущего уровня (Н-6).
  zoomToMagnification(magnification, immediately = true) {
    const { viewport } = this.viewer;
    viewport.zoomTo(viewport.imageToViewportZoom(magnification / this.slide.objective), null, immediately);
    viewport.applyConstraints(immediately);
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

  // Управление увеличением общее на весь экран (см. viewer.js), а в заголовке
  // половины видно её текущее увеличение: так сразу заметно, сопоставимы ли они.
  #initZoomReadout() {
    this.updateZoomReadout = () => {
      this.parts.zoomCurrent.textContent = this.magnificationLabel();
      this.onViewChange?.(this);
    };
    for (const event of ['open', 'animation', 'resize']) {
      this.viewer.addHandler(event, this.updateZoomReadout);
    }
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
    // На телефоне мини-карта закрывала бы половину экрана: сворачиваем сразу,
    // развернуть можно той же кнопкой.
    if (matchMedia('(max-width: 700px)').matches) {
      minimap.classList.add('is-collapsed');
      minimapToggle.textContent = '▸';
      minimapToggle.setAttribute('aria-expanded', 'false');
    }
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
    const { paneLabel, labelPanel, labelFrame, labelImage, labelError, labelClose, labelRotate } = this.parts;
    paneLabel.hidden = false;
    if (!this.slide.has_label) {
      paneLabel.disabled = true;
      paneLabel.title = t('top.label.none');
      return;
    }
    let rotation = 0;
    let loaded = false;
    // Этикетка вписывается в ширину панели при любом повороте; слишком высокая
    // (узкая этикетка боком) ограничивается по высоте.
    const LABEL_MAX_HEIGHT = 320;
    const layout = () => {
      const { naturalWidth, naturalHeight } = labelImage;
      const frameWidth = labelFrame.clientWidth;
      if (!naturalWidth || !frameWidth) return;
      const sideways = rotation % 180 !== 0;
      // размеры на экране после поворота
      const [shownW, shownH] = sideways ? [naturalHeight, naturalWidth] : [naturalWidth, naturalHeight];
      const scale = Math.min(frameWidth / shownW, LABEL_MAX_HEIGHT / shownH);
      labelFrame.style.height = `${Math.round(shownH * scale)}px`;
      labelImage.style.width = `${naturalWidth * scale}px`;
      labelImage.style.height = `${naturalHeight * scale}px`;
      labelImage.style.transform = `translate(-50%, -50%) rotate(${rotation}deg)`;
    };
    labelImage.addEventListener('load', layout);
    this.toggleLabel = (open = labelPanel.hidden) => {
      labelPanel.hidden = !open;
      paneLabel.classList.toggle('is-active', open);
      if (open && !loaded) {
        loaded = true;
        labelImage.src = `/api/slides/${encodeURIComponent(this.slide.id)}/label.jpg`;
      }
      if (open) layout(); // пока панель скрыта, ширина рамки равна нулю
    };
    labelImage.addEventListener('error', () => {
      labelFrame.hidden = true;
      labelError.hidden = false;
      labelError.textContent = t('label.failed');
    });
    paneLabel.addEventListener('click', () => this.toggleLabel());
    labelClose.addEventListener('click', () => this.toggleLabel(false));
    labelRotate.addEventListener('click', () => {
      rotation = (rotation + 90) % 360;
      layout();
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
    return this.#centerMicrons(true);
  }

  // Куда половина едет, а не где она сейчас. Связь запоминается по этому
  // положению: иначе, если отпустить Shift посреди плавного хода, она
  // запомнит середину пути (С-11).
  get targetCenterMicrons() {
    return this.#centerMicrons(false);
  }

  #centerMicrons(current) {
    const { viewport } = this.viewer;
    const center = viewport.viewportToImageCoordinates(viewport.getCenter(current));
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

  // ---------- зеркальное отражение (Т-3) ----------

  get flipped() {
    return this.viewer.viewport.getFlip();
  }

  // Отражается только эта половина. Библиотека зеркалит рисование (и мини-карту),
  // но не пересчёт координат, поэтому всё остальное считается через pixelFromImage
  // и imageFromPixel ниже.
  setFlip(flipped) {
    if (this.flipped === Boolean(flipped)) return;
    this.viewer.viewport.setFlip(Boolean(flipped));
    this.#showFlipMark();
  }

  #showFlipMark() {
    this.parts.flipMark.hidden = !this.flipped;
  }

  // ---------- точка скана ↔ точка на экране ----------

  // Отражение живёт только в рисовании, поэтому зеркалим x сами: без этого
  // аннотации, рулетка и общий курсор встают на противоположный край.
  #mirror(point) {
    if (!this.flipped) return point;
    return new Point(this.viewer.viewport.getContainerSize().x - point.x, point.y);
  }

  // Точка холста библиотеки по положению щелчка: библиотека сама вернула его
  // в незеркальные координаты, а холст нарисован зеркально.
  canvasPoint(position) {
    return this.#mirror(position);
  }

  // Точка скана → точка внутри половины экрана
  pixelFromImage(x, y) {
    const { viewport } = this.viewer;
    return this.#mirror(viewport.pixelFromPoint(viewport.imageToViewportCoordinates(x, y), true));
  }

  // Точка внутри половины экрана → точка скана
  imageFromPixel(point) {
    const { viewport } = this.viewer;
    return viewport.viewportToImageCoordinates(viewport.pointFromPixel(this.#mirror(point), true));
  }

  // То же, но от координат события мыши на странице
  imageFromClient(clientX, clientY) {
    const box = this.parts.osd.getBoundingClientRect();
    return this.imageFromPixel(new Point(clientX - box.left, clientY - box.top));
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
      this.#syncViewportSize();
      this.viewer.viewport.applyConstraints(true);
      this.viewer.forceRedraw();
      this.updateZoomReadout?.();
    });
  }

  // Собственное слежение OpenSeadragon за размером срабатывает не сразу,
  // а до этого он считает увеличение по прежнему контейнеру: у второй
  // половины оно выходило вдвое меньше. Первый аргумент resize это размер,
  // а не флаг: вызов resize(true) ломает пересчёт зума.
  #syncViewportSize() {
    const { clientWidth, clientHeight } = this.parts.osd;
    if (clientWidth && clientHeight) {
      this.viewer.viewport.resize(new OpenSeadragon.Point(clientWidth, clientHeight), false);
    }
  }

  // Настройки изображения этой половины (И-7). Хранятся у самой половины,
  // панель настройки одна и только показывает значения активной.
  get adjustValues() {
    return this.adjuster.values;
  }

  // save: false — для настроек из ссылки: они показываются, но не заменяют
  // сохранённые пользователем для этого скана.
  setAdjust(values, { save = true } = {}) {
    this.adjuster.setValues(clampValues(values, this.adjuster.values));
    if (save) saveValues(this.storageKey, this.adjuster.values);
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
