(() => {
  const $ = (s) => document.querySelector(s);
  const views = { idle: $('#vIdle'), work: $('#vWork'), error: $('#vError'), done: $('#vDone') };
  const COLORS = ['#ffb347', '#7ff3e3', '#ff6a2a', '#e9d98b', '#c9a6ff', '#9fe08a', '#ff8fb1', '#8fc4ff'];
  const STAGES = [
    { id: 'upload', t: 'Передача файла' },
    { id: 'convert', t: 'Конвертация · ffmpeg' },
    { id: 'load', t: 'Загрузка моделей', indet: true },
    { id: 'transcribe', t: 'Распознавание речи' },
    { id: 'align', t: 'Выравнивание по словам' },
    { id: 'diarize', t: 'Определение спикеров' },
  ];

  const opts = { language: 'ru', speakers: 0, diarize: true };
  let job = null;          // текущая задача на сервере
  let pollTimer = null;
  let stageStart = {};     // id этапа -> время начала (для ETA)
  let doc = null;          // { title, blocks, speakers: {id: {name, color, n}}, order, duration, language }
  let dirty = false;       // результат ещё не сохранён

  const native = () => window.pywebview && window.pywebview.api;

  // ---------- общее ----------
  const show = (name) => {
    for (const [k, el] of Object.entries(views)) el.hidden = k !== name;
    const st = $('#hudState');
    st.textContent = { idle: 'ГОТОВ', work: 'ОБРАБОТКА', error: 'СБОЙ', done: 'ТЕКСТ ГОТОВ' }[name];
    st.classList.toggle('busy', name === 'work');
  };
  const pad = (n) => String(n).padStart(2, '0');
  const fmtTime = (sec) => {
    if (sec == null || isNaN(sec)) return '';
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  };
  const plural = (n, one, few, many) => {
    const a = n % 10, b = n % 100;
    return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many;
  };
  const stem = (name) => name.replace(/\.[^.]+$/, '');

  let toastTimer;
  function toast(msg, { bad = false, action = null } = {}) {
    const el = $('#toast');
    el.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = msg;
    el.append(span);
    if (action) {
      const b = document.createElement('button');
      b.textContent = action.label;
      b.onclick = action.fn;
      el.append(b);
    }
    el.classList.toggle('bad', bad);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), action ? 7000 : 3500);
  }

  setInterval(() => {
    const d = new Date();
    $('#clock').textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }, 1000);

  fetch('/api/info').then((r) => r.json()).then((i) => {
    $('#eyebrow').textContent = `Local / Whisper ${i.model} / ${i.device.toUpperCase()} · ${i.compute}`;
    setSeg('#optLang', i.language);
    opts.language = i.language;
    if (!i.ffmpeg) toast('ffmpeg не найден в системе: установите его через brew install ffmpeg', { bad: true });
    if (!i.has_token) {
      $('#idleNote').textContent = 'Токен Hugging Face не найден, поэтому спикеры не определяются. Выполните hf auth login или заполните app/config.json.';
      setSeg('#optSpk', 'off');
      opts.diarize = false; opts.speakers = 0;
      for (const b of document.querySelectorAll('#optSpk button')) if (b.dataset.v !== 'off') b.disabled = true;
    }
  }).catch(() => {});

  // ---------- параметры ----------
  function setSeg(sel, v) {
    for (const b of document.querySelectorAll(`${sel} button`)) b.setAttribute('aria-pressed', String(b.dataset.v === String(v)));
  }
  $('#optLang').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    opts.language = b.dataset.v; setSeg('#optLang', b.dataset.v);
  });
  $('#optSpk').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    setSeg('#optSpk', b.dataset.v);
    opts.diarize = b.dataset.v !== 'off';
    opts.speakers = opts.diarize ? Number(b.dataset.v) : 0;
  });

  // ---------- выбор файла ----------
  $('#drop').addEventListener('click', async () => {
    if (native()) {
      const path = await window.pywebview.api.pick_file();
      if (path) startByPath(path);
    } else {
      $('#fileInput').click();
    }
  });
  $('#fileInput').addEventListener('change', (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (f) startUpload(f);
  });

  let dragDepth = 0;
  const canAccept = () => views.work.hidden; // во время обработки новые файлы не принимаем
  window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    if (!canAccept() || ![...(e.dataTransfer?.types || [])].includes('Files')) return;
    dragDepth++;
    $('#dropveil').hidden = false;
    document.body.classList.add('dragging');
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) { $('#dropveil').hidden = true; document.body.classList.remove('dragging'); }
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    $('#dropveil').hidden = true;
    document.body.classList.remove('dragging');
    if (!canAccept()) return;
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    if (!views.done.hidden && dirty && !confirmDiscard()) return;
    startUpload(f);
  });

  // ---------- запуск ----------
  function prepareWork(filename, withUpload) {
    stageStart = {};
    $('#wName').textContent = filename;
    $('#wMeta').textContent = '';
    $('#wElapsed').textContent = '00:00';
    const list = $('#stages');
    list.innerHTML = '';
    STAGES.filter((s) => (s.id !== 'upload' || withUpload) && (s.id !== 'diarize' || opts.diarize))
      .forEach((s, i) => {
        const li = document.createElement('li');
        li.className = 'stage' + (s.indet ? ' indet' : '');
        li.dataset.id = s.id;
        li.innerHTML = `<span class="n">${pad(i + 1)}</span><span class="t">${s.t}</span><span class="p">—</span>
          <span class="track"><span class="fill"></span></span><span class="eta"></span>`;
        list.append(li);
      });
    show('work');
  }

  function startUpload(file) {
    prepareWork(file.name, true);
    const fd = new FormData();
    fd.append('file', file);
    fd.append('language', opts.language);
    fd.append('speakers', String(opts.speakers));
    fd.append('diarize', String(opts.diarize));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/jobs');
    stageStart.upload = Date.now();
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) renderStages({ stage: 'upload', progress: { upload: (e.loaded / e.total) * 100 } });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) begin(JSON.parse(xhr.responseText));
      else fail(errText(xhr.responseText) || `Ошибка сервера ${xhr.status}`);
    };
    xhr.onerror = () => fail('Не удалось передать файл');
    window._xhr = xhr;
    xhr.send(fd);
  }

  async function startByPath(path) {
    prepareWork(path.split('/').pop(), false);
    try {
      const r = await fetch('/api/jobs/by-path', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, language: opts.language, speakers: opts.speakers, diarize: opts.diarize }),
      });
      if (!r.ok) return fail(errText(await r.text()) || `Ошибка сервера ${r.status}`);
      begin(await r.json());
    } catch (e) { fail(String(e)); }
  }

  const errText = (t) => { try { return JSON.parse(t).detail; } catch { return t; } };

  function begin(j) {
    job = j;
    window._xhr = null;
    poll();
  }

  async function poll() {
    clearTimeout(pollTimer);
    if (!job) return;
    try {
      const r = await fetch(`/api/jobs/${job.id}`);
      const j = await r.json();
      if (!job || j.id !== job.id) return;
      job = j;
      renderStages(j);
      if (j.state === 'done') return finish(j);
      if (j.state === 'error') return fail(j.error);
      if (j.state === 'cancelled') return;
    } catch { /* сервер занят: попробуем ещё */ }
    pollTimer = setTimeout(poll, 600);
  }

  function renderStages(j) {
    const items = [...document.querySelectorAll('.stage')];
    const order = items.map((li) => li.dataset.id);
    const cur = order.indexOf(j.stage);
    if (j.stage && !stageStart[j.stage]) stageStart[j.stage] = Date.now();
    items.forEach((li, i) => {
      const id = li.dataset.id;
      let p = id === 'upload' ? (j.stage === 'upload' ? j.progress.upload : 100) : (j.progress?.[id] ?? 0);
      if (id === 'load') p = i < cur ? 100 : 0;
      const done = i < cur || p >= 100;
      const active = i === cur && !done;
      li.classList.toggle('done', done);
      li.classList.toggle('active', active);
      li.querySelector('.fill').style.width = `${done ? 100 : p}%`;
      li.querySelector('.p').textContent = done ? 'OK' : active && !li.classList.contains('indet') ? `${Math.floor(p)}%` : active ? '…' : '—';
      let eta = '';
      if (active && p > 3 && p < 100 && stageStart[id]) {
        const el = (Date.now() - stageStart[id]) / 1000;
        eta = `осталось на этапе ≈ ${fmtTime((el * (100 - p)) / p)}`;
      }
      li.querySelector('.eta').textContent = eta;
    });
    if (j.elapsed != null) $('#wElapsed').textContent = fmtTime(j.elapsed);
    const meta = [];
    meta.push(`язык: <b>${(j.opts?.language || opts.language).toUpperCase()}</b>`);
    meta.push(`спикеры: <b>${!(j.opts?.diarize ?? opts.diarize) ? 'не делить' : (j.opts?.speakers || 'авто')}</b>`);
    $('#wMeta').innerHTML = meta.join(' · ');
  }

  function fail(msg) {
    clearTimeout(pollTimer);
    job = null;
    $('#errMsg').textContent = msg || 'Неизвестная ошибка';
    show('error');
  }

  $('#cancel').addEventListener('click', async () => {
    clearTimeout(pollTimer);
    if (window._xhr) { window._xhr.abort(); window._xhr = null; }
    if (job) fetch(`/api/jobs/${job.id}`, { method: 'DELETE' }).catch(() => {});
    job = null;
    show('idle');
    toast('Расшифровка остановлена');
  });
  $('#retry').addEventListener('click', () => show('idle'));

  // ---------- результат ----------
  function finish(j) {
    const blocks = j.result.blocks || [];
    const speakers = {};
    const order = [];
    for (const b of blocks) {
      if (!speakers[b.speaker]) {
        const idx = order.length;
        order.push(b.speaker);
        speakers[b.speaker] = {
          name: b.speaker === 'UNKNOWN' ? 'Неизвестный' : `Спикер ${idx + 1}`,
          color: COLORS[idx % COLORS.length], n: 0, sec: 0,
        };
      }
      const s = speakers[b.speaker];
      s.n++;
      s.sec += Math.max(0, (b.end ?? 0) - (b.start ?? 0));
    }
    doc = { title: stem(j.filename), blocks, speakers, order, duration: j.result.duration, language: j.result.language, elapsed: j.elapsed };
    job = null;
    dirty = true;
    renderDoc();
    show('done');
    window.scrollTo(0, 0);
    if (j.warnings?.length) toast(j.warnings[0]);
  }

  function renderDoc() {
    $('#title').value = doc.title;
    const nSp = doc.order.length;
    $('#dMeta').innerHTML = [
      doc.duration ? `<b>${fmtTime(doc.duration)}</b> записи` : '',
      `<b>${nSp}</b> ${plural(nSp, 'спикер', 'спикера', 'спикеров')}`,
      `<b>${doc.blocks.length}</b> ${plural(doc.blocks.length, 'реплика', 'реплики', 'реплик')}`,
      doc.language ? `<b>${doc.language.toUpperCase()}</b>` : '',
      doc.elapsed ? `расшифровано за <b>${fmtTime(doc.elapsed)}</b>` : '',
    ].filter(Boolean).join(' · ');

    // Спикеры
    const box = $('#speakers');
    box.innerHTML = '';
    const total = doc.order.reduce((a, id) => a + doc.speakers[id].sec, 0) || 1;
    if (nSp > 1) {
      for (const id of doc.order) {
        const s = doc.speakers[id];
        const chip = document.createElement('label');
        chip.className = 'chip';
        chip.style.setProperty('--c', s.color);
        chip.title = `${id}: переименовать`;
        chip.innerHTML = `<span class="dot"></span><input spellcheck="false"><small>${Math.round((s.sec / total) * 100)}%</small>`;
        const inp = chip.querySelector('input');
        inp.value = s.name;
        inp.size = Math.max(6, s.name.length + 1);
        inp.addEventListener('input', () => {
          s.name = inp.value || id;
          inp.size = Math.max(6, inp.value.length + 1);
          for (const el of document.querySelectorAll(`.blk[data-spk="${CSS.escape(id)}"] .name`)) el.textContent = s.name;
          dirty = true;
        });
        box.append(chip);
      }
    }

    // Текст
    const art = $('#doc');
    art.innerHTML = '';
    art.classList.toggle('solo', nSp <= 1);
    art.classList.toggle('no-ts', !$('#ts').checked);
    if (!doc.blocks.length) {
      art.innerHTML = '<div class="empty">В записи не удалось распознать речь.</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    doc.blocks.forEach((b, i) => {
      const s = doc.speakers[b.speaker];
      const el = document.createElement('section');
      el.className = 'blk';
      el.dataset.spk = b.speaker;
      el.style.setProperty('--c', s.color);
      const who = document.createElement('div');
      who.className = 'who';
      who.innerHTML = `<span class="name"></span><span class="time">${fmtTime(b.start)}</span>`;
      who.querySelector('.name').textContent = s.name;
      const p = document.createElement('p');
      p.className = 'txt';
      p.contentEditable = 'plaintext-only';
      p.spellcheck = false;
      p.textContent = b.text;
      p.addEventListener('input', () => { doc.blocks[i].text = p.innerText; dirty = true; });
      el.append(who, p);
      frag.append(el);
    });
    art.append(frag);
  }

  $('#title').addEventListener('input', (e) => { doc.title = e.target.value; dirty = true; });
  $('#ts').addEventListener('change', (e) => $('#doc').classList.toggle('no-ts', !e.target.checked));

  function payload() {
    const names = {};
    for (const id of doc.order) names[id] = doc.speakers[id].name;
    return {
      title: (doc.title || 'Расшифровка').trim().replace(/[\/:]/g, '-'),
      timestamps: $('#ts').checked,
      speakers: names,
      blocks: doc.blocks.map((b) => ({ speaker: b.speaker, start: b.start, text: b.text })),
    };
  }

  document.querySelector('.actions .seg').addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b || !doc) return;
    const fmt = b.dataset.fmt;
    const btns = [...document.querySelectorAll('.actions .seg button')];
    btns.forEach((x) => (x.disabled = true));
    try {
      if (native()) {
        const path = await window.pywebview.api.save(fmt, payload());
        if (path) {
          dirty = false;
          toast(`Сохранено: ${path.split('/').pop()}`, { action: { label: 'Показать в Finder', fn: () => window.pywebview.api.reveal(path) } });
        }
      } else {
        const r = await fetch('/api/export', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ format: fmt, ...payload() }),
        });
        if (!r.ok) throw new Error(errText(await r.text()));
        const url = URL.createObjectURL(await r.blob());
        const a = Object.assign(document.createElement('a'), { href: url, download: `${payload().title}.${fmt}` });
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        dirty = false;
        toast(`Файл ${fmt.toUpperCase()} сохранён в «Загрузки»`);
      }
    } catch (err) {
      toast(`Не удалось сохранить: ${err.message || err}`, { bad: true });
    } finally {
      btns.forEach((x) => (x.disabled = false));
    }
  });

  function plainText() {
    const p = payload();
    const solo = doc.order.length <= 1;
    return doc.blocks.map((b) => {
      const head = [solo ? '' : p.speakers[b.speaker], p.timestamps ? fmtTime(b.start) : ''].filter(Boolean).join(' · ');
      return (head ? `[${head}]\n` : '') + b.text.trim();
    }).join('\n\n');
  }

  $('#copy').addEventListener('click', async () => {
    const text = plainText();
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
    }
    toast('Текст скопирован');
  });

  // «Новая запись»: если не сохраняли, просим нажать второй раз
  let armTimer;
  function confirmDiscard() {
    toast('Расшифровка не сохранена. Сохраните её или нажмите «Новая запись» ещё раз', { bad: true });
    return false;
  }
  $('#again').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    if (dirty && !btn.classList.contains('armed')) {
      btn.classList.add('armed');
      btn.textContent = 'Точно? Ещё раз';
      clearTimeout(armTimer);
      armTimer = setTimeout(() => { btn.classList.remove('armed'); btn.textContent = 'Новая запись'; }, 3500);
      return;
    }
    btn.classList.remove('armed');
    btn.textContent = 'Новая запись';
    doc = null; dirty = false;
    show('idle');
  });

  show('idle');
})();
