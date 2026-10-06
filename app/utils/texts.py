"""
Barcha foydalanuvchiga ko'rinadigan matnlar BITTA faylda (o'zbek tilida).

Har qanday xabar / tugma yozuvini shu yerda o'zgartirsangiz, butun botda
o'zgaradi — handler fayllarini ochib o'tirishga hojat yo'q.  HTML teglardan
foydalanish mumkin (parse mode – HTML).

Premium emoji: xabar ICHIDA animatsion (custom) emoji chiqarish uchun
`tg_e(id, fallback)` ishlatilgan.  Bot akkauntida Premium bo'lmasa, xabar
oddiy emoji bilan qayta yuboriladi.

Kirish nazorati (access/ban/allow) OLIB TASHLANGAN — admin panelga /admin
buyrug'i bilan kiriladi.
"""

from __future__ import annotations

from app.emoji_config import EMOJI

# ---------------------------------------------------------------------------
# Xabarlar ICHIDAGI premium (custom) emoji – HAMMASI app/emoji_config.py da.
# IDlarni O'SHA FAYLDA almashtiring — bu yerda faqat qisqa nomlar.
# ---------------------------------------------------------------------------
E_STATS = EMOJI.stats
E_USER = EMOJI.user
E_ID = EMOJI.idcard
E_LINK = EMOJI.link
E_EDIT = EMOJI.edit
E_TRASH = EMOJI.trash
E_INFO = EMOJI.info_tag
E_CHAT = EMOJI.chat
E_CLOCK = EMOJI.clock
E_MISSED = EMOJI.missed

# ---------------------------------------------------------------------------
# /start – asosiy menyu
# ---------------------------------------------------------------------------
WELCOME = (
    f"{EMOJI.ok.tag} <b>Xush kelibsiz!</b>\n\n"
    f"{EMOJI.report_user.tag} Men suhbatdoshlaringizning <b>o‘chirilgan va "
    "tahrirlangan xabarlaridan</b> sizni xabardor qilib turaman — "
    "hatto siz Telegramga kirmagan paytingizda ham.\n\n"
    f"{EMOJI.inbox.tag} <b>Bir martalik (View Once)</b> rasm, video yoki "
    "aylana videoga <b>«?» deb reply</b> qilsangiz, uni shaxsiy bot "
    "chatingizga saqlab beraman.\n\n"
    f"{EMOJI.info.tag} Faqat <b>sizning ruxsatingiz bilan</b> ishlayman.\n\n"
    "<b>Botni ulash:</b>\n"
    "<blockquote>1. <b>Ulanish</b> tugmasini bosing.\n"
    "2. <b>Telegram Business → Chatbotlar</b> bo‘limini oching va "
    "<b>@solutionteamrobot</b> ni qo‘shing.\n"
    f"3. Kerakli chatlar va ruxsatlarni tanlab, <b>Qo‘shish</b>ni bosing "
    f"— tayyor {EMOJI.ok.tag}</blockquote>"
)


def start_message(connected: bool | None) -> str:
    """Welcome screen with the connection status actually read from storage."""
    if connected is None:
        status = f"{EMOJI.paused_dot.tag} <b>Holat:</b> Hozir tekshirib bo‘lmadi"
    elif connected:
        status = f"{EMOJI.online_dot.tag} <b>Holat:</b> Ulangan"
    else:
        status = f"{EMOJI.offline_dot.tag} <b>Holat:</b> Ulanmagan"
    return f"{WELCOME}\n\n{status}"


MENU_HINT = f"Quyidagi amallardan birini tanlang {EMOJI.menu_hint.tag}"

# /start bosilganda, foydalanuvchi ALLAQACHON ulangan bo'lsa.
ALREADY_CONNECTED = (
    f"{EMOJI.online_dot.tag} <b>Siz allaqachon ulangansiz!</b>\n\n"
    "Bot akkauntingizga muvaffaqiyatli ulangan — kuzatuv ishlamoqda.\n"
    "Uzish uchun <i>Telegram Business → Chatbotlar</i> bo'limidan meni o'chirishingiz mumkin."
)

# ---------------------------------------------------------------------------
# "Ulanish"
# ---------------------------------------------------------------------------
CONNECT_TITLE = (
    f"{EMOJI.connect_title.tag} <b>Botni ulash</b>\n\n"
    "Quyidagi bosqichlarni bajaring (~30 soniya):\n\n"
    f"{EMOJI.step_one.tag} «{EMOJI.menu_settings.tag} Sozlamalarni ochish» tugmasini bosing\n"
    f"{EMOJI.step_two.tag} <b>Telegram Business</b> bo'limini oching\n"
    f"{EMOJI.step_three.tag} <b>Chatbotlar</b> bandida\n"
    f"{EMOJI.step_four.tag} <b>Bot qo'shish</b>ni tanlab, <b>@{{bot_username}}</b> ni tanlang\n\n"
    f"{EMOJI.ok.tag} Tayyor! Meni ulashingiz bilan shu yerga tasdiq xabari keladi — "
    "aloqa uzilsa ham darhol xabar beraman."
)

# ---------------------------------------------------------------------------
# "Statistika" — premium emoji bilan bezatilgan
# ---------------------------------------------------------------------------
STATS_TITLE = f"{E_STATS} <b>Sizning statistikangiz</b>\n\n{{body}}"

STATS_BODY = (
    f"{E_USER} Profil: {{mention}}\n"
    f"{E_ID} ID: <code>{{user_id}}</code>\n\n"
    f"{E_LINK} Ulanish: {{connection_line}}\n\n"
    f"{EMOJI.inbox.tag} Yozib olingan xabarlar: <b>{{total}}</b>\n"
    f"{E_EDIT} Tahrirlar: <b>{{edits}}</b>\n"
    f"{E_TRASH} O'chirishlar: <b>{{deletes}}</b>"
)

CONNECTED_LINE = f"{EMOJI.online_dot.tag} ulangan"
NOT_CONNECTED_LINE = f"{EMOJI.disconnected.tag} ulanmagan"

# ---------------------------------------------------------------------------
# Ulanish haqidagi xabarlar
# ---------------------------------------------------------------------------
BUSINESS_CONNECTED = (
    f"{EMOJI.online_dot.tag} <b>Ulanish amalga oshdi!</b>\n\n"
    "Endi men sizning Telegram akkauntingizga ulanganman.\n\n"
    "Xabar faoliyatingiz (tahrirlash, o'chirish, stiker, rasm, video, "
    "musiqa, fayl) endi "
    "shaxsiy hisobotlar bilan SHU chatga keladi — barcha ma'lumotlar faqat "
    "sizning botingizda, alohida saqlanadi.\n\n"
    "Xohlagan vaqtda <i>Telegram Business → Chatbotlar</i> bo'limidan "
    "uzishingiz mumkin."
)

BUSINESS_DISCONNECTED = (
    f"{EMOJI.offline_dot.tag} <b>Ulanish uzildi!</b>\n\n"
    "Akkauntingiz bilan bog'lanish faol emas, kuzatuv to'xtadi.\n"
    "Qayta ulash uchun <i>Telegram Business → Chatbotlar</i> bo'limidan "
    "meni qayta ulang."
)

BUSINESS_ENABLED_AGAIN = (
    f"{EMOJI.online_dot.tag} <b>Ulanish tiklandi!</b>\n\n"
    "Kuzatuv yana ishlayapti."
)

BUSINESS_DISABLED = (
    f"{EMOJI.paused_dot.tag} <b>Ulanish to'xtatildi.</b>\n\n"
    "Bot akkauntingizga nisbatan huquqlarini yo'qotdi. Bu sizning "
    "xohishingiz bo'lmasa, chatbot sozlamalarini tekshiring."
)

# ---------------------------------------------------------------------------
# Hisobotlar.
#
#   * Yuborilgan xabarlar FORVARD qilinmaydi — ular jim keshlanadi.
#   * Matn xabari  -> faqat TAHRIRLANDI yoki O'CHIRILDI deb xabar qilinadi.
#   * Media        -> faqat O'CHIRILDI deb xabar qilinadi (fayl qayta
#     yuboriladi).
#   * Hisobot faqat SUHBATDOSH hodisalari uchun: eganing o'z
#     tahrirlash/o'chirishlari hech qachon xabar qilib berilmaydi.
#
# Format services/reporter.py da yig'iladi, ushbu shablonlar shu yerda.
# ---------------------------------------------------------------------------
REPORT_EDIT = (
    f"{E_EDIT} <b>Xabar tahrirlandi</b>\n\n"
    f"{E_USER} Kim: {{who}}\n"
    f"{EMOJI.report_old.tag} <b>Default:</b>\n"
    f"<blockquote>{{old}}</blockquote>\n"
    f"{EMOJI.report_new.tag} <b>Edited:</b>\n"
    f"<blockquote>{{new}}</blockquote>"
)

REPORT_DELETED_MEDIA = (
    f"{E_TRASH} <b>{{kind}} o'chirildi</b>\n\n"
    f"{E_USER} Kim: {{who}}\n"
    f"{E_ID} Xabar: <code>{{mid}}</code>"
)

# Matn xabari o'chirilganda — ASL MATN bilan.
REPORT_DELETED_TEXT = (
    f"{E_TRASH} <b>Xabar o'chirildi</b>\n\n"
    f"{E_USER} Kim: {{who}}\n"
    f"{EMOJI.report_text.tag} Asl matn: {{original}}"
)

# Media o'chirilganda SAQLANGAN MEDIA QAYTA YUBORILADI — bu sarlavha media
# bilan birga boradi (reporter._resend_media).
REPORT_DELETED_MEDIA_CAPTION = (
    f"{E_TRASH} <b>{{kind}} o'chirildi</b>\n\n"
    f"{E_USER} Kim: {{who}}"
)

# Hisobot oxiridagi qatorlar.
REPORT_FOOTER = f"\n{E_CHAT} Chat: <b>{{chat}}</b>\n{E_CLOCK} Vaqt: <b>{{time}}</b>"
REPORT_FOOTER_DELETED = (
    f"\n{E_CHAT} Chat: <b>{{chat}}</b>\n{E_CLOCK} O'chirilgan: <b>{{time}}</b>"
)

REPORT_RESEND_FILE_FORM = (
    f"\n{E_MISSED} <i>Asl ko'rinishda yuborilmadi ({{reason}}) — "
    f"fayl sifatida yuborildi.</i>"
)
REPORT_RESEND_FAILED = (
    f"\n{E_MISSED} <i>Faylni ham yuborib bo'lmadi: {{reason}}</i>"
)

REPORT_VOICE_SETTING_HINT = (
    f"\n{E_INFO} <i>Ovozli xabarni Telegram maxfiylik sozlamangiz rad etdi. "
    f"Sozlamalar → Maxfiylik va xavfsizlik → Ovozli xabarlar → «Hamma» "
    f"qilib qo‘ying. Keyingi o‘chirilgan ovozlar asl ko‘rinishda yuboriladi.</i>"
)

REPORT_UNCACHED = (
    f"{E_MISSED} <b>O'chirilgan xabar keshda topilmadi</b>\n\n"
    f"{E_CHAT} Chat: <b>{{chat}}</b>\n"
    f"{E_ID} Xabarlar: <code>{{ids}}</code> — {{count}} ta\n"
    f"{E_CLOCK} O'chirilgan: <b>{{time}}</b>\n\n"
    f"<i>Bot bu xabarni umuman ko'rmagan, shuning uchun hisobot yo'q.\n"
    f"Sabab: xabar boshqa nusxa tomonidan qabul qilingan (eski build) yoki "
    f"bot ishga tushishidan oldin yuborilgan.</i>"
)

# Matn bo'sh yoki juda uzun bo'lganda.
NO_TEXT = "<i>(matn yo'q)</i>"
TRUNCATED = "…"

# "Kim"ni aniqlab bo'lmaganda (suhbatdosh haqida ma'lumot yo'q).
WHO_UNKNOWN = "Noma'lum"
# Chat nomi mavjud bo'lmaganda.
UNKNOWN_CHAT = "Noma'lum chat"

# ---------------------------------------------------------------------------
# Turli
# ---------------------------------------------------------------------------
UNKNOWN_ACTION = f"{EMOJI.unknown_action.tag} Noma'lum amal — quyidagi menyudan foydalaning."
ERROR_USER = f"{EMOJI.error.tag} Xatolik yuz berdi. Keyinroq qayta urinib ko'ring."

USER_BANNED = (
    f"{EMOJI.blocked.tag} <b>Sizning botdan foydalanishingiz vaqtincha bloklandi.</b>\n\n"
    "Agar bu xato deb hisoblasangiz, administrator bilan bog'laning."
)

USER_UNBANNED = (
    f"{EMOJI.ok.tag} <b>Sizning botdan foydalanishingiz qayta yoqildi.</b>\n\n"
    "Endi botdan yana odatdagidek foydalanishingiz mumkin."
)

# ---------------------------------------------------------------------------
# ADMIN ROLI HAQIDAGI SHAXSIY BILDIRISHNOMALAR — app/web.py
# ---------------------------------------------------------------------------
ADMIN_ROLE_ASSIGNED = (
    f"{EMOJI.admin_panel.tag} <b>Sizga admin panel huquqi berildi.</b>\n\n"
    "Yangi rolingiz: <b>{role}</b>\n"
    "Endi /admin orqali sizga berilgan bo'limlardan foydalanishingiz mumkin."
)

ADMIN_ROLE_CHANGED = (
    f"{EMOJI.role_changed.tag} <b>Admin panel rolingiz o'zgartirildi.</b>\n\n"
    "Eski rol: <b>{old_role}</b>\n"
    "Yangi rol: <b>{new_role}</b>"
)

ADMIN_ROLE_REMOVED = (
    "ℹ️ <b>Admin panel huquqingiz bekor qilindi.</b>\n\n"
    "Oldingi rolingiz: <b>{old_role}</b>\n"
    "Endi admin panelga kirish huquqingiz yo'q."
)

# ---------------------------------------------------------------------------
# HOLD (texnik xizmat) REJIMI — app/services/maintenance.py
# ---------------------------------------------------------------------------
# Hold mode YOQILGANDA oddiy foydalanuvchi ko'radigan xabar.
HOLD_MODE = (
    f"{EMOJI.maintenance.tag} <b>Bot vaqtincha ish faoliyatini to'xtatdi.</b>\n\n"
    "Hozir botda texnik ishlar olib borilmoqda.\n"
    "Bot qayta ishga tushganda sizga xabar beramiz."
)

# Hold mode O'CHIRILGANDAN keyin (ixtiyoriy, sozlama orqali) yuboriladi.
HOLD_RESUMED = (
    f"{EMOJI.online_dot.tag} <b>Bot yana ishga tushdi!</b>\n\n"
    "Texnik ishlar yakunlandi.\n"
    f"Endi botdan odatdagidek foydalanishingiz mumkin. {EMOJI.ok.tag}"
)

# ---------------------------------------------------------------------------
# IKKI NUSXA (409 Conflict) — faqat adminga (app/services/duplicate_watch.py)
# ---------------------------------------------------------------------------
DUPLICATE_POLLER = (
    f"{EMOJI.warning.tag} <b>DIQQAT: botni IKKI nusxa poll qilmoqda</b>\n\n"
    "Telegram bitta token uchun faqat BITTA nusxaga xabar beradi, shuning "
    "uchun ikkinchi nusxa bilan navbatma-navbat to'qnashyapmiz "
    "(<code>409 Conflict</code>). Natijada update'lar ikki nusxa orasida "
    "bo'linib ketadi: bot ba'zi xabarlarni ko'radi, ba'zilarini ko'rmaydi "
    "va ba'zi hisobotlar umuman kelmaydi.\n\n"
    "Nima qilish kerak:\n"
    "1. Boshqa kompyuter / terminal / VS Code oynasidagi "
    "<code>python run.py</code> ni to'xtating.\n"
    "2. Serverga (Railway / Render / VPS) deploy qilingan nusxa bo'lsa — "
    "uni ham to'xtating yoki eng oxirgi kod bilan yangilang.\n"
    "3. Botni faqat BITTA joyda ishga tushiring.\n\n"
    "Aniqlangan <code>409</code> xatolari: <b>{count}</b>\n"
    "<i>Bu ogohlantirish 30 daqiqada bir martadan ko'p kelmaydi.</i>"
)
