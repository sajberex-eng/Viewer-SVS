// Коррекция изображения поверх уже загруженных тайлов.
// OpenSeadragon рисует тайлы на свой 2D-canvas; после каждого кадра он копируется
// в текстуру и перерисовывается шейдером на WebGL-canvas, лежащий сверху.
// Тайлы повторно не загружаются, мини-карта (отдельный canvas) остаётся без коррекций.
//
// Порядок применения (И-5, раздел 7.3 ТЗ):
//   баланс белого → чёрная и белая точки → яркость и контраст → гамма → насыщенность и оттенок.

// Порядок списка — это и порядок чисел в ссылке на поле зрения (раздел 7.5 ТЗ):
// десять чисел, первые два — чёрная и белая точки. Ссылки прежнего формата
// несут восемь чисел без них (LEGACY_ORDER).
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
];
export const LEGACY_ORDER = ['brightness', 'contrast', 'gamma', 'saturation', 'hue', 'red', 'green', 'blue'];

export const DEFAULTS = Object.fromEntries(PARAMS.map((p) => [p.id, p.def]));

// Значения пресетов предварительные, согласуются с заказчиком на тестовых сканах (И-8).
export const PRESETS = {
  original: { ...DEFAULTS },
  pale: { ...DEFAULTS, black: 20, white: 250, saturation: 35 },
  overstained: { ...DEFAULTS, gamma: 1.4, white: 245 },
};

// Пороги автокоррекции (раздел 7.6 ТЗ); подбираются на тестовых сканах заказчика.
export const AUTO = {
  backgroundPercentile: 0.99, // фон стекла: 99-й процентиль каждого канала
  tissueBelowBackground: 25, // ткань темнее фона не меньше чем на столько уровней
  blackPercentile: 0.01, // чёрная точка: 1-й процентиль яркости ткани…
  blackMax: 50, // …но не выше: миниатюра усредняет ядра с цитоплазмой
};

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
void main() {
  vec4 pixel = texture2D(uImage, vUv);
  vec3 c = pixel.rgb * uGain;
  c = (c - uBlack) * uInvRange;
  c = (c - 0.5) * uContrast + 0.5 + uBrightness;
  c = pow(clamp(c, 0.0, 1.0), vec3(uInvGamma));
  c = mix(vec3(dot(c, LUMA)), c, uSaturation);
  c = c * uHue.x + cross(GRAY_AXIS, c) * uHue.y + GRAY_AXIS * dot(GRAY_AXIS, c) * (1.0 - uHue.x);
  gl_FragColor = vec4(clamp(c, 0.0, 1.0), pixel.a);
}`;

// Та же цепочка на JS для одного цвета 0…255: ею сверяется шейдер
// и считается цвет фона в автокоррекции.
export function applyValues(values, rgb) {
  const v = values;
  const gains = [balanceToGain(v.red), balanceToGain(v.green), balanceToGain(v.blue)];
  const black = v.black / 255;
  const invRange = 255 / (v.white - v.black);
  let c = rgb.map((channel, i) => (channel / 255) * gains[i]);
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

// Автокоррекция по обзорному изображению всего препарата (раздел 7.6 ТЗ):
// белая точка и баланс белого по фону стекла, чёрная точка по ткани.
// Возвращает частичный набор значений или null, если картинка пустая.
export function autoValues(imageData) {
  const { data } = imageData;
  const channels = [new Uint32Array(256), new Uint32Array(256), new Uint32Array(256)];
  let count = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 255) continue;
    channels[0][data[i]] += 1;
    channels[1][data[i + 1]] += 1;
    channels[2][data[i + 2]] += 1;
    count += 1;
  }
  if (!count) return null;

  const background = channels.map((bins) => Math.max(1, percentile(bins, AUTO.backgroundPercentile)));
  const white = Math.max(...background);
  const gains = background.map((level) => white / level);

  // Ткань: заметно темнее фона после выравнивания каналов
  const tissue = new Uint32Array(256);
  const threshold = white - AUTO.tissueBelowBackground;
  let tissueCount = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 255) continue;
    const luma = luminance(data[i] * gains[0], data[i + 1] * gains[1], data[i + 2] * gains[2]);
    if (luma >= threshold) continue;
    tissue[Math.min(255, Math.round(luma))] += 1;
    tissueCount += 1;
  }
  const black = tissueCount ? Math.min(AUTO.blackMax, percentile(tissue, AUTO.blackPercentile)) : 0;

  return {
    black: Math.min(black, white - 1),
    white,
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

  get active() {
    return this.supported && this.ready && !this.bypass && !isDefault(this.values);
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

    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);

    this.uniforms = {};
    for (const name of ['uGain', 'uBlack', 'uInvRange', 'uBrightness', 'uContrast', 'uInvGamma', 'uSaturation', 'uHue']) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }
    this.ready = true;
    return true;
  }
}
