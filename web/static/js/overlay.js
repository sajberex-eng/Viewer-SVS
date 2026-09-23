// Временные пометки над препаратом одной половины экрана: рулетка (Т-4),
// ориентиры привязки (С-10) и общий курсор (С-8).
//
// Ничего из этого не хранится: ни в базе, ни в ссылке. Точки лежат в координатах
// скана, поэтому держатся за те же клетки при любом увеличении, повороте и
// отражении; длина считается по размеру пикселя из файла и от угла не зависит.
// Аннотации (этап 9) живут отдельно, в annotations.js: они постоянные.
import { formatNumber, t } from './i18n.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const TICK = 7;       // половина длины засечки на конце отрезка, в точках экрана
const CROSS = 11;     // половина перекрестия общего курсора (С-8)

export class OverlayLayer {
  constructor({ view, onChange, onLandmark }) {
    this.view = view;
    this.viewer = view.viewer;
    this.onChange = onChange;
    this.onLandmark = onLandmark;
    this.tool = null;       // null | 'ruler' | 'landmarks'
    this.points = [];       // концы измерения, в координатах скана
    this.hover = null;      // куда указывает мышь, пока вторая точка не поставлена
    this.landmarks = [];    // отмеченные ориентиры, в координатах скана
    this.crosshair = null;  // точка общего курсора, в координатах скана
    this.#build();
  }

  #build() {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'overlay-layer');
    this.ruler = document.createElementNS(SVG_NS, 'g');
    this.marks = document.createElementNS(SVG_NS, 'g');
    this.cross = document.createElementNS(SVG_NS, 'path');
    this.cross.setAttribute('class', 'overlay-cross');
    this.cross.setAttribute('hidden', '');

    this.line = document.createElementNS(SVG_NS, 'path');
    this.line.setAttribute('class', 'overlay-line');
    this.ticks = document.createElementNS(SVG_NS, 'path');
    this.ticks.setAttribute('class', 'overlay-line');
    this.label = document.createElementNS(SVG_NS, 'text');
    this.label.setAttribute('class', 'overlay-label');
    this.label.setAttribute('text-anchor', 'middle');
    this.ruler.append(this.line, this.ticks, this.label);

    svg.append(this.ruler, this.marks, this.cross);
    this.svg = svg;
    this.view.parts.stage.append(svg);

    this.viewer.addHandler('update-viewport', () => this.draw());
    this.viewer.addHandler('canvas-click', (event) => this.#onClick(event));
    // Отрезок тянется за указателем, пока вторая точка не поставлена
    this.view.parts.stage.addEventListener('pointermove', (event) => {
      if (this.tool !== 'ruler' || this.points.length !== 1) return;
      const at = this.view.imageFromClient(event.clientX, event.clientY);
      this.hover = [at.x, at.y];
      this.draw();
    });
  }

  destroy() {
    this.svg.remove();
  }

  // Размер пикселя есть не у всех форматов; без него нечего ни мерить, ни
  // привязывать: обе задачи считаются в микрометрах (Т-4, С-10)
  get available() {
    return Boolean(this.view.slide.mpp);
  }

  setTool(tool) {
    this.tool = this.available ? tool : null;
    this.view.parts.stage.classList.toggle('is-marking', Boolean(this.tool));
    // Законченное измерение остаётся на препарате и после выхода из режима:
    // его стирают Esc или новое измерение (Т-4). Недорисованное убирается сразу.
    if (this.tool !== 'ruler' && !this.done) this.clearMeasure();
    if (this.tool !== 'landmarks') this.setLandmarks([]);
  }

  // ---------- рулетка (Т-4) ----------

  // Первая точка приходит из меню по правой кнопке: измерение начинается там,
  // где меню открыли (Т-5)
  startMeasure(point) {
    this.points = [point];
    this.hover = null;
    this.draw();
    this.onChange?.(this);
  }

  clearMeasure() {
    if (!this.points.length) return false;
    this.points = [];
    this.hover = null;
    this.draw();
    this.onChange?.(this);
    return true;
  }

  get done() {
    return this.points.length === 2;
  }

  // Длина в микрометрах: от поворота и отражения не зависит, считается по скану
  get microns() {
    const [from, to] = this.points;
    if (!from || !to) return null;
    return Math.hypot(to[0] - from[0], to[1] - from[1]) * this.view.slide.mpp;
  }

  get lengthLabel() {
    return this.microns === null ? '' : lengthText(this.microns);
  }

  // ---------- ориентиры привязки (С-10) ----------

  setLandmarks(points) {
    if (!this.landmarks.length && !points.length) return;
    this.landmarks = points;
    this.draw();
  }

  // Ориентиры в микрометрах: привязка считается в них, а не в точках скана,
  // потому что у сканов 20× и 40× разный размер пикселя
  get landmarksInMicrons() {
    const scale = this.view.slide.mpp || 1;
    return this.landmarks.map(([x, y]) => ({ x: x * scale, y: y * scale }));
  }

  // ---------- общий курсор (С-8) ----------

  setCrosshair(point) {
    this.crosshair = point;
    this.draw();
  }

  // ---------- щелчки и отрисовка ----------

  #onClick(event) {
    if (!this.tool || !event.quick) return;
    event.preventDefaultAction = true;
    // Положение щелчка библиотека уже вернула в незеркальные координаты
    const at = this.view.image.viewportToImageCoordinates(
      this.viewer.viewport.pointFromPixel(event.position, true),
    );
    if (this.tool === 'landmarks') {
      this.setLandmarks([...this.landmarks, [at.x, at.y]]);
      this.onLandmark?.(this.view);
      return;
    }
    // Третий щелчок начинает новое измерение: прежнее стирается (Т-4)
    this.points = this.done ? [[at.x, at.y]] : [...this.points, [at.x, at.y]];
    this.hover = null;
    this.draw();
    this.onChange?.(this);
  }

  draw() {
    if (!this.viewer.isOpen()) return;
    this.#drawRuler();
    this.#drawLandmarks();
    this.#drawCrosshair();
  }

  #drawRuler() {
    const ends = this.done ? this.points : [...this.points, this.hover].filter(Boolean);
    if (ends.length < 2) {
      this.ruler.setAttribute('hidden', '');
      return;
    }
    this.ruler.removeAttribute('hidden');
    const [from, to] = ends.map(([x, y]) => this.view.pixelFromImage(x, y));
    this.line.setAttribute('d', `M${from.x} ${from.y} L${to.x} ${to.y}`);

    // Засечки поперёк отрезка: видно, где именно стоят концы
    const length = Math.hypot(to.x - from.x, to.y - from.y) || 1;
    const nx = (-(to.y - from.y) / length) * TICK;
    const ny = ((to.x - from.x) / length) * TICK;
    this.ticks.setAttribute('d', [
      `M${from.x - nx} ${from.y - ny} L${from.x + nx} ${from.y + ny}`,
      `M${to.x - nx} ${to.y - ny} L${to.x + nx} ${to.y + ny}`,
    ].join(' '));

    // Подпись над серединой отрезка, всегда горизонтальная: наклонный текст
    // на препарате читается хуже
    const microns = Math.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1]) * this.view.slide.mpp;
    this.label.setAttribute('x', (from.x + to.x) / 2);
    this.label.setAttribute('y', (from.y + to.y) / 2 - 8);
    this.label.textContent = lengthText(microns);
  }

  #drawLandmarks() {
    if (this.marks.childElementCount !== this.landmarks.length) {
      this.marks.replaceChildren();
      this.landmarks.forEach((point, index) => {
        const group = document.createElementNS(SVG_NS, 'g');
        group.setAttribute('class', 'overlay-mark');
        const ring = document.createElementNS(SVG_NS, 'circle');
        ring.setAttribute('r', 6);
        const number = document.createElementNS(SVG_NS, 'text');
        number.setAttribute('class', 'overlay-number');
        number.setAttribute('x', 9);
        number.setAttribute('y', -7);
        number.textContent = String(index + 1);
        group.append(ring, number);
        this.marks.append(group);
      });
    }
    this.landmarks.forEach((point, index) => {
      const at = this.view.pixelFromImage(point[0], point[1]);
      this.marks.children[index].setAttribute('transform', `translate(${at.x} ${at.y})`);
    });
  }

  #drawCrosshair() {
    if (!this.crosshair) {
      this.cross.setAttribute('hidden', '');
      return;
    }
    this.cross.removeAttribute('hidden');
    const at = this.view.pixelFromImage(this.crosshair[0], this.crosshair[1]);
    // Перекрестие с разрывом посередине: саму клетку оно не закрывает (С-8)
    this.cross.setAttribute('d', [
      `M${at.x - CROSS} ${at.y} L${at.x - 3} ${at.y}`,
      `M${at.x + 3} ${at.y} L${at.x + CROSS} ${at.y}`,
      `M${at.x} ${at.y - CROSS} L${at.x} ${at.y - 3}`,
      `M${at.x} ${at.y + 3} L${at.x} ${at.y + CROSS}`,
    ].join(' '));
  }
}

// От 1000 мкм длина показывается в миллиметрах (Т-4)
function lengthText(microns) {
  return microns >= 1000
    ? `${formatNumber(microns / 1000, 2)} ${t('unit.mm')}`
    : `${formatNumber(microns, microns < 10 ? 1 : 0)} ${t('unit.um')}`;
}
