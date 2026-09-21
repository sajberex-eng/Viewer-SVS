// Слой аннотаций одной половины экрана (этап 9, раздел 13.6 ТЗ).
//
// Геометрия хранится и приходит с сервера в координатах скана, поэтому пометка
// держится за ту же клетку при любом увеличении и повороте (А-1, А-2).
// Контуры рисуются в одной группе SVG с матрицей преобразования: пересчитывается
// три точки на кадр, а не все вершины. Маркеры и ручки вершин лежат в отдельной
// группе в экранных координатах — они не должны расти вместе с увеличением (А-6).
import { t } from './i18n.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
// Стрелка указателя: вершина в начале координат, древко уходит вверх-влево,
// поэтому сама отмеченная клетка остаётся открытой (А-1, А-6). Размеры в
// экранных точках: на любом увеличении стрелка одинаковая.
const ARROW_HEAD = 'M0 0 L-11 -4 L-4 -11 Z';
const ARROW_SHAFT = 'M-5.5 -5.5 L-17 -17';
// Номер стоит у хвоста стрелки, правым краем к нему: клетку он не закрывает
const NUMBER_AT = { x: -19, y: -20 };
const HANDLE_SIZE = 9;   // ручка вершины при правке (А-14)
const MIN_POLYGON = 3;

export class AnnotationLayer {
  constructor({ view, onSelect, onSave, onFinishDraft, onDraftChange }) {
    this.view = view;
    this.viewer = view.viewer;
    this.items = [];
    this.visible = true;
    this.tool = null;      // null | 'point' | 'polygon'
    this.draft = [];       // вершины начатого контура, в координатах скана
    this.selectedId = null;
    this.onSelect = onSelect;
    this.onSave = onSave;                 // (item) => Promise: сохранить правку геометрии
    this.onFinishDraft = onFinishDraft;   // (kind, points) => Promise: создать аннотацию
    this.onDraftChange = onDraftChange;   // подсказка показывает число вершин
    this.marks = new Map();               // узел -> точка скана, пересчитывается на кадр
    this.#build();
  }

  // ---------- разметка слоя ----------

  #build() {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'annot-layer');
    this.shapes = document.createElementNS(SVG_NS, 'g');
    this.screen = document.createElementNS(SVG_NS, 'g');
    svg.append(this.shapes, this.screen);
    this.svg = svg;

    this.tip = document.createElement('div');
    this.tip.className = 'annot-tip';
    this.tip.hidden = true;

    this.view.parts.stage.append(svg, this.tip);

    this.viewer.addHandler('update-viewport', () => this.place());
    this.viewer.addHandler('canvas-click', (event) => this.#onClick(event));
    this.viewer.addHandler('canvas-double-click', (event) => this.#onDoubleClick(event));
  }

  destroy() {
    this.svg.remove();
    this.tip.remove();
  }

  // ---------- данные ----------

  setItems(items) {
    this.items = items;
    if (!items.some((item) => item.id === this.selectedId)) this.selectedId = null;
    this.render();
  }

  setVisible(visible) {
    this.visible = visible;
    this.svg.classList.toggle('is-hidden', !visible);
    if (!visible) this.hideTip();
  }

  setTool(tool) {
    this.tool = tool;
    this.draft = [];
    this.view.parts.stage.classList.toggle('is-marking', Boolean(tool));
    this.render();
  }

  select(id) {
    this.selectedId = id;
    this.render();
  }

  // Первая вершина приходит из меню по правой кнопке: контур начинается там,
  // где меню открыли (Т-5)
  startDraft(point) {
    if (this.tool !== 'polygon') return;
    this.draft = [point];
    this.render();
    this.onDraftChange?.();
  }

  cancelDraft() {
    if (!this.draft.length) return false;
    this.draft = [];
    this.render();
    return true;
  }

  // Замкнуть начатый контур по Enter или двойному щелчку (А-2)
  closeDraft() {
    if (this.tool !== 'polygon' || this.draft.length < MIN_POLYGON) return false;
    const points = this.draft;
    this.draft = [];
    this.render();
    this.onFinishDraft?.('polygon', points);
    return true;
  }

  // ---------- отрисовка ----------

  render() {
    this.shapes.replaceChildren();
    this.screen.replaceChildren();
    this.marks.clear();

    // Номер аннотации — её место в списке скана: тот же стоит в списке сбоку
    this.items.forEach((item, index) => {
      if (item.kind === 'polygon') this.#drawPolygon(item, index + 1);
      else this.#drawPoint(item, index + 1);
    });
    if (this.draft.length) this.#drawDraft();
    this.place();
  }

  #drawPolygon(item, number) {
    const selected = item.id === this.selectedId;
    const group = document.createElementNS(SVG_NS, 'g');
    group.setAttribute('class', `annot-shape${colorClass(item)}${selected ? ' is-selected' : ''}`);
    // Широкая прозрачная линия — только чтобы по контуру было легко попасть
    const hit = document.createElementNS(SVG_NS, 'path');
    hit.setAttribute('class', 'annot-hit');
    hit.setAttribute('d', pathOf(item.points));
    const line = document.createElementNS(SVG_NS, 'path');
    line.setAttribute('class', 'annot-line');
    line.setAttribute('d', pathOf(item.points));
    group.append(hit, line);
    this.#bind(group, item);
    this.shapes.append(group);

    // Номер контура — у его первой вершины, в экранных координатах
    const label = document.createElementNS(SVG_NS, 'g');
    label.setAttribute('class', `annot-mark${colorClass(item)}${selected ? ' is-selected' : ''}`);
    label.append(numberNode(number, -6, -6));
    this.#bind(label, item);
    this.screen.append(label);
    this.marks.set(label, item.points[0]);

    if (selected && item.can_edit) {
      item.points.forEach((point, index) => {
        const handle = document.createElementNS(SVG_NS, 'rect');
        handle.setAttribute('class', 'annot-handle');
        handle.setAttribute('width', HANDLE_SIZE);
        handle.setAttribute('height', HANDLE_SIZE);
        this.#bindDrag(handle, item, index);
        this.screen.append(handle);
        this.marks.set(handle, point);
      });
    }
  }

  #drawPoint(item, number) {
    const selected = item.id === this.selectedId;
    const mark = document.createElementNS(SVG_NS, 'g');
    mark.setAttribute('class', `annot-mark${colorClass(item)}${selected ? ' is-selected' : ''}`);
    for (const [name, d] of [['annot-arrow-shaft', ARROW_SHAFT], ['annot-arrow', ARROW_HEAD]]) {
      const part = document.createElementNS(SVG_NS, 'path');
      part.setAttribute('class', name);
      part.setAttribute('d', d);
      mark.append(part);
    }
    mark.append(numberNode(number, NUMBER_AT.x, NUMBER_AT.y));
    this.#bind(mark, item);
    if (selected && item.can_edit) this.#bindDrag(mark, item, 0);
    this.screen.append(mark);
    this.marks.set(mark, item.points[0]);
  }

  #drawDraft() {
    const line = document.createElementNS(SVG_NS, 'path');
    line.setAttribute('class', 'annot-line is-draft');
    line.setAttribute('d', pathOf(this.draft, false));
    this.shapes.append(line);
    for (const point of this.draft) {
      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('class', 'annot-dot');
      dot.setAttribute('r', 3.5);
      this.screen.append(dot);
      this.marks.set(dot, point);
    }
  }

  // Пересчёт положения: матрица для контуров и экранные координаты для маркеров.
  // Координаты берутся у половины экрана, а не у библиотеки: она зеркалит только
  // рисование, и при отражении (Т-3) пометки встали бы на другой край скана.
  place() {
    if (!this.viewer.isOpen()) return;
    const { width, height } = this.view.slide;
    const origin = this.view.pixelFromImage(0, 0);
    const alongX = this.view.pixelFromImage(width, 0);
    const alongY = this.view.pixelFromImage(0, height);
    const a = (alongX.x - origin.x) / width;
    const b = (alongX.y - origin.y) / width;
    const c = (alongY.x - origin.x) / height;
    const d = (alongY.y - origin.y) / height;
    this.shapes.setAttribute('transform', `matrix(${a} ${b} ${c} ${d} ${origin.x} ${origin.y})`);

    for (const [node, point] of this.marks) {
      const at = this.#toScreen(point);
      if (node.tagName === 'g') {          // стрелка указателя
        node.setAttribute('transform', `translate(${at.x} ${at.y})`);
      } else if (node.tagName === 'rect') { // ручка вершины
        node.setAttribute('x', at.x - HANDLE_SIZE / 2);
        node.setAttribute('y', at.y - HANDLE_SIZE / 2);
      } else {                              // точка начатого контура
        node.setAttribute('cx', at.x);
        node.setAttribute('cy', at.y);
      }
    }
  }

  #toScreen([x, y]) {
    return this.view.pixelFromImage(x, y);
  }

  // ---------- подсказка и выбор ----------

  #bind(node, item) {
    node.addEventListener('pointerenter', (event) => this.showTip(item, event));
    node.addEventListener('pointerleave', () => this.hideTip());
    node.addEventListener('click', (event) => {
      event.stopPropagation();
      this.select(item.id);
      this.onSelect?.(item);
    });
  }

  showTip(item, event) {
    if (!this.visible) return;
    const text = item.comment || t('annot.noComment');
    const number = this.items.indexOf(item) + 1;
    this.tip.textContent = `${t('annot.number', { n: number })} ${text} — ${item.author}`;
    this.tip.hidden = false;
    // Подсказка не должна вылезать за край половины: у края она перескакивает
    // влево и вверх от указателя, иначе текст сжимается в столбец
    const box = this.view.parts.stage.getBoundingClientRect();
    const size = this.tip.getBoundingClientRect();
    const x = event.clientX - box.left;
    const y = event.clientY - box.top;
    const left = x + 12 + size.width > box.width ? Math.max(x - 12 - size.width, 8) : x + 12;
    const top = y + 12 + size.height > box.height ? Math.max(y - 12 - size.height, 8) : y + 12;
    this.tip.style.left = `${left}px`;
    this.tip.style.top = `${top}px`;
  }

  hideTip() {
    this.tip.hidden = true;
  }

  // ---------- правка вершин (А-14) ----------

  #bindDrag(node, item, index) {
    node.classList.add('is-draggable');
    node.addEventListener('pointerdown', (event) => {
      event.stopPropagation();  // иначе OpenSeadragon примет это за перетаскивание препарата
      event.preventDefault();
      // Захват указателя: ручка продолжает получать движения, даже если палец
      // или курсор ушёл с неё. Отказ браузера не мешает правке.
      try {
        node.setPointerCapture(event.pointerId);
      } catch { /* указатель уже отпущен */ }
      const points = item.points.map((pair) => [...pair]);
      const move = (moveEvent) => {
        const at = this.view.imageFromClient(moveEvent.clientX, moveEvent.clientY);
        points[index] = [at.x, at.y];
        item.points = points;
        this.render();
      };
      const up = () => {
        node.removeEventListener('pointermove', move);
        node.removeEventListener('pointerup', up);
        node.removeEventListener('pointercancel', up);
        this.onSave?.(item);
      };
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerup', up);
      node.addEventListener('pointercancel', up);
    });
  }

  // ---------- рисование (А-1, А-2) ----------

  #onClick(event) {
    // Щелчок по препарату мимо пометок снимает выделение: белое свечение
    // отмечает ту, с которой сейчас работают, и не должно висеть всё время
    if (!this.tool) {
      if (event.quick && this.selectedId) {
        this.select(null);
        this.onSelect?.(null);
      }
      return;
    }
    if (!event.quick) return;
    event.preventDefaultAction = true;
    // Положение щелчка библиотека зеркалит сама, когда половина отражена,
    // поэтому здесь идёт прямой пересчёт, без view.imageFromPixel (Т-3)
    const at = this.viewer.viewport.viewportToImageCoordinates(
      this.viewer.viewport.pointFromPixel(event.position, true),
    );
    const point = [at.x, at.y];
    if (this.tool === 'point') {
      this.onFinishDraft?.('point', [point]);
      return;
    }
    this.draft.push(point);
    this.render();
    this.onDraftChange?.();
  }

  #onDoubleClick(event) {
    if (this.tool !== 'polygon') return;
    event.preventDefaultAction = true;  // иначе двойной щелчок ещё и приблизит
    // Двойной щелчок приходит после двух обычных: лишняя вершина убирается
    if (this.draft.length > MIN_POLYGON) this.draft.pop();
    this.closeDraft();
  }
}

function colorClass(item) {
  return item.color === 'red' ? ' is-red' : '';
}

function numberNode(number, x, y) {
  const text = document.createElementNS(SVG_NS, 'text');
  text.setAttribute('class', 'annot-number');
  text.setAttribute('x', x);
  text.setAttribute('y', y);
  text.setAttribute('text-anchor', 'end');
  text.textContent = String(number);
  return text;
}

// Контур замкнут (аннотация) или разомкнут (начатый черновик)
function pathOf(points, closed = true) {
  if (!points.length) return '';
  const body = points.map(([x, y], index) => `${index ? 'L' : 'M'}${x} ${y}`).join(' ');
  return closed ? `${body} Z` : body;
}
