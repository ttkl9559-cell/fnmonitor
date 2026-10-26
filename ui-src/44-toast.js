/* =====================================================================
   TOAST & POPOVER
   ===================================================================== */
const toasts = {};
/**
 * 顶部通知条。tone 决定底色：ok=绿（已保存）、err=红（失败）、plain=墨色（普通信息）。
 * 不传就按 icon 推：alert→错误、info→信息、显式 check 或 key='save'→成功。
 */
function toastTone(opt) {
  if (opt.tone) return opt.tone;
  if (opt.icon === 'alert') return 'err';
  if (opt.icon === 'info') return 'plain';
  if (opt.icon === 'check' || opt.key === 'save') return 'ok';
  return 'plain';
}
function toast(msg, opt = {}) {
  const host = $('#toasts');
  const tone = toastTone(opt);
  const ms = opt.ms || (opt.action ? 5000 : 2000);
  if (opt.key && toasts[opt.key] && toasts[opt.key].isConnected) {
    const t = toasts[opt.key]; t.setAttribute('data-tone', tone);
    setText(t.querySelector('span'), msg); clearTimeout(t._tm); t._tm = setTimeout(() => dismiss(t), ms); return t;
  }
  const t = mount(`<div class="toast" role="status" data-tone="${tone}">${ic(opt.icon || 'check', opt.icon && opt.icon !== 'check' ? '' : 'ok')}<span>${esc(msg)}</span>${opt.action ? `<button type="button">${esc(opt.action)}</button>` : ''}</div>`);
  if (opt.action) t.querySelector('button').onclick = () => { dismiss(t); opt.onAction && opt.onAction(); };
  host.append(t); if (opt.key) toasts[opt.key] = t;
  t._tm = setTimeout(() => dismiss(t), ms);
  return t;
}
function dismiss(t) { if (!t.isConnected) return; t.classList.add('out'); setTimeout(() => t.remove(), 260); }

let pop = null;
function closePop() { if (pop) { pop.btn.setAttribute('aria-expanded', 'false'); pop.el.remove(); pop = null; } }
function openPop(btn, title, text) {
  if (pop && pop.btn === btn) return closePop();
  closePop();
  const el = mount(`<div class="popover" role="tooltip"><b>${esc(title)}</b>${esc(text)}</div>`);
  document.body.append(el);
  const r = btn.getBoundingClientRect(), w = 280;
  el.style.left = clamp(r.left + r.width / 2 - w / 2 + scrollX, 12 + scrollX, scrollX + innerWidth - w - 12) + 'px';
  el.style.top = (r.bottom + 10 + scrollY) + 'px';
  btn.setAttribute('aria-expanded', 'true');
  pop = { el, btn };
}
document.addEventListener('click', e => { if (pop && !pop.el.contains(e.target) && !pop.btn.contains(e.target)) closePop(); });

