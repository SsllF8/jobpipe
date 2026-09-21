/* ============================================================
   投递作战台 · 存储层
   ------------------------------------------------------------
   核心设计：**种子数据与用户改动分离**

     seed   —— 构建期内联的岗位池，由采集脚本每天重写，是只读的
     state  —— 用户操作产生的「补丁」，存在浏览器 localStorage，是唯一的写入目标

   渲染时 merge(seed, state)。这样重新采集岗位不会冲掉你记的投递进度，
   反过来你记的进度也能导出成 state.json 丢回项目里跑 Git 版本管理。

   本文件不碰 DOM，纯数据变换，方便被测试直接调用。
   ============================================================ */

(function (root) {
  'use strict';

  /* ---------------------------------------------------------- 常量 */

  const SCHEMA = 1;
  const STORAGE_KEY = 'jobpipe.state.v1';
  const BACKUP_KEY = 'jobpipe.state.v1.bak';

  /* 状态机：5 个进行态 + 3 个结束态 */
  const STATUS = {
    wishlist:  { label: '待投递', closed: false, order: 0 },
    applied:   { label: '已投递', closed: false, order: 1 },
    screening: { label: '沟通中', closed: false, order: 2 },
    interview: { label: '面试中', closed: false, order: 3 },
    offer:     { label: 'Offer',  closed: false, order: 4 },
    rejected:  { label: '被拒',   closed: true,  order: 5 },
    dropped:   { label: '已放弃', closed: true,  order: 6 },
    ghosted:   { label: '无回应', closed: true,  order: 7 }
  };

  const STATUS_KEYS = Object.keys(STATUS);
  const CLOSED_KEYS = STATUS_KEYS.filter((k) => STATUS[k].closed);
  const OPEN_KEYS = STATUS_KEYS.filter((k) => !STATUS[k].closed);

  /* ---------------------------------------------------------- 时间 */

  /* 本地时区，格式 YYYY-MM-DDTHH:MM —— 人可读、可排序、可比较 */
  function stamp(d) {
    const t = d || new Date();
    const p = (n) => String(n).padStart(2, '0');
    return (
      `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}` +
      `T${p(t.getHours())}:${p(t.getMinutes())}`
    );
  }

  function today(d) {
    return stamp(d).slice(0, 10);
  }

  /* ---------------------------------------------------------- 空状态 */

  function emptyState() {
    return { schema: SCHEMA, updated_at: null, patches: {}, added: [], removed: [] };
  }

  /* 把任意来源的对象规范成合法 state，坏字段一律丢弃而不是让页面崩掉 */
  function normalize(raw) {
    const s = emptyState();
    if (!raw || typeof raw !== 'object') return s;

    if (raw.patches && typeof raw.patches === 'object') {
      for (const [id, p] of Object.entries(raw.patches)) {
        if (!id || !p || typeof p !== 'object') continue;
        s.patches[id] = p;
      }
    }
    if (Array.isArray(raw.added)) {
      s.added = raw.added.filter((j) => j && j.id && j.company);
    }
    if (Array.isArray(raw.removed)) {
      s.removed = [...new Set(raw.removed.filter((x) => typeof x === 'string'))];
    }
    s.updated_at = typeof raw.updated_at === 'string' ? raw.updated_at : null;
    return s;
  }

  /* ---------------------------------------------------------- 读写 */

  let memory = null;      // localStorage 不可用时的降级容器
  let persisted = true;   // 当前是否真的写进了 localStorage

  function available() {
    try {
      const k = '__jobpipe_probe__';
      root.localStorage.setItem(k, '1');
      root.localStorage.removeItem(k);
      return true;
    } catch (e) {
      return false;
    }
  }

  function load() {
    if (!available()) {
      persisted = false;
      return memory || emptyState();
    }
    persisted = true;
    try {
      const raw = root.localStorage.getItem(STORAGE_KEY);
      if (!raw) return emptyState();
      return normalize(JSON.parse(raw));
    } catch (e) {
      /* 存档损坏：挪到备份键再重开干净状态，别让用户卡在坏数据上 */
      try {
        const bad = root.localStorage.getItem(STORAGE_KEY);
        if (bad) root.localStorage.setItem(BACKUP_KEY, bad);
        root.localStorage.removeItem(STORAGE_KEY);
      } catch (e2) { /* 忽略 */ }
      return emptyState();
    }
  }

  function save(state) {
    state.updated_at = stamp();
    if (!available()) {
      memory = state;
      persisted = false;
      return state;
    }
    persisted = true;
    try {
      root.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      persisted = false;
    }
    return state;
  }

  function clear() {
    const cur = load();
    if (cur.updated_at) {
      try { root.localStorage.setItem(BACKUP_KEY, JSON.stringify(cur)); } catch (e) { /* 忽略 */ }
    }
    memory = null;
    if (available()) {
      try { root.localStorage.removeItem(STORAGE_KEY); } catch (e) { /* 忽略 */ }
    }
    return emptyState();
  }

  /* ---------------------------------------------------------- 合并 */

  /* 是否被用户改过（用于卡片上打标记，也用于「有多少本地记录」的统计） */
  function isTouched(state, id) {
    return Boolean(state.patches[id]) || state.added.some((j) => j.id === id);
  }

  function merge(seed, state) {
    const removed = new Set(state.removed);
    const out = [];

    for (const j of seed) {
      if (removed.has(j.id)) continue;
      const p = state.patches[j.id];
      out.push(p ? { ...j, ...p, _local: true } : j);
    }

    for (const j of state.added) {
      if (removed.has(j.id)) continue;
      const p = state.patches[j.id];
      out.push({ ...j, ...(p || {}), _local: true, _added: true });
    }

    return out;
  }

  function stats(state, seed) {
    const removed = new Set(state.removed);
    const ids = new Set([...seed.map((j) => j.id), ...state.added.map((j) => j.id)]);
    const touched = [...ids].filter((id) => !removed.has(id) && isTouched(state, id)).length;
    return {
      touched,
      added: state.added.filter((j) => !removed.has(j.id)).length,
      removed: state.removed.length,
      statusChanges: Object.entries(state.patches)
        .filter(([id, p]) => p.status && !removed.has(id)).length,
      updated_at: state.updated_at
    };
  }

  /* ---------------------------------------------------------- 变更 */

  /* 写入补丁。fields 里值为 null 的键会被删除（表示「恢复成种子值」） */
  function patch(state, id, fields) {
    const cur = { ...(state.patches[id] || {}) };
    for (const [k, v] of Object.entries(fields || {})) {
      if (v === null || v === undefined) delete cur[k];
      else cur[k] = v;
    }
    if (Object.keys(cur).length) state.patches[id] = cur;
    else delete state.patches[id];
    return state;
  }

  /* 状态流转：自动写 applied_at、补时间线、记住来路以便「撤销」 */
  function setStatus(state, id, next, opts) {
    const o = opts || {};
    const now = o.now || new Date();
    const prev = o.prev || 'wishlist';
    if (!STATUS[next]) return state;

    const p = state.patches[id] || {};
    const timeline = Array.isArray(p.timeline) ? [...p.timeline] : [];

    timeline.push({
      at: stamp(now),
      kind: 'status',
      from: prev,
      to: next,
      note: o.note || ''
    });

    const fields = { status: next, timeline, prev_status: prev };

    /* 首次投递：自动落投递日期，不用手填 */
    if (next === 'applied' && !p.applied_at && !o.applied_at) {
      fields.applied_at = today(now);
    }
    if (o.applied_at) fields.applied_at = o.applied_at;
    if (o.resume_version) fields.resume_version = o.resume_version;

    /* 结束态默认没有待跟进动作，否则会一直占着「今天要做」。
       这里写空串而不是 null —— null 会删掉补丁键，让种子里的旧日期重新冒出来。 */
    if (STATUS[next].closed) fields.next_action_at = '';

    return patch(state, id, fields);
  }

  /* 撤销上一步状态变更：从时间线里回退 */
  function undoStatus(state, id) {
    const p = state.patches[id];
    const timeline = p && Array.isArray(p.timeline) ? [...p.timeline] : [];
    if (!timeline.length) return state;

    const last = timeline[timeline.length - 1];
    timeline.pop();

    const fields = {
      status: last.from,
      timeline: timeline.length ? timeline : null,
      prev_status: timeline.length ? timeline[timeline.length - 1].to : null,
      next_action_at: ''
    };

    /* 退回「待投递」时把投递日期一并撤掉，不然会留下一条幽灵投递记录 */
    if (last.from === 'wishlist') fields.applied_at = '';

    return patch(state, id, fields);
  }

  /* 手动记一笔进展（面试问题、HR 口头承诺之类） */
  function addNote(state, id, text, opts) {
    const o = opts || {};
    const p = state.patches[id] || {};
    const timeline = Array.isArray(p.timeline) ? [...p.timeline] : [];
    timeline.push({ at: stamp(o.now || new Date()), kind: 'note', note: text });
    return patch(state, id, { timeline });
  }

  /* ---------------------------------------------------------- 增删 */

  function newId(company, now) {
    const slug = String(company || 'job')
      .toLowerCase()
      .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 24) || 'job';
    return `manual-${slug}-${String((now || new Date()).getTime()).slice(-5)}`;
  }

  function addJob(state, job, opts) {
    const o = opts || {};
    const now = o.now || new Date();
    const id = job.id || newId(job.company, now);
    const record = {
      id,
      company: job.company || '未命名公司',
      title: job.title || '未填写岗位',
      salary: job.salary || '',
      area: job.area || '',
      channel: job.channel || '手动录入',
      channel_type: job.channel_type || '手动录入',
      url: job.url || '',
      exp_req: job.exp_req || '',
      edu_req: job.edu_req || '',
      status: job.status || 'wishlist',
      applied_at: job.status === 'applied' ? today(now) : null,
      resume_version: null,
      next_action: '',
      next_action_at: '',
      notes: job.notes || '',
      cover_letter: '',
      found_at: today(now),
      why_match: [],
      gaps: [],
      advice: '',
      _manual: true
    };
    state.added.push(record);
    state.removed = state.removed.filter((x) => x !== id);
    return state;
  }

  /* 归档而非物理删除 —— 误删可恢复，也让「已放弃」和「删掉」语义分开。
     注意：不动 added、也不删 patch。merge() 已经会按 removed 过滤掉它，
     把记录留着才能在「已归档」里完整看到它、并原样恢复。 */
  function removeJob(state, id) {
    if (!state.removed.includes(id)) state.removed.push(id);
    return state;
  }

  function restoreJob(state, id) {
    state.removed = state.removed.filter((x) => x !== id);
    return state;
  }

  /* ---------------------------------------------------------- 序列化 */

  function exportState(state) {
    return {
      schema: SCHEMA,
      app: 'jobpipe',
      exported_at: stamp(),
      updated_at: state.updated_at,
      patches: state.patches,
      added: state.added,
      removed: state.removed
    };
  }

  /* 导入采用**合并**而非覆盖：多设备之间来回同步不会互相清空 */
  function importState(state, raw) {
    const incoming = normalize(raw);
    if (!incoming) return { state, merged: 0 };

    let merged = 0;

    for (const [id, p] of Object.entries(incoming.patches)) {
      const cur = state.patches[id] || {};
      const timeline = [...(cur.timeline || []), ...(p.timeline || [])]
        .filter((t, i, arr) => arr.findIndex((x) => x.at === t.at && x.kind === t.kind) === i)
        .sort((a, b) => String(a.at).localeCompare(String(b.at)));
      state.patches[id] = { ...cur, ...p, ...(timeline.length ? { timeline } : {}) };
      merged++;
    }

    for (const j of incoming.added) {
      if (!state.added.some((x) => x.id === j.id)) state.added.push(j);
      merged++;
    }

    state.removed = [...new Set([...state.removed, ...incoming.removed])];
    return { state, merged };
  }

  /* ---------------------------------------------------------- 导出接口 */

  root.JobStore = {
    SCHEMA,
    STORAGE_KEY,
    BACKUP_KEY,
    STATUS,
    STATUS_KEYS,
    OPEN_KEYS,
    CLOSED_KEYS,
    stamp,
    today,
    emptyState,
    normalize,
    load,
    save,
    clear,
    merge,
    stats,
    isTouched,
    patch,
    setStatus,
    undoStatus,
    addNote,
    addJob,
    removeJob,
    restoreJob,
    exportState,
    importState,
    isPersistent: () => persisted
  };
})(typeof window !== 'undefined' ? window : globalThis);
