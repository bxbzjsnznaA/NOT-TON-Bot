# NOT Telegram Bot — Railway

مشروع Python لبوت NOT مع صفحة TON Connect لربط عنوان محفظة TON.

## الملفات
- `bot.py`: بوت Telegram وخادم FastAPI وصفحة Mini App.
- `web/index.html`: واجهة ربط محفظة TON عبر TON Connect.
- `requirements.txt`: المكتبات.
- `Procfile` و`railway.toml`: إعداد تشغيل Railway.

## النشر على Railway
1. ارفع محتويات هذا المجلد إلى GitHub Repository.
2. في Railway اختر New Project ثم Deploy from GitHub Repo.
3. أضف متغير `BOT_TOKEN` بقيمة توكن BotFather.
4. أضف Volume واربطه بالمسار `/data`.
5. أضف متغير `DB_PATH` وقيمته `/data/not_bot.sqlite3`.
6. انشر الخدمة، ثم فعّل Public Networking وأنشئ نطاقاً HTTPS.
7. أضف متغير `WEBAPP_URL` بقيمة النطاق كاملاً، مثال `https://your-app.up.railway.app`.
8. أعد Deploy بعد إضافة WEBAPP_URL.

## ملاحظات مهمة
- الخدمة تشغّل FastAPI وTelegram polling في العملية نفسها؛ لا تنشئ نسخة ثانية من الخدمة بنفس التوكن.
- صفحة TON Connect لا تطلب seed phrase أو private key ولا توقع معاملات.
- عند الاتصال، يحفظ البوت عنوان TON العام في حساب المستخدم.
- تأكد من أن صفحة Railway تعمل عبر HTTPS حتى يعمل TON Connect.
- التبديل والسحب في هذا الإصدار يسجلان طلبات فقط؛ لا يوجد مزود دفع أو تنفيذ تحويلات مالية.
- هذا مشروع بداية يحتاج اختبارات ومراجعة أمنية قبل استخدامه مع أموال حقيقية.
