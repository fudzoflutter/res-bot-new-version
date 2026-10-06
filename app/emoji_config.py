"""
Barcha EMOJILAR BITTA JOYDA.

Premium (animatsion) emoji IDlarini shu faylda almashtirsangiz, butun botda
darhol almashadi — handlerlarni ochib o'tirish shart emas.

QANDAY ISHLAYDI:
  * har bir maydon 2 qismdan iborat: ID + fallback (oddiy emoji);
  * ID to'ldirilmagan bo'lsa (""), oddiy emoji ko'rinadi;
  * IDlar Telegram Premium emoji paketlaridan olinadi — bot akkaunti
    Premium/Fragment egasi bo'lsa animatsion ko'rinadi, aks holda Telegram
    o'zi oddiy emoji bilan almashtiradi.

FAOL — kodda ishlatiladi; ZAXIRA — hozir hech qayerda ishlatilmaydi.
ID borligi Telegram'da haqiqatan animatsiya ko'ringanini tasdiqlamaydi.
.tag: HTML xabarlar uchun; .plain: HTML ishlamaydigan alert/status/preview uchun.
View Once rasm/video captionda, aylana video esa alohida tasdiq xabarida chiqadi.
Bular custom emoji (stiker fayli emas); emoji_id ni o'zingiz kiriting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# PREMIUM EMOJI YOQISH KALITI (muhim!)
#
# Telegram FAQAT Fragment'da qo'shimcha username/raqam sotib olgan botlarga
# xabar MATNIDA <tg-emoji> yuborishga ruxsat beradi.  Oddiy botda har qanday
# <tg-emoji> "Bad Request: DOCUMENT_INVALID" xatosini beradi — butun ekran
# ochilmaydi.  Shuning uchun standartda O'CHIQ.
#
# Bot akkountiga Fragment kolleksiyoni olganingizdan keyin True qiling.
# ---------------------------------------------------------------------------
ENABLE_PREMIUM_EMOJI_TAGS = True


def tg_e(emoji_id: str | None, fallback: str) -> str:
    """Premium emoji uchun <tg-emoji> tegi.

    Kalit o'chiq bo'lsa YOKI ID bo'sh bo'lsa — oddiy emoji qaytadi
    (Telegram xato bermasligi uchun)."""
    if not ENABLE_PREMIUM_EMOJI_TAGS or not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


@dataclass(frozen=True)
class EmojiEntry:
    """Bitta emoji: premium ID (bo'sh bo'lsa oddiy fallback ko'rinadi).

    ``emoji_id`` ni to'ldirsangiz: xabar ICHIDA ``<tg-emoji>`` ko'rinishida
    (kalit yoqilgan bo'lsa), tugmalarda esa ``icon_custom_emoji_id`` bo'lib
    chiqadi — kodni boshqa joyda o'zgartirish shart emas.
    """

    emoji_id: str
    fallback: str

    @property
    def tag(self) -> str:
        """HTML xabarlar ICHIDA ishlatiladigan ko'rinish."""
        return tg_e(self.emoji_id, self.fallback)

    @property
    def plain(self) -> str:
        """HTML BO'LMAGAN joylar uchun (alert, caption, tugma matni)."""
        return self.fallback


@dataclass(frozen=True)
class EmojiConfig:
    """Butun botdagi emoji yozuvlari — EKRAN BO'YICHA guruhlangan."""

    # ------------------------------------------------------------------
    # /start ASOSIY MENYU (tugma ikonkalari — `icon_custom_emoji_id`)
    # ------------------------------------------------------------------
    # FAOL: menu_stats
    menu_stats: EmojiEntry = field(default_factory=lambda: EmojiEntry("5368324170671202286", "📊"))
    # FAOL: menu_connect
    menu_connect: EmojiEntry = field(default_factory=lambda: EmojiEntry("5333163668629442693", "🔗"))
    # FAOL: menu_settings
    menu_settings: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "⚙️"))
    # FAOL: menu_back
    menu_back: EmojiEntry = field(default_factory=lambda: EmojiEntry("5445585590822480435", "🔙"))

    # ------------------------------------------------------------------
    # FOYDALANUVCHI EKRANLARI (xabar matni ichida — <tg-emoji>)
    # ------------------------------------------------------------------
    # FAOL: stats_header
    stats_header: EmojiEntry = field(default_factory=lambda: EmojiEntry("5368324170671202286", "📊"))
    # FAOL: inbox
    inbox: EmojiEntry = field(default_factory=lambda: EmojiEntry("5472239203590888751", "📥"))
    # FAOL: info
    info: EmojiEntry = field(default_factory=lambda: EmojiEntry("5368324170671202286", "ℹ️"))
    # FAOL: connect_title
    connect_title: EmojiEntry = field(default_factory=lambda: EmojiEntry("5271604874419647061", "🔗"))
    # FAOL: online_dot
    online_dot: EmojiEntry = field(default_factory=lambda: EmojiEntry("5215522595922779944", "🟢"))
    # FAOL: offline_dot
    offline_dot: EmojiEntry = field(default_factory=lambda: EmojiEntry("5411225014148014586", "🔴"))
    # FAOL: paused_dot
    paused_dot: EmojiEntry = field(default_factory=lambda: EmojiEntry("6271271702408204490", "🟡"))
    # ZAXIRA — hozir chaqirilmaydi: users
    users: EmojiEntry = field(default_factory=lambda: EmojiEntry("5958460691550572213", "👥"))

    # ------------------------------------------------------------------
    # HISOBOTLAR (tahrirlandi / o'chirildi)
    # ------------------------------------------------------------------
    # FAOL: report_edit
    report_edit: EmojiEntry = field(default_factory=lambda: EmojiEntry("5395444784611480792", "✏️"))
    # FAOL: report_delete
    report_delete: EmojiEntry = field(default_factory=lambda: EmojiEntry("5445267414562389170", "🗑"))
    # FAOL: report_user
    report_user: EmojiEntry = field(default_factory=lambda: EmojiEntry("5818715087237549366", "👤"))
    # FAOL: report_id
    report_id: EmojiEntry = field(default_factory=lambda: EmojiEntry("5974526806995242353", "🆔"))
    # FAOL: report_text
    report_text: EmojiEntry = field(default_factory=lambda: EmojiEntry("5334882760735598374", "📝"))
    # FAOL: report_old
    report_old: EmojiEntry = field(default_factory=lambda: EmojiEntry("5269609320944772058", "📱"))
    # FAOL: report_new
    report_new: EmojiEntry = field(default_factory=lambda: EmojiEntry("6046412391887935443", "📲"))
    # FAOL: report_chat
    report_chat: EmojiEntry = field(default_factory=lambda: EmojiEntry("5240369183593615679", "💬"))
    # FAOL: report_clock
    report_clock: EmojiEntry = field(default_factory=lambda: EmojiEntry("6323361327767099558", "🕒"))
    # Diagnostika: o'chirilgan xabar keshda umuman topilmadi (hisobot chiqmaydi).
    # FAOL: report_missed
    report_missed: EmojiEntry = field(default_factory=lambda: EmojiEntry("5420323339723881652", "⚠️"))

    # ------------------------------------------------------------------
    # HOLAT BELGILARI (oddiy emoji — semantik)
    # ------------------------------------------------------------------
    # FAOL: ok
    ok: EmojiEntry = field(default_factory=lambda: EmojiEntry("5355278615531495452", "✅"))

    # ------------------------------------------------------------------
    # SAHIFALASH (admin panel ro'yxatlari)
    #   page_prev/next  ⬅️ ➡️
    # ------------------------------------------------------------------
    # ZAXIRA — hozir chaqirilmaydi: page_prev
    page_prev: EmojiEntry = field(default_factory=lambda: EmojiEntry("5976535107933050770", "⬅️"))
    # ZAXIRA — hozir chaqirilmaydi: page_next
    page_next: EmojiEntry = field(default_factory=lambda: EmojiEntry("5435955998479102657", "➡️"))

    # .plain joylarda Premium HTML teglar qo‘llanmaydi (alert/status/Web UI).
    # YANGI EMOJILAR — FAOL; bo‘sh IDlarni Premium ID bilan almashtiring.
    # FAOL: View Once rasm
    # FAOL: view_once_photo
    view_once_photo: EmojiEntry = field(default_factory=lambda: EmojiEntry("5222038117145392675", "🖼"))
    # FAOL: View Once video
    # FAOL: view_once_video
    view_once_video: EmojiEntry = field(default_factory=lambda: EmojiEntry("5463200135678796607", "🎬"))
    # FAOL: View Once aylana video
    # FAOL: view_once_video_note
    view_once_video_note: EmojiEntry = field(default_factory=lambda: EmojiEntry("5328108441963604719", "📹"))
    # FAOL: Salomlashish
    # FAOL: welcome
    welcome: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "👋"))
    # FAOL: Menyu yo‘riqnomasi
    # FAOL: menu_hint
    menu_hint: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "👇"))
    # FAOL: Ulash 1-qadam
    # FAOL: step_one
    step_one: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "1️⃣"))
    # FAOL: Ulash 2-qadam
    # FAOL: step_two
    step_two: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "2️⃣"))
    # FAOL: Ulash 3-qadam
    # FAOL: step_three
    step_three: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "3️⃣"))
    # FAOL: Ulash 4-qadam
    # FAOL: step_four
    step_four: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "4️⃣"))
    # FAOL: Ulanmagan holat
    # FAOL: disconnected
    disconnected: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "⚪️"))
    # FAOL: Noma’lum amal
    # FAOL: unknown_action
    unknown_action: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🤔"))
    # FAOL: Xatolik xabari
    # FAOL: error
    error: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "😔"))
    # FAOL: Bloklangan foydalanuvchi
    # FAOL: blocked
    blocked: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🚫"))
    # FAOL: Admin xabari va tugmasi
    # FAOL: admin_panel
    admin_panel: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🛡"))
    # FAOL: Rol o‘zgartirildi
    # FAOL: role_changed
    role_changed: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🔄"))
    # FAOL: Texnik xizmat
    # FAOL: maintenance
    maintenance: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🔧"))
    # FAOL: Hold paytida ulanish
    # FAOL: maintenance_connection
    maintenance_connection: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🛠"))
    # FAOL: Ogohlantirish
    # FAOL: warning
    warning: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "⚠️"))
    # FAOL: O‘chirilgan/failed holat
    # FAOL: failed
    failed: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "❌"))
    # FAOL: Kritik alert
    # FAOL: critical
    critical: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🚨"))
    # FAOL: Owner roli, plain
    # FAOL: role_owner
    role_owner: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "👑"))
    # FAOL: Moderator roli, plain
    # FAOL: role_moderator
    role_moderator: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "⚖️"))
    # FAOL: Viewer roli, plain
    # FAOL: role_viewer
    role_viewer: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "👁"))
    # FAOL: Reklama preview, plain
    # FAOL: preview_media
    preview_media: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🖼"))
    # FAOL: Reklama tugmalari preview, plain
    # FAOL: preview_buttons
    preview_buttons: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🔘"))
    # FAOL: Watchdog baza xabari
    # FAOL: database
    database: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "🗄"))
    # FAOL: Watchdog log xabari
    # FAOL: activity_log
    activity_log: EmojiEntry = field(default_factory=lambda: EmojiEntry("", "📋"))

    # ------------------------------------------------------------------
    # QULAYLIK: qisqa nomlar (texts.py ishlatadigan E_STATS, E_USER, ...)
    # ------------------------------------------------------------------
    @property
    def stats(self) -> str:
        return self.stats_header.tag

    @property
    def user(self) -> str:
        return self.report_user.tag

    @property
    def idcard(self) -> str:
        return self.report_id.tag

    @property
    def link(self) -> str:
        return self.connect_title.tag

    @property
    def edit(self) -> str:
        return self.report_edit.tag

    @property
    def trash(self) -> str:
        return self.report_delete.tag

    @property
    def info_tag(self) -> str:
        return self.info.tag

    @property
    def chat(self) -> str:
        return self.report_chat.tag

    @property
    def clock(self) -> str:
        return self.report_clock.tag

    @property
    def missed(self) -> str:
        return self.report_missed.tag


EMOJI = EmojiConfig()
"""Global emoji registry — `from app.emoji_config import EMOJI` qiling."""
