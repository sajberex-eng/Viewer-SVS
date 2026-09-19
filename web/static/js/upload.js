// Загрузка сканов частями с докачкой. Одновременно передаются два файла (ТЗ Х-6),
// остальные ждут в очереди: канал у администратора один, а сервер слабый.
import { ApiError, api } from './api.js';
import { t } from './i18n.js';

const PARALLEL = 2;
const RETRY_PAUSE_MS = 3000;
const MAX_RETRIES = 5;

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

class Task {
  constructor(file, folderId, queue) {
    this.file = file;
    this.folderId = folderId;
    this.queue = queue;
    this.id = `t${Date.now()}${Math.random().toString(16).slice(2, 8)}`;
    this.uploadId = null;
    this.sent = 0;
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

  async run() {
    this.status = 'running';
    this.queue.changed();
    try {
      const state = await api('/api/uploads', {
        method: 'POST',
        body: { folder_id: this.folderId, name: this.file.name, size: this.file.size },
      });
      this.uploadId = state.id;
      this.sent = state.received; // докачка после обрыва связи
      await this.sendParts(state.part_size);
      if (this.cancelled) return;
      const done = await api(`/api/uploads/${this.uploadId}/complete`, { method: 'POST' });
      this.status = 'done';
      this.slideId = done.slide_id;
    } catch (error) {
      if (this.cancelled) return;
      this.status = 'error';
      this.detail = error.message;
    }
    this.queue.changed();
  }

  async sendParts(partSize) {
    let started = performance.now();
    let startedAt = this.sent;
    let failures = 0;

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
        const received = await this.sendOne(buffer, checksum);
        this.sent = received;
        failures = 0;
      } catch (error) {
        // Обрыв связи: ждём и продолжаем с того места, которое подтвердил сервер
        if (++failures > MAX_RETRIES) throw error;
        this.detail = t('upload.retrying', { n: failures });
        this.queue.changed();
        await new Promise((resolve) => setTimeout(resolve, RETRY_PAUSE_MS));
        const fresh = await api('/api/uploads', {
          method: 'POST',
          body: { folder_id: this.folderId, name: this.file.name, size: this.file.size },
        }).catch(() => null);
        if (fresh) {
          this.uploadId = fresh.id;
          this.sent = fresh.received;
        }
        this.detail = '';
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
    const response = await fetch(`/api/uploads/${this.uploadId}?offset=${this.sent}`, {
      method: 'PUT',
      body: buffer,
      headers,
      credentials: 'same-origin',
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new ApiError(response.status, data.detail || t('common.error'));
    }
    return (await response.json()).received;
  }
}

export class UploadQueue {
  constructor(onChange) {
    this.tasks = [];
    this.onChange = onChange;
    this.running = 0;
  }

  changed() {
    this.onChange(this.tasks);
  }

  get active() {
    return this.tasks.filter((task) => task.status === 'waiting' || task.status === 'running');
  }

  add(files, folderId) {
    for (const file of files) this.tasks.push(new Task(file, folderId, this));
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
