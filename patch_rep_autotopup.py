#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""پچ «شارژ خودکار کیف پول نماینده» برای rebecca-bot.

اجرا:  cd /opt/rebecca-bot && python3 patch_rep_autotopup.py
- فقط وقتی تغییر می‌دهد که همه‌ی نقطه‌های پچ دقیقاً پیدا شوند؛ وگرنه هیچ فایلی را تغییر نمی‌دهد.
- قبل از تغییر از فایل‌ها نسخه‌ی پشتیبان *.bak-repauto می‌گیرد.
"""
import os
import py_compile
import shutil
import sys

D = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
EDITS: dict[str, list[tuple[str, str, int]]] = {}


def add(fname: str, old: str, new: str, count: int = 1) -> None:
    EDITS.setdefault(fname, []).append((old, new, count))


OPT: dict[str, list[tuple[str, str]]] = {}      # اصلاح‌های اختیاری: اگر قبلاً اعمال شده باشند رد می‌شوند


def add_opt(fname: str, old: str, new: str) -> None:
    OPT.setdefault(fname, []).append((old, new))


# ───────── اصلاح ي/ك عربی در پیامک بانک (اگر قبلاً اعمال نشده باشد) ─────────
add_opt("shop.py",
        '_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")',
        '_DIGITS = str.maketrans({**{c: str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}, **{c: str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}, "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "ـ": ""})')
add_opt("shop.py",
        'musts = [m.strip().lower() for m in re.split(r"[,،|\\n]", a["must"]) if m.strip()]',
        'musts = [sms_norm(m).lower() for m in re.split(r"[,،|\\n]", a["must"]) if m.strip()]')

# ───────────────────────── common.py ─────────────────────────
add("common.py",
    '''                tu.append({"id": str(t["id"]), "rid": str(t["rid"]), "amount": max(0, int(t.get("amount") or 0)), "note": str(t.get("note") or "")[:200],
                           "ts": int(t.get("ts") or 0), "status": t.get("status") if t.get("status") in ("pending", "approved", "rejected") else "pending",
                           "done": int(t.get("done") or 0)})
''',
    '''                _tr = {"id": str(t["id"]), "rid": str(t["rid"]), "amount": max(0, int(t.get("amount") or 0)), "note": str(t.get("note") or "")[:200],
                       "ts": int(t.get("ts") or 0), "status": t.get("status") if t.get("status") in ("pending", "approved", "rejected") else "pending",
                       "done": int(t.get("done") or 0)}
                if t.get("mode") == "auto":                       # شارژ خودکار با پیامک بانک
                    _tr.update(mode="auto", base=max(0, int(t.get("base") or 0)), pay_amount=max(0, int(t.get("pay_amount") or 0)),
                               exp=int(t.get("exp") or 0), hold=str(t.get("hold") or "")[:120], auto_ok=bool(t.get("auto_ok")), cts=int(t.get("cts") or 0))
                tu.append(_tr)
''')

add("common.py",
    '''def rep_owner(cfg: dict, pid: str, username: str) -> str | None:''',
    '''def topup_credit_in(c: dict, t: dict, amt: int, note: str = "شارژ خودکار") -> None:
    """داخل update_config: کیف پول نماینده را شارژ و درخواست را «تأیید» می‌کند (شارژ خودکار با پیامک بانک).
    بررسی سقف و وضعیت قبل از فراخوانی انجام شده؛ اینجا فقط اعمال می‌شود."""
    rid = t["rid"]
    c["reps"][rid]["balance"] += int(amt)
    _wlog(c, rid, "topup", int(amt), note)
    c["wallet_income"].append({"ts": time.time(), "rid": rid, "amount": int(amt)})
    c["wallet_tx"].append({"id": "topup:" + t["id"], "ts": time.time(), "rid": rid, "kind": "topup", "amount": int(amt),
                           "balance": c["reps"][rid]["balance"], "state": "done", "user": "", "alerted": False})
    t["amount"] = int(amt)
    t["status"] = "approved"
    t["done"] = int(time.time())


def rep_owner(cfg: dict, pid: str, username: str) -> str | None:''')

# ───────────────────────── shop.py ─────────────────────────
add("shop.py",
    '''        _auto_prepare(s, o, int(amount))''',
    '''        _auto_prepare(s, o, int(amount), c)''')

add("shop.py",
    '''def _auto_prepare(s: dict, o: dict, base: int) -> None:''',
    '''def _rep_taken(c: dict | None, now: int) -> set:
    """مبلغ‌های یکتای درخواست‌های شارژ خودکارِ نماینده‌ها که هنوز باز یا در قرنطینه‌اند (تداخل با سفارش‌های فروشگاه نشود)."""
    return {int(t["pay_amount"]) for t in ((c or {}).get("topups") or [])
            if t.get("mode") == "auto" and t.get("pay_amount") and (t["status"] == "pending" or now - int(t.get("ts") or 0) < AUTO_QUARANTINE)}


def _rep_used(c: dict | None, now: int) -> int:
    """جمع شارژهای خودکارِ نماینده‌ها در ۲۴ ساعت اخیر (در سقف روزانه‌ی تأیید خودکار حساب می‌شود)."""
    return sum(int(t.get("pay_amount") or 0) for t in ((c or {}).get("topups") or []) if t.get("auto_ok") and int(t.get("cts") or 0) > now - 86400)


def rep_topup_auto_new(rid: str, base: int, note: str = "") -> dict:
    """درخواست شارژ خودکار کیف پول نماینده: مبلغ یکتا (۱ تا ۹۹۹ تومان بیشتر) + مهلت. تأیید با پیامک بانک (همان وب‌هوک فروشگاه)."""
    out: dict = {}

    def mut(c):
        if rid not in c["reps"]:
            raise ValueError("نماینده پیدا نشد.")
        s = c["shop"]
        if int(base) < s["min_topup"] or int(base) > s["max_topup"]:
            raise ValueError(f"مبلغ شارژ خودکار باید بین {money(s['min_topup'])} و {money(s['max_topup'])} تومان باشد.")
        if sum(1 for t in c["topups"] if t["rid"] == rid and t["status"] == "pending") >= 5:
            raise ValueError("۵ درخواست در انتظار دارید؛ یکی را لغو کنید یا صبر کنید.")
        o: dict = {}
        _auto_prepare(s, o, int(base), c)
        rec = {"id": "t" + secrets.token_hex(4), "rid": rid, "amount": int(o["pay_amount"]), "note": (str(note).strip() or "شارژ خودکار")[:200],
               "ts": int(time.time()), "status": "pending", "done": 0, "mode": "auto", "base": int(base), "pay_amount": int(o["pay_amount"]),
               "exp": int(o["exp"]), "hold": "", "auto_ok": False, "cts": 0}
        c["topups"].append(rec)
        out.update(rec)
        out["cards"] = [dict(x) for x in s["cards"]]
    C.update_config(mut)
    C.audit_event("نماینده", "درخواست شارژ خودکار", rid, f"مبلغ: {money(out['pay_amount'])} تومان")
    return out


def _auto_prepare(s: dict, o: dict, base: int, c: dict | None = None) -> None:''')

add("shop.py",
    '''    taken = {int(x["pay_amount"]) for x in s["orders"]
             if x.get("mode") == "auto" and x.get("pay_amount") and (x["status"] in ("new", "pending", "processing") or now - int(x.get("ts") or 0) < AUTO_QUARANTINE)}''',
    '''    taken = {int(x["pay_amount"]) for x in s["orders"]
             if x.get("mode") == "auto" and x.get("pay_amount") and (x["status"] in ("new", "pending", "processing") or now - int(x.get("ts") or 0) < AUTO_QUARANTINE)} | _rep_taken(c, now)''')

add("shop.py",
    '''used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400)''',
    '''used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400) + _rep_used(c, now)''',
    count=2)

add("shop.py",
    '''        if not cand:
            late = [''',
    '''        rcand = [x for x in c["topups"] if x.get("mode") == "auto" and x["status"] == "pending" and not x.get("hold")
                 and int(x.get("pay_amount") or 0) in vals and now <= int(x.get("exp") or 0) + AUTO_GRACE]
        if rcand:                                                   # شارژ خودکار کیف پول نماینده
            if cand or len(rcand) > 1:
                log("ambiguous", ",".join([x["id"] for x in cand] + [x["id"] for x in rcand])[:40], int(vals[0]))
                return
            rt = rcand[0]
            amt = int(rt["pay_amount"])
            rp = c["reps"].get(rt["rid"]) or {}
            used = sum(int(x.get("pay_amount") or 0) for x in s["orders"] if x.get("auto_ok") and int(x.get("cts") or 0) > now - 86400) + _rep_used(c, now)
            why = ("نماینده مسدود است" if rp.get("blocked") else "مبلغ از سقف تأیید خودکار بیشتر است" if amt > a["max_amount"]
                   else "سقف روزانه‌ی تأیید خودکار پر شده" if used + amt > a["daily_max"]
                   else "سقف موجودی کیف پول رد می‌شود" if int(rp.get("balance") or 0) + amt > C.WALLET_MAX else "")
            if why:
                rt["hold"] = why
                log("hold", rt["id"], amt)
            else:
                C.topup_credit_in(c, rt, amt, "شارژ خودکار با پیامک بانک")
                rt.update(auto_ok=True, cts=now)
                log("ok", rt["id"], amt)
            out["topup"] = dict(rt)
            return
        if not cand:
            late = [''')

add("shop.py",
    '''    try:
        if res == "ok" and o:
''',
    '''    try:
        tp = r.get("topup")
        if tp:                                                      # شارژ خودکار کیف پول نماینده
            _rp = C.load_config_ro()["reps"].get(tp["rid"]) or {}
            who = esc(_rp.get("label") or _rp.get("name") or tp["rid"])
            if res == "ok":
                C.audit_event("تأیید خودکار", "شارژ خودکار کیف پول نماینده", tp["rid"], f"{money(tp['amount'])} تومان", amount=int(tp["amount"]), cat="wallet")
                await _notify_admins(bot, f"✅ <b>شارژ خودکار نماینده</b>\\nنماینده: <b>{who}</b>\\nمبلغ: <b>{money(tp['amount'])}</b> تومان")
            elif res == "hold":
                await _notify_admins(bot, f"⚠️ <b>واریز نماینده {who} دیده شد ولی شارژ خودکار انجام نشد</b>\\nمبلغ: <b>{money(tp['pay_amount'])}</b> تومان\\nعلت: {esc(tp.get('hold') or '')}\\n\\nاز وب‌پنل ← نماینده‌ها، درخواست را دستی تأیید یا رد کنید.")
            return {k: v for k, v in r.items() if k not in ("order", "topup")}
        if res == "ok" and o:
''')

add("shop.py",
    '''    def mut(c):
        for o in c["shop"]["orders"]:
            if o["status"] == "new" and (now > int''',
    '''    def mut(c):
        for t in c["topups"]:                                       # شارژ خودکار نماینده که مهلتش تمام شد
            if t.get("mode") == "auto" and t["status"] == "pending" and not t.get("hold") and now > int(t.get("exp") or 0) + AUTO_GRACE:
                t.update(status="rejected", done=now, hold="مهلت واریز تمام شد")
        for o in c["shop"]["orders"]:
            if o["status"] == "new" and (now > int''')

# ───────────────────────── webapp.py ─────────────────────────
add("webapp.py",
    '''    note: str = Field(default="", max_length=200)


@app.post("/api/rep/topup")
async def rep_topup(r: RepTopupReq, rep=Depends(rep_guard)):
    rid = rep[0]
    try:
        rec = await asyncio.to_thread(C.topup_add, rid, r.amount, r.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
''',
    '''    note: str = Field(default="", max_length=200)
    auto: bool = False


@app.post("/api/rep/topup")
async def rep_topup(r: RepTopupReq, rep=Depends(rep_guard)):
    rid = rep[0]
    if r.auto:                                                         # شارژ خودکار با پیامک بانک
        try:
            rec = await asyncio.to_thread(shop.rep_topup_auto_new, rid, r.amount, r.note)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": rec["id"], "auto": True, "pay_amount": rec["pay_amount"], "pay_rial": rec["pay_amount"] * 10,
                "exp": rec["exp"], "now": int(time.time()), "cards": rec["cards"]}
    try:
        rec = await asyncio.to_thread(C.topup_add, rid, r.amount, r.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
''')

add("webapp.py",
    '''async def _next_rep_name(cfg: dict, rp: dict)''',
    '''@app.get("/api/rep/topup/{tid}")
async def rep_topup_status(tid: str, rep=Depends(rep_guard)):
    rid = rep[0]
    cfg = load_config()
    t = next((x for x in cfg["topups"] if x["id"] == tid and x["rid"] == rid), None)
    if not t:
        raise HTTPException(404, "درخواست پیدا نشد.")
    return {"status": t["status"], "hold": t.get("hold", ""), "amount": t["amount"], "exp": t.get("exp", 0), "now": int(time.time()),
            "balance": cfg["reps"][rid]["balance"]}


@app.post("/api/rep/topup/{tid}/cancel")
async def rep_topup_cancel(tid: str, rep=Depends(rep_guard)):
    rid = rep[0]

    def mut(c):
        t = next((x for x in c["topups"] if x["id"] == tid and x["rid"] == rid), None)
        if not t or t["status"] != "pending" or t.get("mode") != "auto" or t.get("hold"):
            raise ValueError("این درخواست قابل لغو نیست.")
        t.update(status="rejected", done=int(time.time()), hold="لغو توسط نماینده")
    try:
        await asyncio.to_thread(C.update_config, mut)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


async def _next_rep_name(cfg: dict, rp: dict)''')

add("webapp.py",
    '''"topups": [t for t in reversed(cfg["topups"]) if t["rid"] == rid][:15]}''',
    '''"topups": [t for t in reversed(cfg["topups"]) if t["rid"] == rid][:15],
            "auto_ok": bool(cfg["shop"]["auto"]["enabled"] and cfg["shop"]["cards"]), "auto_cards": [dict(x) for x in cfg["shop"]["cards"]], "now": int(time.time())}''')

# ───────────────────────── index.html ─────────────────────────
add("index.html",
    '''    const lgRow = txRow;
    mount(h('div', { class: 'card bigbal' }, h('span', {}, 'موجودی کیف پول'), h('b', {}, toman(w.balance))),
''',
    '''    const lgRow = txRow;
    // ⚡ شارژ خودکار: مبلغ یکتا + کارت نشان داده می‌شود؛ بعد از واریز، پیامک بانک کیف پول را خودکار شارژ می‌کند
    const aAmt = moneyIn(0, 'مبلغ شارژ (تومان) مثلاً 1,000,000');
    const aBox = h('div', {});
    const aBtn = h('button', { class: 'btn pri', type: 'button', style: 'width:100%;margin-top:12px' }, '⚡ دریافت مبلغ و کارت برای واریز');
    let aTimer = null;
    const aStop = () => { if (aTimer) { clearInterval(aTimer); aTimer = null; } };
    const aShow = (r) => {
      aStop();
      const mmss = s => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
      const endAt = Date.now() + Math.max(0, (+r.exp || 0) - (+r.now || 0)) * 1000;
      const left = h('b', { dir: 'ltr' }, '');
      const msg = h('p', { class: 'hint' }, '⏳ منتظر واریز و تأیید خودکار…');
      const rial = h('code', { dir: 'ltr', style: 'font-size:20px;cursor:pointer;display:block;margin:6px 0', onclick: () => copy(String(r.pay_rial)) }, String(r.pay_rial));
      const cancelB = h('button', { class: 'btn sm', type: 'button' }, '✖ لغو');
      cancelB.onclick = () => run(cancelB, async () => { await api('/rep/topup/' + r.id + '/cancel', 'POST', {}); aStop(); aBox.replaceChildren(); toast('لغو شد'); });
      aBox.replaceChildren(h('div', { class: 'banner', style: 'margin-top:12px' },
        h('p', {}, 'دقیقاً این مبلغ را با تمام ارقام (ریال) به یکی از کارت‌ها واریز کنید. برای کپی روی عدد بزنید:'),
        rial,
        ...(r.cards || []).map(c => h('div', { class: 'lg' }, h('span', {}, [c.holder, c.bank].filter(Boolean).join(' — ')), h('b', { dir: 'ltr', style: 'cursor:pointer', onclick: () => copy(c.number) }, c.number))),
        h('p', { class: 'hint' }, 'مبلغ را رُند نکنید و فقط یک‌بار واریز کنید. زمان باقی‌مانده: ', left),
        msg, cancelB));
      let n = 0, busy = false;
      aTimer = setInterval(async () => {
        if (!aBox.isConnected) return aStop();
        left.textContent = mmss(Math.max(0, Math.round((endAt - Date.now()) / 1000)));
        if (++n % 8 || busy) return;
        busy = true;
        try {
          const s = await api('/rep/topup/' + r.id);
          if (s.status === 'approved') { aStop(); toast('✅ پرداخت شناسایی شد و کیف پول شما شارژ شد'); await views.wallet(); }
          else if (s.status === 'rejected') { aStop(); msg.textContent = '✖ ' + (s.hold || 'درخواست بسته شد') + ' — اگر واریز کرده‌اید، درخواست دستی ثبت کنید.'; }
          else if (s.hold) { msg.textContent = '🕓 پرداخت دیده شد و برای بررسی نهایی به ادمین رفت.'; }
        } catch (e) { /* خطای موقت شبکه؛ دفعه بعد دوباره */ } finally { busy = false; }
      }, 1000);
    };
    aBtn.onclick = () => run(aBtn, async () => { const v = mv(aAmt); if (!(v >= 1000)) throw new Error('حداقل مبلغ ۱٬۰۰۰ تومان است.'); aShow(await api('/rep/topup', 'POST', { amount: v, auto: true })); });
    const aPend = (w.topups || []).find(t => t.mode === 'auto' && t.status === 'pending' && !t.hold && (+t.exp || 0) > (+w.now || 0));
    if (aPend) aShow({ id: aPend.id, pay_rial: aPend.pay_amount * 10, exp: aPend.exp, now: w.now, cards: w.auto_cards });
    const autoCard = w.auto_ok ? h('div', { class: 'card' }, h('h2', {}, '⚡ شارژ خودکار'), field('مبلغ (تومان)', aAmt), aBtn, aBox,
      h('p', { class: 'hint' }, 'بعد از واریز دقیقِ مبلغ نشان‌داده‌شده، کیف پول شما چند دقیقه بعد خودکار شارژ می‌شود؛ نیازی به ثبت درخواست نیست.')) : null;
    mount(h('div', { class: 'card bigbal' }, h('span', {}, 'موجودی کیف پول'), h('b', {}, toman(w.balance))),
      autoCard,
''')


def main() -> None:
    plan = {}
    for fname, edits in EDITS.items():
        p = os.path.join(D, fname)
        if not os.path.isfile(p):
            sys.exit(f"❌ {fname} پیدا نشد (مسیر: {D})")
        s = open(p, encoding="utf-8").read()
        if "rep_topup_auto_new" in s or "topup_credit_in" in s:
            sys.exit("ℹ️ این پچ قبلاً روی این فایل‌ها اعمال شده؛ تغییری داده نشد.")
        for old, new, count in edits:
            n = s.count(old)
            if n != count:
                sys.exit(f"❌ {fname}: نقطه‌ی پچ پیدا نشد (انتظار {count}، یافت {n}) — نسخه‌ی شما با نسخه‌ی من فرق دارد. هیچ فایلی تغییر نکرد.\n   {old.strip().splitlines()[0][:90]}")
            s = s.replace(old, new)
        for old, new in OPT.get(fname, []):
            if old in s:
                s = s.replace(old, new)
        plan[p] = s
    for p, s in plan.items():
        shutil.copy2(p, p + ".bak-repauto")
        open(p, "w", encoding="utf-8").write(s)
        if p.endswith(".py"):
            try:
                py_compile.compile(p, doraise=True)
            except py_compile.PyCompileError as e:
                for q in plan:
                    shutil.copy2(q + ".bak-repauto", q)
                sys.exit(f"❌ خطای نحوی بعد از پچ ({os.path.basename(p)}): {e}\nهمه‌ی فایل‌ها برگردانده شد.")
    print("✅ پچ اعمال شد:", ", ".join(os.path.basename(p) for p in plan))
    print("حالا اجرا کن:  systemctl restart rebecca-bot rebecca-webapp")


if __name__ == "__main__":
    main()
