// Взаимное положение двух половин экрана: чистая математика без DOM.
//
// Положение записывается преобразованием микрометров первой половины
// в микрометры второй:
//
//   b = R(angle) · F · a + t,
//
// где R — поворот, F зеркалит x, если срезы отражены друг к другу (mirror),
// а t — сдвиг. Масштаб в преобразование не входит: микрометры у обеих половин
// одни и те же, размер пикселя берётся из файлов и по точкам не подбирается (С-10).
//
// Этим же преобразованием считаются поворот второй половины (С-9), общий
// курсор (С-8) и привязка по ориентирам (С-10).

// Угол приводится к промежутку от −180° до 180°: в окошке поворота и в ссылке
// на поле зрения он должен читаться, а не расти после каждого оборота (Т-2).
export function normalizeAngle(degrees) {
  const turned = ((degrees % 360) + 360) % 360;
  return turned > 180 ? turned - 360 : turned;
}

export function turnPoint({ x, y }, degrees) {
  const radians = (degrees * Math.PI) / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return { x: x * cos - y * sin, y: x * sin + y * cos };
}

const mirrorPoint = ({ x, y }) => ({ x: -x, y });

// Точка первой половины → точка второй
export function forward(bind, point) {
  const turned = turnPoint(bind.mirror ? mirrorPoint(point) : point, bind.angle);
  return { x: turned.x + bind.tx, y: turned.y + bind.ty };
}

// Точка второй половины → точка первой
export function backward(bind, point) {
  const turned = turnPoint({ x: point.x - bind.tx, y: point.y - bind.ty }, -bind.angle);
  return bind.mirror ? mirrorPoint(turned) : turned;
}

// Угол одной половины по углу другой. У отражённой половины поворот виден
// зеркально, поэтому знак угла меняется — без этого перевёрнутый срез не
// связать ничем (С-9).
export function rotationFor(bind, degrees, toSecond) {
  if (bind.mirror) return -degrees - bind.angle;
  return toSecond ? degrees - bind.angle : degrees + bind.angle;
}

// Достраивает сдвиг так, чтобы точка from первой половины попадала в точку to второй
export function withOffset(bind, from, to) {
  const turned = turnPoint(bind.mirror ? mirrorPoint(from) : from, bind.angle);
  return { mirror: bind.mirror, angle: bind.angle, tx: to.x - turned.x, ty: to.y - turned.y };
}

// Связь по тому, как половины стоят сейчас (С-6): пользователь совмещает
// одинаковые участки вручную и нажимает «Связать». Отражена ровно одна
// половина — значит, срезы зеркальны друг другу.
export function bindFromViews(first, second) {
  const mirror = first.flipped !== second.flipped;
  const angle = mirror ? -(first.rotation + second.rotation) : first.rotation - second.rotation;
  return withOffset({ mirror, angle }, first.centerMicrons, second.centerMicrons);
}

// Привязка по ориентирам (С-10): по парам отмеченных точек считается сдвиг и
// поворот, масштаб остаётся единичным. Двух пар хватает для неотражённых
// срезов; третья определяет отражение и показывает расхождение.
//
// pairs: [{ first: {x, y}, second: {x, y} }, …] в микрометрах.
// mirror задан — считаем только поворот и сдвиг; не задан — пробуем оба
// варианта и берём тот, где ориентиры сходятся точнее.
export function fitBind(pairs, mirror) {
  if (mirror === undefined) {
    const straight = fitBind(pairs, false);
    const mirrored = fitBind(pairs, true);
    return straight.residual <= mirrored.residual ? straight : mirrored;
  }
  const from = pairs.map((pair) => (mirror ? mirrorPoint(pair.first) : pair.first));
  const to = pairs.map((pair) => pair.second);
  const fromMid = middle(from);
  const toMid = middle(to);

  // Поворот, при котором облака точек совпадают лучше всего (наименьшие квадраты)
  let along = 0;
  let across = 0;
  for (let i = 0; i < pairs.length; i += 1) {
    const u = { x: from[i].x - fromMid.x, y: from[i].y - fromMid.y };
    const v = { x: to[i].x - toMid.x, y: to[i].y - toMid.y };
    along += u.x * v.x + u.y * v.y;
    across += u.x * v.y - u.y * v.x;
  }
  const angle = normalizeAngle((Math.atan2(across, along) * 180) / Math.PI);
  // Точки уже зеркалены выше, поэтому сдвиг считается здесь, а не через
  // withOffset: та зеркалила бы их второй раз
  const turnedMid = turnPoint(fromMid, angle);
  const bind = { mirror, angle, tx: toMid.x - turnedMid.x, ty: toMid.y - turnedMid.y };

  // Расхождение ориентиров в микрометрах: по нему видно, насколько растянут срез
  const misses = pairs.map((pair) => {
    const at = forward(bind, pair.first);
    return Math.hypot(at.x - pair.second.x, at.y - pair.second.y);
  });
  return { ...bind, residual: Math.max(...misses, 0) };
}

function middle(points) {
  const sum = points.reduce((acc, point) => ({ x: acc.x + point.x, y: acc.y + point.y }), { x: 0, y: 0 });
  return { x: sum.x / points.length, y: sum.y / points.length };
}
