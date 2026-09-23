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
  $('cellRun').addEventListener('click', start);
  $('cellCancel').addEventListener('click', cancel);
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

  function schedulePoll(pane) {
    clearTimeout(pane.cell.timer);
    const run = pane.cell.status?.run;
    if (!run || !['queued', 'running'].includes(run.status)) return;
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
    renderRun(status, canEdit);
    $('cellExport').hidden = !(status && host.isAdmin());
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
    $('cellRun').disabled = !counts.tissue;
  }

  function renderRun(status, canEdit) {
    const run = status?.run ?? null;
    const active = run && ['queued', 'running'].includes(run.status);
    $('cellRunBlock').hidden = !status;
    $('cellProgress').hidden = !active;
    $('cellRun').hidden = Boolean(active);
    $('cellCancel').hidden = !active || !canEdit;
    $('cellResolution').disabled = Boolean(active);
    const state = $('cellState');
    state.className = 'cell-state';
    if (!run) {
      state.textContent = t('cell.state.none');
    } else if (run.status === 'queued') {
      state.textContent = run.queue_ahead
        ? t('cell.state.queued', { n: run.queue_ahead })
        : t('cell.state.starting');
    } else if (run.status === 'running') {
      const p = run.progress;
      state.textContent = p
        ? t('cell.state.running', { i: p.fragment, of: p.of, stage: t(`cell.stage.${p.stage}`) })
        : t('cell.state.starting');
    } else if (run.status === 'failed') {
      state.classList.add('is-error');
      state.textContent = t('cell.state.failed', { error: run.error ?? '' });
    } else if (run.status === 'cancelled') {
      state.textContent = t('cell.state.cancelled');
    } else {
      state.textContent = t('cell.state.done', {
        who: run.started_by, date: formatDateTime(run.finished_at), res: t(`cell.resolution.${run.resolution}`),
        um: formatNumber(run.pixel_um, 2), s: formatNumber(run.elapsed_s ?? 0, 0),
      });
    }
    if (active && run.progress) {
      $('cellProgressBar').style.width = `${Math.round(run.progress.fraction * 100)}%`;
    } else if (active) {
      $('cellProgressBar').style.width = '0%';
    }
    const done = run?.status === 'done' && run.result;
    $('cellResult').hidden = !done;
    if (done) renderResult(run);
  }

  function renderResult(run) {
    $('cellStale').hidden = !run.stale;
    const table = $('cellTable');
    const head = ['', ...run.result.fragments.map((f) => String(f.fragment)), t('cell.total')];
    const rows = [
      ['tissue_mm2', 'mm2'], ['marrow_mm2', 'mm2'], ['bone_mm2', 'mm2'], ['hemato_mm2', 'mm2'],
      ['adip_mm2', 'mm2'], ['imv_mm2', 'mm2'], ['other_mm2', 'mm2'], ['artifacts_mm2', 'mm2'],
      ['cellularity_eq1_pct', 'pct'], ['cellularity_eq2_pct', 'pct'], ['adiposity_pct', 'pct'],
      ['imv_pct', 'pct'], ['other_pct', 'pct'], ['adipocytes', 'n'],
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

  async function start() {
    const pane = host.getActive();
    if (!pane) return;
    const resolution = $('cellResolution').value;
    $('cellRun').disabled = true;
    try {
      pane.cell.status.run = await api(`/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/runs`, {
        method: 'POST', body: { resolution },
      });
    } catch (error) {
      host.showNote(error.message);
      $('cellRun').disabled = false;
      return;
    }
    render();
    schedulePoll(pane);
  }

  async function cancel() {
    const pane = host.getActive();
    const run = pane?.cell?.status?.run;
    if (!run) return;
    try {
      pane.cell.status.run = await api(
        `/api/slides/${encodeURIComponent(pane.slide.id)}/cellularity/runs/${encodeURIComponent(run.id)}/cancel`,
        { method: 'POST' },
      );
    } catch (error) {
      return host.showNote(error.message);
    }
    render();
    schedulePoll(pane);
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
