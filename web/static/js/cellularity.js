// Оценка клеточности костного мозга (этап 11, раздел 13.8 ТЗ): панель, запуск
// расчёта, ход, результат и маски поверх препарата.
//
// Контуры «Ткань» и «Артефакт» — аннотации видов tissue и artifact: рисуются тем же
// слоем, что и пометки для обучения (КЛ-2). Маски (КЛ-6) приходят тайлами той же
// сетки, что сам скан, и ложатся вторым слоем OpenSeadragon: поворот и отражение
// действуют на них сами собой. Кнопка есть только у сканов H&E (ОК-4).
import { api } from './api.js';
import { confirmDialog } from './dialog.js';
import { formatDateTime, formatNumber, t } from './i18n.js';

const $ = (id) => document.getElementById(id);
const POLL_MS = 1500;
const CONTOUR_KINDS = ['tissue', 'artifact'];
const PARTS = ['bone', 'hemato', 'adip', 'imv', 'other', 'artifacts'];
// Цвета частей — как в MarrowQuant (КЛ-6), те же, что в палитре карт на сервере
const COLORS = { bone: '#9eedd0', hemato: '#190e91', adip: '#fff03c', imv: '#ff3aa3', other: '#c8c8c8', artifacts: '#000000' };

export { CONTOUR_KINDS };

export function isContour(item) {
  return CONTOUR_KINDS.includes(item.kind);
}

export function initCellularity(host) {
  // host: getActive, getPanes, canDraw, setTool, activeTool, reloadAnnotations,
  //       focusAnnotation, removeAnnotation, showNote
  const panel = $('cellPanel');
  let masksShown = localStorage.getItem('viewer.cellMasks') !== 'off';
  let contoursShown = localStorage.getItem('viewer.cellContours') !== 'off';
  let opacity = Number(localStorage.getItem('viewer.cellOpacity') ?? 0.6);
  if (!(opacity >= 0 && opacity <= 1)) opacity = 0.6;

  const toggle = (open = panel.hidden) => {
    panel.hidden = !open;
    $('btnCellularity').setAttribute('aria-expanded', String(open));
    $('btnCellularity').classList.toggle('is-active', open);
    if (open) {
      const pane = host.getActive();
      if (pane && !pane.cell?.status) load(pane);
      render();
    } else if (CONTOUR_KINDS.includes(host.activeTool())) {
      host.setTool(null);
    }
  };
  $('btnCellularity').addEventListener('click', () => toggle());
  $('cellClose').addEventListener('click', () => toggle(false));
  $('cellToolTissue').addEventListener('click', () => host.setTool(host.activeTool() === 'tissue' ? null : 'tissue'));
  $('cellToolArtifact').addEventListener('click', () => host.setTool(host.activeTool() === 'artifact' ? null : 'artifact'));
  $('cellPropose').addEventListener('click', propose);
  $('cellClearContours').addEventListener('click', clearContours);
  $('cellRun').addEventListener('click', () => start('marrowquant'));   // не start: иначе в способ уходит событие
  $('cellCancel').addEventListener('click', () => cancel('marrowquant'));
  $('cellDelete').addEventListener('click', () => remove('marrowquant'));
  $('cellAiRun').addEventListener('click', () => start('ai'));
  $('cellAiCancel').addEventListener('click', () => cancel('ai'));
  $('cellAiDelete').addEventListener('click', () => remove('ai'));
  // Контуры «Ткань» и «Артефакт» выключаются так же, как маски; выбор запоминается в браузере
  const setContoursShown = (shown) => {
    contoursShown = shown;
    localStorage.setItem('viewer.cellContours', shown ? 'on' : 'off');
    $('cellContours').checked = shown;
    for (const pane of host.getPanes()) pane.annotations?.setContoursVisible(shown);
  };
  $('cellContours').addEventListener('change', () => setContoursShown($('cellContours').checked));
  $('cellMasks').addEventListener('change', () => {
    masksShown = $('cellMasks').checked;
    localStorage.setItem('viewer.cellMasks', masksShown ? 'on' : 'off');
    for (const pane of host.getPanes()) applyMask(pane);
  });
  $('cellOpacity').addEventListener('input', () => {
    opacity = Number($('cellOpacity').value) / 100;
    localStorage.setItem('viewer.cellOpacity', String(opacity));
    for (const pane of host.getPanes()) applyMask(pane);
  });
  $('cellExport').addEventListener('click', () => { location.href = '/api/cellularity/export.csv'; });

  const legend = $('cellLegend');
  for (const part of PARTS) {
    const row = document.createElement('span');
    row.className = 'cell-legend-item';
    const swatch = document.createElement('i');
    swatch.style.background = COLORS[part];
    row.append(swatch, t(`cell.part.${part}`));
    legend.append(row);
  }

  // ---------- состояние ----------

  function available(pane) {
    return Boolean(pane && pane.slide.stain === 'HE' && pane.slide.mpp);
  }

  function updateButton() {
    const pane = host.getActive();
    const show = available(pane);
    $('btnCellularity').hidden = !show;
    if (!show && !panel.hidden) toggle(false);
  }

  async function load(pane) {
    if (!available(pane)) return;
    pane.cell ??= {};
    try {
      pane.cell.status = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity`);
    } catch (error) {
      pane.cell.status = null;
      if (pane === host.getActive() && !panel.hidden) host.showNote(error.message);
      return;
    }
    applyMask(pane);
    if (pane === host.getActive()) render();
    schedulePoll(pane);
  }

  function isActive(run) {
    return Boolean(run && ['queued', 'running'].includes(run.status));
  }

  function schedulePoll(pane) {
    clearTimeout(pane.cell.timer);
    const status = pane.cell.status;
    if (!isActive(status?.run) && !isActive(status?.ai_run)) return;
    pane.cell.timer = setTimeout(() => {
      if (host.getPanes().includes(pane)) load(pane);
    }, POLL_MS);
  }

  // ---------- маски (КЛ-6) ----------

  function applyMask(pane) {
    const run = pane.cell?.status?.run;
    const wanted = masksShown && run?.status === 'done' ? run : null;
    const current = pane.cell?.maskRunId ?? null;
    if (!wanted && current) {
      pane.cell.maskItem?.setOpacity(0);
      return;
    }
    if (!wanted) return;
    if (wanted.id === current) {
      pane.cell.maskItem?.setOpacity(opacity);
      return;
    }
    if (!pane.viewer.isOpen()) {
      pane.viewer.addOnceHandler('open', () => applyMask(pane));
      return;
    }
    if (pane.cell.maskItem) {
      pane.viewer.world.removeItem(pane.cell.maskItem);
      pane.cell.maskItem = null;
    }
    pane.cell.maskRunId = wanted.id;
    pane.viewer.addTiledImage({
      tileSource: {
        Image: {
          xmlns: 'http://schemas.microsoft.com/deepzoom/2008',
          Url: wanted.masks_url,
          Format: 'png',
          Overlap: String(pane.slide.tiles.overlap),
          TileSize: String(pane.slide.tiles.tile_size),
          Size: { Width: String(pane.slide.width), Height: String(pane.slide.height) },
        },
      },
      opacity,
      success: (event) => { pane.cell.maskItem = event.item; },
    });
  }

  // ---------- панель ----------

  function contours(pane) {
    return (pane?.annotationItems ?? []).filter(isContour);
  }

  function render() {
    if (panel.hidden) return;
    const pane = host.getActive();
    const status = pane?.cell?.status;
    const canEdit = host.canDraw() && Boolean(status?.can_run);
    panel.classList.toggle('can-edit', canEdit);
    $('cellNoRights').hidden = canEdit || !status || host.canDraw() === false && matchMedia('(max-width: 700px)').matches;
    $('cellToolTissue').classList.toggle('is-active', host.activeTool() === 'tissue');
    $('cellToolArtifact').classList.toggle('is-active', host.activeTool() === 'artifact');
    renderContours(pane, canEdit);
    $('cellContoursRow').hidden = !contours(pane).length;   // переключатель нужен, только когда есть что прятать
    $('cellContours').checked = contoursShown;
    renderRun(status, canEdit);
    renderAi(status, canEdit);
    $('cellExport').hidden = !(status && host.isAdmin());
  }

  // Оценка ИИ по полям зрения: второй способ рядом с алгоритмом
  function renderAi(status, canEdit) {
    const run = status?.ai_run ?? null;
    const show = Boolean(status) && (Boolean(run) || (status.ai_available && canEdit));
    $('cellAi').hidden = !show;
    $('cellAiBlock').hidden = !run || run.status !== 'done';
    if (!show) return;
    const active = isActive(run);
    const done = run?.status === 'done' && run.result;
    $('cellAiProgress').hidden = !active;
    $('cellAiRun').hidden = Boolean(active) || !status.ai_available;
    $('cellAiRun').textContent = t(done ? 'cell.ai.rerun' : 'cell.ai.run');
    $('cellAiRun').disabled = false;
    $('cellAiCancel').hidden = !active || !canEdit;
    $('cellAiStale').hidden = !(done && run.stale);
    $('cellAiDelete').hidden = !canEdit;
    const big = $('cellAiBig');
    const sub = $('cellAiSub');
    const state = $('cellAiState');
    state.className = 'cell-state';
    big.textContent = '—';
    sub.textContent = '';
    state.textContent = '';
    if (!run) {
      state.textContent = t('cell.ai.state.none');
    } else if (active) {
      big.textContent = '…';
      const p = run.progress;
      state.textContent = p?.of
        ? t('cell.ai.state.running', { i: p.fragment, of: p.of, stage: t(`cell.ai.stage.${p.stage}`) })
        : t('cell.state.starting');
      $('cellAiProgressBar').style.width = `${Math.round((p?.fraction ?? 0) * 100)}%`;
    } else if (run.status === 'failed') {
      state.classList.add('is-error');
      state.textContent = t('cell.state.failed', { error: run.error ?? '' });
    } else if (run.status === 'cancelled') {
      state.textContent = t('cell.state.cancelled');
    } else if (done) {
      const total = run.result.total?.cellularity_eq1_pct;
      big.textContent = total == null ? '—' : `${formatNumber(total, 0)} %`;
      const parts = run.result.fragments.map((f) => `T${f.fragment} ${f.cellularity_eq1_pct == null ? '—' : formatNumber(f.cellularity_eq1_pct, 0)} %`);
      sub.textContent = parts.length > 1 ? t('cell.byFragments', { list: parts.join(', ') }) : '';
      const total_ = run.result.total ?? {};
      const lo = total_.regional_min_pct;
      const hi = total_.regional_max_pct;
      if (total == null) {
        state.textContent = t('cell.ai.note');
      } else if (lo != null && hi != null && hi > lo) {
        state.textContent = t(total_.heterogeneous ? 'cell.ai.heterogeneous' : 'cell.ai.homogeneous',
          { min: formatNumber(lo, 0), max: formatNumber(hi, 0) });
      } else {
        state.textContent = t(total_.heterogeneous ? 'cell.ai.heterogeneous.plain' : 'cell.ai.homogeneous.plain');
      }
      renderAiText(run);
      renderAiRegions(run);
    }
    $('cellAiText').hidden = !done;
  }

  // Строка по фрагменту под числом, без раскрытия «Подробнее»: одно число при неоднородном
  // мозге вводит в заблуждение (заказчик, 2026-09-24); с 2026-09-25 — коротко: однородный
  // или нет, за счёт чего, одна фраза модели
  function renderAiText(run) {
    const box = $('cellAiText');
    const many = run.result.fragments.length > 1;
    box.replaceChildren(...run.result.fragments.map((fragment) => {
      const p = document.createElement('p');
      if (many) {
        const b = document.createElement('b');
        b.textContent = `T${fragment.fragment}: `;
        p.append(b);
      }
      let verdict;
      if (fragment.cellularity_eq1_pct == null) {
        verdict = t('cell.ai.frag.none');
      } else if (fragment.heterogeneous) {
        const causes = (fragment.causes ?? []).join(', ');
        verdict = causes ? t('cell.ai.frag.hetero', { causes }) : t('cell.ai.heterogeneous.plain');
      } else {
        verdict = t('cell.ai.homogeneous.plain');
      }
      const note = (fragment.description || '').trim();
      p.append(note ? `${verdict}. ${note}` : verdict);
      return p;
    }));
  }

  // Участки ×20, которые модель выбрала и рассмотрела: щелчок ведёт к участку на препарате
  function renderAiRegions(run) {
    const pane = host.getActive();
    const usage = run.result.usage;
    $('cellAiMeta').textContent = t('cell.ai.meta', {
      who: run.started_by, date: formatDateTime(run.finished_at ?? run.created_at), model: run.result.algorithm,
    }) + (usage?.requests ? t('cell.ai.usage', {
      n: usage.requests, k: formatNumber((usage.input_tokens + usage.output_tokens) / 1000, 1),
    }) : '');
    const list = $('cellAiFields');
    list.replaceChildren(...run.result.fragments.flatMap((fragment) => (fragment.regions ?? []).map((region) => {
      const row = document.createElement('li');
      row.className = 'annot-row cell-field';
      const text = document.createElement('span');
      text.className = 'annot-row-text';
      text.textContent = t('cell.ai.region', { fragment: fragment.fragment, n: region.n });
      const meta = document.createElement('span');
      meta.className = 'annot-row-meta';
      meta.textContent = region.reason || '';
      row.append(text, meta);
      row.addEventListener('click', () => {
        if (!pane) return;
        const { viewport } = pane.viewer;
        viewport.panTo(pane.image.imageToViewportCoordinates(region.x + region.size / 2, region.y + region.size / 2));
        viewport.applyConstraints();
      });
      return row;
    })));
  }

  function renderContours(pane, canEdit) {
    const list = $('cellContours');
    const items = contours(pane);
    const counts = { tissue: 0, artifact: 0 };
    list.replaceChildren(...items.map((item) => {
      const index = ++counts[item.kind];
      const row = document.createElement('li');
      row.className = `annot-row cell-contour is-${item.kind}`;
      const text = document.createElement('span');
      text.className = 'annot-row-text';
      text.textContent = `${t(`cell.contour.${item.kind}`)} ${index}`;
      const meta = document.createElement('span');
      meta.className = 'annot-row-meta';
      meta.textContent = item.comment || `${item.author}, ${formatDateTime(item.created_at)}`;
      row.append(text, meta);
      row.addEventListener('click', () => {
        if (!contoursShown) setContoursShown(true);   // перейти к спрятанному контуру нельзя, не показав его
        pane.annotations.select(item.id);
        host.focusAnnotation(item);
      });
      if (canEdit && item.can_edit) {
        const actions = document.createElement('span');
        actions.className = 'annot-row-actions';
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'link-btn is-danger';
        remove.textContent = t('common.delete');
        remove.addEventListener('click', (event) => {
          event.stopPropagation();
          host.removeAnnotation(item);
        });
        actions.append(remove);
        row.append(actions);
      }
      return row;
    }));
    $('cellContoursEmpty').hidden = items.length > 0;
    $('cellClearContours').hidden = !items.length;
  }

  function renderRun(status, canEdit) {
    const run = status?.run ?? null;
    const active = run && ['queued', 'running'].includes(run.status);
    const done = run?.status === 'done' && run.result;
    $('cellRunBlock').hidden = !status;
    $('cellProgress').hidden = !active;
    $('cellRun').hidden = Boolean(active);
    $('cellCancel').hidden = !active || !canEdit;
    $('cellRun').disabled = false;
    // после готового результата запуск — это пересчёт: новый расчёт заменяет прежний
    $('cellRun').textContent = t(done ? 'cell.rerun' : 'cell.run');
    $('cellMasksRow').hidden = !done;
    $('cellStale').hidden = !(done && run.stale);

    // Крупно — одно число: клеточность по формуле 1 итогом по стеклу; рядом по фрагментам
    const big = $('cellBig');
    const sub = $('cellSub');
    const state = $('cellState');
    state.className = 'cell-state';
    big.textContent = '—';
    sub.textContent = '';
    state.textContent = '';
    if (!run) {
      state.textContent = t(canEdit ? 'cell.state.none.run' : 'cell.state.none');
    } else if (run.status === 'queued') {
      big.textContent = '…';
      state.textContent = run.queue_ahead ? t('cell.state.queued', { n: run.queue_ahead }) : t('cell.state.starting');
    } else if (run.status === 'running') {
      big.textContent = '…';
      const p = run.progress;
      state.textContent = p
        ? t('cell.state.running', { i: p.fragment, of: p.of, stage: t(`cell.stage.${p.stage}`) })
        : t('cell.state.starting');
      $('cellProgressBar').style.width = `${Math.round((p?.fraction ?? 0) * 100)}%`;
    } else if (run.status === 'failed') {
      state.classList.add('is-error');
      state.textContent = t('cell.state.failed', { error: run.error ?? '' });
    } else if (run.status === 'cancelled') {
      state.textContent = t('cell.state.cancelled');
    } else if (done) {
      const total = run.result.total?.cellularity_eq1_pct;
      big.textContent = total == null ? '—' : `${formatNumber(total, 0)} %`;
      const parts = run.result.fragments.map((f) => `T${f.fragment} ${f.cellularity_eq1_pct == null ? '—' : formatNumber(f.cellularity_eq1_pct, 0)} %`);
      sub.textContent = parts.length > 1 ? t('cell.byFragments', { list: parts.join(', ') }) : '';
      state.textContent = t('cell.headline.note');
    }
    $('cellRunMeta').textContent = run && !active && run.status !== 'failed' ? t('cell.state.done', {
      who: run.started_by, date: formatDateTime(run.finished_at ?? run.created_at), res: t(`cell.resolution.${run.resolution}`),
      um: formatNumber(run.pixel_um ?? 0, 2), s: formatNumber(run.elapsed_s ?? 0, 0),
    }) : '';
    $('cellResult').hidden = !done;
    $('cellDelete').hidden = !canEdit;
    if (done) renderResult(run);
  }

  function renderResult(run) {
    const table = $('cellTable');
    const head = ['', ...run.result.fragments.map((f) => `T${f.fragment}`), t('cell.total')];
    const rows = [
      ['cellularity_eq1_pct', 'pct'], ['cellularity_eq2_pct', 'pct'],
      ['tissue_mm2', 'mm2'], ['marrow_mm2', 'mm2'], ['bone_mm2', 'mm2'], ['hemato_mm2', 'mm2'],
      ['adip_mm2', 'mm2'], ['imv_mm2', 'mm2'], ['other_mm2', 'mm2'], ['artifacts_mm2', 'mm2'],
      ['adiposity_pct', 'pct'], ['imv_pct', 'pct'], ['other_pct', 'pct'], ['adipocytes', 'n'],
    ];
    const cells = [...run.result.fragments, run.result.total];
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    for (const text of head) {
      const th = document.createElement('th');
      th.textContent = text;
      hr.append(th);
    }
    thead.append(hr);
    const tbody = document.createElement('tbody');
    for (const [key, unit] of rows) {
      const tr = document.createElement('tr');
      if (key.startsWith('cellularity')) tr.className = 'is-main';
      const th = document.createElement('th');
      th.textContent = t(`cell.row.${key}`);
      tr.append(th);
      for (const item of cells) {
        const td = document.createElement('td');
        const value = item[key];
        td.textContent = value == null ? '—'
          : unit === 'pct' ? `${formatNumber(value, 1)} %`
            : unit === 'mm2' ? formatNumber(value, 2)
              : formatNumber(value, 0);
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.replaceChildren(thead, tbody);
    const warnings = [...new Set(cells.flatMap((item) => item.warnings ?? []))];
    $('cellWarnings').replaceChildren(...warnings.map((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      return li;
    }));
    $('cellWarnings').hidden = !warnings.length;
    $('cellMasks').checked = masksShown;
    $('cellOpacity').value = String(Math.round(opacity * 100));
  }

  // ---------- действия ----------

  async function start(method = 'marrowquant') {
    const pane = host.getActive();
    if (!pane) return;
    // Одна кнопка: контуры, если их нет, сервер найдёт сам; разрешение задано в настройках
    const button = $(method === 'ai' ? 'cellAiRun' : 'cellRun');
    button.disabled = true;
    try {
      const run = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/runs`, {
        method: 'POST', body: { propose: true, method },
      });
      pane.cell.status[method === 'ai' ? 'ai_run' : 'run'] = run;
    } catch (error) {
      host.showNote(error.message);
      button.disabled = false;
      return;
    }
    render();
    schedulePoll(pane);
    host.reloadAnnotations(pane);  // контуры могли появиться по миниатюре
  }

  async function cancel(method = 'marrowquant') {
    const pane = host.getActive();
    const key = method === 'ai' ? 'ai_run' : 'run';
    const run = pane?.cell?.status?.[key];
    if (!run) return;
    try {
      pane.cell.status[key] = await api(
        `/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/runs/${encodeURIComponent(run.id)}/cancel`,
        { method: 'POST' },
      );
    } catch (error) {
      return host.showNote(error.message);
    }
    render();
    schedulePoll(pane);
  }

  async function remove(method = 'marrowquant') {
    const pane = host.getActive();
    const run = pane?.cell?.status?.[method === 'ai' ? 'ai_run' : 'run'];
    if (!run) return;
    const ok = await confirmDialog({
      title: t('cell.delete.title'), text: t('cell.delete.text'), submitLabel: t('common.delete'),
    });
    if (!ok) return;
    try {
      await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/runs/${encodeURIComponent(run.id)}`,
        { method: 'DELETE' });
    } catch (error) {
      return host.showNote(error.message);
    }
    load(pane);
  }

  async function clearContours() {
    const pane = host.getActive();
    const items = contours(pane);
    if (!pane || !items.length) return;
    const ok = await confirmDialog({
      title: t('cell.clear.title'), text: t('cell.clear.text', { n: items.length }), submitLabel: t('common.delete'),
    });
    if (!ok) return;
    let result;
    try {
      result = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/contours`, { method: 'DELETE' });
    } catch (error) {
      return host.showNote(error.message);
    }
    host.showNote(t(result.kept ? 'cell.clear.kept' : 'cell.clear.done', { n: result.deleted, kept: result.kept }),
      { error: false });
    await host.reloadAnnotations(pane);
    load(pane);
  }

  async function propose() {
    const pane = host.getActive();
    if (!pane) return;
    if (contours(pane).some((item) => item.kind === 'tissue')) {
      const ok = await confirmDialog({
        title: t('cell.propose'), text: t('cell.propose.exists'), submitLabel: t('cell.propose.add'),
      });
      if (!ok) return;
    }
    $('cellPropose').disabled = true;
    try {
      const created = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/propose`, { method: 'POST' });
      host.showNote(t('cell.propose.done', { n: created.length }), { error: false });
      if (created.length && !contoursShown) setContoursShown(true);   // найденное сразу видно
    } catch (error) {
      host.showNote(error.message);
    } finally {
      $('cellPropose').disabled = false;
    }
    await host.reloadAnnotations(pane);
    load(pane);
  }

  return {
    // половина создана: сведения о расчёте нужны сразу — маски видят все (КЛ-9)
    onPaneAdded(pane) {
      pane.cell = { status: null, maskItem: null, maskRunId: null, timer: null };
      pane.annotations?.setContoursVisible(contoursShown);
      load(pane);
    },
    onPaneRemoved(pane) {
      clearTimeout(pane.cell?.timer);
    },
    onActiveChanged() {
      updateButton();
      render();
    },
    // контуры изменились: счётчики, пометка «устарел» и кнопка запуска
    onAnnotationsChanged(pane) {
      if (pane === host.getActive()) render();
      if (pane.cell?.status?.run?.status === 'done') load(pane);
    },
    onToolChanged() {
      $('cellToolTissue').classList.toggle('is-active', host.activeTool() === 'tissue');
      $('cellToolArtifact').classList.toggle('is-active', host.activeTool() === 'artifact');
    },
    isOpen: () => !panel.hidden,
    open: () => toggle(true),
  };
}
