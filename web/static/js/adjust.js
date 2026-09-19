// Коррекция изображения поверх уже загруженных тайлов.
// OpenSeadragon рисует тайлы на свой 2D-canvas; после каждого кадра он копируется
// в текстуру и перерисовывается шейдером на WebGL-canvas, лежащий сверху.
// Тайлы повторно не загружаются, мини-карта (отдельный canvas) остаётся без коррекций.
//
// Порядок применения (И-5):
//   баланс каналов → яркость и контраст → гамма → насыщенность и оттенок.

export const PARAMS = [
  { id: 'brightness', label: 'adjust.brightness', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'contrast', label: 'adjust.contrast', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'gamma', label: 'adjust.gamma', min: 0.2, max: 3, step: 0.01, def: 1, digits: 2 },
  { id: 'saturation', label: 'adjust.saturation', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'hue', label: 'adjust.hue', min: -180, max: 180, step: 1, def: 0, digits: 0 },
  { id: 'red', label: 'adjust.red', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'green', label: 'adjust.green', min: -100, max: 100, step: 1, def: 0, digits: 0 },
  { id: 'blue', label: 'adjust.blue', min: -100, max: 100, step: 1, def: 0, digits: 0 },
];

export const DEFAULTS = Object.fromEntries(PARAMS.map((p) => [p.id, p.def]));

// Значения пресетов предварительные, согласуются с заказчиком на тестовых сканах (И-8).
export const PRESETS = {
  original: { ...DEFAULTS },
  pale: { ...DEFAULTS, contrast: 25, saturation: 35 },
  overstained: { ...DEFAULTS, gamma: 1.4, brightness: 10 },
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
const balanceToGain = (balance) => 2 ** (balance / 100);
export const gainToBalance = (gain) => 100 * Math.log2(gain);

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
  c = (c - 0.5) * uContrast + 0.5 + uBrightness;
  c = pow(clamp(c, 0.0, 1.0), vec3(uInvGamma));
  c = mix(vec3(dot(c, LUMA)), c, uSaturation);
  c = c * uHue.x + cross(GRAY_AXIS, c) * uHue.y + GRAY_AXIS * dot(GRAY_AXIS, c) * (1.0 - uHue.x);
  gl_FragColor = vec4(clamp(c, 0.0, 1.0), pixel.a);
}`;

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
    for (const name of ['uGain', 'uBrightness', 'uContrast', 'uInvGamma', 'uSaturation', 'uHue']) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }
    this.ready = true;
    return true;
  }
}
