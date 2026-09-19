// Загрузка сканов частями с докачкой. Одновременно передаются два файла (ТЗ Х-6),
// остальные ждут в очереди: канал у администратора один, а сервер слабый.
import { ApiError, api } from './api.js';
import { t } from './i18n.js';

// Однофайловые форматы сканов; тот же список на сервере (storage.SLIDE_EXTENSIONS)
export const SLIDE_EXTENSIONS = ['.svs', '.ndpi', '.scn', '.tiff', '.tif', '.bif', '.czi', '.avs', '.svslide', '.kfb'];

export const isSlideFile = (name) => SLIDE_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext));

const PARALLEL = 2;
const RETRY_FIRST_MS = 3000;
const RETRY_MAX_MS = 60000;

// Ответы, после которых стоит просто подождать и повторить: нет связи (0),
// сервер перезапускается (Caddy отвечает 502–504), хранилище временно
// недоступно (503). 409 значит «сверься с сервером», остальное окончательно:
// нет места, загрузка удалена, файл отклонён, сессия истекла (ЗГ-1).
const TEMPORARY = new Set([0, 408, 429, 500, 502, 503, 504]);

// Контрольная сумма части (ТЗ Х-5). Web Crypto доступен только в защищённом
// контексте: по HTTPS и на localhost. Если его нет, часть уходит без суммы,
// целостность файла всё равно проверяется при открытии скана.
async function sha256hex(buffer) {
  if (!crypto?.subtle) return null;
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function speedText(bytesPerSecond, remainingBytes) {
  if (!bytesPerSecond) return '';
  const mbs = bytesPerSecond / 1e6;
  const seconds = Math.round(remainingBytes / bytesPerSecond);
  const left = seconds >= 60
    ? t('upload.leftMinutes', { n: Math.ceil(seconds / 60) })
    : t('upload.leftSeconds', { n: Math.max(seconds, 1) });
  return t('upload.speed', { mbs: mbs.toFixed(1), left });
}

// Пауза до следующей попытки; событие браузера «связь появилась» её прерывает.
function waitForRetry(ms) {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      removeEventListener('online', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    addEventListener('online', done);
  });
}

// Запрос загрузки. В отличие от api(), истёкшая сессия не уводит со страницы:
// загрузка останавливается, а после входа в другой вкладке её можно повторить.
// Когда браузер замечает, что связь пропала, запрос обрывается сразу, а не
// висит до тайм-аута соединения.
async function request(path, { method = 'POST', body, headers } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  addEventListener('offline', abort);
  let response;
  try {
    response = await fetch(path, { method, body, headers, credentials: 'same-origin', signal: controller.signal });
  } catch {
    throw new ApiError(0, t('common.networkError'));
  } finally {
    removeEventListener('offline', abort);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = response.status === 401 ? t('upload.sessionExpired') : data.detail;
    throw new ApiError(response.status, typeof message === 'string' ? message : t('common.error'));
  }
  return data;
}

class Task {
  constructor(file, folderId, queue, startedAt = 0) {
    this.file = file;
    this.folderId = folderId;
    this.queue = queue;
    this.id = `t${Date.now()}${Math.random().toString(16).slice(2, 8)}`;
    this.uploadId = null;
    this.sent = startedAt; // уже принятое сервером: полоса не начинается с нуля
    this.status = 'waiting'; // waiting | running | done | error | canceled
    this.detail = '';
    this.cancelled = false;
  }

  get progress() {
    return this.file.size ? this.sent / this.file.size : 0;
  }

  cancel() {
    this.cancelled = true;
    this.status = 'canceled';
    if (this.uploadId) api(`/api/uploads/${this.uploadId}`, { method: 'DELETE' }).catch(() => {});
    this.queue.changed();
  }

  // «Повторить» у загрузки с ошибкой (ЗГ-2): файл ещё открыт на странице,
  // сервер помнит принятое, поэтому загрузка продолжается с места остановки.
  retry() {
    if (this.status !== 'error') return;
    this.status = 'waiting';
    this.detail = '';
    this.queue.changed();
    this.queue.pump();
  }

  async run() {
    this.status = 'running';
    this.detail = '';
    this.queue.changed();
    try {
      const partSize = await this.patiently(() => this.sync());
      for (;;) {
        await this.sendParts(partSize);
        if (this.cancelled) return;
        try {
          await this.patiently(() => request(`/api/uploads/${this.uploadId}/complete`));
          break;
        } catch (error) {
          if (error.status !== 409) throw error;
          await this.patiently(() => this.sync()); // сервер принял не всё: досылаем
        }
      }
      this.status = 'done';
    } catch (error) {
      if (this.cancelled) return;
      this.status = 'error';
      this.detail = error.message;
    }
    this.queue.changed();
  }

  // Начать загрузку или узнать у сервера, сколько уже принято: докачка
  // идёт с последнего подтверждённого байта.
  async sync() {
    const state = await request('/api/uploads', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: this.folderId, name: this.file.name, size: this.file.size }),
    });
    this.uploadId = state.id;
    this.sent = state.received;
    return state.part_size;
  }

  // Повторяет действие, пока ошибка временная. Пауза растёт от 3 секунд
  // до минуты; число попыток не ограничено (ЗГ-1).
  async patiently(action) {
    let pause = RETRY_FIRST_MS;
    for (;;) {
      try {
        const result = await action();
        if (pause !== RETRY_FIRST_MS) this.detail = '';
        return result;
      } catch (error) {
        if (this.cancelled || !TEMPORARY.has(error.status)) throw error;
        this.detail = t('upload.offline', { n: Math.round(pause / 1000) });
        this.queue.changed();
        await waitForRetry(pause);
        if (this.cancelled) throw error;
        pause = Math.min(pause * 2, RETRY_MAX_MS);
      }
    }
  }

  async sendParts(partSize) {
    let started = performance.now();
    let startedAt = this.sent;

    while (this.sent < this.file.size && !this.cancelled) {
      const chunk = this.file.slice(this.sent, this.sent + partSize);
      let buffer;
      try {
        buffer = await chunk.arrayBuffer();
      } catch {
        throw new ApiError(0, t('upload.readError'));
      }
      const checksum = await sha256hex(buffer);
      try {
        this.sent = await this.patiently(() => this.sendOne(buffer, checksum));
      } catch (error) {
        if (error.status !== 409) throw error;
        // Часть повреждена или сервер принял не столько, сколько мы думали:
        // сверяемся и продолжаем с подтверждённого места
        await this.patiently(() => this.sync());
        started = performance.now();
        startedAt = this.sent;
        continue;
      }
      const elapsed = (performance.now() - started) / 1000;
      if (elapsed > 1.5) {
        this.detail = speedText((this.sent - startedAt) / elapsed, this.file.size - this.sent);
        started = performance.now();
        startedAt = this.sent;
      }
      this.queue.changed();
    }
  }

  async sendOne(buffer, checksum) {
    const headers = checksum ? { 'x-part-sha256': checksum } : {};
    const state = await request(`/api/uploads/${this.uploadId}?offset=${this.sent}`, { method: 'PUT', body: buffer, headers });
    return state.received;
  }
}

export class UploadQueue {
  constructor(onChange) {
    this.tasks = [];
    this.onChange = onChange;
    this.running = 0;
    // Уход со страницы обрывает загрузку: браузер переспрашивает (ЗГ-3)
    addEventListener('beforeunload', (event) => {
      if (!this.active.length) return;
      event.preventDefault();
      event.returnValue = ''; // старые браузеры задают вопрос только так
    });
  }

  changed() {
    this.onChange(this.tasks);
  }

  get active() {
    return this.tasks.filter((task) => task.status === 'waiting' || task.status === 'running');
  }

  add(files, folderId, startedAt = 0) {
    for (const file of files) this.tasks.push(new Task(file, folderId, this, startedAt));
    this.changed();
    this.pump();
  }

  clearFinished() {
    this.tasks = this.active;
    this.changed();
  }

  async pump() {
    while (this.running < PARALLEL) {
      const next = this.tasks.find((task) => task.status === 'waiting');
      if (!next) return;
      this.running += 1;
      next.run().finally(() => {
        this.running -= 1;
        this.pump();
      });
    }
  }
}
