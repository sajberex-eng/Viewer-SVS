// Коррекция изображения поверх уже загруженных тайлов.
// OpenSeadragon рисует тайлы на свой 2D-canvas; после каждого кадра он копируется
// в текстуру и перерисовывается шейдером на WebGL-canvas, лежащий сверху.
// Тайлы повторно не загружаются, мини-карта (отдельный canvas) остаётся без коррекций.
//
// Порядок применения (И-5, раздел 7.3 ТЗ):
//   профиль сканера → разделение окрасок → баланс белого → чёрная и белая точки →
//   яркость и контраст → гамма → насыщенность и оттенок.

// Порядок списка — это и порядок чисел в ссылке на поле зрения (раздел 7.5 ТЗ):
// одиннадцать чисел; ссылки прежних форматов несут восемь (LEGACY_ORDER) или
// десять (без последнего, «окраска»).
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
  { id: 'red', label: 'adjust.balance', min: -100, max: 100, step: 1, def: 0, digits: 2 },
  { id: 'green', label: 'adjust.balance', min: -100, max: 100, step: 1, def: 0, digits: 2 },
  { id: 'blue', label: 'adjust.balance', min: -100, max: 100, step: 1, def: 0, digits: 2 },
  // Разделение окрасок: 0 — вся, 1 — только гематоксилин (ядра), 2 — только эозин
  { id: 'stain', label: 'adjust.stain', min: 0, max: 2, step: 1, def: 0, digits: 0 },
];
export const LEGACY_ORDER = ['brightness', 'contrast', 'gamma', 'saturation', 'hue', 'red', 'green', 'blue'];

export const DEFAULTS = Object.fromEntries(PARAMS.map((p) => [p.id, p.def]));

// Значения пресетов предварительные, согласуются с заказчиком на тестовых сканах (И-8).
export const PRESETS = {
  original: { ...DEFAULTS },
  pale: { ...DEFAULTS, black: 20, white: 250, saturation: 35 },
  overstained: { ...DEFAULTS, gamma: 1.4, white: 245 },
  hematoxylin: { ...DEFAULTS, stain: 1 },
  eosin: { ...DEFAULTS, stain: 2 },
};

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

  render() {
    if (!this.supported) return;
    // Без коррекций слой скрыт, и OpenSeadragon работает без накладных расходов.
    this.canvas.hidden = !this.active;
    if (!this.active) return;

    const source = this.viewer.drawer.canvas;
    if (!source.width || !source.height) return;
    const gl = this.gl;
    if (this.canvas.width !== source.width || this.canvas.height !== source.height) {
      this.canvas.width = source.width;
      this.canvas.height = source.height;
    }
    gl.viewport(0, 0, source.width, source.height);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

    const v = this.values;
    const hue = (v.hue * Math.PI) / 180;
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
    return histogramOf(context.getImageData(0, 0, width, height));
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
    const program = gl.createProgram();
    for (const [type, source] of [[gl.VERTEX_SHADER, VERTEX_SHADER], [gl.FRAGMENT_SHADER, FRAGMENT_SHADER]]) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error('Шейдер коррекции не собран:', gl.getShaderInfoLog(shader));
        return false;
      }
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return false;
    gl.useProgram(program);

    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'aPosition');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

    const textureSetup = (filter) => {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    };
    // Блок 1 — таблица цвета (линейная интерполяция по красному и зелёному),
    // блок 0 — кадр; он остаётся активным, и render() пишет в него
    gl.activeTexture(gl.TEXTURE1);
    this.lutTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.lutTexture);
    textureSetup(gl.LINEAR);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    textureSetup(gl.NEAREST);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);

    this.uniforms = {};
    for (const name of ['uImage', 'uLut', 'uLutSize', 'uStain', 'uStainMat', 'uStainInv', 'uGain', 'uBlack',
      'uInvRange', 'uBrightness', 'uContrast', 'uInvGamma', 'uSaturation', 'uHue']) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }
    gl.uniform1i(this.uniforms.uImage, 0);
    gl.uniform1i(this.uniforms.uLut, 1);
    gl.uniformMatrix3fv(this.uniforms.uStainMat, false, STAIN_MATRIX);
    gl.uniformMatrix3fv(this.uniforms.uStainInv, false, STAIN_INVERSE);
    this.ready = true;
    return true;
  }
}
