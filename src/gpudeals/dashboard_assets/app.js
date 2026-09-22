(() => {
  const $ = (s, r=document) => r.querySelector(s);
  const $$ = (s, r=document) => [...r.querySelectorAll(s)];

  // theme
  const btn = $('#theme');
  const upd = () => {
    const d = document.documentElement.dataset.theme === 'dark';
    if (!btn) return;
    btn.textContent = d ? '☀ Светлая' : '☾ Тёмная';
    btn.setAttribute('aria-label', d ? 'Включить светлую тему' : 'Включить тёмную тему');
  };
  if (btn) {
    btn.hidden = false; upd();
    btn.addEventListener('click', () => {
      const n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = n;
      try { localStorage.setItem('gpu-deals-theme', n); } catch {}
      upd();
    });
  }

  // image fallback
  $$('.visual img').forEach(img => {
    const fb = () => { img.hidden = true; img.parentElement.classList.remove('has-image'); };
    img.addEventListener('error', fb);
    if (img.complete && !img.naturalWidth) fb();
  });

  // empty tables helper
  $$('.table-wrap tbody, .table-scroll tbody').forEach(body => {
    if (!body.children.length) {
      const r = body.insertRow(); const c = r.insertCell();
      c.colSpan = body.closest('table').querySelectorAll('th').length;
      c.className = 'empty-table'; c.textContent = 'Подходящих данных в этом срезе пока нет.';
    }
  });

  const form = $('#filters');
  const grid = $('#products');
  if (!form || !grid) return;
  const cards = [...grid.children];
  const dataEl = $('#gpu-data');
  let items = [];
  try { items = JSON.parse(dataEl?.textContent || '{"items":[]}').items || []; } catch {}

  // favorites (localStorage)
  const FAV_KEY = 'gpu-deals-fav';
  const loadFav = () => { try { return new Set(JSON.parse(localStorage.getItem(FAV_KEY) || '[]')); } catch { return new Set(); } };
  let fav = loadFav();
  const saveFav = () => { try { localStorage.setItem(FAV_KEY, JSON.stringify([...fav])); } catch {} };
  const syncFavUI = () => {
    $$('[data-fav]').forEach(b => {
      const on = fav.has(b.dataset.fav);
      b.classList.toggle('is-on', on);
      b.textContent = on ? '★' : '☆';
      b.closest('.product-card')?.classList.toggle('is-fav', on);
      b.setAttribute('aria-pressed', String(on));
    });
  };
  grid.addEventListener('click', e => {
    const b = e.target.closest('[data-fav]');
    if (!b) return;
    const id = b.dataset.fav;
    if (fav.has(id)) fav.delete(id); else fav.add(id);
    saveFav(); syncFavUI(); filter();
    toast(fav.has(id) ? 'Добавлено в избранное' : 'Убрано из избранного');
  });
  syncFavUI();

  // compare
  const MAX_CMP = 4;
  let cmp = new Set();
  const bar = $('#compare-bar');
  const cntEl = $('#compare-count');
  const dlg = $('#compare-dialog');
  const cmpTable = $('#compare-table');
  const syncCmp = () => {
    const n = cmp.size;
    if (bar) bar.hidden = n === 0;
    if (cntEl) cntEl.textContent = `${n} выбрано`;
    $$('[data-compare]').forEach(cb => { cb.checked = cmp.has(cb.dataset.compare); });
  };
  const buildCompare = () => {
    if (!cmpTable) return;
    const picked = items.filter(it => cmp.has(it.identity));
    if (!picked.length) { cmpTable.innerHTML = '<p class="empty">Выберите до 4 карт галочкой «Сравнить» на карточке.</p>'; return; }
    const rows = [
      ['Магазин', ...picked.map(p => p.shop)],
      ['Цена', ...picked.map(p => `${p.price.toLocaleString('ru-RU')} ₸`)],
      ['Чип', ...picked.map(p => p.chip || '—')],
      ['Память', ...picked.map(p => p.memory_gb ? `${p.memory_gb} ГБ` : '—')],
      ['Наличие', ...picked.map(p => p.in_stock ? 'В наличии' : 'Нет')],
      ['Свежесть', ...picked.map(p => p.fresh ? 'За 24ч' : 'Старше')],
      ['Цель', ...picked.map(p => p.target ? `${p.target.toLocaleString('ru-RU')} ₸${p.reached ? ' ✓' : ''}` : '—')],
      ['₸/балл', ...picked.map(p => p.pp != null ? String(p.pp) : '—')],
      ['Ссылка', ...picked.map(p => p.url ? `<a href="${p.url}" target="_blank" rel="noopener">Открыть ↗</a>` : '—')],
    ];
    const head = `<tr><th>Параметр</th>${picked.map(p => `<th>${esc(p.title.slice(0,44))}</th>`).join('')}</tr>`;
    const body = rows.map(([label, ...vals]) => `<tr><th>${label}</th>${vals.map(v => `<td>${v}</td>`).join('')}</tr>`).join('');
    cmpTable.innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  };
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
  grid.addEventListener('change', e => {
    const cb = e.target.closest('[data-compare]');
    if (!cb) return;
    const id = cb.dataset.compare;
    if (cb.checked) {
      if (cmp.size >= MAX_CMP) { cb.checked = false; toast(`Можно сравнить до ${MAX_CMP} карт`); return; }
      cmp.add(id);
    } else cmp.delete(id);
    syncCmp();
  });
  $('#compare-open')?.addEventListener('click', () => { buildCompare(); dlg?.showModal(); });
  $('#compare-clear')?.addEventListener('click', () => { cmp.clear(); syncCmp(); });
  $('#clear-compare')?.addEventListener('click', () => { cmp.clear(); syncCmp(); toast('Сравнение сброшено'); });
  syncCmp();

  // filters + share + export
  const fields = ['search','kind','shop','chip','budget','sort','stock','fresh','target','favonly'];
  const params = new URLSearchParams(location.search);
  const hasParams = fields.some(id => params.has(id));
  if (!hasParams) { params.set('stock', '1'); params.set('fresh', '1'); }
  for (const id of fields) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = params.get(id) === '1';
    else if (params.has(id)) el.value = params.get(id);
  }
  const sortEl = $('#sort');
  if (sortEl && !sortEl.value) sortEl.value = 'price';

  const toastEl = $('#toast');
  let toastT;
  const toast = msg => {
    if (!toastEl) return;
    toastEl.textContent = msg; toastEl.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => toastEl.hidden = true, 2200);
  };

  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  const PAGE = 24;
  let limit = PAGE;
  $('#more')?.addEventListener('click', () => { limit += PAGE; filter(); });

  function filter() {
    const v = id => ($('#'+id)?.value ?? '');
    const q = v('search').trim().toLocaleLowerCase('ru');
    let n = 0;
    for (const c of cards) {
      const d = c.dataset;
      const favOk = !$('#favonly')?.checked || fav.has(d.identity);
      const ok = (!q || c.textContent.toLocaleLowerCase('ru').includes(q))
        && ['kind','shop','chip'].every(id => !v(id) || d[id] === v(id))
        && (!v('budget') || Number(d.price) <= Number(v('budget')))
        && ['stock','fresh','target'].every(id => {
          const el = document.getElementById(id);
          return !el || !el.checked || d[id] === '1';
        })
        && favOk;
      c.hidden = !ok; if (ok) n++;
    }
    const sort = v('sort');
    const sorted = [...cards].sort((a,b) => sort === 'title'
      ? a.querySelector('h3').textContent.localeCompare(b.querySelector('h3').textContent,'ru')
      : Number(a.dataset[sort] || 0) - Number(b.dataset[sort] || 0));
    sorted.forEach(c => grid.append(c));
    // Постранично: первые `limit` подходящих, остальное — по кнопке.
    let shown = 0;
    for (const c of sorted) {
      if (c.hidden) continue;
      shown++;
      if (shown > limit) c.hidden = true;
    }
    const moreRow = $('#more-row');
    if (moreRow) {
      moreRow.hidden = n <= limit;
      const more = $('#more');
      if (more) more.textContent = `Показать ещё (${Math.min(PAGE, n - limit)} из ${n - limit})`;
    }
    const counter = $('#count'); if (counter) counter.textContent = `${n} из ${cards.length} предложений`;
    const live = $('#live-count'); if (live) live.textContent = `${n} из ${cards.length} предложений`;
    const empty = $('#empty'); if (empty) empty.hidden = n !== 0;
    const url = new URL(location.href);
    for (const id of fields) {
      const el = document.getElementById(id);
      if (!el) continue;
      const val = el.type === 'checkbox' ? (el.checked ? '1' : '') : el.value;
      const isDefault = (id === 'sort' && val === 'price');
      if (val && !isDefault) url.searchParams.set(id, val);
      else url.searchParams.delete(id);
    }
    history.replaceState(null,'',url);
  }

  const filterDebounced = debounce(filter, 120);
  form.hidden = false;
  form.addEventListener('submit', e => e.preventDefault());
  const search = $('#search');
  if (search) search.addEventListener('input', filterDebounced);
  form.addEventListener('input', e => { if (e.target.id !== 'search') { limit = PAGE; filter(); } });
  form.addEventListener('change', () => { limit = PAGE; filter(); });
  form.addEventListener('reset', () => queueMicrotask(() => { fav = loadFav(); syncFavUI(); syncCmp(); filter(); }));

  // share
  $('#share')?.addEventListener('click', async () => {
    const url = location.href;
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(url);
      else { const ta=document.createElement('textarea'); ta.value=url; document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
      toast('Ссылка скопирована');
    } catch { toast(url); }
  });

  // export CSV (visible only)
  $('#export')?.addEventListener('click', () => {
    const visible = cards.filter(c => !c.hidden);
    const header = ['title','shop','chip','memory_gb','price','kind','in_stock','fresh','url'];
    const lines = [header.join(',')];
    for (const c of visible) {
      const it = items.find(x => x.identity === c.dataset.identity);
      if (!it) continue;
      const row = [it.title, it.shop, it.chip||'', it.memory_gb||'', it.price, it.kind, it.in_stock?1:0, it.fresh?1:0, it.url||''];
      lines.push(row.map(v => `"${String(v).replace(/"/g,'""')}"`).join(','));
    }
    const blob = new Blob([lines.join('\n')], {type:'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `gpu-deals-${new Date().toISOString().slice(0,10)}.csv`;
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
    toast(`Экспорт: ${visible.length} строк`);
  });

  document.addEventListener('keydown', e => {
    if (e.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); search?.focus(); }
    if (e.key === 'Escape' && document.activeElement === search) search.blur();
    if (e.key === 'Escape' && dlg?.open) dlg.close();
  });

  filter();
})();
