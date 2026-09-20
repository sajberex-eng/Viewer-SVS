// Память о выбранных файлах между сеансами (ЗГ-6).
//
// Браузер не может открыть файл с диска сам: разрешение даёт человек. Но
// Chrome и Edge умеют сохранять «ручку» файла (FileSystemFileHandle) в
// IndexedDB, и после перезапуска браузера достаточно одного подтверждения —
// окно выбора файла больше не открывается. Firefox и Safari этого не умеют:
// там всё работает как раньше, через выбор файла (ЗГ-7).
//
// В хранилище лежит только ручка, имя и размер файла. Содержимое скана сюда
// не попадает.

const DB_NAME = 'viewer-uploads';
const STORE = 'handles';

// Ручка файла бывает только вместе с File System Access API: без него
// сохранять нечего.
export const canRemember = typeof window.showOpenFilePicker === 'function' && 'indexedDB' in window;

let dbPromise = null;

function open() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    }).catch((error) => {
      dbPromise = null; // следующая попытка откроет заново
      throw error;
    });
  }
  return dbPromise;
}

// Любая ошибка хранилища — не беда: загрузка просто попросит выбрать файл.
async function run(mode, action) {
  if (!canRemember) return null;
  try {
    const db = await open();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const request = action(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(request ? request.result : null);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } catch {
    return null;
  }
}

export function remember(uploadId, handle, { name, size }) {
  if (!handle) return Promise.resolve(null);
  return run('readwrite', (store) => store.put({ handle, name, size, at: Date.now() }, uploadId));
}

export function recall(uploadId) {
  return run('readonly', (store) => store.get(uploadId));
}

export function forget(uploadId) {
  return run('readwrite', (store) => store.delete(uploadId));
}

// Уборка: записи о загрузках, которых на сервере уже нет (завершены,
// отменены, убраны как брошенные).
export async function keepOnly(uploadIds) {
  const known = new Set(uploadIds);
  const keys = await run('readonly', (store) => store.getAllKeys());
  if (!keys) return;
  for (const key of keys) {
    if (!known.has(key)) await forget(key);
  }
}
