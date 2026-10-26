/* =====================================================================
   UPS 电源 — 独立「电源」页（设置里启用后才出现）
   数据源：/api/ups（采集线程）、/api/ups/events、/api/ups/guard
   ===================================================================== */
const fVolt = v => [F.d1(v), 'V'];
const fMin = v => [Math.round((+v || 0) / 60), '分'];
const UPS_CHARTS = [
  { id: 'lo', title: '负载与电量', yMin: 0, yMax: 100, yTick: v => v + '%', peak: true,
    series: [{ m: 'ups_load', label: '负载', color: M.cpu, fmt: fPct }, { m: 'ups_chg', label: '电量', color: M.mem, fmt: fPct }] },
  { id: 'vv', title: '电压', yMin: 0, yTick: v => v + 'V',
    series: [{ m: 'ups_in_v', label: '输入', color: M.io, fmt: fVolt }, { m: 'ups_out_v', label: '输出', color: M.io2, fmt: fVolt }, { m: 'ups_bat_v', label: '电池', color: M.temp2, fmt: fVolt }] },
  { id: 'rt', title: '剩余续航', yMin: 0, yTick: v => v + '分',
    series: [{ m: 'ups_rt', label: '续航', color: M.pwr, fmt: fMin }] },
  { id: 'tp', title: '电池温度', yTick: v => v + '°',
    series: [{ m: 'ups_temp', label: '电池', color: M.temp, fmt: fDeg }] },
];
const UPS_REASON = {
  off: ['未启用 UPS 监控', '到 设置 → UPS 电源 打开开关；启用后这里会出现实时读数、曲线与事件。'],
  init: ['正在读取 UPS…', '采集线程已启动，等第一帧数据（约 5 秒）。'],
  no_nut: ['未检测到 NUT', '系统没装 NUT 或 nut-server 没运行。飞牛「设置 → 硬件与电源 → UPS」启用后即有。'],
  no_device: ['没有发现 UPS', 'NUT 在运行，但没读到设备。检查 UPS 的 USB 线是否接在 NAS 上。'],
  stale: ['UPS 通信中断', 'UPS 驱动与设备失去联系（data stale）。下面的读数是最后有效的一帧。'],
  error: ['读取失败', 'upsc 返回错误，详情见设置页。'],
};
const upsCfg = () => (S.ups && S.ups.config) || null;
const upsEnabled = () => !!(S.ups && +((S.ups.config || {}).enabled || S.ups.enabled));
const upsLive = () => (S.ups && S.ups.live) || {};
const upsReason = st => UPS_REASON[st] || null;

/** 实时值进环形缓冲：历史 60 秒一条，最后一段靠它补齐，否则曲线比读数慢一分钟 */
function feedUps(j) {
  const ts = +j.ts || 0;
  if (!ts || ts === S._upsTs) return;
  S._upsTs = ts;
  const lv = j.live || {};
  const P = (k, v) => { if (ok(v)) push(S.live[k], [ts, +v]); };
  P('upsLoad', lv.load); P('upsChg', lv.chg); P('upsRt', lv.rt);
  P('upsInV', lv.in_v); P('upsOutV', lv.out_v); P('upsBatV', lv.bat_v); P('upsTemp', lv.temp);
}

const upsKpi = (ref, label, unit) => `<div class="stat-cell"><div class="eyebrow">${label}</div>
  <div class="dnum"><span class="dm" data-ref="${ref}"></span>${unit ? `<span class="unit">${unit}</span>` : ''}</div>
  <div class="sub" data-ref="${ref}T"></div></div>`;
const upsKV = rows => `<dl class="kv">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>`;

/* ---------------- 概览 ---------------- */
defineModule({
  id: 'upsov', page: 'ups', kicker: 'POWER', title: 'UPS 概览', big: true,
  dek: '在线状态、关键读数与电池健康',
  build(body, m) {
    body.innerHTML = `<div data-ref="banner"></div>
      <div class="stat-cells dk-sum">
        ${upsKpi('kLoad', '负载', '%')}${upsKpi('kChg', '电池电量', '%')}${upsKpi('kRt', '剩余续航', '')}
        ${upsKpi('kBatV', '电池电压', 'V')}${upsKpi('kInV', '输入电压', 'V')}${upsKpi('kOutV', '输出电压', 'V')}
      </div>
      <div class="grid g-2">
        <article class="card pad"><div class="eyebrow">电池健康</div><div class="dnum up-score"><span class="dm" data-ref="score"></span><span class="unit">分</span></div>
          <div class="sub" data-ref="verdict"></div><p class="up-basis" data-ref="basis"></p><ul class="up-notes" data-ref="notes"></ul></article>
        <article class="card pad"><div class="eyebrow">设备</div><div data-ref="dev"></div></article>
      </div>`;
    m.x = collect(body);
  },
  update(o, m) {
    const x = m.x, j = S.ups || {}, lv = upsLive(), st = j.state || (upsEnabled() ? 'init' : 'off');
    const why = upsReason(st);
    if (!upsEnabled()) { setHTML(x.banner, `<div class="note info">${ic('battery')}<span>${esc(UPS_REASON.off[0])}。${esc(UPS_REASON.off[1])}</span></div>`); }
    else if (why && st !== 'ok') {
      setHTML(x.banner, `<div class="note ${st === 'stale' ? 'warn' : 'warn'}">${ic('alert')}<span><b>${esc(why[0])}</b>　${esc(j.error || why[1])}</span></div>`);
    } else if (lv.labels && lv.labels.length) {
      setHTML(x.banner, `<div class="ups-chips">${(lv.labels || []).map(l => `<span class="pill ${/低电|超载|告警/.test(l) ? 'crit' : /电池供电/.test(l) ? 'warn' : 'ok'}"><span class="dot"></span>${esc(l)}</span>`).join('')}</div>`);
    } else setHTML(x.banner, '');
    dnum(x.kLoad, lv.load); dnum(x.kChg, lv.chg);
    dnum(x.kRt, lv.rt == null ? null : Math.round(lv.rt / 60));
    setText(x.kRtT, lv.rt == null ? '' : F.dur(lv.rt));
    dnum(x.kBatV, lv.bat_v, F.d1); dnum(x.kInV, lv.in_v, F.d1); dnum(x.kOutV, lv.out_v, F.d1);
    setText(x.kLoadT, lv.rp ? F.int(lv.rp) + ' W' : '');
    setText(x.kChgT, lv.rp && lv.chg ? '负载 ' + F.int(lv.rp) + ' W' : '');
    setText(x.kInVT, lv.in_hz ? F.d1(lv.in_hz) + ' Hz' : '');
    setText(x.kOutVT, lv.out_hz ? F.d1(lv.out_hz) + ' Hz' : '');
    setText(x.kBatVT, lv.status ? lv.status : '');
    const h = j.health || {}, d = j.device || {};
    dnum(x.score, h.score);
    setText(x.verdict, [h.grade_cn, h.capacity_pct != null ? `实测中位期望 ${F.int(h.capacity_pct)}%` : ''].filter(Boolean).join(' · ') || '数据不足');
    setText(x.basis, h.basis || '');
    setHTML(x.notes, (h.notes || []).map(t => `<li>${esc(t)}</li>`).join(''));
    setHTML(x.dev, upsKV([
      ['型号', dash([d.mfr, d.model].filter(Boolean).join(' ') || '')],
      ['序列号', `<span class="mono">${dash(d.serial)}</span>`],
      ['驱动', `<span class="mono">${dash([d.driver, d.driver_data].filter(Boolean).join(' '))}</span>`],
      ['NUT 版本', `<span class="mono">${dash(d.nut)}</span>`],
      ['额定容量', lv.nominal_va ? F.int(lv.nominal_va) + ' VA' : dash(null)],
      ['电源来源', dash(lv.source)],
      ['数据新鲜度', j.age_s != null ? (j.age_s <= 2 ? '实时' : j.age_s + ' 秒前') : '—'],
    ]));
  },
});

/* ---------------- 曲线 ---------------- */
defineModule({
  id: 'upstrend', page: 'ups', kicker: 'TRENDS', title: '曲线', dek: '负载、电量、电压、续航与温度的历史，指针悬停可对齐四张图',
  actions: m => (m.seg = makeSeg(RANGES, UI.range, v => { UI.range = v; saveUI(); m.load(); }, 'sm')),
  build(body, m) {
    body.innerHTML = `<div class="grid g-2">${UPS_CHARTS.map(d => `<div class="card chart-card"><div class="chart-head"><span class="chart-title">${d.title}</span><div class="chart-legend" data-ref="leg-${d.id}"></div></div><div data-ref="ch-${d.id}"></div></div>`).join('')}</div><p class="hist-status faint" data-ref="hs"></p>`;
    const x = collect(body); m.x = x;
    m.charts = UPS_CHARTS.map(d => ({ d, c: new LineChart(x['ch-' + d.id], chartOpt(d, { group: 'ups', legend: x['leg-' + d.id] })) }));
    m.keys = [...new Set(UPS_CHARTS.flatMap(d => d.series.map(s => s.m)))];
    m.load = async () => {
      const range = UI.range;
      try { m.res = await fetchSeries(m.keys, range); } catch (e) { m.res = {}; }
      if (range !== UI.range) return;
      m.range = range; m.draw(true);
    };
    m.draw = force => {
      if (!m.res) return;
      if (!force && !['10m', '1h', '6h'].includes(m.range)) return;
      m.charts.forEach(({ d, c }) => c.setData(d.series.map(se => seriesOf(m.res, se.m, m.range)), RANGE_SEC[m.range]));
      const n = m.keys.reduce((a, k) => a + seriesOf(m.res, k, m.range).length, 0);
      setText(x.hs, n ? `${RANGE_TXT[m.range]} · 每 ${histIv()} 秒记录一条 · ${n} 个采样点` : `暂无历史数据 · 启用 UPS 后后台每 ${histIv()} 秒记录一条`);
    };
    if (S.upsTab) m.load();          // 没启用 UPS 就别白拉 7 条历史
  },
  update(o, m) { if (!m.res && S.upsTab) m.load(); else m.draw(false); },
});

/* ---------------- 事件 ---------------- */
defineModule({
  id: 'upsevents', page: 'ups', kicker: 'EVENTS', title: '事件', dek: '停电、告警与通信状态变化',
  actions: m => {
    const b = mount(`<button class="btn sm" type="button">${ic('refresh')}刷新</button>`);
    b.onclick = () => Poll.run('upsev');
    return b;
  },
  build(body, m) {
    body.innerHTML = `<div class="card pad up-ev" data-ref="list"></div>`;
    m.x = collect(body);
  },
  update(o, m) {
    const evs = S.upsev || [];
    if (!evs.length) { setHTML(m.x.list, emptyHTML('暂无事件', '市电中断、低电量、通信中断等变化会记在这里', 'battery')); return; }
    setHTML(m.x.list, evs.map(e => `<div class="ev-row" data-lv="${esc(e.level || 'info')}">
        <span class="ev-ts">${esc(F.mdhm(e.ts))}</span>
        <span class="ev-tag">${esc(e.label || e.type || '')}</span>
        <span class="ev-note">${esc(e.note || '')}</span>
        <span class="ev-meta">${[e.chg != null ? F.int(e.chg) + '%' : '', e.rt ? Math.round(e.rt / 60) + ' 分' : ''].filter(Boolean).join(' · ')}</span>
      </div>`).join(''));
  },
});

/* ---------------- 关机保护条（页面级常驻，不进版块列表） ---------------- */
const GUARD_TONE = { armed: 'crit', alerted: 'crit', fired: 'crit', held: 'warn', blocked: 'warn', unknown: 'warn', idle: '', watching: '' };
function renderUpsGuard() {
  const host = $('#upsGuard'); if (!host) return;
  const g = S.upsGuard || {};
  const st = g.state || 'idle';
  if (!g.enabled) { setHTML(host, ''); return; }
  const left = g.left_s, note = g.note || (g.state_cn || '');
  const tone = GUARD_TONE[st] == null ? '' : GUARD_TONE[st];
  const txt = {
    idle: '关机保护已就绪 · 市电正常', held: `关机保护 · 服务启动静默期（还剩 ${g.boot_hold_left || 0} 秒）`,
    watching: `电池供电中 · 断电 ${F.dur(g.on_batt_s || 0)}`, armed: '待执行关机', alerted: '已告警（只告警模式，不会关机）',
    fired: '已下发关机命令', blocked: '已阻止自动关机',
  }[st] || g.state_cn || st;
  setHTML(host, `<div class="ups-guard" data-state="${esc(st)}" data-tone="${esc(tone)}">
      <span class="ug-dot"></span><b>${esc(txt)}</b>
      ${note && note !== txt ? `<span class="ug-note">${esc(note)}</span>` : ''}
      ${left != null ? `<span class="ug-count tnum">${F.p2(Math.floor(left / 60))}:${F.p2(left % 60)}</span>` : ''}
      ${g.time_left_s != null && st === 'watching' ? `<span class="ug-note">距时间判据还有 ${F.dur(g.time_left_s)}</span>` : ''}
      ${st === 'armed' || st === 'alerted' ? `<button class="btn danger sm" type="button" data-act="cancel">取消关机</button>` : ''}
      <button class="link-btn" type="button" data-goset="ups">配置</button>
    </div>`);
  const cbtn = $('[data-act="cancel"]', host);
  if (cbtn) cbtn.onclick = async () => {
    try {
      const j = await API.post('/api/ups/guard/cancel', {});
      toast(j.message || (j.ok ? '已取消' : '取消失败'), { key: 'guard', icon: j.ok ? 'check' : 'alert' });
      Poll.run('upsguard');
    } catch (e) { toast('取消失败：' + e.message, { icon: 'alert' }); }
  };
  const gl = $('[data-goset]', host);
  if (gl) gl.onclick = () => { go('settings'); setTimeout(() => focusGroup('ups'), 60); };
}

/* ---------------- 设置 → UPS 电源 ---------------- */
const UPS_ROW = (label, hint, ctl, attr = '') => `<div class="set-row inline" ${attr}><div><div class="lbl">${label}</div>${hint ? `<div class="hint">${hint}</div>` : ''}</div><div class="ctl">${ctl}</div></div>`;
const upsNum = (k, lo, hi, step, unit, attr = 'data-dep') => `<label class="numfield" ${attr}><input type="number" data-k="${k}" min="${lo}" max="${hi}" step="${step}" aria-label="${k}"><span class="u">${unit}</span></label>`;
const upsText = (k, ph) => `<input class="textfield mono" type="text" data-k="${k}" placeholder="${ph}" aria-label="${k}">`;

function renderUps(card) {
  const EV = [
    ['POWER_LOSS', '市电中断'], ['POWER_RESTORE', '市电恢复'], ['LOW_BATTERY', '电量过低'],
    ['REPLACE_BATTERY', '需换电池'], ['ALARM', 'UPS 告警'], ['OVERLOAD', '输出过载'],
    ['FORCED_SHUTDOWN', '强制关机'], ['DEVICE_LOST', '通信中断'], ['DEVICE_BACK', '通信恢复'],
  ];
  card.innerHTML = `
    ${UPS_ROW('启用 UPS 监控', '关闭后不读取 UPS、不记录历史、不发通知，「电源」页与导航一起隐藏，下面这些设置也一起收起来', `<button class="switch" type="button" role="switch" aria-checked="false" data-k="enabled" aria-label="启用 UPS 监控"></button>`)}
    <div data-dep-body>
    <div class="set-sub">采集</div>
    ${UPS_ROW('UPS 设备名', '留空 = 自动发现（NUT 里有多台时才需要指定）', upsText('ups_name', '自动发现'), 'data-dep')}
    ${UPS_ROW('采集周期', '默认 5 秒。NUT 驱动自身也有 2 秒轮询，设太小只是多花 CPU', upsNum('poll_interval', 3, 60, 1, '秒'), 'data-dep')}
    <div class="set-sub">电池与健康评估</div>
    ${UPS_ROW('电池容量', '铭牌 Ah（如 12V 9Ah 填 9）。填错会让健康判定整体偏移', upsNum('battery_ah', 0.5, 200, 0.5, 'Ah'), 'data-dep')}
    ${UPS_ROW('允许放电深度', '铅酸一般 80~85%，深循环可到 100%', upsNum('battery_dod', 30, 100, 1, '%'), 'data-dep')}
    <div class="set-sub">告警（走 设置 → 通知 的渠道）</div>
    <div class="set-row" data-dep><div><div class="lbl">通知方式</div><div class="hint">事件发到哪几个通道；都不选就不发，事件仍记录在「事件记录」里</div></div><div class="ctl" data-ref="route"></div></div>
    <div class="set-row block" data-dep><div><div class="lbl">告警事件</div><div class="hint">「严重」级别不受静默时段约束</div></div>
      <div class="ctl up-evs" data-ref="evs">${EV.map(([k, l]) => `<button class="btn sm" type="button" data-ev="${k}" aria-pressed="false">${l}</button>`).join('')}</div></div>
    ${UPS_ROW('同类事件去抖', '同一类事件在该窗口内只发一次', upsNum('notify_debounce_s', 0, 3600, 10, '秒'), 'data-dep')}
    ${UPS_ROW('每分钟上限', '避免反复抖动把邮箱打爆', upsNum('notify_max_per_min', 1, 60, 1, '条'), 'data-dep')}
    ${UPS_ROW('静默时段', '形如 23:00-07:00（可跨零点），留空不静默；严重告警照发', upsText('notify_quiet', '留空不静默'), 'data-dep')}
    ${UPS_ROW('测试通知', '按当前通知渠道发一条 UPS 样例告警', `<button class="btn sm" type="button" data-act="testnotify">${ic('bell')}发送测试</button>`, 'data-dep')}
    <div class="set-sub">关机保护（高危，默认关闭）</div>
    <div class="set-row block" data-dep><div><div class="lbl">冲突检测</div><div class="hint">飞牛侧的 UPS 保护还开着时，两边会各关一次</div></div><div class="ctl up-conf" data-ref="conf"></div></div>
    ${UPS_ROW('启用关机保护', '断电超时或电池电压见底时告警 / 关机；本应用只关 NAS，不断 UPS 输出', `<button class="switch" type="button" role="switch" aria-checked="false" data-k="guard_enabled" aria-label="启用关机保护"></button>`, 'data-dep')}
    ${UPS_ROW('模式', '「只告警」不会执行任何命令', `<span data-ref="mode"></span>`, 'data-dep')}
    ${UPS_ROW('断电持续分钟', '判据①：市电断开超过该时长仍未恢复', upsNum('guard_time_min', 1, 120, 1, '分钟', 'data-gdep'))}
    ${UPS_ROW('电池电压下限', `判据②：电池组电压低于该值立即触发。硬下限 ${21.4} V，填更低会被抬回 21.4`, upsNum('guard_low_v', 21.4, 30, 0.1, 'V', 'data-gdep'))}
    ${UPS_ROW('宽限期', '触发后仍有这段时间可取消 / 等来电', upsNum('guard_grace_s', 10, 600, 5, '秒', 'data-gdep'))}
    ${UPS_ROW('连续确认期', '必须在电池上连续这么久才认，防切换瞬间抖动', upsNum('guard_min_ob_s', 10, 600, 5, '秒', 'data-gdep'))}
    ${UPS_ROW('启动静默期', '服务启动后这段时间内不动手，防重启循环关机', upsNum('guard_boot_hold_s', 0, 3600, 10, '秒', 'data-gdep'))}
    ${UPS_ROW('我已知冲突风险', '仅在冲突检测为「有冲突」时需要勾上，否则真关机保存会被拒绝', `<button class="switch" type="button" role="switch" aria-checked="false" data-k="guard_allow_conflict" aria-label="已知冲突风险"></button>`, 'data-gdep')}
    ${UPS_ROW('演练与取消', '演练只跑判定逻辑，绝不关机；取消只对「待执行」有效', `<span class="up-acts"><button class="btn sm" type="button" data-act="sim">${ic('play')}演练判定</button><button class="btn sm" type="button" data-act="cancelnow">取消待执行</button></span>`, 'data-gdep')}
    <p class="sub up-warn" data-gdep>${ic('alert')}真关机不可撤销：只在下拉确认后使用。运行前请先确认飞牛侧 UPS 保护已关闭，并核对电压下限与断电时长。</p>
    </div>`;

  const x = collect(card);
  const cfg = () => (S.ups && S.ups.config) || {};
  // 事件勾选的本地镜像：点击立刻翻转视觉，不等 POST 回来；连点也不会因为读到
  // 旧配置而互相打架。保存失败时 _sync() 会按服务端真值整组重画。
  let evSel = new Set(cfg().notify_events || []);
  const paintEvs = () => $$('[data-ev]', card).forEach(b => b.setAttribute('aria-pressed', String(evSel.has(b.dataset.ev))));
  const save = async (patch, label) => {
    try {
      const j = await API.post('/api/ups/save', patch);
      if (j.config) S.ups = Object.assign({}, S.ups || {}, { config: j.config });
      if (!j.ok) throw new Error(j.error || '未知错误');
      toast('已保存 · ' + label, { key: 'save' });
      Poll.run('ups'); Poll.run('upsguard');
      card._sync();
      return true;
    } catch (e) {
      toast('保存失败 · ' + label + '：' + String(e.message).slice(0, 90), { icon: 'alert', ms: 6000 });
      card._sync();
      return false;
    }
  };
  // 通知方式：本地镜像 + 立刻保存。为空表示这个功能不发通知（notify_enabled 一起归零）
  let upsCh = null;
  const upsRoute = () => {
    if (upsCh !== null) return upsCh;
    const c = cfg();
    const on = c.notify_enabled === undefined ? true : !!+c.notify_enabled;
    return on ? notifyRouteGet('ups') : [];
  };
  const routeEl = notifyRouteCtl(upsRoute, v => {
    upsCh = v;
    save({ notify_channels: v, notify_enabled: v.length ? 1 : 0 }, '通知方式').then(ok => {
      if (!ok) upsCh = null;
      card._sync();
    });
  });
  if (x.route) x.route.append(routeEl);
  const readK = el => {
    if (el.classList.contains('switch')) return el.getAttribute('aria-checked') === 'true' ? 1 : 0;
    if (el.dataset.k === 'notify_quiet' || el.dataset.k === 'ups_name') return el.value.trim();
    const v = +el.value;
    if (!isFinite(v)) return 0;
    // 界面按百分数填，配置里存 0.3~1.0
    return el.dataset.k === 'battery_dod' ? Math.max(30, Math.min(100, v)) / 100 : v;
  };
  const labelOf = el => (el.dataset.k === 'guard_low_v' ? '电池电压下限' : el.dataset.k === 'guard_time_min' ? '断电持续分钟'
    : el.dataset.k === 'notify_quiet' ? '静默时段' : el.dataset.k === 'ups_name' ? 'UPS 设备名'
    : el.dataset.k === 'poll_interval' ? '采集周期' : el.dataset.k === 'battery_ah' ? '电池容量'
    : el.dataset.k === 'battery_dod' ? '允许放电深度' : el.dataset.k === 'notify_debounce_s' ? '同类事件去抖'
    : el.dataset.k === 'notify_max_per_min' ? '每分钟上限' : el.dataset.k === 'guard_grace_s' ? '宽限期'
    : el.dataset.k === 'guard_min_ob_s' ? '连续确认期' : el.dataset.k === 'guard_boot_hold_s' ? '启动静默期'
    : el.dataset.k === 'guard_allow_conflict' ? '已知冲突风险' : el.dataset.k === 'guard_enabled' ? '关机保护'
    : el.dataset.k === 'notify_enabled' ? '告警开关' : el.dataset.k);

  x.mode = makeSeg([['notify', '只告警'], ['shutdown', '真关机']], (cfg().guard_mode || 'notify'),
    v => save({ guard_mode: v }, '模式'), 'sm');
  $('[data-ref="mode"]', card).append(x.mode);

  card.addEventListener('click', async e => {
    const sw = e.target.closest('.switch[data-k]');
    if (sw) {
      const on = sw.getAttribute('aria-checked') !== 'true';
      if (sw.dataset.k === 'guard_enabled' && on && !confirm('关机保护会真的执行关机命令（模式选「真关机」时）。确认开启？')) return;
      sw.setAttribute('aria-checked', String(on));
      if (sw.dataset.k === 'enabled' && on) toast('正在读取 UPS…', { key: 'upson', ms: 1800 });
      return save({ [sw.dataset.k]: on ? 1 : 0 }, labelOf(sw));
    }
    const ev = e.target.closest('[data-ev]');
    if (ev) {
      const code = ev.dataset.ev;
      evSel.has(code) ? evSel.delete(code) : evSel.add(code);
      paintEvs();
      return save({ notify_events: [...evSel] }, '告警事件');
    }
    const act = e.target.closest('[data-act]');
    if (act) {
      const a = act.dataset.act;
      if (a === 'testnotify') {
        act.disabled = true;
        try { const j = await API.post('/api/ups/notify/test', {}); toast(j.ok ? '测试通知已发送' : ('发送失败：' + (j.error || '')), { icon: j.ok ? 'check' : 'alert', ms: j.ok ? 2400 : 6000 }); }
        catch (err) { toast('发送失败：' + err.message, { icon: 'alert' }); }
        act.disabled = false; return;
      }
      if (a === 'sim') {
        try {
          const j = await API.post('/api/ups/guard/sim', {});
          toast(`演练：${j.note}`, { icon: 'info', ms: 9000, key: 'sim' });
        } catch (err) { toast('演练失败：' + err.message, { icon: 'alert' }); }
        return;
      }
      if (a === 'cancelnow') {
        try { const j = await API.post('/api/ups/guard/cancel', {}); toast(j.message || '已取消', { key: 'guard' }); Poll.run('upsguard'); }
        catch (err) { toast('取消失败：' + err.message, { icon: 'alert' }); }
      }
    }
  });
  card.addEventListener('change', e => {
    const el = e.target.closest('[data-k]');
    if (!el || el.classList.contains('switch')) return;
    save({ [el.dataset.k]: readK(el) }, labelOf(el));
  });

  card._sync = () => {
    const c = cfg(), on = !!+c.enabled, g = on && !!+c.guard_enabled;
    // 总开关关闭 → 整块（采集 / 告警 / 关机保护）一起收起，只留上面那一行；
    // 用容器而不是逐行标记，以后往卡片里加行不会再漏掉。
    const body = $('[data-dep-body]', card);
    if (body) body.hidden = !on;
    $$('[data-dep]', card).forEach(el => { el.hidden = !on; });
    $$('[data-gdep]', card).forEach(el => { el.hidden = !g; });
    $$('.set-sub', card).forEach(el => { el.hidden = !on; });
    if (upsCh === null) routeEl.sync();
    $$('[data-k]', card).forEach(el => {
      const k = el.dataset.k, v = c[k];
      if (el.classList.contains('switch')) el.setAttribute('aria-checked', String(!!+v));
      else if (k === 'notify_quiet' || k === 'ups_name') { if (document.activeElement !== el) el.value = v || ''; }
      else if (document.activeElement !== el) el.value = (k === 'battery_dod') ? Math.round((+v || 0) * 100) : (v == null ? '' : v);
    });
    const evs = new Set(c.notify_events || []);
    evSel = evs;
    paintEvs();
    if (x.mode && x.mode.set) x.mode.set(c.guard_mode || 'notify');
    const conf = (S.upsGuard && S.upsGuard.conflicts) || {};
    if (conf.risk) setHTML(x.conf, `<span class="pill ${conf.risk === 'high' ? 'crit' : conf.risk === 'medium' ? 'warn' : 'ok'}">${esc({ high: '有冲突', medium: '需确认', none: '无冲突' }[conf.risk] || conf.risk)}</span><span class="ug-note">${esc(conf.verdict_cn || '')}</span>${(conf.items || []).filter(i => i.level !== 'info').map(i => `<div class="ug-note">· ${esc(i.title)}：${esc(i.fix || '')}</div>`).join('')}`);
    else setHTML(x.conf, '<span class="ug-note">尚未检测</span>');
  };
  card._sync();
  Poll.run('upsguard');
}
