// Снимок поля зрения в буфер обмена (просьба заказчика 2026-10-22): щелчок по
// кнопке — всё поле активной половины, рамка мышью — участок. На снимке
// препарат с настройками изображения, аннотации (если показаны) и масштабная
// линейка; кнопки, мини-карта, рулетка и курсор не попадают.

// rect — участок в точках элемента вьювера {x, y, w, h}; null — всё поле.
export async function captureRegion(pane, rect = null) {
  const source = pane.viewer.drawer.canvas;
  const ratio = source.width / source.clientWidth || 1;
  const area = rect ?? { x: 0, y: 0, w: source.clientWidth, h: source.clientHeight };
  const width = Math.max(1, Math.round(area.w * ratio));
  const height = Math.max(1, Math.round(area.h * ratio));
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d');
  const sx = Math.round(area.x * ratio);
  const sy = Math.round(area.y * ratio);
  context.drawImage(source, sx, sy, width, height, 0, 0, width, height);

  // Слой коррекций: буфер WebGL без preserveDrawingBuffer живёт до конца
  // задачи, поэтому кадр рисуется заново и тут же копируется
  const adjuster = pane.adjuster;
  if (adjuster?.active) {
    adjuster.render();
    context.drawImage(adjuster.canvas, sx, sy, width, height, 0, 0, width, height);
  }

  const annotations = pane.annotations?.svg;
  if (annotations && !annotations.classList.contains('is-hidden')) {
    await drawSvg(context, annotations, -sx, -sy, ratio);
  }
  drawScalebar(context, pane, ratio);
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
}

// SVG-слой аннотаций берёт цвета из CSS страницы: в картинку они не попадут,
// поэтому вычисленные стили переписываются в атрибуты копии
async function drawSvg(context, svg, dx, dy, ratio) {
  const clone = svg.cloneNode(true);
  const originals = svg.querySelectorAll('*');
  const copies = clone.querySelectorAll('*');
  const props = ['stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap', 'fill', 'opacity',
    'font-size', 'font-weight', 'font-family', 'display', 'filter'];
  originals.forEach((node, i) => {
    const style = getComputedStyle(node);
    const copy = copies[i];
    for (const name of props) copy.style.setProperty(name, style.getPropertyValue(name));
    copy.removeAttribute('class');
  });
  const w = svg.clientWidth;
  const h = svg.clientHeight;
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('width', w);
  clone.setAttribute('height', h);
  clone.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const blob = new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml' });
  const url = URL.createObjectURL(blob);
  try {
    const image = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('svg'));
      img.src = url;
    });
    context.drawImage(image, dx, dy, w * ratio, h * ratio);
  } catch {
    // аннотации не нарисовались — снимок остаётся без них
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Масштабная линейка в левом нижнем углу: та же длина и подпись, что на экране
function drawScalebar(context, pane, ratio) {
  const line = pane.parts?.scalebarLine;
  const label = pane.parts?.scalebarLabel;
  if (!line || !label) return;
  const length = parseFloat(line.style.width) * ratio;
  if (!(length > 0)) return;
  const margin = 12 * ratio;
  const x = margin;
  const y = context.canvas.height - margin;
  const font = `600 ${Math.round(12 * ratio)}px system-ui, sans-serif`;
  context.lineCap = 'butt';
  context.lineJoin = 'round';
  // Белый ореол, затем чёрная линия и засечки
  for (const [color, w] of [['#fff', 5 * ratio], ['#111', 2 * ratio]]) {
    context.strokeStyle = color;
    context.lineWidth = w;
    context.beginPath();
    context.moveTo(x, y);
    context.lineTo(x + length, y);
    context.moveTo(x, y - 6 * ratio);
    context.lineTo(x, y);
    context.moveTo(x + length, y - 6 * ratio);
    context.lineTo(x + length, y);
    context.stroke();
  }
  context.font = font;
  context.textAlign = 'center';
  context.textBaseline = 'bottom';
  context.lineWidth = 4 * ratio;
  context.strokeStyle = '#fff';
  context.strokeText(label.textContent, x + length / 2, y - 6 * ratio);
  context.fillStyle = '#111';
  context.fillText(label.textContent, x + length / 2, y - 6 * ratio);
}

// В буфер обмена; где буфера нет (Firefox, HTTP) — файлом. Возвращает, куда ушло.
export async function deliverSnapshot(blob, filename) {
  if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      return 'clipboard';
    } catch {
      // отказ браузера — ниже файлом
    }
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return 'download';
}
