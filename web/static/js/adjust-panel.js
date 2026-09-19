import { DEFAULTS, PARAMS, PRESETS, clampParam, gainToBalance, isDefault } from './adjust.js';
import { t } from './i18n.js';

const BACKGROUND_MIN_LEVEL = 140; // темнее этого участок фоном стекла не считается

// Панель ползунков. Настройки хранятся в браузере отдельно для пользователя и слайда (И-7).
export class AdjustPanel {
  constructor({ panel, adjuster, viewer, storageKey, onChange }) {
    this.panel = panel;
    this.adjuster = adjuster;
    this.viewer = viewer;
    this.storageKey = storageKey;
    this.onChange = onChange;
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

    if (!adjuster.supported) {
      this.hint.textContent = t('adjust.noWebgl');
      panel.querySelectorAll('input, select, button:not(#adjustClose)').forEach((el) => { el.disabled = true; });
    }
  }

  loadSaved() {
    try {
      const saved = JSON.parse(localStorage.getItem(this.storageKey));
      if (saved) this.setValues(saved, { save: false });
    } catch {
      // повреждённая или недоступная запись: остаются значения по умолчанию
    }
  }

  setValues(values, { save = true } = {}) {
    for (const param of PARAMS) {
      if (values[param.id] !== undefined) this.values[param.id] = clampParam(param, Number(values[param.id]));
    }
    this._refresh();
    this.adjuster.setValues(this.values);
    if (save) this._save();
    this.onChange(this.values);
  }

  // Компактная запись для ссылки на поле зрения (И-11).
  serialize() {
    return PARAMS.map((p) => this.values[p.id]).join(',');
  }

  static deserialize(text) {
    const numbers = text.split(',').map(Number);
    if (numbers.length !== PARAMS.length || numbers.some(Number.isNaN)) return null;
    return Object.fromEntries(PARAMS.map((p, i) => [p.id, numbers[i]]));
  }

  _save() {
    try {
      if (isDefault(this.values)) localStorage.removeItem(this.storageKey);
      else localStorage.setItem(this.storageKey, JSON.stringify(this.values));
    } catch {
      // хранилище браузера недоступно (приватный режим): настройки живут до закрытия страницы
    }
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
  _bindPipette(button) {
    const stop = () => {
      this.picking = false;
      button.classList.remove('is-active');
      this.viewer.element.classList.remove('is-picking');
      this.hint.textContent = '';
    };
    button.addEventListener('click', () => {
      if (this.picking) return stop();
      this.picking = true;
      button.classList.add('is-active');
      this.viewer.element.classList.add('is-picking');
      this.hint.textContent = t('adjust.pipette.hint');
    });
    document.addEventListener('keydown', (event) => {
      if (event.code === 'Escape' && this.picking) stop();
    });
    this.viewer.addHandler('canvas-click', (event) => {
      if (!this.picking || !event.quick) return;
      event.preventDefaultAction = true;
      const color = this.adjuster.sampleSource(event.position);
      if (!color || Math.max(...color) < BACKGROUND_MIN_LEVEL) {
        this.hint.textContent = t('adjust.pipette.dark');
        return;
      }
      const target = Math.max(...color);
      const [red, green, blue] = color.map((channel) => gainToBalance(target / channel));
      stop();
      this.setValues({ red, green, blue });
    });
  }
}
