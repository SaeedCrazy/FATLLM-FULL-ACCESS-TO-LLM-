# FATLLM: دسترسی کامل Claude به سرور

یک دستور روی سرور می‌زنی، یک **کلید** می‌گیری.
کلید را به Claude می‌دهی و Claude با دسترسی root سرور را مدیریت می‌کند.

## خلاصه: سه قدم

**۱. روی سرور این را paste کن** (هر کاربری؛ اگر root نباشی خودش sudo می‌زند):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD/install.sh)
```

هیچ سؤالی نمی‌پرسد. آخرش یک خط چاپ می‌کند:

```
FATLLM key: https://1-2-3-4.sslip.io/fat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**۲. یک بار در تنظیمات محیط ابری Claude Code** (منوی محیط بالای جلسه → Edit):
- Network access را **Full** کن.
- (اختیاری) متغیر `FATLLM_KEY` را برابر کلید بگذار تا دیگر هیچ‌وقت کلید نفرستی.

**۳. یک جلسه روی همین ریپو باز کن** و آن خط را بفرست، بعد کارت را بگو.
- Claude خودش وصل می‌شود و برای دستورها تأیید نمی‌خواهد.
- اگر `FATLLM_KEY` را گذاشته باشی، از اول جلسه وصل است؛ فقط کارت را بگو.

### خودش چه کارهایی می‌کند
- پایتون و curl را اگر نباشد نصب می‌کند.
- گواهی HTTPS واقعی می‌گیرد؛ دامنه لازم نیست (`sslip.io`).
- اگر 443 پر بود (مثلاً x-ui) پورت دیگری برمی‌دارد و فایروال ufw/firewalld را باز می‌کند.
- اگر 80 و 443 هر دو پر بودند، تونل Cloudflare می‌زند.
- دوباره زدن همین دستور یعنی تعمیر یا آپدیت؛ کلید عوض نمی‌شود.

گزینه‌های اختیاری (بعد از دستور): `--domain my.site.com`، `--port 8443`، `--tunnel`، `--expire 7d`.

### راه دیگر: چت معمولی claude.ai
Settings → Connectors → Add custom connector، آدرس = کلید + `/mcp`.
در تنظیمات همان کانکتور ابزارها را روی **Always allow** بگذار.

## دستورهای سرور

```
fatllm key          نمایش کلید
fatllm rotate       کلید تازه؛ کلید قبلی فوراً باطل می‌شود
fatllm off / on     خاموش و روشن کردن دسترسی
fatllm status       وضعیت و تست
fatllm log          کارهایی که Claude کرده
fatllm expire 12h   کلید بعد از ۱۲ ساعت باطل شود (never = هیچ‌وقت)
fatllm relay ...    وصل کردن یک سرور دیگر (پایین را ببین)
fatllm update       آپدیت، کلید می‌ماند
fatllm uninstall    حذف کامل
```

## وصل کردن دو سرور (رله): هم سایت ایرانی، هم خارجی

«رله» یعنی یک سرور، ترافیکی که خودش به آن نمی‌رسد را از طریق سرور دیگر می‌فرستد.
- سایت‌های ایرانی (میکسین، درگاه‌ها) IP خارجی را بلاک می‌کنند → باید از IP ایران بروی.
- سایت‌های خارجی (تلگرام، AI) از داخل ایران فیلترند → باید از IP خارج بروی.

**روش پیشنهادی:** FATLLM روی **سرور خارج** (چون گواهی HTTPS و اتصال Claude راحت است)، و یک رله به **سرور ایران**. بعد روی همان سرور خارج این را بزن:

```
fatllm relay add --host <آی‌پی سرور ایران> --user root --name iran
```

پسوردِ روتِ سرور ایران را یک‌بار (پنهان) می‌پرسد؛ ذخیره‌اش **نمی‌کند**، فقط با آن یک کلید SSH نصب می‌کند و از آن به بعد با کلید کار می‌کند. (برای خودکار: `echo 'پسورد' | fatllm relay add --host <ip> --name iran`.)

بعدش:
- سایت ایرانی: به Claude بگو «با رله‌ی iran چک کن» → `srv --via iran 'curl -s https://mixin.ir'` (از IP ایران بیرون می‌رود).
- بقیه: عادی، مستقیم از سرور خارج.

دستورهای رله:
```
fatllm relay list           رله‌ها و اینکه تونل هرکدام بالاست یا نه
fatllm relay test iran      آی‌پی این سرور در برابر آی‌پی‌ای که از رله بیرون می‌رود
fatllm relay remove iran    حذف رله
```

اگر برعکس، FATLLM را روی سرور ایران گذاشتی، رله را به سرور خارج بزن تا کارهای تلگرام/AI «via» سرور خارج بروند. (نکته: گرفتن گواهی Let's Encrypt از داخل ایران گاهی مشکل دارد؛ برای همین گذاشتن FATLLM روی سرور خارج ساده‌تر است.)

## امنیت
- کلید یعنی **root کامل**. به کسی نده، جایی نگذار.
- اگر لو رفت: `fatllm rotate`. اگر کار نداری: `fatllm off`.
- همه‌چیز روی HTTPS است. کلید ۲۵۶ بیتی است و درخواست بدون کلید فقط 404 می‌گیرد.
- همه‌ی دستورهای Claude در `/var/log/fatllm/audit.log` ثبت می‌شود (`fatllm log`).

## اگر کار نکرد
- `fatllm status` را بزن و خروجی‌اش را به Claude بده.
- پورتی که آخر نصب گفته را در فایروال پنل سرور (Hetzner، Arvan، …) باز کن.
- در حالت تونل، آدرس بعد از ری‌استارت سرور عوض می‌شود؛ `fatllm key` را دوباره بزن.

---

### English (short)
`install.sh` installs a small Python agent (stdlib only) on 127.0.0.1 with HTTPS in front
(its own Caddy + Let's Encrypt on `<ip>.sslip.io`, or a Cloudflare quick tunnel), and prints a
key `https://host/fat_<token>`. `GET <key>` returns a usage guide for the AI; `POST <key>/run`
runs bash as root; `<key>/mcp` is an MCP server (Streamable HTTP) for claude.ai custom
connectors. Manage it with `fatllm` on the server. Tests: `python3 -m unittest discover -s test -v`.
