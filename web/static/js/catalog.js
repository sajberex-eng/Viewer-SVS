import { api, logout } from './api.js';
import { applyI18n, formatNumber, setLanguage, t } from './i18n.js';

const $ = (id) => document.getElementById(id);

setLanguage('ru');
applyI18n();
main();

async function main() {
  const user = await api('/api/me');
  $('userName').textContent = user.name || user.login;
  $('btnLogout').addEventListener('click', logout);

  let slides = [];
  const render = () => {
    const needle = $('search').value.trim().toLowerCase();
    const visible = slides.filter((slide) => slide.title.toLowerCase().includes(needle));
    $('grid').replaceChildren(...visible.map(card));
    if (!slides.length) setNote(t('catalog.empty'));
    else if (!visible.length) setNote(t('catalog.nothingFound'));
    else setNote('');
  };
  const load = async () => {
    setNote(t('common.loading'));
    try {
      slides = await api('/api/slides');
      render();
    } catch (error) {
      setNote(error.message, true); // в том числе «хранилище недоступно»
    }
  };

  $('search').addEventListener('input', render);
  if (user.role === 'admin') {
    $('btnRescan').hidden = false;
    $('btnRescan').addEventListener('click', async () => {
      $('btnRescan').disabled = true;
      try {
        const result = await api('/api/slides/rescan', { method: 'POST' });
        await load();
        setNote(t('catalog.rescan.done', result));
      } catch (error) {
        setNote(error.message, true);
      }
      $('btnRescan').disabled = false;
    });
  }
  load();
}

function setNote(text, isError = false) {
  $('note').textContent = text;
  $('note').classList.toggle('is-error', isError);
}

function card(slide) {
  const item = document.createElement('li');
  const link = document.createElement('a');
  link.className = 'slide-card';
  link.href = `/viewer?slide=${encodeURIComponent(slide.id)}`;

  const image = document.createElement('img');
  image.src = `/api/slides/${encodeURIComponent(slide.id)}/thumbnail.jpg`;
  image.alt = '';
  image.loading = 'lazy';

  const title = document.createElement('span');
  title.className = 'slide-card-title';
  title.textContent = slide.title;

  const details = document.createElement('span');
  details.className = 'muted';
  const scan = slide.objective ? t('catalog.scan', { objective: `${formatNumber(slide.objective)}×` }) : '';
  const size = slide.size_bytes >= 1e9
    ? `${formatNumber(slide.size_bytes / 1e9, 2)} ГБ`
    : `${formatNumber(slide.size_bytes / 1e6)} МБ`;
  details.textContent = [scan, size].filter(Boolean).join(' · ');

  link.append(image, title, details);
  item.append(link);
  return item;
}
