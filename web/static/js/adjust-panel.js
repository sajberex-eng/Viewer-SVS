import {
  DEFAULTS, LEGACY_ORDER, PARAMS, PRESETS, autoValues, balanceToGain, clampParam, gainToBalance, isDefault, loadImage,
} from './adjust.js';
import { formatNumber, t } from './i18n.js';

const BACKGROUND_MIN_LEVEL = 140; // темнее этого участок фоном стекла не считается
const HISTOGRAM_INTERVAL_MS = 250; // гистограмма не чаще 4 раз в секунду (И-12)
const LEVELS = ['black', 'gamma', 'white']; // маркеры шкалы уровней слева направо
const EXTRA = ['brightness', 'contrast', 'saturation', 'hue']; // блок «Дополнительно»
const BALANCE = ['red', 'green', 'blue'];
const LINK_ORDERS = { // число чисел в ссылке → порядок параметров (раздел 7.5 ТЗ)
  [LEGACY_ORDER.length]: LEGACY_ORDER,
  [PARAMS.length - 1]: PARAMS.slice(0, -1).map((p) => p.id), // до появления «окраски»
  [PARAMS.length]: PARAMS.map((p) => p.id),
};
const byId = Object.fromEntries(PARAMS.map((p) => [p.id, p]));

// Полный набор значений в допустимых пределах; недостающие берутся из base.
// Чёрная точка всегда меньше белой: ограничивается тот маркер, что двигался (раздел 7.2 ТЗ).
export function clampValues(values, base = DEFAULTS) {
  const result = Object.fromEntries(PARAMS.map((p) => [p.id, clampParam(p, Number(values[p.id] ?? base[p.id]))]));
  if (result.black >= result.white) {
    if ('white' in values && !('black' in values)) result.white = result.black + 1;
    else result.black = result.white - 1;
  }
  return result;
}

// Настройки хранятся в браузере отдельно для пользователя и скана (И-7).
// Запись прежнего формата без чёрной и белой точек читается как есть:
// недостающие берут значения по умолчанию (раздел 7.5 ТЗ).
export function loadSavedValues(storageKey) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    return saved ? clampValues(saved) : null;
  } catch {
    return null; // повреждённая или недоступная запись
  }
}

export function saveValues(storageKey, values) {
  try {
    if (isDefault(values)) localStorage.removeItem(storageKey);
    else localStorage.setItem(storageKey, JSON.stringify(values));
  } catch {
    // хранилище браузера недоступно (приватный режим): настройки живут до закрытия страницы
  }
}

// Маркер гаммы стоит между чёрной и белой точками: 1,00 посередине, сдвиг
// влево увеличивает гамму (средние тона светлеют), как в Levels редакторов.
// Шкала логарифмическая и несимметричная: слева до 3, справа до 0,2.
const gammaToFraction = (gamma) => (
  gamma >= 1 ? 0.5 - Math.log(gamma) / Math.log(3) / 2 : 0.5 + Math.log(gamma) / Math.log(0.2) / 2
);
const fractionToGamma = (fraction) => (
  fraction <= 0.5 ? 3 ** ((0.5 - fraction) * 2) : 0.2 ** ((fraction - 0.5) * 2)
);

// Панель настройки изображения (раздел 7 ТЗ). Она одна на странице и
// переключается между половинами экрана: в режиме сравнения действует на
// активную половину (С-4). Сами значения хранит половина (SlideView.adjustValues),
// панель их только показывает и меняет.
export class AdjustPanel {
  constructor({ panel, onChange }) {
    this.panel = panel;
    this.onChange = onChange;
    this.pane = null;
    this.adjuster = null;
    this.viewer = null;
    this.values = { ...DEFAULTS };
    this.open = false;
    this.controls = {};
    this.markers = {};
    this.fields = {};
    this.presetSelect = panel.querySelector('#adjustPreset');
    this.hint = panel.querySelector('#adjustHint');
    this.histogram = panel.querySelector('#adjustHistogram');
    this.balanceText = panel.querySelector('#adjustBalance');
    this.profileCheck = panel.querySelector('#adjustProfile');
    this.stainSelect = panel.querySelector('#adjustStain');
    this.histogramHandler = () => this._scheduleHistogram();

    this._buildLevels(panel.querySelector('#adjustLevels'), panel.querySelector('#adjustLevelFields'));
    this._buildSliders(panel.querySelector('#adjustSliders'));
    this._bindPresets();
    this._bindCompare(panel.querySelector('#adjustCompare'));
    this._bindPipette(panel.querySelector('#adjustPipette'));
    this._bindAuto(panel.querySelector('#adjustAuto'));
    // «Сбросить всё» возвращает и профиль сканера: по умолчанию он включён, если есть в файле
    panel.querySelector('#adjustResetAll').addEventListener('click', () => {
      this.setValues(DEFAULTS);
      if (this.pane?.profileAvailable) this.pane.setProfile(true);
      this.refreshProfile();
    });
    this.profileCheck.addEventListener('change', () => {
      this.pane?.setProfile(this.profileCheck.checked);
      this._drawHistogram();
    });
    this.stainSelect.addEventListener('change', () => this.setValues({ stain: Number(this.stainSelect.value) }));
    panel.querySelector('#adjustBalanceReset').addEventListener('click', () => {
      this.setValues(Object.fromEntries(BALANCE.map((id) => [id, 0])));
    });
    this._refresh();
  }

  // Переключение на другую половину: панель показывает её настройки.
  attachTo(pane) {
    if (this.picking) this._stopPicking();
    this.viewer?.removeHandler('update-viewport', this.histogramHandler);
    this.pane = pane;
    this.adjuster = pane.adjuster;
    this.viewer = pane.viewer;
    this.viewer.addHandler('update-viewport', this.histogramHandler);
    this.values = { ...pane.adjustValues };
    this._refresh();
    this.onChange(this.values);

    const unsupported = !this.adjuster.supported;
    this.hint.textContent = unsupported ? t('adjust.noWebgl') : '';
    this.panel.classList.toggle('is-disabled', unsupported);
    this.panel.querySelectorAll('input, select, button:not(#adjustClose)').forEach((el) => {
      el.disabled = unsupported;
    });
    for (const marker of Object.values(this.markers)) marker.tabIndex = unsupported ? -1 : 0;
    this.refreshProfile();
    this._drawHistogram();
  }

  // Отметка «Цвет как в сканере»: доступна, когда в файле есть профиль (таблица
  // приходит с сервера после открытия скана, половина сообщает через onProfile).
  refreshProfile() {
    const available = Boolean(this.pane?.profileAvailable) && Boolean(this.adjuster?.supported);
    this.profileCheck.disabled = !available;
    this.profileCheck.checked = available && this.pane.profileEnabled;
    this.profileCheck.parentElement.title = t(available ? 'adjust.profile.tip' : 'adjust.profile.none');
  }

  // Гистограмма считается только при открытой панели (И-12).
  setOpen(open) {
    this.open = open;
    if (open) this._drawHistogram();
  }

  setValues(values, { save = true } = {}) {
    this.values = clampValues(values, this.values);
    this._refresh();
    this.pane?.setAdjust(this.values, { save });
    this.onChange(this.values);
  }

  // Компактная запись для ссылки на поле зрения (И-11): десять чисел в
  // порядке PARAMS. Ссылка прежнего формата несёт восемь (раздел 7.5 ТЗ).
  static serialize(values) {
    return PARAMS.map((p) => values[p.id]).join(',');
  }

  static deserialize(text) {
    const numbers = text.split(',').map(Number);
    if (numbers.some(Number.isNaN)) return null;
    const order = LINK_ORDERS[numbers.length];
    return order ? clampValues(Object.fromEntries(order.map((id, i) => [id, numbers[i]]))) : null;
  }

  _refresh() {
    const v = this.values;
    for (const id of EXTRA) {
      const { range, number } = this.controls[id];
      range.value = v[id];
      number.value = v[id];
    }
    for (const id of LEVELS) this.fields[id].value = v[id];
    this.stainSelect.value = String(v.stain);

    const left = v.black / 255;
    const right = v.white / 255;
    const positions = { black: left, white: right, gamma: left + (right - left) * gammaToFraction(v.gamma) };
    for (const id of LEVELS) {
      const marker = this.markers[id];
      marker.style.left = `${positions[id] * 100}%`;
      marker.setAttribute('aria-valuenow', v[id]);
      marker.setAttribute('aria-valuetext', formatNumber(v[id], byId[id].digits));
    }
    const gains = BALANCE.map((id) => balanceToGain(v[id]));
    this.balanceText.textContent = BALANCE.every((id) => v[id] === 0)
      ? t('adjust.balance.none')
      : gains.map((gain) => formatNumber(gain, 2)).join(' · ');

    const preset = Object.keys(PRESETS).find((name) => PARAMS.every((p) => PRESETS[name][p.id] === v[p.id]));
    this.presetSelect.value = preset ?? 'custom';
    this._drawHistogram();
  }

  // Шкала уровней: полоса от чёрного к белому и три маркера, которые
  // двигаются мышью и стрелками (И-2). Под ними числовые поля.
  _buildLevels(container, fieldsContainer) {
    const bar = document.createElement('div');
    bar.className = 'levels-bar';
    container.append(bar);
    container.setAttribute('aria-label', t('adjust.levels'));

    for (const id of LEVELS) {
      const param = byId[id];
      const marker = document.createElement('div');
      marker.className = `levels-marker is-${id}`;
      marker.tabIndex = 0;
      marker.setAttribute('role', 'slider');
      marker.setAttribute('aria-label', t(param.label));
      marker.setAttribute('aria-valuemin', param.min);
      marker.setAttribute('aria-valuemax', param.max);
      marker.title = t('adjust.marker.tip');

      marker.addEventListener('pointerdown', (event) => {
        if (this.panel.classList.contains('is-disabled')) return;
        marker.setPointerCapture(event.pointerId);
        marker.focus();
        event.preventDefault();
      });
      marker.addEventListener('pointermove', (event) => {
        if (!marker.hasPointerCapture(event.pointerId)) return;
        const rect = bar.getBoundingClientRect();
        const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        this.setValues({ [id]: this._levelFromFraction(id, fraction) });
      });
      marker.addEventListener('dblclick', () => this.setValues({ [id]: param.def }));
      marker.addEventListener('keydown', (event) => {
        let direction = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[event.code];
        if (!direction) return;
        // Шаг ±1 (гамма ±0,05), с Shift в десять раз крупнее; стрелки не
        // должны дойти до горячих клавиш вьювера и сдвинуть препарат.
        // Маркер гаммы движется влево при росте гаммы, стрелки идут за маркером.
        event.preventDefault();
        event.stopPropagation();
        if (id === 'gamma') direction = -direction;
        const step = (id === 'gamma' ? 0.05 : 1) * (event.shiftKey ? 10 : 1);
        this.setValues({ [id]: this.values[id] + direction * step });
      });
      container.append(marker);
      this.markers[id] = marker;

      const label = document.createElement('label');
      label.textContent = t(param.label);
      const number = document.createElement('input');
      Object.assign(number, { type: 'number', min: param.min, max: param.max, step: param.step });
      number.setAttribute('aria-label', t(param.label));
      number.addEventListener('change', () => this.setValues({ [id]: number.valueAsNumber }));
      label.append(number);
      fieldsContainer.append(label);
      this.fields[id] = number;
    }
  }

  _levelFromFraction(id, fraction) {
    if (id !== 'gamma') return Math.round(fraction * 255);
    const { black, white } = this.values;
    const inner = Math.min(1, Math.max(0, (fraction * 255 - black) / (white - black)));
    return fractionToGamma(inner);
  }

  _buildSliders(container) {
    for (const id of EXTRA) {
      const param = byId[id];
      const row = document.createElement('div');
      row.className = 'adjust-row';
      const label = document.createElement('label');
      label.textContent = t(param.label);
      label.htmlFor = `adj-${id}`;

      const range = document.createElement('input');
      Object.assign(range, { type: 'range', id: `adj-${id}`, min: param.min, max: param.max, step: param.step });
      range.title = t('adjust.slider.tip');

      const number = document.createElement('input');
      Object.assign(number, { type: 'number', min: param.min, max: param.max, step: param.step });
      number.setAttribute('aria-label', t(param.label));

      range.addEventListener('input', () => this.setValues({ [id]: range.valueAsNumber }));
      range.addEventListener('dblclick', () => this.setValues({ [id]: param.def }));
      number.addEventListener('change', () => this.setValues({ [id]: number.valueAsNumber }));

      row.append(label, number, range);
      container.append(row);
      this.controls[id] = { range, number };
    }
  }

  _bindPresets() {
    this.presetSelect.addEventListener('change', () => {
      const preset = PRESETS[this.presetSelect.value];
      if (preset) this.setValues(preset);
    });
  }

  // «До/После»: исходное изображение видно, пока кнопка удерживается (И-4).
  // Гистограмма при этом не меняется: она и так по исходному изображению.
  _bindCompare(button) {
    const set = (bypass) => {
      button.classList.toggle('is-active', bypass);
      this.adjuster.setBypass(bypass);
    };
    button.addEventListener('pointerdown', (event) => {
      set(true);
      button.setPointerCapture(event.pointerId); // pointerup придёт, даже если курсор ушёл с кнопки
    });
    for (const type of ['pointerup', 'pointercancel', 'blur']) button.addEventListener(type, () => set(false));
    button.addEventListener('keydown', (event) => {
      if (event.code === 'Space' || event.code === 'Enter') set(true);
    });
    button.addEventListener('keyup', () => set(false));
  }

  // Пипетка белого (И-9): баланс белого выравнивает цвет пустого фона до
  // нейтрального, белая точка встаёт на его яркость. Цвет берётся после профиля
  // сканера, как его видит шейдер. Щелчок ловится на уровне страницы, поэтому
  // пипетка работает и после переключения на другую половину.
  _bindPipette(button) {
    this.pipetteButton = button;
    button.addEventListener('click', () => {
      if (this.picking) return this._stopPicking();
      this.picking = true;
      button.classList.add('is-active');
      this.viewer?.element.classList.add('is-picking');
      this.hint.textContent = t('adjust.pipette.hint');
    });
    document.addEventListener('keydown', (event) => {
      if (event.code === 'Escape' && this.picking) this._stopPicking();
    });
  }

  // Вызывается половиной экрана при щелчке по изображению.
  pickWhite(position) {
    if (!this.picking) return false;
    const raw = this.adjuster.sampleSource(position);
    const color = raw && this.adjuster.profileColor(raw);
    if (!color || Math.max(...color) < BACKGROUND_MIN_LEVEL) {
      this.hint.textContent = t('adjust.pipette.dark');
      return true;
    }
    const target = Math.max(...color);
    const [red, green, blue] = color.map((channel) => gainToBalance(target / channel));
    this._stopPicking();
    this.setValues({ red, green, blue, white: Math.round(target) });
    return true;
  }

  _stopPicking() {
    this.picking = false;
    this.pipetteButton.classList.remove('is-active');
    this.viewer?.element.classList.remove('is-picking');
    this.hint.textContent = '';
  }

  // «Авто» (И-13): как в сканере — баланс белого и белая точка по фону стекла
  // на миниатюре всего препарата (раздел 7.6 ТЗ), поэтому результат не зависит
  // от поля зрения; чёрная точка не трогается.
  _bindAuto(button) {
    button.addEventListener('click', async () => {
      if (!this.pane || button.disabled) return;
      const pane = this.pane;
      button.disabled = true;
      try {
        const image = await loadImage(`/api/slides/${encodeURIComponent(pane.slide.id)}/thumbnail.jpg`);
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        context.drawImage(image, 0, 0);
        const lut = pane.adjuster.profileActive ? (rgb) => pane.adjuster.profileColor(rgb) : null;
        const values = autoValues(context.getImageData(0, 0, canvas.width, canvas.height), lut);
        if (!values) throw new Error('empty');
        if (this.pane === pane) this.setValues(values); // пока грузилась миниатюра, половину могли сменить
        else pane.setAdjust(values);
      } catch {
        this.hint.textContent = t('adjust.auto.error');
      } finally {
        button.disabled = !this.adjuster?.supported;
      }
    });
  }

  _scheduleHistogram() {
    if (!this.open || this.histogramTimer) return;
    this.histogramTimer = setTimeout(() => {
      this.histogramTimer = null;
      this._drawHistogram();
    }, HISTOGRAM_INTERVAL_MS);
  }

  // Гистограмма яркости видимого поля до коррекций: корневая ось, чтобы пик
  // фона стекла не скрывал ткань; участки за чёрной и белой точками затенены.
  _drawHistogram() {
    if (!this.open || !this.adjuster?.supported) return;
    const canvas = this.histogram;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.round(canvas.clientWidth * ratio) || canvas.width;
    const height = Math.round(canvas.clientHeight * ratio) || canvas.height;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, width, height);

    const bins = this.adjuster.sourceHistogram();
    if (bins) {
      const peak = Math.sqrt(Math.max(...bins)) || 1;
      context.fillStyle = '#7a7f8c';
      const step = width / 256;
      for (let level = 0; level < 256; level += 1) {
        const bar = (Math.sqrt(bins[level]) / peak) * (height - 2);
        if (bar > 0) context.fillRect(level * step, height - bar, Math.max(1, step), bar);
      }
    }
    context.fillStyle = 'rgba(0, 0, 0, 0.16)';
    context.fillRect(0, 0, (this.values.black / 255) * width, height);
    context.fillRect((this.values.white / 255) * width, 0, width, height);
  }
}
