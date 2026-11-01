import os, re, time, asyncio, hashlib, logging
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN=os.getenv("BOT_TOKEN","").strip()
CHECK_INTERVAL=max(45,int(os.getenv("CHECK_INTERVAL","90")))
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("avito-radar")
# Persistent storage on host disk (if the host provides one)
DATA_FILE=os.getenv("DATA_FILE","data.json")
db={"users":{}}
def load_db():
    global db
    try:
        with open(DATA_FILE,"r",encoding="utf-8") as f: db=json.load(f)
    except Exception: db={"users":{}}
def save_db():
    tmp=DATA_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(db,f,ensure_ascii=False,indent=2)
    os.replace(tmp,DATA_FILE)
def user_data(uid):
    return db["users"].setdefault(str(uid),{"searches":[]})
def valid_avito_url(s):
    try:
        u=urlparse(s.strip())
        return u.scheme in ("http","https") and (u.hostname=="avito.ru" or (u.hostname or "").endswith(".avito.ru"))
    except: return False
def fetch_search(url):
    # Avito may rate-limit or require browser verification. This script does not bypass CAPTCHA.
    headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
             "Accept-Language":"ru-RU,ru;q=0.9,en;q=0.8"}
    r=requests.get(url,headers=headers,timeout=25)
    r.raise_for_status()
    if any(x in r.text[:5000].lower() for x in ["captcha","доступ ограничен","robot check"]):
        raise RuntimeError("Авито показал проверку/капчу. Автоматическая загрузка сейчас недоступна.")
    soup=BeautifulSoup(r.text,"html.parser")
    items=[]; seen=set()
    # Search result cards: tolerate several Avito markup variants
    for card in soup.select('[data-marker="item"], [data-testid="item"], article'):
        a=card.select_one('a[href]')
        if not a: continue
        href=a.get("href","")
        if not href: continue
        if href.startswith("/"): href="https://www.avito.ru"+href
        if "avito.ru" not in href: continue
        href=href.split("?")[0]
        if href in seen: continue
        seen.add(href)
        title=(card.select_one('[itemprop="name"], h3, [data-marker="item-title"]') or a).get_text(" ",strip=True)
        price_el=card.select_one('[itemprop="price"], [data-marker="item-price"], strong')
        price=price_el.get_text(" ",strip=True) if price_el else ""
        img=card.select_one("img")
        image=(img.get("src") or img.get("data-src") or img.get("srcset","").split(" ")[0]) if img else ""
        items.append({"id":href,"url":href,"title":title[:250],"price":price,"image":image})
    # JSON-LD fallback
    if not items:
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                obj=json.loads(s.string or s.get_text())
                objs=obj if isinstance(obj,list) else [obj]
                for o in objs:
                    for v in o.get("itemListElement",[]):
                        x=v.get("item",v); href=x.get("url")
                        if href and href not in seen:
                            seen.add(href); items.append({"id":href,"url":href,"title":x.get("name","Объявление"),
                            "price":str(x.get("offers",{}).get("price","")),"image":x.get("image","")})
            except Exception: pass
    return items[:50]
def fetch_detail(url):
    headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
             "Accept-Language":"ru-RU,ru;q=0.9,en;q=0.8"}
    r=requests.get(url,headers=headers,timeout=25); r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    def meta(*names):
        for n in names:
            x=soup.select_one(f'meta[property="{n}"], meta[name="{n}"]')
            if x and x.get("content"): return x["content"]
        return ""
    title=meta("og:title") or (soup.select_one("h1").get_text(" ",strip=True) if soup.select_one("h1") else "Объявление")
    desc=meta("og:description") or ""
    price=meta("product:price:amount","og:price:amount")
    images=[]
    ogimg=meta("og:image")
    if ogimg: images.append(ogimg)
    for im in soup.select("img"):
        src=im.get("src") or im.get("data-src")
        if src and ("avito" in src or src.startswith("https://")): images.append(src)
    images=list(dict.fromkeys(images))[:8]
    body=soup.get_text("\n",strip=True)
    # Find key details conservatively; absence means "not identified", not "no"
    delivery="Упоминание доставки найдено" if re.search(r"авито доставка|доставк",body,re.I) else "Не удалось определить"
    guarantee="Упоминание гарантии/защиты найдено" if re.search(r"авито гарант|защит.*покуп|безопасная сделк",body,re.I) else "Не удалось определить"
    desc_el=soup.select_one('[data-marker="item-view/item-description"]')
    if desc_el: desc=desc_el.get_text("\n",strip=True)
    params=soup.select_one('[data-marker="item-view/item-params"]')
    specs=params.get_text("\n",strip=True) if params else ""
    address=soup.select_one('[data-marker="item-view/address"]')
    location=address.get_text(" ",strip=True) if address else ""
    return {"title":title[:300],"description":desc[:3500],"price":price,"images":images,
            "specs":specs[:2500],"delivery":delivery,"guarantee":guarantee,"location":location}
def escape(s): return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
async def send_listing(bot,chat_id,item):
    try:
        d=await asyncio.to_thread(fetch_detail,item["url"])
    except Exception as e:
        log.warning("detail fetch failed: %s",e)
        d={"title":item.get("title","Объявление"),"description":"Подробности не удалось загрузить; откройте объявление по ссылке.",
           "price":item.get("price",""),"images":[item.get("image","")] if item.get("image") else [],
           "specs":"","delivery":"Не удалось определить","guarantee":"Не удалось определить","location":""}
    title=escape(d.get("title") or item.get("title"))
    price=escape(d.get("price") or item.get("price") or "Цена не распознана")
    caption=f"🆕 <b>{title}</b>\n💰 <b>{price}</b>\n"
    caption+=f"🚚 {escape(d.get('delivery','Не удалось определить'))}\n🛡️ {escape(d.get('guarantee','Не удалось определить'))}\n"
    if d.get("location"): caption+=f"📍 {escape(d['location'])}\n"
    if d.get("description"): caption+="\n📝 "+escape(d["description"][:900])
    if d.get("specs"): caption+="\n\n⚙️ <b>Характеристики</b>\n"+escape(d["specs"][:900])
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть объявление ↗",url=item["url"])]])
    imgs=d.get("images") or []
    try:
        if imgs:
            await bot.send_photo(chat_id,imgs[0],caption=caption[:1024],parse_mode="HTML",reply_markup=keyboard)
            for img in imgs[1:4]:
                try: await bot.send_photo(chat_id,img)
                except Exception: pass
        else: await bot.send_message(chat_id,caption[:4000],parse_mode="HTML",reply_markup=keyboard)
    except Exception:
        await bot.send_message(chat_id,caption[:4000],parse_mode="HTML",reply_markup=keyboard)
async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    user_data(update.effective_user.id)
    await update.message.reply_text(
      "Привет! Я Avito Radar 🤖\n\nПришли ссылку на поиск Авито целиком — с любыми категориями и фильтрами.\n"
      "Команды:\n/add — добавить поиск (или просто отправь ссылку)\n/list — список поисков\n/pause — пауза\n/resume — продолжить\n/delete — удалить поиск\n/help — помощь")
async def help_cmd(update,context):
    await update.message.reply_text("Отправь мне полную ссылку на страницу результатов поиска Авито. Фильтры сохраняются в самой ссылке.\n"
    f"Проверка примерно раз в {CHECK_INTERVAL} сек. Бот работает в облаке, поэтому Telegram может получать уведомления, даже если телефон закрыт.\n"
    "Важно: Авито может ограничивать автоматические запросы или показывать капчу. Я не обхожу её; в таком случае поиск временно остановится.")
async def add_url(update,context):
    uid=str(update.effective_user.id); data=user_data(uid)
    url=(update.message.text or "").strip()
    if not valid_avito_url(url):
        await update.message.reply_text("Пришли полную ссылку на поиск avito.ru (не токен бота).");return
    if any(x["url"]==url for x in data["searches"]):
        await update.message.reply_text("Эта ссылка уже отслеживается.");return
    data["searches"].append({"url":url,"active":True,"known":[],"title":url[:90],"last_error":""})
    save_db()
    await update.message.reply_text("✅ Поиск добавлен. Сейчас проверю выдачу; уже существующие объявления не буду присылать, только новые.")
    # baseline: register current results, don't send old ones
    try:
        items=await asyncio.to_thread(fetch_search,url)
        s=data["searches"][-1]; s["known"]=[x["id"] for x in items]
        s["title"]=items[0]["title"][:80] if items else url[:90]
        save_db()
        await update.message.reply_text(f"Первичная проверка завершена. В выдаче увидел {len(items)} карточек. Буду сообщать о новых.")
    except Exception as e:
        data["searches"][-1]["last_error"]=str(e);save_db()
        await update.message.reply_text("Ссылка сохранена, но первичная проверка не прошла: "+str(e)[:700]+
          "\nПопробую снова автоматически. Если Авито требует капчу, мониторинг может быть недоступен.")
async def list_cmd(update,context):
    data=user_data(update.effective_user.id); arr=data["searches"]
    if not arr: await update.message.reply_text("Пока нет поисков. Пришли ссылку на Авито.");return
    lines=[]
    for i,s in enumerate(arr,1):
        state="🟢" if s.get("active",True) else "⏸"
        lines.append(f"{i}. {state} {s.get('title',s['url'])}\n{s['url'][:220]}")
    await update.message.reply_text("\n\n".join(lines))
async def pause_cmd(update,context):
    data=user_data(update.effective_user.id)
    for s in data["searches"]: s["active"]=False
    save_db(); await update.message.reply_text("⏸ Все поиски поставлены на паузу.")
async def resume_cmd(update,context):
    data=user_data(update.effective_user.id)
    for s in data["searches"]: s["active"]=True
    save_db(); await update.message.reply_text("▶ Мониторинг возобновлён.")
async def delete_cmd(update,context):
    data=user_data(update.effective_user.id)
    if not data["searches"]: await update.message.reply_text("Список пуст.");return
    context.user_data["await_delete"]=True
    await update.message.reply_text("Отправь номер поиска из /list, который удалить, или слово «все».")
async def handle_delete_choice(update,context):
    if not context.user_data.get("await_delete"): return False
    context.user_data["await_delete"]=False
    data=user_data(update.effective_user.id); val=(update.message.text or "").strip().lower()
    if val=="все": data["searches"]=[];save_db();await update.message.reply_text("Удалил все поиски.");return True
    try:
        idx=int(val)-1
        if idx<0 or idx>=len(data["searches"]): raise ValueError()
        s=data["searches"].pop(idx);save_db();await update.message.reply_text("Удалён поиск: "+s.get("title",s["url"]))
    except: await update.message.reply_text("Не понял номер. Посмотри /list и повтори /delete.")
    return True
async def text_handler(update,context):
    if await handle_delete_choice(update,context):return
    await add_url(update,context)
async def poll_job(context:ContextTypes.DEFAULT_TYPE):
    bot=context.bot
    for uid,data in list(db.get("users",{}).items()):
        for s in data.get("searches",[]):
            if not s.get("active",True):continue
            try:
                items=await asyncio.to_thread(fetch_search,s["url"])
                old=set(s.get("known",[])); new=[x for x in items if x["id"] not in old]
                s["known"]=(list(old)+[x["id"] for x in items])[-800:]
                s["last_error"]="";save_db()
                for item in reversed(new[:10]):
                    await send_listing(bot,int(uid),item)
                if new: log.info("user %s: %d new items",uid,len(new))
            except Exception as e:
                msg=str(e)[:500]
                if s.get("last_error")!=msg:
                    s["last_error"]=msg;save_db()
                    try: await bot.send_message(int(uid),"⚠️ Не удалось проверить поиск:\n"+escape(msg)+"\nПопробую снова при следующем цикле.")
                    except Exception: pass
            await asyncio.sleep(1.2)
def main():
    if not TOKEN: raise SystemExit("Set BOT_TOKEN environment variable")
    load_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start));app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("add",lambda u,c:add_url(u,c)))
    app.add_handler(CommandHandler("list",list_cmd));app.add_handler(CommandHandler("pause",pause_cmd))
    app.add_handler(CommandHandler("resume",resume_cmd));app.add_handler(CommandHandler("delete",delete_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.job_queue.run_repeating(poll_job,interval=CHECK_INTERVAL,first=15)
    app.run_polling(drop_pending_updates=True)
if __name__=="__main__": main()
