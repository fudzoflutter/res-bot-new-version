# Premium obuna — manual karta + chek

Bu versiyada Premium tizimi default holatda **OFF**.

## OFF holati
- User menyusida Premium tugmasi ko'rinmaydi.
- View Once uchun `?` avvalgidek bepul ishlaydi.
- O'chirilgan/tahrirlangan xabar monitoringi o'zgarmaydi.

## ON holati
Admin Mini App → **Menyu → Obuna va to'lovlar**:
1. Narx, muddat, karta raqami va karta egasini kiriting.
2. Premium rejim switch'ini yoqing.
3. `Sozlamalarni saqlash` ni bosing.

Shundan keyin:
- User menyusida `💎 Premium obuna` ko'rinadi.
- Karta raqami tugmasining o'zi bosilganda Telegram `copy_text` orqali nusxalanadi.
- User `✅ To'lov qildim` → JPG/PNG/WEBP yoki PDF chek yuboradi (max 10 MB).
- Bir userda bir vaqtning o'zida faqat bitta PENDING chek bo'ladi.
- Admin paneldan chekni ko'rib `Tasdiqlash` yoki `Rad etish` mumkin.
- Tasdiqlansa tarif muddati mavjud aktiv muddat ustiga qo'shiladi.
- Admin user profilidan +7/+30/+90 kun, aniq sana, cheksiz obuna yoki bekor qilishni boshqaradi.

## View Once Premium gate
Premium rejim ON bo'lganda `?` yuborilganda avval connection egasining obunasi tekshiriladi.
Obuna ACTIVE bo'lmasa media `get_file/download` qilinmaydi; private bot chatiga Premium kerakligi haqida xabar va `💎 Obuna sotib olish` tugmasi yuboriladi.

## Database
`subscriptions` va `payment_requests` jadvallari startup paytida avtomatik yaratiladi (Postgres ham, SQLite fallback ham).
Qo'shimcha SQL fayl: `migrations/002_subscriptions.sql`.
Yangi ENV o'zgaruvchisi talab qilinmaydi.
