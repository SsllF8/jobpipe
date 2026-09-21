/* ============================================================
   投递作战台 · 前端逻辑
   原生 JS，无框架、无外部依赖、无网络请求。

   数据来源
   --------
   window.__DATA__.applications  岗位池（构建期内联，只读）
   window.__STATE__              内嵌的初始操作记录（可为 null）
   localStorage                  你的操作记录（唯一写入目标）
   merge(岗位池, 本地记录) → 页面数据

   M1：只读渲染 + 自荐信复制
   M2：状态流转 / 跟进计划 / 备注 / 时间线 / 手动新增 / 导入导出
   M3：自荐信生成（DeepSeek）
   ============================================================ */

(function () {
  'use strict';

  var S = window.JobStore;
  if (!S) { console.error('JobStore 未加载'); return; }

  var META = window.__META__ || {};
  var SEED = (window.__DATA__ || {}).applications || [];
  var BUILTIN = window.__STATE__ || null;

  var STATUS = S.STATUS;
  var GHOST_DAYS = 14;

  /* ---------------------------------------------------------- 初始化 */

  var STATE = S.load();

  /* 只有本地完全为空时才采纳构建期内嵌的 state。
     否则每次重新部署都会把手机上记的进度回滚掉。 */
  if (!STATE.updated_at && BUILTIN) {
    STATE = S.normalize(BUILTIN);
    S.save(STATE);
  }

  var JOBS = S.merge(SEED, STATE);

  function commit() {
    S.save(STATE);
    JOBS = S.merge(SEED, STATE);
    render();
  }

  /* ---------------------------------------------------------- 工具 */

  var $ = function (sel) { return document.querySelector(sel); };

  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  function parseDay(iso) {
    if (!iso) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  }

  /* 目标日期 - 今天，正数 = 未来 */
  function diffDays(iso) {
    var d = parseDay(iso);
    if (!d) return null;
    var now = new Date();
    var base = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((d - base) / 86400000);
  }

  function humanDate() {
    var d = new Date();
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 周' + '日一二三四五六'[d.getDay()];
  }

  /* 公司简称：去掉括号说明和后缀，便于窄屏一行读完 */
  function shortCo(name) {
    if (!name) return '未知公司';
    var raw = String(name).trim();

    /* 整个名字被括号包住 = 平台匿名发布，括号里的来源信息反而是最有用的标识 */
    var wrapped = /^[（(]([^）)]*)[）)]$/.exec(raw);
    if (wrapped) {
      var inner = wrapped[1].trim();
      return inner.length > 16 ? inner.slice(0, 16) : inner;
    }

    var s = raw.replace(/^[（(][^）)]*[）)]\s*/, '');
    s = s.replace(/[（(].*$/, '').trim();
    s = s.replace(/(集团)?(股份)?有限公司$/, '').replace(/有限责任公司$/, '');
    if (!s) s = raw.replace(/[（(].*$/, '').trim() || raw;
    return s.length > 16 ? s.slice(0, 16) : s;
  }

  function tsShort(at) {
    return String(at || '').replace('T', ' ').slice(5, 16);
  }

  /* ---------------------------------------------------------- 派生字段 */

  function displayScore(j) {
    if (j.score != null) return Number(j.score);
    if (j.score_auto != null) return Number(j.score_auto);
    return null;
  }

  function displayTier(j) {
    if (j.tier) return j.tier;
    if (j.tier_auto) return j.tier_auto;
    return displayScore(j) == null ? '—' : 'C';
  }

  /* CSS 类名只认 S/A/B/C，其余归到 X，避免出现非法选择器 */
  function tcls(t) {
    return ['S', 'A', 'B', 'C'].indexOf(t) >= 0 ? t : 'X';
  }

  function statusOf(j) {
    return STATUS[j.status] ? j.status : 'wishlist';
  }

  function findJob(id) {
    for (var i = 0; i < JOBS.length; i++) if (JOBS[i].id === id) return JOBS[i];
    var arch = archivedJobs();
    for (var k = 0; k < arch.length; k++) if (arch[k].id === id) return arch[k];
    return null;
  }

  function archivedJobs() {
    var removed = {};
    STATE.removed.forEach(function (x) { removed[x] = true; });
    var pool = SEED.concat(STATE.added);
    return pool
      .filter(function (j) { return removed[j.id]; })
      .map(function (j) {
        var p = STATE.patches[j.id] || {};
        var o = {}; for (var k in j) o[k] = j[k];
        for (var k2 in p) o[k2] = p[k2];
        o._archived = true;
        return o;
      });
  }

  function byScore(a, b) {
    var sa = displayScore(a), sb = displayScore(b);
    if (sa == null && sb == null) {
      return String(b.found_at || '').localeCompare(String(a.found_at || ''));
    }
    if (sa == null) return 1;
    if (sb == null) return -1;
    return sb - sa;
  }

  /* 逾期未跟进：已投递/沟通中/面试中，且计划动作日期已过 */
  function overdueDays(j) {
    var st = statusOf(j);
    if (['applied', 'screening', 'interview'].indexOf(st) < 0) return null;
    var d = diffDays(j.next_action_at);
    if (d == null || d >= 0) return null;
    return -d;
  }

  /* 投递后沉默天数 */
  function silentDays(j) {
    if (statusOf(j) !== 'applied') return null;
    var d = diffDays(j.applied_at);
    if (d == null) return null;
    return -d;
  }

  function counts() {
    var c = { wishlist: 0, active: 0, interview: 0, offer: 0, overdue: 0, total: JOBS.length };
    JOBS.forEach(function (j) {
      var st = statusOf(j);
      if (st === 'wishlist') c.wishlist++;
      if (st === 'applied' || st === 'screening') c.active++;
      if (st === 'interview') { c.interview++; c.active++; }
      if (st === 'offer') c.offer++;
      if (overdueDays(j) != null) c.overdue++;
    });
    return c;
  }

  /* ---------------------------------------------------------- 今天要做 */

  function buildTodos() {
    var items = [];
    var seen = {};

    JOBS.forEach(function (j) {
      var od = overdueDays(j);
      if (od != null) {
        items.push({
          p: 0, overdue: true,
          title: shortCo(j.company) + ' · 跟进逾期 ' + od + ' 天',
          sub: j.next_action ? '计划动作：' + j.next_action : '建议尽快联系 HR',
          job: j
        });
        seen[j.id] = true;
        return;
      }

      var d = diffDays(j.next_action_at);
      var st = statusOf(j);
      if (d != null && d >= 0 && d <= 1 && ['wishlist', 'applied', 'screening', 'interview'].indexOf(st) >= 0) {
        items.push({
          p: 1,
          title: shortCo(j.company) + ' · ' + (d === 0 ? '今天' : '明天') + '｜' + (j.next_action || '处理此岗位'),
          sub: displayTier(j) + ' 级 · ' + j.title,
          job: j
        });
        seen[j.id] = true;
        return;
      }

      var sd = silentDays(j);
      if (sd != null && sd >= GHOST_DAYS) {
        items.push({
          p: 2,
          title: shortCo(j.company) + ' · 投递 ' + sd + ' 天无回复',
          sub: '确认是否转为「无回应」，或换个渠道再投',
          job: j
        });
        seen[j.id] = true;
      }
    });

    /* 剩余待投递的 S 级岗位聚合成一条 */
    var sPending = JOBS.filter(function (j) {
      return statusOf(j) === 'wishlist' && displayTier(j) === 'S' && !seen[j.id];
    });
    if (sPending.length) {
      items.push({
        p: 3,
        goto: 'jobs',
        title: sPending.length === 1
          ? shortCo(sPending[0].company) + ' · S 级待投递'
          : sPending.length + ' 个 S 级岗位待投递',
        sub: sPending.map(function (j) { return shortCo(j.company); }).join(' · '),
        job: sPending[0]
      });
    }

    return items.sort(function (a, b) { return a.p - b.p; });
  }

  /* ---------------------------------------------------------- 片段 */

  function ringHTML(score, tier) {
    var col = score == null ? '#3a3a3a' : ({ S: '#c41e3a', A: '#d9903c', B: '#7b8794', C: '#5a5a5a' }[tier] || '#5a5a5a');
    var r = 17, c = 2 * Math.PI * r;
    var off = score == null ? c : c * (1 - Math.min(100, score) / 100);
    return '<div class="ring"><svg viewBox="0 0 42 42">' +
      '<circle cx="21" cy="21" r="' + r + '" fill="none" stroke="#242424" stroke-width="3.5"/>' +
      '<circle cx="21" cy="21" r="' + r + '" fill="none" stroke="' + col + '" stroke-width="3.5"' +
      ' stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '"' +
      ' stroke-linecap="round" transform="rotate(-90 21 21)"/></svg>' +
      '<div class="num" style="color:' + col + '">' + (score == null ? '—' : Math.round(score)) + '</div></div>';
  }

  function jobCardHTML(j) {
    var tier = displayTier(j);
    var st = STATUS[statusOf(j)];
    var arch = j._archived;
    return '<article class="card ' + tcls(tier).toLowerCase() + (arch ? ' arch' : '') + '" data-id="' + esc(j.id) + '">' +
      '<div class="card-top">' + ringHTML(displayScore(j), tier) +
      '<div class="card-body">' +
        '<div class="co">' + esc(shortCo(j.company)) + (j._local && !arch ? '<span class="ldot" title="本地有改动"></span>' : '') + '</div>' +
        '<div class="jt">' + esc(j.title) + '</div>' +
        '<div class="mt"><span class="sal">' + esc(j.salary || '薪资面议') + '</span><span>' + esc(j.area || '') + '</span></div>' +
      '</div></div>' +
      '<div class="tags">' +
        '<span class="tag t-' + tcls(tier) + '">' + (tier === '—' ? '待评估' : tier + ' 级') + '</span>' +
        (j._manual ? '<span class="tag manual">手动录入</span>' : '') +
        '<span class="tag">' + esc(j.channel || '未知来源') + '</span>' +
        '<span class="tag st">' + esc(st.label) + '</span>' +
      '</div></article>';
  }

  /* ---------------------------------------------------------- 视图：今日 */

  function viewToday() {
    var c = counts();
    var todos = buildTodos();
    var sJobs = JOBS.filter(function (j) { return displayTier(j) === 'S'; }).sort(byScore);

    var kpi = [
      { n: c.wishlist, l: '待投递' },
      { n: c.active, l: '在投' },
      { n: c.interview, l: '面试中' },
      { n: c.overdue, l: '逾期跟进', hot: c.overdue > 0 }
    ].map(function (k) {
      return '<div class="kpi ' + (k.hot ? 'hot' : '') + '"><div class="n">' + k.n + '</div><div class="l">' + k.l + '</div></div>';
    }).join('');

    var todoHTML = todos.length
      ? '<div class="todos">' + todos.map(function (t) {
          return '<div class="todo ' + (t.overdue ? 'overdue' : '') + '" ' +
            (t.goto ? 'data-goto="' + esc(t.goto) + '"' : 'data-id="' + esc(t.job.id) + '"') + '>' +
            '<div class="dot"></div><div class="tx">' +
            '<div class="t1">' + esc(t.title) + '</div>' +
            '<div class="t2">' + esc(t.sub) + '</div></div></div>';
        }).join('') + '</div>'
      : '<div class="empty">今天没有待办。所有岗位都在按计划推进。</div>';

    var sHTML = sJobs.length
      ? sJobs.map(jobCardHTML).join('')
      : '<div class="empty">暂无 S 级岗位</div>';

    var warn = S.isPersistent() ? '' :
      '<div class="warnbar">⚠ 当前环境无法写入本地存储，你记的进度刷新后会丢失。' +
      '建议用 http 地址打开，或先「导出数据」备份。</div>';

    var demoBar = META.demo
      ? '<div class="warnbar demo">演示数据 —— 岗位状态、备注、操作记录均为虚构，' +
        '仅用于展示「用过一段时间后」的界面。你的真实记录不会用到这份文件。</div>'
      : '';

    return '<div class="view">' + demoBar + warn +
      '<div class="slab">今天要做<span class="count">' + todos.length + ' 项</span></div>' + todoHTML +
      '<div class="slab">总览</div><div class="kpis">' + kpi + '</div>' +
      '<div class="slab">S 级机会<span class="count">' + sJobs.length + ' 个</span></div>' + sHTML +
      '</div>';
  }

  /* ---------------------------------------------------------- 视图：岗位 */

  var jobFilter = 'ALL';

  function viewJobs() {
    var all = JOBS.slice().sort(byScore);
    var list = jobFilter === 'ALL' ? all : all.filter(function (j) { return displayTier(j) === jobFilter; });

    var tiers = ['ALL', 'S', 'A', 'B', 'C'];
    var manualN = all.filter(function (j) { return displayTier(j) === '—'; }).length;
    if (manualN) tiers.push('—');

    var chips = tiers.map(function (t) {
      var n = t === 'ALL' ? all.length : all.filter(function (j) { return displayTier(j) === t; }).length;
      return '<button class="chip ' + (jobFilter === t ? 'on' : '') + '" data-tier="' + esc(t) + '">' +
        (t === 'ALL' ? '全部' : t === '—' ? '待评估' : t + ' 级') + ' ' + n + '</button>';
    }).join('');

    var total = STATE.added.filter(function (j) { return STATE.removed.indexOf(j.id) < 0; }).length;
    var foot = '共 ' + all.length + ' 个岗位 · 数据 ' + esc(META.updated_at || '') +
      (total ? ' · 手动新增 ' + total + ' 个' : '');

    return '<div class="view"><div class="chips">' + chips + '</div>' +
      (list.length ? list.map(jobCardHTML).join('') : '<div class="empty">该分档暂无岗位</div>') +
      '<div class="slab gray">' + foot + '</div></div>';
  }

  /* ---------------------------------------------------------- 视图：投递 */

  var pipeFilter = 'open';

  function viewPipeline() {
    var arch = archivedJobs();
    var list = JOBS.slice().sort(function (a, b) {
      var da = diffDays(a.next_action_at), db = diffDays(b.next_action_at);
      if (da == null && db == null) return byScore(a, b);
      if (da == null) return 1;
      if (db == null) return -1;
      return da - db;
    });

    function closed(j) { return STATUS[statusOf(j)].closed; }

    var groups = [
      { key: 'open', label: '在跟', test: function (j) { return !closed(j); } },
      { key: 'wishlist', label: '待投递', test: function (j) { return statusOf(j) === 'wishlist'; } },
      { key: 'applied', label: '已投递', test: function (j) { return statusOf(j) === 'applied'; } },
      { key: 'screening', label: '沟通中', test: function (j) { return statusOf(j) === 'screening'; } },
      { key: 'interview', label: '面试中', test: function (j) { return statusOf(j) === 'interview'; } },
      { key: 'offer', label: 'Offer', test: function (j) { return statusOf(j) === 'offer'; } },
      { key: 'closed', label: '已结束', test: function (j) { return closed(j); } }
    ];
    if (arch.length) groups.push({ key: 'archived', label: '已归档', test: function () { return false; } });

    var chips = groups.map(function (g) {
      var n = g.key === 'archived' ? arch.length : list.filter(g.test).length;
      return '<button class="chip ' + (pipeFilter === g.key ? 'on' : '') + '" data-pipe="' + esc(g.key) + '">' +
        g.label + ' ' + n + '</button>';
    }).join('');

    var cur = null;
    for (var i = 0; i < groups.length; i++) if (groups[i].key === pipeFilter) cur = groups[i];
    if (!cur) cur = groups[0];

    var rows = cur.key === 'archived' ? arch : list.filter(cur.test);

    var body = rows.length ? rows.map(function (j) {
      var od = overdueDays(j), sd = silentDays(j), d = diffDays(j.next_action_at);
      var note = '';
      if (j._archived) note = '<div class="rn">已归档 · 点开可恢复</div>';
      else if (od != null) note = '<div class="rn warn">跟进逾期 ' + od + ' 天 · ' + esc(j.next_action || '') + '</div>';
      else if (sd != null && sd >= GHOST_DAYS) note = '<div class="rn warn">已投递 ' + sd + ' 天无回复</div>';
      else if (d != null && d >= 0) note = '<div class="rn">' + (d === 0 ? '今天' : d === 1 ? '明天' : d + ' 天后') + ' · ' + esc(j.next_action || '') + '</div>';
      else if (statusOf(j) === 'wishlist') note = '<div class="rn">还没投递</div>';
      else if (j.applied_at) note = '<div class="rn">投递于 ' + esc(j.applied_at) + '</div>';

      return '<div class="row ' + (od != null ? 'urgent' : '') + '" data-id="' + esc(j.id) + '">' +
        '<div class="r1"><div class="rt">' + esc(shortCo(j.company)) + '</div>' +
        '<span class="tag t-' + tcls(displayTier(j)) + '">' + (displayTier(j) === '—' ? '—' : displayTier(j)) + '</span></div>' +
        '<div class="rc">' + esc(j.title) + ' · ' + esc(STATUS[statusOf(j)].label) + '</div>' + note + '</div>';
    }).join('') : '<div class="empty">这一档暂时是空的</div>';

    return '<div class="view"><div class="chips">' + chips + '</div>' + body +
      '<div class="slab gray">共 ' + list.length + ' 条在册投递记录</div></div>';
  }

  /* ---------------------------------------------------------- 视图：自荐信 */

  function viewLetters() {
    var sJobs = JOBS.filter(function (j) { return displayTier(j) === 'S'; }).sort(byScore);
    var ready = sJobs.filter(function (j) { return (j.cover_letter || '').trim(); });
    var pending = sJobs.filter(function (j) { return !(j.cover_letter || '').trim(); });

    function rowHTML(j, has) {
      var lc = (j.cover_letter || '').replace(/\s/g, '').length;
      return '<div class="row" data-id="' + esc(j.id) + '">' +
        '<div class="r1"><div class="rt">' + esc(shortCo(j.company)) + '</div>' +
        '<span class="tag t-' + tcls(displayTier(j)) + '">' + displayTier(j) + '</span></div>' +
        '<div class="rc">' + esc(j.title) + (has ? ' · ' + lc + ' 字' : '') + '</div>' +
        '<div class="card-acts">' +
        (has
          ? '<button class="btn primary" data-copy="' + esc(j.id) + '">复制自荐信</button>'
          : '<button class="btn muted" data-nogen="1">未生成</button>') +
        '<button class="btn ghost fixed" data-id="' + esc(j.id) + '" data-detail="1">详情</button>' +
        '</div></div>';
    }

    return '<div class="view">' +
      '<div class="slab">已就绪<span class="count">' + ready.length + ' 封</span></div>' +
      (ready.length ? ready.map(function (j) { return rowHTML(j, true); }).join('') : '<div class="empty">还没有生成自荐信</div>') +
      (pending.length
        ? '<div class="slab gray">待生成<span class="count">' + pending.length + ' 封</span></div>' +
          pending.map(function (j) { return rowHTML(j, false); }).join('')
        : '') +
      '<div class="slab gray">规则</div>' +
      '<div class="row"><div class="rc">只对 <b>S 级</b>（匹配分 ≥78）岗位生成。生成一次即写入缓存，看板上不会重复调用 AI。</div></div>' +
      '<div class="row"><div class="rc">生成方式：电脑上运行 <code>python tools/gen_letter.py</code>，再重新构建看板。</div></div>' +
      '</div>';
  }

  /* ---------------------------------------------------------- 时间线 */

  function timelineHTML(j) {
    var tl = (j.timeline || []).slice().reverse();
    if (!tl.length) return '<div class="empty">还没有操作记录</div>';
    return '<ol class="tl">' + tl.map(function (t) {
      var at = '<div class="tl-at">' + esc(tsShort(t.at)) + '</div>';
      if (t.kind === 'status') {
        var from = STATUS[t.from] ? STATUS[t.from].label : (t.from || '—');
        var to = STATUS[t.to] ? STATUS[t.to].label : (t.to || '—');
        return '<li class="st">' + at + '<div class="tl-tx">' +
          '<span class="flow">' + esc(from) + '<i>→</i><b>' + esc(to) + '</b></span>' +
          (t.note ? '<div class="tl-note">' + esc(t.note) + '</div>' : '') + '</div></li>';
      }
      return '<li>' + at + '<div class="tl-tx">' + esc(t.note || '') + '</div></li>';
    }).join('') + '</ol>';
  }

  /* ---------------------------------------------------------- 详情面板 */

  function statusChipsHTML(j) {
    var cur = statusOf(j);
    var open = S.OPEN_KEYS.map(function (k) {
      return '<button class="stchip' + (k === cur ? ' on' : '') + '" data-set-status="' + k +
        '" data-set-status-id="' + esc(j.id) + '">' + STATUS[k].label + '</button>';
    }).join('');
    var close = S.CLOSED_KEYS.map(function (k) {
      return '<button class="stchip end' + (k === cur ? ' on' : '') + '" data-set-status="' + k +
        '" data-set-status-id="' + esc(j.id) + '">' + STATUS[k].label + '</button>';
    }).join('');
    return '<div class="strow">' + open + '</div><div class="strow">' + close + '</div>';
  }

  function resumeHTML(j) {
    var st = statusOf(j);
    if (st === 'wishlist') return '';
    var cur = j.resume_version || '';
    var opts = ['A', 'B'].map(function (v) {
      return '<button class="rchip' + (cur === v ? ' on' : '') + '" data-set-resume="' + v +
        '" data-set-resume-id="' + esc(j.id) + '">' + v + ' 版</button>';
    }).join('');
    return '<div class="fieldrow"><div class="fl">用哪版简历</div><div class="fr"><div class="rchips">' + opts +
      '<button class="rchip' + (cur ? '' : ' on') + '" data-set-resume="" data-set-resume-id="' + esc(j.id) + '">未标</button>' +
      '</div></div></div>';
  }

  function sheetBodyHTML(j) {
    var tier = displayTier(j);
    var score = displayScore(j);
    var arch = j._archived;
    /* 采集层贴进来的原始 JD（只有 tools/ingest_jd.py 录入的岗位才有）。
       它同时是打分引擎的首选匹配文本，值得让人能点开回看。 */
    var jd = (j.jd_text || '').trim();

    var list = function (arr, cls) {
      return (arr || []).length
        ? '<ul class="' + cls + '">' + arr.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
        : '<div class="empty">无</div>';
    };

    var grid = [
      ['薪资', j.salary || '未公示'],
      ['经验门槛', j.exp_req || '未标明'],
      ['学历', j.edu_req || '未标明'],
      ['信息来源', j.channel || '未知'],
      ['地区', j.area || '—'],
      ['投递日期', j.applied_at || '—']
    ].map(function (kv) {
      return '<div><div class="k">' + esc(kv[0]) + '</div><div class="v">' + esc(kv[1]) + '</div></div>';
    }).join('');

    var letter = (j.cover_letter || '').trim();
    var head = j._archived
      ? '<div class="savenote">此岗位已归档，恢复后重新出现在岗位池里</div>'
      : statusChipsHTML(j) + resumeHTML(j);

    return '<div class="grip"></div>' +
      '<div class="saveline"><span class="tag t-' + tcls(tier) + '">' +
        (tier === '—' ? '待评估' : tier + ' 级 · ' + Math.round(score) + ' 分') + '</span>' +
        '<span class="saved" id="savebar"></span></div>' +
      '<h2>' + esc(j.title) + '</h2>' +
      '<div class="sub">' + esc(j.company) + (j._manual ? ' · 手动录入' : '') + '</div>' +
      head +
      '<div class="slab">跟进计划</div>' +
      '<div class="fieldrow"><div class="fl">下一步</div><div class="fr">' +
        '<input class="tin" type="text" data-field="next_action" data-field-id="' + esc(j.id) + '"' +
        ' value="' + esc(j.next_action || '') + '" placeholder="如：跟进 HR / 准备笔试" enterkeyhint="done">' +
      '</div></div>' +
      '<div class="fieldrow"><div class="fl">日期</div><div class="fr">' +
        '<input class="tin" type="date" data-field="next_action_at" data-field-id="' + esc(j.id) + '"' +
        ' value="' + esc((j.next_action_at || '').slice(0, 10)) + '">' +
      '</div></div>' +
      '<div class="slab">备注</div>' +
      '<textarea class="tarea" data-field="notes" data-field-id="' + esc(j.id) + '"' +
      ' placeholder="这个岗位的长期备注…">' + esc(j.notes || '') + '</textarea>' +
      '<div class="quick">' +
        '<input class="tin" id="note-quick" type="text" placeholder="记一笔进展，如「HR 加了微信」" enterkeyhint="done">' +
        '<button class="btn ghost fixed" data-note-add="' + esc(j.id) + '">记录</button>' +
      '</div>' +
      '<div class="slab">操作记录<span class="count">' + ((j.timeline || []).length) + ' 条</span></div>' +
      timelineHTML(j) +
      '<div class="slab">基本信息</div>' +
      '<div class="meta-grid">' + grid + '</div>' +
      (j.advice ? '<div class="slab">投递建议</div><div class="row" style="margin:0"><div class="rc">' + esc(j.advice) + '</div></div>' : '') +
      '<div class="slab">为什么匹配<span class="count">' + ((j.why_match || []).length) + ' 条</span></div>' +
      list(j.why_match, 'hit') +
      '<div class="slab">你的能力缺口<span class="count">' + ((j.gaps || []).length) + ' 条</span></div>' +
      list(j.gaps, 'gap') +
      (jd
        ? '<div class="slab">原始 JD<span class="count">' + jd.length + ' 字</span></div>' +
          '<details class="jd"><summary>展开查看录入时的原文</summary><pre>' +
          esc(jd) + '</pre></details>'
        : '') +
      (letter
        ? '<div class="slab">自荐信<span class="count">' + letter.replace(/\s/g, '').length + ' 字</span></div>' +
          '<div class="letter">' + esc(letter) + '</div>' +
          '<div class="card-acts"><button class="btn primary" data-copy="' + esc(j.id) + '">复制自荐信</button></div>'
        : (tier === 'S'
          ? '<div class="slab">自荐信</div><div class="row" style="margin:0"><div class="rc">' +
            '还没生成。电脑上运行 <code>python tools/gen_letter.py</code> 再重新构建，正文会出现在这里。</div></div>'
          : '')) +
      '<div class="card-acts" style="margin-top:18px">' +
        (j.url ? '<a class="btn ghost" href="' + esc(j.url) + '" target="_blank" rel="noopener">打开招聘页</a>' : '') +
        (!arch && (j.timeline || []).length ? '<button class="btn ghost" data-undo="' + esc(j.id) + '">撤销上一步</button>' : '') +
      '</div>' +
      '<div class="card-acts">' +
        (arch
          ? '<button class="btn primary" data-restore="' + esc(j.id) + '">从归档恢复</button>'
          : '<button class="btn ghost danger" data-archive="' + esc(j.id) + '">归档此岗位</button>') +
      '</div>' +
      '<button class="close" data-close="1">关闭</button>';
  }

  function openSheet(id, opts) {
    var o = opts || {};
    var prev = document.querySelector('.sheet');
    var keep = o.preserveScroll && prev ? prev.scrollTop : 0;
    if (prev) closeSheet(true);

    var j = findJob(id);
    if (!j) return;

    var html = '<div class="mask"><div class="sheet" data-stop="1">' + sheetBodyHTML(j) + '</div></div>';
    var wrap = document.createElement('div');
    wrap.innerHTML = html;
    document.body.appendChild(wrap.firstElementChild);
    document.body.style.overflow = 'hidden';
    document.body.classList.add('sheet-open');

    if (keep) {
      var sh = document.querySelector('.sheet');
      if (sh) sh.scrollTop = keep;
    }
  }

  function closeSheet(silent) {
    if (!silent) flushSheetFields();
    var m = document.querySelector('.mask');
    if (m) m.remove();
    document.body.style.overflow = '';
    document.body.classList.remove('sheet-open');
  }

  /* ---------------------------------------------------------- 通用面板 */

  function openPanel(innerHTML, opts) {
    var o = opts || {};
    if (document.querySelector('.mask')) closeSheet(true);
    var html = '<div class="mask"><div class="sheet" data-stop="1">' +
      '<div class="grip"></div>' + innerHTML +
      '<button class="close" data-close="1">' + (o.closeLabel || '关闭') + '</button>' +
      '</div></div>';
    var wrap = document.createElement('div');
    wrap.innerHTML = html;
    document.body.appendChild(wrap.firstElementChild);
    document.body.style.overflow = 'hidden';
    document.body.classList.add('sheet-open');
  }

  /* ---------------------------------------------------------- 字段保存 */

  var saveTimer = null;

  function flashSaved(msg) {
    var el = document.getElementById('savebar');
    if (!el) return;
    el.textContent = msg || '已保存';
    el.classList.add('on');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { el.classList.remove('on'); }, 1400);
  }

  function saveField(el, quiet) {
    if (!el || !el.dataset || !el.dataset.field) return;
    var id = el.dataset.fieldId;
    var key = el.dataset.field;
    var job = findJob(id);
    if (!job) return;

    var next = el.value == null ? '' : String(el.value);
    var cur = job[key] == null ? '' : String(job[key]);
    if (next === cur) return;

    S.patch(STATE, id, (function () { var o = {}; o[key] = next; return o; })());
    commit();
    if (!quiet) flashSaved();
  }

  function flushSheetFields() {
    var fields = document.querySelectorAll('.sheet [data-field]');
    for (var i = 0; i < fields.length; i++) saveField(fields[i], true);
  }

  /* ---------------------------------------------------------- 操作 */

  function applyStatus(id, next) {
    var job = findJob(id);
    if (!job || !STATUS[next]) return;
    var prev = statusOf(job);
    if (prev === next) return;
    flushSheetFields();
    S.setStatus(STATE, id, next, { prev: prev });
    commit();
    openSheet(id, { preserveScroll: true });
    flashSaved(STATUS[prev].label + ' → ' + STATUS[next].label);
  }

  function applyResume(id, version) {
    var job = findJob(id);
    if (!job) return;
    S.patch(STATE, id, { resume_version: version || '' });
    commit();
    openSheet(id, { preserveScroll: true });
    flashSaved();
  }

  function undoLast(id) {
    var job = findJob(id);
    if (!job) return;
    var tl = job.timeline || [];
    var last = tl[tl.length - 1];
    if (!last || last.kind !== 'status') { toast('没有可撤销的状态变更'); return; }
    flushSheetFields();
    S.undoStatus(STATE, id);
    commit();
    openSheet(id, { preserveScroll: true });
    flashSaved('已撤销 ' + STATUS[last.to].label);
  }

  function addQuickNote(id) {
    var input = document.getElementById('note-quick');
    if (!input) return;
    var text = (input.value || '').trim();
    if (!text) { toast('先写点什么'); return; }
    S.addNote(STATE, id, text);
    commit();
    openSheet(id, { preserveScroll: true });
    flashSaved('已记录');
  }

  function archiveJob(id) {
    var job = findJob(id);
    if (!job) return;
    S.removeJob(STATE, id);
    commit();
    closeSheet(true);
    toast(shortCo(job.company) + ' 已归档 · 在「投递 → 已归档」可恢复');
  }

  function restoreJob(id) {
    var job = findJob(id);
    S.restoreJob(STATE, id);
    commit();
    closeSheet(true);
    toast((job ? shortCo(job.company) : '岗位') + ' 已恢复到岗位池');
  }

  /* ---------------------------------------------------------- 新增岗位 */

  function openAddPanel() {
    var html =
      '<h2>手动录入岗位</h2>' +
      '<div class="sub">采集脚本上线前，看到合适的岗位先记进来，状态和备注照常可写。</div>' +
      '<div class="form">' +
        '<label><span>公司 <i>*</i></span><input class="tin" id="f-company" type="text" placeholder="公司全称或简称"></label>' +
        '<label><span>岗位 <i>*</i></span><input class="tin" id="f-title" type="text" placeholder="岗位名称"></label>' +
        '<label><span>薪资</span><input class="tin" id="f-salary" type="text" placeholder="如 12-18k"></label>' +
        '<label><span>地区</span><input class="tin" id="f-area" type="text" placeholder="如 上海 · 徐汇区"></label>' +
        '<label><span>来源</span><input class="tin" id="f-channel" type="text" placeholder="如 BOSS / 内推 / 官网"></label>' +
        '<label><span>链接</span><input class="tin" id="f-url" type="url" placeholder="https://"></label>' +
        '<label><span>备注</span><textarea class="tarea" id="f-notes" placeholder="投递渠道、联系人…"></textarea></label>' +
      '</div>' +
      '<div class="card-acts"><button class="btn primary" data-add-submit="1">保存</button></div>' +
      '<div class="empty" style="margin-top:10px">手动录入的岗位没有 AI 匹配分，会标「待评估」排在最后，不占 S 级名单。</div>';

    openPanel(html, { closeLabel: '取消' });
    var first = document.getElementById('f-company');
    if (first) setTimeout(function () { first.focus(); }, 120);
  }

  function submitAdd() {
    var v = function (id) {
      var el = document.getElementById(id);
      return el ? (el.value || '').trim() : '';
    };
    var company = v('f-company'), title = v('f-title');
    if (!company || !title) { toast('公司和岗位是必填的'); return; }

    S.addJob(STATE, {
      company: company, title: title, salary: v('f-salary'), area: v('f-area'),
      channel: v('f-channel') || '手动录入', url: v('f-url'), notes: v('f-notes')
    });
    var id = STATE.added[STATE.added.length - 1].id;
    commit();
    closeSheet(true);
    openSheet(id);
    toast('已录入 ' + shortCo(company));
  }

  /* ---------------------------------------------------------- 数据面板 */

  function exportText() {
    return JSON.stringify(S.exportState(STATE), null, 2);
  }

  function openMorePanel() {
    var st = S.stats(STATE, SEED);
    var persisted = S.isPersistent();
    /* 顺手刷一次缓存版本：用户点开这个面板时最可能想知道「我看到的是不是旧的」 */
    readSwVersion();
    var ctrl = navigator.serviceWorker && navigator.serviceWorker.controller;

    var html =
      '<h2>数据与设置</h2>' +
      '<div class="sub">所有记录只存在这台设备的浏览器里，不会上传到任何服务器。</div>' +

      '<div class="slab">本地记录<span class="count">' + st.touched + ' 个岗位</span></div>' +
      '<div class="meta-grid">' +
        '<div><div class="k">状态变更</div><div class="v">' + st.statusChanges + ' 个岗位</div></div>' +
        '<div><div class="k">手动新增</div><div class="v">' + st.added + ' 个</div></div>' +
        '<div><div class="k">已归档</div><div class="v">' + st.removed + ' 个</div></div>' +
        '<div><div class="k">最后保存</div><div class="v">' + esc(st.updated_at ? tsShort(st.updated_at) : '—') + '</div></div>' +
      '</div>' +

      '<div class="fieldrow"><div class="fl">存储状态</div><div class="fr">' +
        '<div class="' + (persisted ? 'ok' : 'bad') + '">' +
        (persisted ? '已启用本地保存' : '不可用 —— 进度刷新后会丢失') + '</div></div></div>' +

      '<div class="fieldrow"><div class="fl">离线缓存</div><div class="fr">' +
        (isHttp()
          ? (ctrl
              ? '<div class="ok">已启用 · 缓存版本 ' + esc(SW_VERSION) + '</div>'
              : '<div class="dim">正在准备…</div>')
          : '<div class="dim">未启用（本地文件模式）</div>') +
      '</div></div>' +

      '<div class="slab">备份与迁移</div>' +
      '<div class="card-acts">' +
        '<button class="btn primary" data-export-copy="1">复制数据</button>' +
        '<button class="btn ghost" data-export-file="1">下载文件</button>' +
      '</div>' +
      '<div class="card-acts">' +
        '<button class="btn ghost" data-import-file="1">从文件导入</button>' +
        '<button class="btn ghost" data-import-clip="1">从剪贴板导入</button>' +
      '</div>' +
      '<input type="file" id="import-input" accept="application/json,.json" hidden>' +
      '<div class="hint">导出的 JSON 可以丢回项目 <code>data/state.json</code>，' +
      '用 <code>python build.py</code> 构建出带数据的个人版留档；导入采用合并，不会清空已有记录。</div>' +

      '<div class="slab">危险操作</div>' +
      '<div class="card-acts">' +
        '<button class="btn ghost" data-load-builtin="1"' + (BUILTIN ? '' : ' disabled') + '>载入内嵌数据</button>' +
        '<button class="btn ghost danger" data-reset="1">清空本地记录</button>' +
      '</div>' +
      '<div class="hint">「清空」会删掉全部状态变更、备注与手动录入，岗位池回到初始状态。清空前会先自动留一份备份。</div>';

    openPanel(html);
  }

  /* ---------------------------------------------------------- 提示 */

  function toast(msg) {
    var old = document.querySelector('.toast');
    if (old) old.remove();
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 2200);
  }

  function copyText(text, okMsg) {
    if (!text) return;
    var done = function (ok) { toast(ok ? okMsg : '复制失败，请长按手动选择'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { fallback(); });
    } else fallback();

    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      done(ok);
    }
  }

  function downloadExport() {
    var blob = new Blob([exportText()], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'jobpipe-state-' + S.today() + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    toast('已下载 ' + a.download);
  }

  function doImport(text, label) {
    var raw;
    try { raw = JSON.parse(text); }
    catch (e) { toast('不是合法的 JSON'); return; }
    var r = S.importState(STATE, raw);
    commit();
    closeSheet(true);
    toast('已从' + label + '合并 ' + r.merged + ' 条记录');
  }

  function importFromFile() {
    var input = document.getElementById('import-input');
    if (!input) return;
    input.value = '';
    input.onchange = function () {
      var f = input.files && input.files[0];
      if (!f) return;
      var rd = new FileReader();
      rd.onload = function () { doImport(String(rd.result || ''), '文件'); };
      rd.onerror = function () { toast('文件读不出来'); };
      rd.readAsText(f);
    };
    input.click();
  }

  function importFromClipboard() {
    if (!navigator.clipboard || !navigator.clipboard.readText) { toast('浏览器不支持读剪贴板，请用文件导入'); return; }
    navigator.clipboard.readText().then(function (t) {
      if (!t) { toast('剪贴板是空的'); return; }
      doImport(t, '剪贴板');
    }, function () { toast('读不到剪贴板，请用文件导入'); });
  }

  function resetAll() {
    var ok = window.confirm(
      '清空本机所有投递记录？\n\n' +
      '状态变更、备注、操作记录、手动录入的岗位都会消失，岗位池回到初始状态。\n' +
      '清空前会自动留一份备份（可在浏览器存储里找回）。\n\n' +
      '建议先「复制数据」备份。'
    );
    if (!ok) return;
    STATE = S.clear();
    commit();
    closeSheet(true);
    toast('已清空本地记录');
  }

  function loadBuiltin() {
    if (!BUILTIN) { toast('这份产物没有内嵌数据'); return; }
    var ok = window.confirm('把内嵌数据合并进本机记录？同岗位以内嵌版本为准。');
    if (!ok) return;
    var r = S.importState(STATE, BUILTIN);
    commit();
    closeSheet(true);
    toast('已合并内嵌数据 ' + r.merged + ' 条');
  }

  /* ---------------------------------------------------------- 渲染 */

  var tab = 'today';
  var VIEWS = { today: viewToday, jobs: viewJobs, pipeline: viewPipeline, letters: viewLetters };

  function render() {
    var c = counts();

    document.querySelectorAll('.tabbar button').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tab === tab);
    });

    var badge = $('#tab-badge');
    if (badge) {
      badge.textContent = c.overdue;
      badge.classList.toggle('hidden', c.overdue <= 0);
    }

    var main = $('#view');
    main.innerHTML = VIEWS[tab]();
    document.title = (c.overdue > 0 ? '(' + c.overdue + ') ' : '') + '求职作战台';
    window.scrollTo(0, 0);
  }

  /* 事件委派：所有按钮只在这一处判断，避免"某个按钮被前面的规则吞掉" */

  function onClick(e) {
    /* --- 关闭类最先判定（面板内部的关闭按钮不能被"点面板不关"规则吞掉） --- */
    if (e.target.closest('button[data-close]')) { e.preventDefault(); closeSheet(); return; }
    if (e.target.closest('.mask') && !e.target.closest('.sheet')) { closeSheet(); return; }

    /* --- 顶栏 --- */
    if (e.target.closest('#btn-add')) { openAddPanel(); return; }
    if (e.target.closest('#btn-more')) { openMorePanel(); return; }

    /* --- Tab --- */
    var tabBtn = e.target.closest('.tabbar button');
    if (tabBtn) { tab = tabBtn.dataset.tab; render(); return; }

    /* --- 筛选 --- */
    var chipTier = e.target.closest('[data-tier]');
    if (chipTier) { jobFilter = chipTier.dataset.tier; render(); return; }
    var chipPipe = e.target.closest('[data-pipe]');
    if (chipPipe) { pipeFilter = chipPipe.dataset.pipe; render(); return; }

    /* --- 今日待办跳转 --- */
    var goto = e.target.closest('[data-goto]');
    if (goto) {
      if (goto.dataset.goto === 'jobs') { jobFilter = 'S'; tab = 'jobs'; render(); }
      return;
    }

    /* --- 详情面板内的操作 --- */
    var stBtn = e.target.closest('[data-set-status]');
    if (stBtn) { e.preventDefault(); applyStatus(stBtn.dataset.setStatusId, stBtn.dataset.setStatus); return; }

    var rBtn = e.target.closest('[data-set-resume]');
    if (rBtn) { e.preventDefault(); applyResume(rBtn.dataset.setResumeId, rBtn.dataset.setResume); return; }

    var undoBtn = e.target.closest('[data-undo]');
    if (undoBtn) { e.preventDefault(); undoLast(undoBtn.dataset.undo); return; }

    var noteBtn = e.target.closest('[data-note-add]');
    if (noteBtn) { e.preventDefault(); addQuickNote(noteBtn.dataset.noteAdd); return; }

    var archBtn = e.target.closest('[data-archive]');
    if (archBtn) { e.preventDefault(); archiveJob(archBtn.dataset.archive); return; }

    var resBtn = e.target.closest('[data-restore]');
    if (resBtn) { e.preventDefault(); restoreJob(resBtn.dataset.restore); return; }

    /* --- 数据面板 --- */
    if (e.target.closest('[data-add-submit]')) { e.preventDefault(); submitAdd(); return; }
    if (e.target.closest('[data-export-copy]')) { e.preventDefault(); copyText(exportText(), '数据已复制到剪贴板'); return; }
    if (e.target.closest('[data-export-file]')) { e.preventDefault(); downloadExport(); return; }
    if (e.target.closest('[data-import-file]')) { e.preventDefault(); importFromFile(); return; }
    if (e.target.closest('[data-import-clip]')) { e.preventDefault(); importFromClipboard(); return; }
    if (e.target.closest('[data-reset]')) { e.preventDefault(); resetAll(); return; }
    if (e.target.closest('[data-load-builtin]')) { e.preventDefault(); loadBuiltin(); return; }

    /* --- 自荐信 --- */
    var copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      e.stopPropagation();
      var cj = findJob(copyBtn.dataset.copy);
      if (cj) copyText(cj.cover_letter, '已复制 ' + shortCo(cj.company) + ' 的自荐信');
      return;
    }
    if (e.target.closest('[data-nogen]')) {
      e.stopPropagation();
      toast('在电脑上运行 python tools/gen_letter.py 即可生成');
      return;
    }

    /* --- 打开详情（放在最后，前面的按钮都已 return） --- */
    if (e.target.closest('[data-detail]')) { openSheet(e.target.closest('[data-id]').dataset.id); return; }
    var hit = e.target.closest('[data-id]');
    if (hit && !e.target.closest('.sheet')) openSheet(hit.dataset.id);
  }

  function bind() {
    document.addEventListener('click', onClick);

    /* 文本框：失焦/值变更即存，避免手机上"写完直接切走"丢数据 */
    document.addEventListener('blur', function (e) {
      if (e.target && e.target.dataset && e.target.dataset.field) saveField(e.target);
    }, true);

    document.addEventListener('change', function (e) {
      if (e.target && e.target.dataset && e.target.dataset.field) saveField(e.target);
    });

    /* 快捷记一笔：回车即提交 */
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeSheet(); return; }
      if (e.key !== 'Enter') return;
      var t = e.target;
      if (t && t.id === 'note-quick') {
        var btn = document.querySelector('[data-note-add]');
        if (btn) { e.preventDefault(); addQuickNote(btn.dataset.noteAdd); }
      }
    });

    /* 页面被切走/关闭前兜底保存 */
    window.addEventListener('beforeunload', function () { flushSheetFields(); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flushSheetFields();
    });
  }

  /* ---------------------------------------------------------- PWA */

  /* 当前 Service Worker 的缓存版本，由 sw.js 回报。
     显示出来的唯一目的是回答一个很实际的问题：
     「我手机上看到的是最新版，还是三天前那份旧缓存？」 */
  var SW_VERSION = '—';

  /* 只有用户点了「刷新」才允许重载。
     为什么必须加这个开关：首次安装时 SW 的 clients.claim() 同样会触发
     controllerchange，无条件 reload 会让页面凭空多加载一次。 */
  var pendingReload = false;

  function isHttp() {
    return /^https?:$/.test(location.protocol);
  }

  function readSwVersion() {
    var ctrl = navigator.serviceWorker && navigator.serviceWorker.controller;
    if (!ctrl || typeof MessageChannel === 'undefined') return;
    try {
      var mc = new MessageChannel();
      mc.port1.onmessage = function (e) {
        if (e.data && e.data.type === 'VERSION') SW_VERSION = String(e.data.version || '—');
      };
      ctrl.postMessage({ type: 'VERSION' }, [mc.port2]);
    } catch (err) {
      /* 拿不到就维持 '—'，不影响任何功能 */
    }
  }

  function showUpdateBar(reg) {
    if (document.querySelector('.updatebar')) return;
    var el = document.createElement('div');
    el.className = 'updatebar';
    el.innerHTML = '<span>看板有新版本</span>' +
      '<button type="button" data-sw-reload="1">刷新</button>' +
      '<button type="button" class="x" data-sw-dismiss="1" aria-label="稍后提醒">✕</button>';

    el.querySelector('[data-sw-reload]').addEventListener('click', function () {
      pendingReload = true;
      if (reg && reg.waiting) reg.waiting.postMessage({ type: 'SKIP_WAITING' });
      else location.reload();
    });
    /* 提示条常驻不自动消失（升级提示一旦自己溜走就再也找不到了），
       但得给一个「稍后」——否则它会一直压着列表最后一张卡。
       关掉只是本次会话不再显示，下次打开仍会提示。 */
    el.querySelector('[data-sw-dismiss]').addEventListener('click', function () {
      el.remove();
      document.body.classList.remove('has-update');
    });

    document.body.appendChild(el);
    /* 补出等高的底部留白，保证内容能滚到提示条上方看全 */
    document.body.classList.add('has-update');
  }

  function initPWA() {
    /* 双击打开（file://）时不注册：Service Worker 要求 http(s) 同源，
       在 file:// 下注册会直接抛错刷满 console；而且单文件本身就自包含，
       file:// 场景压根不缺离线能力。 */
    if (!('serviceWorker' in navigator) || !isHttp()) return;

    /* 只有带 manifest 引用的产物才是 PWA 入口 —— 构建时只给可发布的
       index.html 注入 manifest，演示版/个人版没有。这道判断用来隔开它们：
       同一目录下的 preview-demo.html / local.html 若也去注册 sw.js，
       会与 index.html 争抢同一个 SW scope，导航回退时可能互相顶掉对方的页面。 */
    if (!document.querySelector('link[rel="manifest"]')) return;

    readSwVersion();
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (pendingReload) location.reload();
    });

    window.addEventListener('load', function () {
      navigator.serviceWorker.register('./sw.js').then(function (reg) {
        /* 上次打开时新版本已经下好但没接管 → 直接提示 */
        if (reg.waiting && navigator.serviceWorker.controller) showUpdateBar(reg);

        reg.addEventListener('updatefound', function () {
          var sw = reg.installing || reg.waiting;
          if (!sw) return;

          /* 判断是否该提示，抽成函数是为了能「立刻执行一次」。
             为什么不能只靠 statechange 事件：新 SW 安装可能非常快
             （本地服务、缓存命中的场合尤其明显），等 updatefound 回调跑起来时
             state 已经是 installed，那个 statechange 早错过了 —— 表现就是
             「确实装了新版本，却永远不提示」。所以监听之外还要主动查一次。 */
          function notify() {
            /* 必须是「已有 controller」才算升级。
               首次安装时 controller 还是空的，提示「有新版本」没有意义。 */
            if (sw.state === 'installed' && navigator.serviceWorker.controller) {
              showUpdateBar(reg);
            }
          }

          sw.addEventListener('statechange', notify);
          notify();
        });
      }).catch(function (err) {
        /* 注册失败不影响主体功能 —— 这份 HTML 是自包含的。
           常见原因：非 https、无痕模式、或直接双击打开。 */
        console.warn('[pwa] Service Worker 未注册：' +
          (err && err.message ? err.message : err));
      });
    });
  }

  /* ---------------------------------------------------------- 启动 */

  function boot() {
    var d = $('#today-date');
    if (d) d.textContent = humanDate();
    bind();
    render();
    initPWA();
    /* SW 就绪后补读一次版本（首次打开时 controller 还没挂上） */
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('controllerchange', readSwVersion);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  /* 供端到端测试驱动（不影响正常使用） */
  window.__APP__ = {
    state: function () { return STATE; },
    jobs: function () { return JOBS; },
    exportText: exportText,
    reset: function () { STATE = S.clear(); commit(); },
    /* PWA 状态快照：测试要能在页面里直接问「SW 到底控没控制住」 */
    pwa: function () {
      var sw = navigator.serviceWorker;
      return {
        protocol: location.protocol,
        supported: !!sw,
        controlled: !!(sw && sw.controller),
        version: SW_VERSION,
        updateShown: !!document.querySelector('.updatebar')
      };
    }
  };
})();
