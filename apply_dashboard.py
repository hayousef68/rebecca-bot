#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_dashboard.py — نصب خودکار داشبورد گرافیکی روی پروژه rebecca-bot

اجرا (از داخل پوشه پروژه، مثلاً /opt/rebecca-bot):
    python3 apply_dashboard.py

سپس:
    systemctl restart rebecca-webapp
"""

import os
import sys
import shutil
import time

BASE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(BASE, "webapp.py")
INDEX  = os.path.join(BASE, "index.html")


def backup(path):
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{path}.backup-{ts}"
    shutil.copy2(path, dst)
    print(f"  💾 پشتیبان: {os.path.basename(dst)}")


def write(path, src):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)


def main():
    if not os.path.exists(WEBAPP):
        sys.exit(f"✗ webapp.py پیدا نشد در: {BASE}")
    if not os.path.exists(INDEX):
        sys.exit(f"✗ index.html پیدا نشد در: {BASE}")

    print(f"📁 پوشه پروژه: {BASE}\n")

    # ═══════════ 1. WEBAPP.PY ═══════════
    print("─── 1. webapp.py ───")
    with open(WEBAPP, encoding="utf-8") as f:
        src = f.read()

    if "rep_dashboard" in src:
        print("  ✓ قبلاً اعمال شده، skip")
    else:
        marker = "# ───────────────────────────── پنل‌های ربکا و تست اتصال ─────────────────────────────"
        if marker not in src:
            sys.exit("  ✗ marker «پنل‌های ربکا و تست اتصال» پیدا نشد")
        src = src.replace(marker, WEBAPP_INSERT + marker, 1)
        backup(WEBAPP)
        write(WEBAPP, src)
        print("  ✓ ۲ endpoint داشبورد اضافه شد")

    # ═══════════ 2. INDEX.HTML ═══════════
    print("\n─── 2. index.html ───")
    with open(INDEX, encoding="utf-8") as f:
        src = f.read()
    changed = False

    # 2a: توابع چارت
    if "_chartColors" in src:
        print("  ✓ توابع چارت از قبل هست، skip")
    else:
        marker = "async function repApp(me) {"
        if marker not in src:
            sys.exit("  ✗ marker «async function repApp» پیدا نشد")
        src = src.replace(marker, CHARTS_JS + marker, 1)
        print("  ✓ توابع چارت اضافه شد")
        changed = True

    # 2b: view داشبورد + تغییر صفحه پیش‌فرض
    if "views.dashboard" in src:
        print("  ✓ داشبورد از قبل هست، skip")
    else:
        old_line = "refreshBal().catch(() => {}).finally(() => show('users'));"
        if old_line not in src:
            sys.exit("  ✗ آخرِ repApp پیدا نشد")
        new_line = ("views.dashboard = async () => {" + DASHBOARD_JS
                    + "  };\n\n  refreshBal().catch(() => {}).finally(() => show('dashboard'));")
        src = src.replace(old_line, new_line, 1)
        print("  ✓ view داشبورد + صفحه‌ی پیش‌فرض اضافه شد")
        changed = True

    # 2c: منوی نماینده
    old_items = "const items = [['buy', '➕', 'ساخت اکانت'], ['users', '👥', 'کاربران من'], ['wallet', '💳', 'کیف پول و افزایش موجودی']];"
    new_items = "const items = [['dashboard', '📊', 'داشبورد'], ['buy', '➕', 'ساخت اکانت'], ['users', '👥', 'کاربران من'], ['wallet', '💳', 'کیف پول و افزایش موجودی']];"
    if old_items in src:
        src = src.replace(old_items, new_items, 1)
        print("  ✓ منوی «📊 داشبورد» اضافه شد")
        changed = True
    elif "['dashboard', '📊', 'داشبورد']" in src:
        print("  ✓ منوی داشبورد از قبل هست")

    if changed:
        backup(INDEX)
        write(INDEX, src)
        print("  ✓ index.html ذخیره شد")

    print("\n" + "=" * 50)
    print("🎉 نصب کامل شد!")
    print("=" * 50)
    print("\nگام بعدی:")
    print("    systemctl restart rebecca-webapp")
    print("\nسپس نماینده وارد شود → منو ← 📊 داشبورد")


# ─────────────────── محتوای تزریق به webapp.py ───────────────────
WEBAPP_INSERT = r'''# ───────────────────────────── داشبورد نماینده ─────────────────────────────
@app.get("/api/rep/dashboard")
async def rep_dashboard(rep=Depends(rep_guard)):
    """خلاصه‌ی عددی + سری زمانی ۳۰ روز فروش برای نمودارهای داشبورد نماینده."""
    rid = rep[0]
    cfg = load_config()
    rp = cfg["reps"][rid]
    sales_30d = await asyncio.to_thread(sales.rep_daily_sales, rid, 30)
    today = sales_30d[0] if sales_30d else {"total": 0, "count": 0, "new_count": 0, "renew_count": 0, "breakdown": []}
    month_total = sum(int(x.get("total") or 0) for x in sales_30d)
    month_count = sum(int(x.get("count") or 0) for x in sales_30d)

    templates: dict = {}
    for day in sales_30d:
        for x in day.get("breakdown", []):
            name = x.get("template") or "—"
            rec = templates.setdefault(name, {"name": name, "count": 0})
            rec["count"] += int(x.get("count") or 0)

    total_users = sum(1 for m in cfg["rep_users"].values() for r in m.values() if r == rid)

    def _row(x):
        return {"day": x["day"], "date": x["date"], "total": int(x.get("total") or 0), "count": int(x.get("count") or 0)}

    return {
        "balance": int(rp["balance"]),
        "today": {"total": int(today.get("total") or 0), "count": int(today.get("count") or 0),
                  "new_count": int(today.get("new_count") or 0), "renew_count": int(today.get("renew_count") or 0)},
        "month": {"total": month_total, "count": month_count, "days": 30},
        "sales_30d": [_row(x) for x in reversed(sales_30d)],
        "sales_7d": [_row(x) for x in reversed(sales_30d[:7])],
        "templates": sorted(templates.values(), key=lambda t: -t["count"])[:8],
        "total_users": total_users,
        "next_plan_enabled": bool(rp.get("next_plan")),
    }


@app.get("/api/rep/dashboard/user-stats")
async def rep_dashboard_user_stats(rep=Depends(rep_guard)):
    """توزیع وضعیت کاربرهای یک نماینده (نمونه‌گیری اگر > ۲۰۰ باشد)."""
    rid = rep[0]
    cfg = load_config()
    items: list = []
    for pid, m in cfg["rep_users"].items():
        for u, r in m.items():
            if r == rid:
                items.append((pid, u))
    total = len(items)
    stats = {"active": 0, "on_hold": 0, "expired": 0, "limited": 0, "disabled": 0, "unknown": 0}
    cap = 200
    sample = items if total <= cap else items[:cap]
    sem = asyncio.Semaphore(10)

    async def one(pid, u):
        async with sem:
            try:
                ap = get_api(pid)
                if not ap.is_configured():
                    return None
                raw = await ap.request("GET", ap.upath(u))
                return raw.get("status")
            except Exception:  # noqa: BLE001
                return None

    statuses = await asyncio.gather(*(one(p, u) for p, u in sample))
    for s in statuses:
        if s in stats:
            stats[s] += 1
        elif s is None:
            stats["unknown"] += 1

    if total > cap:
        ratio = total / cap
        for k in stats:
            stats[k] = int(round(stats[k] * ratio))

    return {"total": total, "status_stats": stats, "sampled": total > cap}


'''


# ─────────────────── توابع چارت (قبل از repApp) ───────────────────
CHARTS_JS = r'''/* ───────── نمودارهای SVG سبک برای داشبورد نماینده ───────── */
function _chartColors() {
    const cs = getComputedStyle(document.documentElement);
    const g = k => cs.getPropertyValue(k).trim();
    return {
        ac: g('--ac') || '#8b7cff', ac2: g('--ac2') || '#4cc2ff',
        ok: g('--ok') || '#3ecf8e', bad: g('--bad') || '#f2706b',
        warn: g('--warn') || '#f5a524', info: g('--info') || '#5aa9ff',
        mu: g('--mu') || '#8497aa', tx: g('--tx') || '#e9f0f6',
        line: g('--line') || '#222d3d', bg: g('--bg') || '#080c14',
        s1: g('--s1') || '#0f1622',
    };
}

function svgBarChart(data) {
    const C = _chartColors();
    const W = 340, H = 150, padTop = 22, padBot = 26, padL = 6, padR = 6;
    const chartW = W - padL - padR, chartH = H - padTop - padBot;
    const maxV = Math.max(...data.map(d => d.value), 1);
    const barW = Math.max(6, chartW / data.length - 8);
    let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" xmlns="http://www.w3.org/2000/svg">`;
    svg += `<defs><linearGradient id="bg-bar" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${C.ac}"/><stop offset="100%" stop-color="${C.ac2}"/></linearGradient></defs>`;
    for (let i = 0; i <= 3; i++) {
        const y = padTop + (chartH * i / 3);
        svg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="${C.line}" stroke-dasharray="3 3" opacity="0.4"/>`;
    }
    data.forEach((d, i) => {
        const x = padL + i * (chartW / data.length) + 4;
        const barH = d.value > 0 ? (d.value / maxV) * chartH : 0;
        const y = padTop + chartH - barH;
        svg += `<rect x="${x}" y="${y}" width="${barW}" height="${Math.max(2, barH)}" rx="4" fill="url(#bg-bar)" opacity="${d.value ? 1 : 0.25}"><title>${d.title || d.label}: ${d.value}</title></rect>`;
        if (d.value > 0 && data.length <= 10) {
            svg += `<text x="${x + barW / 2}" y="${y - 6}" text-anchor="middle" fill="${C.tx}" font-size="9" font-weight="700">${d.shortValue || ''}</text>`;
        }
        svg += `<text x="${x + barW / 2}" y="${H - 8}" text-anchor="middle" fill="${C.mu}" font-size="9">${d.label}</text>`;
    });
    svg += `</svg>`;
    return svg;
}

function svgLineChart(data) {
    const C = _chartColors();
    const W = 340, H = 140, padTop = 18, padBot = 24, padL = 6, padR = 6;
    const chartW = W - padL - padR, chartH = H - padTop - padBot;
    const maxV = Math.max(...data.map(d => d.value), 1);
    const pts = data.map((d, i) => ({ x: padL + (i / Math.max(1, data.length - 1)) * chartW, y: padTop + chartH - (d.value / maxV) * chartH }));
    let linePath = '';
    if (pts.length === 1) {
        linePath = `M ${pts[0].x} ${pts[0].y} L ${pts[0].x} ${pts[0].y}`;
    } else {
        linePath = `M ${pts[0].x} ${pts[0].y}`;
        for (let i = 1; i < pts.length; i++) {
            const p = pts[i - 1], c = pts[i], cx = (p.x + c.x) / 2;
            linePath += ` C ${cx} ${p.y}, ${cx} ${c.y}, ${c.x} ${c.y}`;
        }
    }
    const areaPath = linePath + ` L ${pts[pts.length - 1].x} ${padTop + chartH} L ${pts[0].x} ${padTop + chartH} Z`;
    let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" xmlns="http://www.w3.org/2000/svg">`;
    svg += `<defs><linearGradient id="lg-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${C.ac}" stop-opacity="0.35"/><stop offset="100%" stop-color="${C.ac}" stop-opacity="0"/></linearGradient><linearGradient id="lg-line" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="${C.ac}"/><stop offset="100%" stop-color="${C.ac2}"/></linearGradient></defs>`;
    for (let i = 0; i <= 3; i++) {
        const y = padTop + (chartH * i / 3);
        svg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="${C.line}" stroke-dasharray="3 3" opacity="0.4"/>`;
    }
    svg += `<path d="${areaPath}" fill="url(#lg-area)"/>`;
    svg += `<path d="${linePath}" fill="none" stroke="url(#lg-line)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
    if (pts.length) {
        const last = pts[pts.length - 1];
        svg += `<circle cx="${last.x}" cy="${last.y}" r="4" fill="${C.ac}" stroke="${C.s1}" stroke-width="2"/>`;
    }
    const step = Math.max(1, Math.ceil(data.length / 6));
    data.forEach((d, i) => {
        if (i % step === 0 || i === data.length - 1) {
            svg += `<text x="${pts[i].x}" y="${H - 6}" text-anchor="middle" fill="${C.mu}" font-size="9">${d.label}</text>`;
        }
    });
    svg += `</svg>`;
    return svg;
}

function svgDonut(items, opts) {
    opts = opts || {};
    const C = _chartColors();
    const size = opts.size || 140, cx = size / 2, cy = size / 2, r = size * 0.36, sw = size * 0.13;
    const total = items.reduce((s, x) => s + x.value, 0) || 1;
    const Ccirc = 2 * Math.PI * r;
    let svg = `<svg viewBox="0 0 ${size} ${size}" style="width:${size}px;height:${size}px;flex:none" xmlns="http://www.w3.org/2000/svg">`;
    let offset = 0;
    items.forEach(item => {
        const dash = (item.value / total) * Ccirc;
        svg += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${item.color}" stroke-width="${sw}" stroke-dasharray="${dash} ${Ccirc}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${cx} ${cy})"><title>${item.label}: ${item.value}</title></circle>`;
        offset += dash;
    });
    svg += `<text x="${cx}" y="${cy - 2}" text-anchor="middle" fill="${C.mu}" font-size="10">${opts.sublabel || 'کل'}</text>`;
    svg += `<text x="${cx}" y="${cy + 16}" text-anchor="middle" fill="${C.tx}" font-size="${opts.centerSize || 18}" font-weight="800">${opts.centerLabel || total}</text>`;
    svg += `</svg>`;
    return svg;
}

function svgStatusBars(items) {
    const C = _chartColors();
    const W = 340, rowH = 32, labelW = 90, valueW = 50;
    const H = items.length * rowH + 6;
    const barX = labelW, barMaxW = W - labelW - valueW - 4;
    const total = items.reduce((s, x) => s + x.value, 0) || 1;
    let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" xmlns="http://www.w3.org/2000/svg">`;
    items.forEach((s, i) => {
        const y = i * rowH + 6;
        const barW = Math.max(2, (s.value / total) * barMaxW);
        svg += `<text x="${labelW / 2}" y="${y + 16}" text-anchor="middle" fill="${C.tx}" font-size="10.5" font-weight="600">${s.label}</text>`;
        svg += `<rect x="${barX}" y="${y + 6}" width="${barMaxW}" height="14" rx="7" fill="${C.bg}" stroke="${C.line}" stroke-width="0.6"/>`;
        svg += `<rect x="${barX}" y="${y + 6}" width="${barW}" height="14" rx="7" fill="${s.color}"><title>${s.label}: ${s.value}</title></rect>`;
        svg += `<text x="${W - valueW / 2}" y="${y + 16}" text-anchor="middle" fill="${C.tx}" font-size="11" font-weight="700">${s.value}</text>`;
    });
    svg += `</svg>`;
    return svg;
}

'''


# ─────────────────── بدنه‌ی view داشبورد ───────────────────
DASHBOARD_JS = r"""
        mount(h('p', { class: 'hint' }, 'در حال بارگذاری داشبورد…'));
        let d;
        try { d = await api('/rep/dashboard'); }
        catch (e) { return mount(h('div', { class: 'card' }, h('p', { class: 'err' }, e.message))); }

        const fa = n => (+n || 0).toLocaleString('fa-IR');
        const toman = n => (+n || 0).toLocaleString('en-US') + ' تومان';
        const shortToman = n => {
            const v = +n || 0;
            if (v >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
            if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
            if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
            return String(v);
        };
        const persianDay = s => {
            const p = String(s).split(' ');
            return p.length >= 2 ? p[1].split('/')[2].replace(/^۰/, '') : String(s);
        };

        const kpiGrid = h('div', { class: 'stat', style: 'grid-template-columns:repeat(2,1fr);margin-bottom:12px' },
            h('div', { style: '--c:var(--ac)' }, h('span', {}, '💳 موجودی'), h('strong', { dir: 'ltr', style: 'text-align:right;font-size:var(--fs-lg)' }, toman(d.balance))),
            h('div', { style: '--c:var(--ok)' }, h('span', {}, '📈 فروش امروز'), h('strong', { dir: 'ltr', style: 'text-align:right;font-size:var(--fs-lg)' }, toman(d.today.total)), h('span', {}, fa(d.today.count) + ' سفارش')),
            h('div', { style: '--c:var(--info)' }, h('span', {}, '📅 فروش ۳۰ روز'), h('strong', { dir: 'ltr', style: 'text-align:right;font-size:var(--fs-lg)' }, toman(d.month.total)), h('span', {}, fa(d.month.count) + ' سفارش')),
            h('div', { style: '--c:var(--warn)' }, h('span', {}, '👥 کاربران'), h('strong', { dir: 'ltr', style: 'text-align:right;font-size:var(--fs-lg)' }, fa(d.total_users)))
        );

        const chart7Data = d.sales_7d.map(x => ({ label: String(x.date).split(' ')[0], value: x.total, shortValue: x.total > 0 ? shortToman(x.total) : '', title: x.date }));
        const chart7Div = h('div', { style: 'margin:6px 0 2px' });
        chart7Div.innerHTML = svgBarChart(chart7Data);
        const card7 = h('div', { class: 'card' }, h('h2', {}, '📊 فروش ۷ روز اخیر'), chart7Div);

        const chart30Data = d.sales_30d.map(x => ({ label: persianDay(x.date), value: x.total, title: x.date }));
        const chart30Div = h('div', { style: 'margin:6px 0 2px' });
        chart30Div.innerHTML = svgLineChart(chart30Data);
        const card30 = h('div', { class: 'card' }, h('h2', {}, '📈 روند فروش ۳۰ روز'), chart30Div);

        const COLORS = ['#8b7cff', '#4cc2ff', '#3ecf8e', '#f5a524', '#5aa9ff', '#f2706b', '#a3e635', '#fb7185'];
        const tplTotal = d.templates.reduce((s, x) => s + x.count, 0);
        const tplItems = d.templates.map((t, i) => Object.assign({}, t, { color: COLORS[i % COLORS.length] }));
        let cardTpl;
        if (tplItems.length) {
            const donutWrap = h('div', { style: 'display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:8px' });
            const donutDiv = h('div');
            donutDiv.innerHTML = svgDonut(tplItems, { centerLabel: fa(tplTotal), sublabel: 'فروش' });
            donutWrap.appendChild(donutDiv);
            const legend = h('div', { style: 'flex:1;min-width:140px;font-size:var(--fs-sm)' });
            tplItems.forEach(t => {
                legend.appendChild(h('div', { style: 'display:flex;align-items:center;gap:6px;margin-bottom:6px' },
                    h('span', { style: 'width:10px;height:10px;border-radius:3px;background:' + t.color + ';flex:none' }),
                    h('span', { style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap', title: t.name }, t.name),
                    h('b', { style: 'color:var(--mu);flex:none' }, fa(t.count))
                ));
            });
            donutWrap.appendChild(legend);
            cardTpl = h('div', { class: 'card' }, h('h2', {}, '🎯 تمپلت‌های پرکاربرد (۳۰ روز)'), donutWrap);
        } else {
            cardTpl = h('div', { class: 'card' }, h('h2', {}, '🎯 تمپلت‌های پرکاربرد'), h('p', { class: 'hint' }, 'هنوز فروشی با جزئیات ثبت نشده.'));
        }

        const statusBody = h('div', {}, h('p', { class: 'hint' }, 'در حال بارگذاری وضعیت کاربران…'));
        const cardStatus = h('div', { class: 'card' }, h('h2', {}, '👥 وضعیت کاربران'), statusBody);

        const actions = h('div', { class: 'btns', style: 'margin-bottom:12px' },
            h('button', { class: 'btn pri', type: 'button', onclick: () => show('buy') }, '➕ ساخت اکانت'),
            h('button', { class: 'btn', type: 'button', onclick: () => show('users') }, '👥 کاربران'),
            h('button', { class: 'btn', type: 'button', onclick: () => show('wallet') }, '💳 کیف پول')
        );

        mount(kpiGrid, card7, card30, cardTpl, cardStatus, actions);

        try {
            const s = await api('/rep/dashboard/user-stats');
            const C2 = _chartColors();
            const statusList = [
                { label: 'فعال', value: s.status_stats.active, color: C2.ok },
                { label: 'خام', value: s.status_stats.on_hold, color: C2.info },
                { label: 'منقضی', value: s.status_stats.expired, color: C2.bad },
                { label: 'محدود', value: s.status_stats.limited, color: C2.warn },
                { label: 'غیرفعال', value: s.status_stats.disabled, color: C2.mu },
            ].filter(x => x.value > 0);
            if (statusList.length) {
                const barsDiv = h('div', { style: 'margin-top:8px' });
                barsDiv.innerHTML = svgStatusBars(statusList);
                statusBody.replaceChildren(barsDiv);
                if (s.sampled) statusBody.appendChild(h('p', { class: 'hint', style: 'margin-top:6px;font-size:var(--fs-xs)' }, 'تخمینی از ' + fa(s.total) + ' کاربر'));
            } else {
                statusBody.replaceChildren(h('p', { class: 'hint' }, 'هنوز کاربری به شما تخصیص داده نشده.'));
            }
        } catch (e) {
            statusBody.replaceChildren(h('p', { class: 'hint' }, 'بارگذاری ممکن نشد.'));
        }
"""


if __name__ == "__main__":
    main()
