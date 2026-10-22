// Перетаскивание плавающих окошек поля просмотра: мини-карты, этикетки,
// панелей «Изображение», «Аннотации», «Клеточность». Тянется за ручку
// (заголовок), окошко остаётся внутри поля. Положение не запоминается.
export function makeDraggable(box, handle, field) {
  let drag = null;
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || event.target.closest('button, input, select, a, label')) return;
    const rect = box.getBoundingClientRect();
    const area = field.getBoundingClientRect();
    drag = { dx: event.clientX - rect.left, dy: event.clientY - rect.top, area, w: rect.width, h: rect.height };
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const left = Math.min(Math.max(event.clientX - drag.dx - drag.area.left, 0), drag.area.width - drag.w);
    const top = Math.min(Math.max(event.clientY - drag.dy - drag.area.top, 0), drag.area.height - drag.h);
    box.style.left = `${Math.round(left)}px`;
    box.style.top = `${Math.round(top)}px`;
    box.style.right = 'auto';
    box.style.bottom = 'auto';
  });
  for (const name of ['pointerup', 'pointercancel']) handle.addEventListener(name, () => { drag = null; });
}

// Окошко возвращается в своё место по умолчанию (из CSS)
export function resetPosition(box) {
  box.style.left = '';
  box.style.top = '';
  box.style.right = '';
  box.style.bottom = '';
}
