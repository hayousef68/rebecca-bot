--- index.html.orig	2026-11-08 18:22:18.727283628 +0000
+++ index.html	2026-11-08 18:22:34.217672046 +0000
@@ -38,6 +38,16 @@
 nav button.on{color:var(--ac);background:linear-gradient(180deg,color-mix(in srgb,var(--ac) 17%,transparent),color-mix(in srgb,var(--ac) 6%,transparent))}
 nav button.on svg{transform:translateY(-1px) scale(1.08)}
 nav button.on::after{content:"";position:absolute;bottom:2px;width:16px;height:3px;border-radius:3px;background:var(--ac)}
+/* منوی کشویی «مدیریت» (تمپلت / فروشگاه) */
+#navmenu{position:fixed;z-index:9;min-width:168px;background:color-mix(in srgb,var(--s1) 94%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:6px;display:flex;flex-direction:column;gap:2px;animation:nmin .14s ease-out}
+#navmenu button{position:relative;display:flex;align-items:center;gap:10px;width:100%;background:none;border:0;color:var(--tx);font:inherit;font-size:var(--fs-sm);font-weight:600;padding:11px 14px;border-radius:13px;cursor:pointer;text-align:start}
+#navmenu button svg{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
+#navmenu button:hover{background:var(--s2)}
+#navmenu button.on{color:var(--ac);background:color-mix(in srgb,var(--ac) 12%,transparent)}
+#navmenu .nbadge{position:static;margin-inline-start:auto}
+@keyframes nmin{from{opacity:0;transform:translateY(6px) scale(.97)}to{opacity:1;transform:none}}
+@media (prefers-reduced-motion:reduce){#navmenu{animation:none}}
+
 :focus-visible{outline:2px solid var(--ac);outline-offset:2px}
 .card{background:linear-gradient(180deg,var(--s1),var(--s0));border:1px solid var(--line);border-radius:var(--r);padding:15px;margin-bottom:12px;box-shadow:0 2px 10px #0004}
 .card h2{font-size:var(--fs-sm);font-weight:800;margin-bottom:12px;color:var(--tx);display:flex;align-items:center;gap:8px}
@@ -491,6 +501,7 @@
 
 const S = { st: null, panel: null, tab: 'create', users: { q: '', status: '', off: 0 }, last: null };
 const ICONS = {   // آیکون‌های خطی (ثابت و داخلی)
+  more: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.8"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.8"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.8"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.8"/>',
   wallet: '<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M3 10h18M16 14.5h2"/>',
   create: '<path d="M12 5v14M5 12h14"/>',
   users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/><path d="M16 5.2a3 3 0 0 1 0 5.6M18 14.8c1.8.7 3 2.4 3 5.2"/>',
@@ -500,13 +511,54 @@
   set: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8"/>'
 };
 const svgIcon = k => { const w = document.createElement('span'); w.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[k] || ''}</svg>`; return w.firstChild; };
-const TABS = [['create', 'create', 'ساخت'], ['users', 'users', 'کاربران'], ['home', 'home', 'خانه'], ['tpl', 'tpl', 'تمپلت'], ['shop', 'shop', 'فروشگاه'], ['set', 'set', 'تنظیمات']];
+const TABS = [['create', 'create', 'ساخت'], ['users', 'users', 'کاربران'], ['home', 'home', 'خانه'],
+  ['grp', 'more', 'مدیریت', [['tpl', 'tpl', 'تمپلت'], ['shop', 'shop', 'فروشگاه']]],      // منوی کشویی: تمپلت و فروشگاه
+  ['set', 'set', 'تنظیمات']];
+
+const navBadge = n => h('i', { class: 'nbadge' }, n > 99 ? '99+' : String(n));
+const grpOf = k => TABS.find(t => t[3] && t[3].some(x => x[0] === k));
+let _menuBtn = null;
+
+function closeMenu() {
+  const m = $('#navmenu'); if (m) m.remove();
+  if (_menuBtn) _menuBtn.setAttribute('aria-expanded', 'false');
+  document.removeEventListener('pointerdown', _menuOut, true); document.removeEventListener('keydown', _menuKey, true);
+}
+const _menuOut = e => { if (!e.target.closest('#navmenu') && !e.target.closest('#nav button[data-grp]')) closeMenu(); };
+const _menuKey = e => { if (e.key === 'Escape') { closeMenu(); if (_menuBtn) _menuBtn.focus(); } };
+function openMenu(btn, items) {
+  closeMenu(); _menuBtn = btn; btn.setAttribute('aria-expanded', 'true');
+  const m = h('div', { id: 'navmenu', role: 'menu' }, items.map(([k, ic, t]) =>
+    h('button', { class: S.tab === k ? 'on' : '', role: 'menuitem', type: 'button', onclick: () => { closeMenu(); go(k); } }, svgIcon(ic), t,
+      k === 'shop' && S.shopPend ? navBadge(S.shopPend) : null)));
+  document.body.appendChild(m);
+  const r = btn.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight, W = innerWidth, H = innerHeight;
+  if (document.documentElement.classList.contains('desk')) {                       // دسکتاپ: کنار دکمه‌ی عمودی
+    m.style.top = Math.max(8, Math.min(r.top, H - mh - 8)) + 'px';
+    if (r.left > W / 2) m.style.right = (W - r.left + 8) + 'px'; else m.style.left = (r.right + 8) + 'px';
+  } else {                                                                         // موبایل: بالای نوار پایین
+    m.style.bottom = (H - r.top + 8) + 'px';
+    m.style.left = Math.max(8, Math.min(r.left + r.width / 2 - mw / 2, W - mw - 8)) + 'px';
+  }
+  document.addEventListener('pointerdown', _menuOut, true); document.addEventListener('keydown', _menuKey, true);
+  const first = m.querySelector('button.on') || m.querySelector('button'); if (first) first.focus();
+}
 
 function renderNav() {
-  const nav = $('#nav'); nav.style.setProperty('--n', TABS.length); nav.replaceChildren(...TABS.map(([k, ic, t]) =>
-    h('button', { class: S.tab === k ? 'on' : '', onclick: () => go(k), 'aria-label': t + (k === 'set' && S.pend ? ` (${S.pend} درخواست)` : '') + (k === 'shop' && S.shopPend ? ` (${S.shopPend} مورد)` : '') }, svgIcon(ic), t,
-      k === 'set' && S.pend ? h('i', { class: 'nbadge' }, S.pend > 99 ? '99+' : String(S.pend)) : null,
-      k === 'shop' && S.shopPend ? h('i', { class: 'nbadge' }, S.shopPend > 99 ? '99+' : String(S.shopPend)) : null)));
+  const nav = $('#nav'); nav.style.setProperty('--n', TABS.length);
+  const wasOpen = !!$('#navmenu'); if (wasOpen) closeMenu();
+  nav.replaceChildren(...TABS.map(([k, ic, t, sub]) => {
+    if (sub) {
+      const on = sub.some(x => x[0] === S.tab), badge = sub.some(x => x[0] === 'shop') && S.shopPend;
+      const b = h('button', { class: on ? 'on' : '', type: 'button', 'data-grp': k, 'aria-haspopup': 'menu', 'aria-expanded': 'false',
+        'aria-label': t + (badge ? ` (${S.shopPend} مورد)` : ''), onclick: () => { if ($('#navmenu')) closeMenu(); else openMenu(b, sub); } },
+        svgIcon(ic), on ? (sub.find(x => x[0] === S.tab) || [])[2] : t, badge ? navBadge(S.shopPend) : null);
+      return b;
+    }
+    return h('button', { class: S.tab === k ? 'on' : '', onclick: () => { closeMenu(); go(k); }, 'aria-label': t + (k === 'set' && S.pend ? ` (${S.pend} درخواست)` : '') }, svgIcon(ic), t,
+      k === 'set' && S.pend ? navBadge(S.pend) : null);
+  }));
+  if (wasOpen) { const gb = nav.querySelector('button[data-grp]'); const sub = (grpOf('tpl') || [])[3]; if (gb && sub) openMenu(gb, sub); }   // با تازه‌شدن نشان‌ها باز بماند
 }
 /* درخواست‌های افزایش موجودی نماینده: اعلان بالای صفحه + عدد روی «تنظیمات» (هر ۳۰ ثانیه و بعد از هر تأیید/رد) */
 S.pend = 0; S.pendItems = []; S.pendHide = '';
