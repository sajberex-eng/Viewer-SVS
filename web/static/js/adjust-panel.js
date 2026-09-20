import { DEFAULTS, PARAMS, PRESETS, clampParam, gainToBalance, isDefault } from './adjust.js';
import { t } from './i18n.js';

const BACKGROUND_MIN_LEVEL = 140; // темнее этого участок фоном стекла не считается

// Полный набор значений в допустимых пределах; недостающие берутся из base.
export function clampValues(values, base = DEFAULTS) {
  return Object.fromEntries(PARAMS.map((p) => [p.id, clampParam(p, Number(values[p.id] ?? base[p.id]))]));
}

// Настройки хранятся в браузере отдельно для пользователя и скана (И-7).
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

// Панель ползунков. Она одна на странице и переключается между половинами
// экрана: в режиме сравнения ползунки действуют на активную половину (С-4).
// Сами значения хранит половина (SlideView.adjustValues), панель их только меняет.
export class AdjustPanel {
  constructor({ panel, onChange }) {
    this.panel = panel;
    this.onChange = onChange;
    this.pane = null;
    this.adjuster = null;
    this.viewer = null;
    this.values = { ...DEFAULTS };
    this.controls = {};
    this.presetSelect = panel.querySelector('#adjustPreset');
    this.hint = panel.querySelector('#adjustHint');

    this._buildSliders(panel.querySelector('#adjustSliders'));
    this._bindPresets();
    this._bindCompare(panel.querySelector('#adjustCompare'));
    this._bindPipette(panel.querySelector('#adjustPipette'));
    panel.querySelector('#adjustResetAll').addEventListener('click', () => this.setValues(DEFAULTS));
    this._refresh();
  }

  // Переключение на другую половину: ползунки показывают её настройки.
  attachTo(pane) {
    if (this.picking) this._stopPicking();
    this.pane = pane;
    this.adjuster = pane.adjuster;
    this.viewer = pane.viewer;
    this.values = { ...pane.adjustValues };
    this._refresh();
    this.onChange(this.values);

    const unsupported = !this.adjuster.supported;
    if (unsupported) this.hint.textContent = t('adjust.noWebgl');
    this.panel.querySelectorAll('input, select, button:not(#adjustClose)').forEach((el) => {
      el.disabled = unsupported;
    });
  }

  setValues(values, { save = true } = {}) {
    this.values = clampValues(values, this.values);
    this._refresh();
    this.pane?.setAdjust(this.values, { save });
    this.onChange(this.values);
  }

  // Компактная запись для ссылки на поле зрения (И-11).
  static serialize(values) {
    return PARAMS.map((p) => values[p.id]).join(',');
  }

  static deserialize(text) {
    const numbers = text.split(',').map(Number);
    if (numbers.length !== PARAMS.length || numbers.some(Number.isNaN)) return null;
    return Object.fromEntries(PARAMS.map((p, i) => [p.id, numbers[i]]));
  }

  _refresh() {
    for (const param of PARAMS) {
      const { range, number } = this.controls[param.id];
      range.value = this.values[param.id];
      number.value = this.values[param.id];
    }
    const preset = Object.keys(PRESETS).find((name) => PARAMS.every((p) => PRESETS[name][p.id] === this.values[p.id]));
    this.presetSelect.value = preset ?? 'custom';
  }

  _buildSliders(container) {
    for (const param of PARAMS) {
      const row = document.createElement('div');
      row.className = 'adjust-row';
      const label = document.createElement('label');
      label.textContent = t(param.label);
      label.htmlFor = `adj-${param.id}`;

      const range = document.createElement('input');
      Object.assign(range, { type: 'range', id: `adj-${param.id}`, min: param.min, max: param.max, step: param.step });
      range.title = t('adjust.slider.tip');

      const number = document.createElement('input');
      Object.assign(number, { type: 'number', min: param.min, max: param.max, step: param.step });
      number.setAttribute('aria-label', t(param.label));

      range.addEventListener('input', () => this.setValues({ [param.id]: range.valueAsNumber }));
      range.addEventListener('dblclick', () => this.setValues({ [param.id]: param.def }));
      number.addEventListener('change', () => this.setValues({ [param.id]: number.valueAsNumber }));

      row.append(label, number, range);
      container.append(row);
      this.controls[param.id] = { range, number };
    }
  }

  _bindPresets() {
    this.presetSelect.addEventListener('change', () => {
      const preset = PRESETS[this.presetSelect.value];
      if (preset) this.setValues(preset);
    });
  }

  // «До/После»: исходное изображение видно, пока кнопка удерживается (И-4).
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

  // Пипетка белого: баланс каналов выравнивает цвет пустого фона до нейтрального (И-9).
  // Щелчок ловится на уровне страницы, поэтому пипетка работает и после
  // переключения на другую половину экрана.
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
    const color = this.adjuster.sampleSource(position);
    if (!color || Math.max(...color) < BACKGROUND_MIN_LEVEL) {
      this.hint.textContent = t('adjust.pipette.dark');
      return true;
    }
    const target = Math.max(...color);
    const [red, green, blue] = color.map((channel) => gainToBalance(target / channel));
    this._stopPicking();
    this.setValues({ red, green, blue });
    return true;
  }

  _stopPicking() {
    this.picking = false;
    this.pipetteButton.classList.remove('is-active');
    this.viewer?.element.classList.remove('is-picking');
    this.hint.textContent = '';
  }
}
