// Коррекция изображения поверх уже загруженных тайлов.
// OpenSeadragon рисует тайлы на свой 2D-canvas; после каждого кадра он копируется
// в текстуру и перерисовывается шейдером на WebGL-canvas, лежащий сверху.
// Тайлы повторно не загружаются, мини-карта (отдельный canvas) остаётся без коррекций.
//
// Порядок применения (И-5, раздел 7.3 ТЗ):
//   профиль сканера → разделение окрасок → баланс белого → чёрная и белая точки →
//   яркость и контраст → гамма → насыщенность и оттенок → резкость (High Pass).
// Резкость — соседний фильтр (размытие и наложение), поэтому в applyValues
// (цепочка для одной точки) её нет; считается только шейдером, на текущем кадре.

// Порядок списка — это и порядок чисел в ссылке на поле зрения (раздел 7.5 ТЗ):
// пятнадцать чисел; ссылки прежних форматов несут восемь (LEGACY_ORDER), десять
// (без «окраски») или одиннадцать (ORDER_11, без резкости).
export const PARAMS = [
  { id: 'black', label: 'adjust.black', min: 0, max: 254, step: 1, def: 0, digits: 0 },
  { id: 'white', label: 'adjust.white', min: 1, max: 255, step: 1, def: 255, digits: 0 },
  { id: 'gamma', label: 'adjust.gamma', min: 0.2, max: 3, step: 0.01, def: 1, digits: 2 },
  { id: 'brightness', label: 'adjust.brightness', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'contrast', label: 'adjust.contrast', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'saturation', label: 'adjust.saturation', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'hue', label: 'adjust.hue', min: -180, max: 180, step: 1, def: 0, digits: 0 },
  // Баланс белого хранится и передаётся в прежних единицах −100…+100
  // (коэффициент 2^(b/100)), чтобы сохранённые настройки и ссылки прежнего
  // формата читались без преобразования; в панели показан коэффициентами.
  { id: 'red', label: 'adjust.red', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'green', label: 'adjust.green', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'blue', label: 'adjust.blue', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  // Разделение окрасок: 0 — вся, 1 — только гематоксилин (ядра), 2 — только эозин
  { id: 'stain', label: 'adjust.stain', min: 0, max: 2, step: 1, def: 0, digits: 0 },
  // Резкость High Pass (документ заказчика 2026-10-22): включена ли, сила в
  // процентах, радиус размытия в пикселях экрана, режим наложения
  // (0 Overlay, 1 Soft Light, 2 Multiply)
  { id: 'sharpen', label: 'adjust.sharpen', min: 0, max: 1, step: 1, def: 0, digits: 0 },
  { id: 'sharpAmount', label: 'adjust.sharpAmount', min: 0, max: 100, step: 1, def: 50, digits: 0 },
  { id: 'sharpRadius', label: 'adjust.sharpRadius', min: 0.3, max: 3, step: 0.1, def: 0.8, digits: 1 }, // в пикселях скана
  { id: 'sharpMode', label: 'adjust.sharpMode', min: 0, max: 2, step: 1, def: 0, digits: 0 },
];
export const LEGACY_ORDER = ['brightness', 'contrast', 'gamma', 'saturation', 'hue', 'red', 'green', 'blue'];
// Ссылки этапа 12 (до резкости): одиннадцать чисел
export const ORDER_11 = PARAMS.slice(0, 11).map((p) => p.id);

export const DEFAULTS = Object.fromEntries(PARAMS.map((p) => [p.id, p.def]));

// Значения пресетов предварительные, согласуются с заказчиком на тестовых сканах (И-8).
export const PRESETS = {
  original: { ...DEFAULTS },
  pale: { ...DEFAULTS, black: 20, white: 250, saturation: 35 },
  overstained: { ...DEFAULTS, gamma: 1.4, white: 245 },
};

// Резкость считается только от этого увеличения (решение заказчика 2026-10-22):
// ниже тайлы берутся из уменьшенных уровней пирамиды, и фильтр лишь добавляет
// зернистость. Радиус задан в пикселях скана и пересчитывается в пиксели
// холста по текущему увеличению, так что на 20× и 40× он значит одно и то же.
export const SHARPEN_MIN_MAGNIFICATION = 10;

// «Авто» для резкости (просьба заказчика 2026-10-22): сила подбирается по
// размытости видимого поля. Мера — разброс лапласиана яркости, делённый на
// разброс самой яркости по пикселям ткани (без фона стекла): у резкого кадра
// около sharp, у мягкого — около soft. Сырая мера сильно зависит от
// увеличения (уменьшенный кадр резче), поэтому приводится к масштабу «один
// пиксель скана = один пиксель холста» степенью scaleExponent. Калибровка
// 2026-10-22 на настоящем H&E (Aperio 40×): сырая мера 0,39 на 40× и 1,20 на
// 20×, приведённая — около 0,45 там и там, сила получается ≈ 60 %.
export const AUTO_SHARPEN = {
  soft: 0.3,          // приведённая мера мягкого кадра: сила maxAmount
  sharp: 0.7,         // резкого: сила minAmount
  scaleExponent: 1.6, // как растёт сырая мера при уменьшении кадра
  minAmount: 10,
  maxAmount: 90,
  tissueBelow: 235,   // яркость, ниже которой пиксель считается тканью
  minTissueShare: 0.05, // меньше ткани в кадре — оценивать нечего
  sample: 384,        // сторона центрального участка кадра, пикселей холста
};

// Сила резкости по мере размытости: линейно между soft и sharp
export function autoSharpenAmount(metric) {
  const { soft, sharp, minAmount, maxAmount } = AUTO_SHARPEN;
  const fraction = Math.min(1, Math.max(0, (sharp - metric) / (sharp - soft)));
  return Math.round(minAmount + (maxAmount - minAmount) * fraction);
}

// Мера размытости по участку кадра (ImageData): std(лапласиан) / std(яркость)
// по пикселям ткани, приведённая к масштабу один к одному (pixelScale —
// пикселей холста на пиксель скана); null — ткани мало.
export function sharpnessMetric(imageData, pixelScale = 1) {
  const { data, width, height } = imageData;
  const luma = new Float32Array(width * height);
  const tissue = new Uint8Array(width * height);
  let count = 0;
  for (let i = 0, p = 0; i < data.length; i += 4, p += 1) {
    const y = luminance(data[i], data[i + 1], data[i + 2]);
    luma[p] = y;
    if (data[i + 3] === 255 && y < AUTO_SHARPEN.tissueBelow) {
      tissue[p] = 1;
      count += 1;
    }
  }
  if (count < AUTO_SHARPEN.minTissueShare * width * height) return null;
  let sum = 0;
  let sumSq = 0;
  let lapSq = 0;
  let lapCount = 0;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const p = y * width + x;
      if (!tissue[p]) continue;
      sum += luma[p];
      sumSq += luma[p] * luma[p];
      const lap = 4 * luma[p] - luma[p - 1] - luma[p + 1] - luma[p - width] - luma[p + width];
      lapSq += lap * lap;
      lapCount += 1;
    }
  }
  if (lapCount < 16) return null;
  const mean = sum / lapCount;
  const std = Math.sqrt(Math.max(0, sumSq / lapCount - mean * mean));
  if (std < 1) return null; // однотонный участок: мерить нечего
  return (Math.sqrt(lapSq / lapCount) / std) * pixelScale ** AUTO_SHARPEN.scaleExponent;
}
const SIGMA_MIN = 0.3;
const SIGMA_MAX = 3;

// Автокоррекция «как в сканере» (раздел 7.6 ТЗ, решение заказчика 2026-09-29):
// баланс белого и белая точка по фону стекла, фон ложится на backgroundLevel,
// чёрная точка не трогается.
export const AUTO = {
  backgroundPercentile: 0.99, // фон стекла: 99-й процентиль каждого канала
  backgroundLevel: 245, // куда сканеры кладут пустое стекло: не в чистый белый
};

// Векторы окрасок H&E по Ruifrok и Johnston (те же, что «H&E default» в QuPath);
// третий — остаток, дополняющий базис.
const STAIN_VECTORS = [[0.65, 0.70, 0.29], [0.07, 0.99, 0.11], [0.27, 0.57, 0.78]].map((v) => {
  const length = Math.hypot(...v);
  return v.map((x) => x / length);
});
// Столбцы матрицы — векторы окрасок: OD = M · концентрации. Хранится по столбцам,
// как ждёт WebGL (uniformMatrix3fv).
export const STAIN_MATRIX = STAIN_VECTORS.flat();
export const STAIN_INVERSE = invert3(STAIN_MATRIX);

// Обратная матрица 3×3 для массива по столбцам.
function invert3(m) {
  const [a, d, g, b, e, h, c, f, i] = m; // m[col*3 + row]
  const A = e * i - f * h; const B = -(d * i - f * g); const C = d * h - e * g;
  const det = a * A + b * B + c * C;
  const D = -(b * i - c * h); const E = a * i - c * g; const F = -(a * h - b * g);
  const G = b * f - c * e; const H = -(a * f - c * d); const I = a * e - b * d;
  // Результат тоже по столбцам: столбец 0 = (A, B, C) / det и т. д.
  return [A, B, C, D, E, F, G, H, I].map((x) => x / det);
}

const mul3 = (m, v) => [
  m[0] * v[0] + m[3] * v[1] + m[6] * v[2],
  m[1] * v[0] + m[4] * v[1] + m[7] * v[2],
  m[2] * v[0] + m[5] * v[1] + m[8] * v[2],
];

export function isDefault(values) {
  return PARAMS.every((p) => values[p.id] === p.def);
}

export function clampParam(param, value) {
  const number = Number.isFinite(value) ? value : param.def;
  const clamped = Math.min(param.max, Math.max(param.min, number));
  return Number(clamped.toFixed(param.digits));
}

// Баланс канала −100…+100 соответствует усилению 0,5…2.
export const balanceToGain = (balance) => 2 ** (balance / 100);
export const gainToBalance = (gain) => 100 * Math.log2(gain);

const LUMA = [0.2126, 0.7152, 0.0722];
export const luminance = (r, g, b) => LUMA[0] * r + LUMA[1] * g + LUMA[2] * b;

export function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('image'));
    image.src = url;
  });
}

const VERTEX_SHADER = `
attribute vec2 aPosition;
varying vec2 vUv;
void main() {
  vUv = aPosition * 0.5 + 0.5;
  gl_Position = vec4(aPosition, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
varying vec2 vUv;
uniform sampler2D uImage;
uniform sampler2D uLut;
uniform float uLutSize;   // 0 — профиль не применяется
uniform float uStain;     // 0 вся окраска, 1 гематоксилин, 2 эозин
uniform mat3 uStainMat;
uniform mat3 uStainInv;
uniform vec3 uGain;
uniform float uBlack;
uniform float uInvRange;
uniform float uBrightness;
uniform float uContrast;
uniform float uInvGamma;
uniform float uSaturation;
uniform vec2 uHue; // cos, sin
const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);
const vec3 GRAY_AXIS = vec3(0.57735);

// Таблица цвета: срезы по синему в ряд, внутри среза x — красный, y — зелёный.
// По красному и зелёному интерполирует сама текстура, по синему — вручную.
vec3 lutLookup(vec3 c) {
  float n = uLutSize;
  float b = c.b * (n - 1.0);
  float b0 = floor(b);
  float b1 = min(b0 + 1.0, n - 1.0);
  float y = (c.g * (n - 1.0) + 0.5) / n;
  float x0 = (b0 * n + c.r * (n - 1.0) + 0.5) / (n * n);
  float x1 = (b1 * n + c.r * (n - 1.0) + 0.5) / (n * n);
  return mix(texture2D(uLut, vec2(x0, y)).rgb, texture2D(uLut, vec2(x1, y)).rgb, b - b0);
}

void main() {
  vec4 pixel = texture2D(uImage, vUv);
  vec3 c = pixel.rgb;
  if (uLutSize > 0.0) c = lutLookup(c);
  if (uStain > 0.5) {
    vec3 od = -log(max(c, vec3(1.0 / 255.0)));
    vec3 conc = uStainInv * od;
    conc *= (uStain < 1.5) ? vec3(1.0, 0.0, 0.0) : vec3(0.0, 1.0, 0.0);
    c = exp(-(uStainMat * conc));
  }
  c = c * uGain;
  c = (c - uBlack) * uInvRange;
  c = (c - 0.5) * uContrast + 0.5 + uBrightness;
  c = pow(clamp(c, 0.0, 1.0), vec3(uInvGamma));
  c = mix(vec3(dot(c, LUMA)), c, uSaturation);
  c = c * uHue.x + cross(GRAY_AXIS, c) * uHue.y + GRAY_AXIS * dot(GRAY_AXIS, c) * (1.0 - uHue.x);
  gl_FragColor = vec4(clamp(c, 0.0, 1.0), pixel.a);
}`;

// Размытие Гаусса по одной оси: два прохода (по x и по y) дают размытую копию
// кадра для High Pass. Сигма равна радиусу в пикселях, окно ±3σ, не больше 10 точек.
const BLUR_SHADER = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uImage;
uniform vec2 uStep;    // шаг на одну точку по оси размытия
uniform float uSigma;
void main() {
  vec3 sum = vec3(0.0);
  float total = 0.0;
  for (int i = -10; i <= 10; i++) {
    float d = float(i);
    if (abs(d) > uSigma * 3.0 + 0.5) continue;
    float w = exp(-d * d / (2.0 * uSigma * uSigma));
    sum += texture2D(uImage, vUv + uStep * d).rgb * w;
    total += w;
  }
  gl_FragColor = vec4(sum / total, 1.0);
}`;

// Наложение High Pass на скорректированный кадр, как в Photoshop: копия с
// выделенными перепадами (исходное минус размытое, вокруг среднего серого)
// накладывается в режиме Overlay, Soft Light или Multiply; сила — доля смеси.
// Multiply взят с удвоенным High Pass: средний серый тогда не затемняет кадр.
const SHARPEN_SHADER = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uBase;
uniform sampler2D uBlur;
uniform float uAmount;
uniform float uMode;   // 0 Overlay, 1 Soft Light, 2 Multiply
vec3 overlay(vec3 b, vec3 s) {
  return mix(2.0 * b * s, 1.0 - 2.0 * (1.0 - b) * (1.0 - s), step(0.5, b));
}
vec3 softLight(vec3 b, vec3 s) {
  vec3 d = mix(((16.0 * b - 12.0) * b + 4.0) * b, sqrt(b), step(0.25, b));
  vec3 dark = b - (1.0 - 2.0 * s) * b * (1.0 - b);
  vec3 light = b + (2.0 * s - 1.0) * (d - b);
  return mix(dark, light, step(0.5, s));
}
void main() {
  vec4 pixel = texture2D(uBase, vUv);
  vec3 b = pixel.rgb;
  vec3 hp = clamp(b - texture2D(uBlur, vUv).rgb + 0.5, 0.0, 1.0);
  vec3 blended = uMode < 0.5 ? overlay(b, hp) : (uMode < 1.5 ? softLight(b, hp) : b * hp * 2.0);
  gl_FragColor = vec4(clamp(mix(b, blended, uAmount), 0.0, 1.0), pixel.a);
}`;

// Разделение окрасок на JS, цвет в долях 0…1.
export function stainOnly(rgb, stain) {
  if (!stain) return rgb;
  const od = rgb.map((x) => -Math.log(Math.max(x, 1 / 255)));
  const conc = mul3(STAIN_INVERSE, od);
  const kept = stain === 1 ? [conc[0], 0, 0] : [0, conc[1], 0];
  return mul3(STAIN_MATRIX, kept).map((x) => Math.exp(-x));
}

// Та же цепочка на JS для одного цвета 0…255 (без профиля сканера): ею
// сверяется шейдер. lut — функция цвета профиля (ImageAdjuster.profileColor) или null.
export function applyValues(values, rgb, lut = null) {
  const v = values;
  const source = lut ? lut(rgb) : rgb;
  const gains = [balanceToGain(v.red), balanceToGain(v.green), balanceToGain(v.blue)];
  const black = v.black / 255;
  const invRange = 255 / (v.white - v.black);
  let c = stainOnly(source.map((channel) => channel / 255), v.stain);
  c = c.map((x, i) => x * gains[i]);
  c = c.map((x) => (x - black) * invRange);
  c = c.map((x) => (x - 0.5) * 3 ** (v.contrast / 100) + 0.5 + v.brightness / 200);
  c = c.map((x) => Math.min(1, Math.max(0, x)) ** (1 / v.gamma));
  const luma = luminance(...c);
  const s = 1 + v.saturation / 100;
  c = c.map((x) => luma + (x - luma) * s);
  const hue = (v.hue * Math.PI) / 180;
  const [cos, sin] = [Math.cos(hue), Math.sin(hue)];
  const k = 0.57735;
  const dot = k * (c[0] + c[1] + c[2]);
  const cross = [k * (c[2] - c[1]), k * (c[0] - c[2]), k * (c[1] - c[0])];
  c = c.map((x, i) => x * cos + cross[i] * sin + k * dot * (1 - cos));
  return c.map((x) => Math.min(1, Math.max(0, x)) * 255);
}

// Трилинейная выборка из таблицы цвета (данные PNG с сервера, раздел 7 ТЗ).
export function sampleLut(data, size, rgb) {
  const width = size * size;
  const coords = rgb.map((x) => Math.min(size - 1, Math.max(0, (x / 255) * (size - 1))));
  const lo = coords.map(Math.floor);
  const hi = lo.map((x) => Math.min(size - 1, x + 1));
  const f = coords.map((x, i) => x - lo[i]);
  const at = (r, g, b) => {
    const index = (g * width + b * size + r) * 4;
    return [data[index], data[index + 1], data[index + 2]];
  };
  const mix = (a, b, t) => a.map((x, i) => x + (b[i] - x) * t);
  const c00 = mix(at(lo[0], lo[1], lo[2]), at(hi[0], lo[1], lo[2]), f[0]);
  const c10 = mix(at(lo[0], hi[1], lo[2]), at(hi[0], hi[1], lo[2]), f[0]);
  const c01 = mix(at(lo[0], lo[1], hi[2]), at(hi[0], lo[1], hi[2]), f[0]);
  const c11 = mix(at(lo[0], hi[1], hi[2]), at(hi[0], hi[1], hi[2]), f[0]);
  return mix(mix(c00, c10, f[1]), mix(c01, c11, f[1]), f[2]);
}

// Гистограмма яркости (256 корзин) по пикселям изображения; прозрачные
// (за краем скана) не считаются. Возвращает null, если пикселей нет.
export function histogramOf(imageData) {
  const bins = new Uint32Array(256);
  const { data } = imageData;
  let count = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 255) continue;
    bins[Math.round(luminance(data[i], data[i + 1], data[i + 2]))] += 1;
    count += 1;
  }
  return count ? bins : null;
}

// Значение, ниже которого лежит доля fraction всех отсчётов гистограммы.
export function percentile(bins, fraction) {
  const total = bins.reduce((sum, n) => sum + n, 0);
  const target = fraction * total;
  let acc = 0;
  for (let level = 0; level < bins.length; level += 1) {
    acc += bins[level];
    if (acc >= target) return level;
  }
  return bins.length - 1;
}

// Автокоррекция «как в сканере» по обзорному изображению препарата (раздел 7.6 ТЗ):
// фон стекла делается нейтральным (баланс белого) и ложится на AUTO.backgroundLevel
// (белая точка); чёрная точка не меняется. lut — цвет профиля сканера или null:
// картинка миниатюры без профиля, а шейдер применяет профиль первым.
// Возвращает частичный набор значений или null, если картинка пустая.
export function autoValues(imageData, lut = null) {
  const { data } = imageData;
  const channels = [new Uint32Array(256), new Uint32Array(256), new Uint32Array(256)];
  let count = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 255) continue;
    const rgb = lut ? lut([data[i], data[i + 1], data[i + 2]]) : [data[i], data[i + 1], data[i + 2]];
    channels[0][Math.round(rgb[0])] += 1;
    channels[1][Math.round(rgb[1])] += 1;
    channels[2][Math.round(rgb[2])] += 1;
    count += 1;
  }
  if (!count) return null;

  const background = channels.map((bins) => Math.max(1, percentile(bins, AUTO.backgroundPercentile)));
  const level = Math.max(...background);
  const gains = background.map((channel) => level / channel);
  return {
    white: Math.min(255, Math.round((level * 255) / AUTO.backgroundLevel)),
    red: gainToBalance(gains[0]),
    green: gainToBalance(gains[1]),
    blue: gainToBalance(gains[2]),
    brightness: 0,
    contrast: 0,
  };
}

// Ширина уменьшенной копии кадра для гистограммы: 256 точек хватает,
// а чтение с 2D-canvas остаётся дешёвым и не роняет частоту кадров (И-12).
const HISTOGRAM_SAMPLE_WIDTH = 256;

export class ImageAdjuster {
  constructor(viewer) {
    this.viewer = viewer;
    this.values = { ...DEFAULTS };
    this.bypass = false;
    this.lutImage = null; // PNG таблицы цвета из профиля сканера
    this.lutData = null;
    this.lutSize = 0;
    this.profileOn = true;
    // Половина экрана сообщает увеличение и масштаб (пикселей экрана на пиксель скана)
    this.viewInfo = () => ({ magnification: null, pixelScale: 1 });
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'adjust-canvas';
    this.canvas.hidden = true;
    this.gl = this.canvas.getContext('webgl', { premultipliedAlpha: false, antialias: false });
    this.supported = Boolean(this.gl) && this._init();
    if (!this.supported) return;

    viewer.canvas.appendChild(this.canvas);
    viewer.addHandler('update-viewport', () => this.render());
    this.canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      this.ready = false;
    });
    this.canvas.addEventListener('webglcontextrestored', () => {
      this.ready = this._init();
      if (this.lutImage) this._uploadLut();
      this.render();
    });
  }

  setValues(values) {
    this.values = { ...this.values, ...values };
    this.render();
  }

  setBypass(bypass) {
    this.bypass = bypass;
    this.render();
  }

  // Таблица цвета профиля сканера («Цвет как в сканере»). Хранится и как
  // пиксели: ею же считаются «Авто» и пипетка, чтобы сходиться с шейдером.
  setLut(image) {
    this.lutImage = image;
    this.lutSize = image.height;
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(image, 0, 0);
    this.lutData = context.getImageData(0, 0, image.width, image.height).data;
    if (this.supported && this.ready) this._uploadLut();
    this.render();
  }

  get profileAvailable() {
    return Boolean(this.lutData);
  }

  get profileActive() {
    return this.profileOn && this.profileAvailable;
  }

  setProfile(on) {
    this.profileOn = on;
    this.render();
  }

  // Цвет 0…255 после профиля сканера (или как есть, если профиль выключен).
  profileColor(rgb) {
    return this.profileActive ? sampleLut(this.lutData, this.lutSize, rgb) : rgb;
  }

  get active() {
    return this.supported && this.ready && !this.bypass && (!isDefault(this.values) || this.profileActive);
  }

  // Резкость включена и увеличение позволяет: ниже порога проходы размытия
  // и наложения пропускаются, и кадр стоит столько же, сколько без резкости
  get sharpenApplies() {
    const { magnification } = this.viewInfo();
    return magnification === null || magnification > SHARPEN_MIN_MAGNIFICATION;
  }

  get sharpenActive() {
    return this.values.sharpen > 0 && this.values.sharpAmount > 0 && this.sharpenApplies;
  }

  render() {
    if (!this.supported) return;
    // Без коррекций слой скрыт, и OpenSeadragon работает без накладных расходов.
    this.canvas.hidden = !this.active;
    if (!this.active) return;

    const source = this.viewer.drawer.canvas;
    if (!source.width || !source.height) return;
    const gl = this.gl;
    const { width, height } = source;
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
      this._resizeTargets(width, height);
    }
    gl.viewport(0, 0, width, height);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.frameTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

    const v = this.values;
    const hue = (v.hue * Math.PI) / 180;
    const sharpen = this.sharpenActive;
    // Цветовая цепочка: на экран, а при резкости — в промежуточную текстуру
    this._use(this.color);
    gl.bindFramebuffer(gl.FRAMEBUFFER, sharpen ? this.targets[0].fbo : null);
    gl.uniform1f(this.uniforms.uLutSize, this.profileActive ? this.lutSize : 0);
    gl.uniform1f(this.uniforms.uStain, v.stain);
    gl.uniform3f(this.uniforms.uGain, balanceToGain(v.red), balanceToGain(v.green), balanceToGain(v.blue));
    gl.uniform1f(this.uniforms.uBlack, v.black / 255);
    gl.uniform1f(this.uniforms.uInvRange, 255 / (v.white - v.black));
    gl.uniform1f(this.uniforms.uBrightness, v.brightness / 200);
    gl.uniform1f(this.uniforms.uContrast, 3 ** (v.contrast / 100));
    gl.uniform1f(this.uniforms.uInvGamma, 1 / v.gamma);
    gl.uniform1f(this.uniforms.uSaturation, 1 + v.saturation / 100);
    gl.uniform2f(this.uniforms.uHue, Math.cos(hue), Math.sin(hue));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    if (!sharpen) return;

    // Размытие: targets[0] → [1] по x → [2] по y; блоки текстур 2, 3, 4
    this._use(this.blur);
    // Радиус из пикселей скана в пиксели холста: масштаб экрана и плотность точек
    const ratio = width / (source.clientWidth || width);
    const sigma = Math.min(SIGMA_MAX, Math.max(SIGMA_MIN, v.sharpRadius * this.viewInfo().pixelScale * ratio));
    gl.uniform1f(this.blur.uniforms.uSigma, sigma);
    gl.uniform1i(this.blur.uniforms.uImage, 2);
    gl.uniform2f(this.blur.uniforms.uStep, 1 / width, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.targets[1].fbo);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.uniform1i(this.blur.uniforms.uImage, 3);
    gl.uniform2f(this.blur.uniforms.uStep, 0, 1 / height);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.targets[2].fbo);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    // Наложение на экран
    this._use(this.sharp);
    gl.uniform1i(this.sharp.uniforms.uBase, 2);
    gl.uniform1i(this.sharp.uniforms.uBlur, 4);
    gl.uniform1f(this.sharp.uniforms.uAmount, v.sharpAmount / 100);
    gl.uniform1f(this.sharp.uniforms.uMode, v.sharpMode);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  _use(program) {
    const gl = this.gl;
    gl.useProgram(program.program);
    gl.enableVertexAttribArray(program.position);
    gl.vertexAttribPointer(program.position, 2, gl.FLOAT, false, 0, 0);
  }

  // Промежуточные текстуры резкости: размер кадра, блоки 2–4
  _resizeTargets(width, height) {
    const gl = this.gl;
    this.targets.forEach((target, i) => {
      gl.activeTexture(gl.TEXTURE2 + i);
      gl.bindTexture(gl.TEXTURE_2D, target.texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    });
    gl.activeTexture(gl.TEXTURE0);
  }

  // Гистограмма яркости видимого поля после коррекций (документ заказчика
  // 2026-10-22): уменьшенный кадр пропускается через ту же цепочку на JS.
  // Резкость — соседний фильтр, в гистограмму не входит.
  outputHistogram() {
    const sample = this._sampleFrame();
    if (!sample) return null;
    if (!this.active) return histogramOf(sample);
    const lut = this.profileActive ? (rgb) => this.profileColor(rgb) : null;
    const { data } = sample;
    const bins = new Uint32Array(256);
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 255) continue;
      const [r, g, b] = applyValues(this.values, [data[i], data[i + 1], data[i + 2]], lut);
      bins[Math.min(255, Math.round(luminance(r, g, b)))] += 1;
    }
    return bins;
  }

  // Средний исходный (без коррекций) цвет вокруг точки в координатах элемента вьювера.
  sampleSource(point, radius = 3) {
    const source = this.viewer.drawer.canvas;
    const scale = source.width / source.clientWidth;
    const size = Math.round(radius * 2 * scale) + 1;
    const x = Math.round(point.x * scale - size / 2);
    const y = Math.round(point.y * scale - size / 2);
    const { data } = source.getContext('2d').getImageData(x, y, size, size);
    const sum = [0, 0, 0];
    let count = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 255) continue;
      sum[0] += data[i];
      sum[1] += data[i + 1];
      sum[2] += data[i + 2];
      count += 1;
    }
    return count ? sum.map((channel) => channel / count) : null;
  }

  // Гистограмма яркости видимого поля до коррекций (И-4, И-12): кадр
  // уменьшается до HISTOGRAM_SAMPLE_WIDTH точек по ширине и читается с 2D-canvas.
  sourceHistogram() {
    const sample = this._sampleFrame();
    return sample ? histogramOf(sample) : null;
  }

  _sampleFrame() {
    const source = this.viewer.drawer.canvas;
    if (!source.width || !source.height) return null;
    if (!this.sampleCanvas) this.sampleCanvas = document.createElement('canvas');
    const width = Math.min(HISTOGRAM_SAMPLE_WIDTH, source.width);
    const height = Math.max(1, Math.round((source.height * width) / source.width));
    const sample = this.sampleCanvas;
    if (sample.width !== width || sample.height !== height) {
      sample.width = width;
      sample.height = height;
    }
    const context = sample.getContext('2d', { willReadFrequently: true });
    context.clearRect(0, 0, width, height);
    context.drawImage(source, 0, 0, width, height);
    return context.getImageData(0, 0, width, height);
  }

  // Мера резкости центра видимого поля (исходные пиксели холста OpenSeadragon),
  // приведённая к масштабу скана; null — ткани нет или увеличение ниже порога
  sharpnessMetric() {
    if (!this.sharpenApplies) return null;
    const source = this.viewer.drawer.canvas;
    if (!source.width || !source.height) return null;
    const size = Math.min(AUTO_SHARPEN.sample, source.width, source.height);
    const x = Math.round((source.width - size) / 2);
    const y = Math.round((source.height - size) / 2);
    const sample = source.getContext('2d').getImageData(x, y, size, size);
    const ratio = source.width / (source.clientWidth || source.width);
    return sharpnessMetric(sample, this.viewInfo().pixelScale * ratio);
  }

  _uploadLut() {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.lutTexture);
    // Таблица лежит в PNG строками g = 0 сверху: без переворота, в отличие от кадра
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, this.lutImage);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.activeTexture(gl.TEXTURE0);
  }

  _init() {
    const gl = this.gl;
    const build = (fragment, names) => {
      const program = gl.createProgram();
      for (const [type, source] of [[gl.VERTEX_SHADER, VERTEX_SHADER], [gl.FRAGMENT_SHADER, fragment]]) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          console.error('Шейдер коррекции не собран:', gl.getShaderInfoLog(shader));
          return null;
        }
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return null;
      const uniforms = Object.fromEntries(names.map((name) => [name, gl.getUniformLocation(program, name)]));
      return { program, uniforms, position: gl.getAttribLocation(program, 'aPosition') };
    };
    this.color = build(FRAGMENT_SHADER, ['uImage', 'uLut', 'uLutSize', 'uStain', 'uStainMat', 'uStainInv', 'uGain',
      'uBlack', 'uInvRange', 'uBrightness', 'uContrast', 'uInvGamma', 'uSaturation', 'uHue']);
    this.blur = build(BLUR_SHADER, ['uImage', 'uStep', 'uSigma']);
    this.sharp = build(SHARPEN_SHADER, ['uBase', 'uBlur', 'uAmount', 'uMode']);
    if (!this.color || !this.blur || !this.sharp) return false;
    this.uniforms = this.color.uniforms;

    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

    const textureSetup = (filter) => {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    };
    // Блок 0 — кадр, блок 1 — таблица цвета (линейная интерполяция по красному
    // и зелёному), блоки 2–4 — промежуточные текстуры резкости
    gl.activeTexture(gl.TEXTURE1);
    this.lutTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.lutTexture);
    textureSetup(gl.LINEAR);
    this.targets = [0, 1, 2].map((i) => {
      gl.activeTexture(gl.TEXTURE2 + i);
      const texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      textureSetup(gl.NEAREST);
      const fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
      return { texture, fbo };
    });
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.activeTexture(gl.TEXTURE0);
    this.frameTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.frameTexture);
    textureSetup(gl.NEAREST);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    if (this.canvas.width && this.canvas.height) this._resizeTargets(this.canvas.width, this.canvas.height);

    this._use(this.color);
    gl.uniform1i(this.uniforms.uImage, 0);
    gl.uniform1i(this.uniforms.uLut, 1);
    gl.uniformMatrix3fv(this.uniforms.uStainMat, false, STAIN_MATRIX);
    gl.uniformMatrix3fv(this.uniforms.uStainInv, false, STAIN_INVERSE);
    this.ready = true;
    return true;
  }
}
