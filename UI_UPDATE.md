# SolutionTeam Bot — sodda UI

- Tasdiqlangan sodda dizayn: oddiy ro‘yxatlar, kamroq bezak va yagona ranglar tizimi.
- Sarlavha: SolutionTeam, uning pastida Bot.
- Oq/to‘q fon, ko‘k amallar, yashil faol holat va qizil cheklovlar.
- Telegram mavzusiga mos ochiq/to‘q ranglar va themeChanged yangilanishi.
- Asosiy sahifadagi takroriy tezkor tugmalar olib tashlandi.
- Qidiruv Userlarda; Reklama, Bloklash, Sozlamalar, Adminlar va Profil Menyuda.
- User kartalarida ism, username, ulanish holati va profilni ochish saqlandi.
- ID nusxalash, bloklash/blokdan olish, o‘chirish, faollik va bevosita xabar user profilida ishlaydi.
- Xabarlar jurnali sahifasi, ruxsati va API yo‘li olib tashlangan.
- API avtorizatsiyasi, serverdagi tekshiruvlar va boshqa backend funksiyalar o‘zgartirilmadi.

## Tekshiruv

Python/JavaScript sintaksisi va statik UI tekshiruvlari o‘tdi: qolgan boshqaruv IDlari, sahifa nishonlari va JavaScript element bog‘lanishlari saqlangan.
Brauzer regressiya testlari yangi profil joylashuviga moslashtirildi, ammo muhit cheklovi sabab bajarilmadi. pytest o‘rnatilmagan.
Telegram Mini App ichida real yakuniy tekshiruv talab qilinadi.

## O‘rnatish

To‘liq loyiha ZIP ichida. Faqat UI yangilanishi uchun public/index.html, public/app.js va public/style.css birgalikda almashtiriladi.
Agar eski, jurnal mavjud versiyadan o‘tilayotgan bo‘lsa, ZIPdagi app/web.py va app/services/permissions.py o‘zgarishlari ham kerak.
.env va ishlab turgan ma’lumotlar bazasini saqlang. Ushbu arxiv deploy qilinmagan.
