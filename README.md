# FATLLM: دسترسی کامل Claude به سرور

یک دستور روی سرور می‌زنی، یک **کلید** می‌گیری.
کلید را به Claude می‌دهی و Claude با دسترسی root سرور را مدیریت می‌کند.

## ۱. نصب (روی سرور، با root)

```bash
bash <(curl -Ls https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD/install.sh)
```

آخرش یک کلید چاپ می‌شود، شبیه این:

```
https://1-2-3-4.sslip.io/fat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- دامنه لازم نیست. خودش با `sslip.io` گواهی HTTPS واقعی می‌گیرد.
- اگر پورت 443 پر بود (مثلاً x-ui)، پورت دیگری برمی‌دارد (8443، 2053، …).
- اگر پورت 80 و 443 هر دو پر بودند، خودکار از تونل Cloudflare استفاده می‌کند.
- دوباره زدن همین دستور یعنی تعمیر یا آپدیت؛ کلید عوض نمی‌شود.

گزینه‌ها (بعد از دستور بنویس):
- `--domain my.site.com` دامنه‌ی خودت (رکورد A باید به سرور اشاره کند)
- `--port 8443` پورت دلخواه
- `--tunnel` فقط تونل Cloudflare (بدون باز کردن پورت)
- `--expire 7d` کلید بعد از ۷ روز باطل شود

## ۲. دادن کلید به Claude

**الف) Claude Code (تب Code در اپ):**
- یک جلسه روی همین ریپو باز کن.
- کلید را بفرست و بگو: «این کلید سرورمه، کار X رو انجام بده».
- تنظیمات این ریپو اجازه را از قبل داده؛ Claude برای هر دستور تأیید نمی‌خواهد.
- برای اینکه هر بار کلید نفرستی: در تنظیمات محیط ابری، متغیر `FATLLM_KEY` را برابر کلید بگذار.

**ب) claude.ai (چت معمولی):**
- Settings → Connectors → Add custom connector
- URL = کلید + `/mcp` (دستور `fatllm key` همین را هم چاپ می‌کند)
- در تنظیمات همان کانکتور، ابزارها را روی **Always allow** بگذار تا تأیید نخواهد.

**ج) هر جلسه‌ی دیگر Claude Code:** فقط کلید را بفرست. Claude با curl راهنمای داخل کلید را می‌خواند و کار می‌کند (ممکن است برای دستورها تأیید بخواهد).

### شبکه‌ی محیط ابری
Claude Code در ابر فقط به دامنه‌های مجاز وصل می‌شود. در تنظیمات محیط (Network access):
- یا **Full** بگذار،
- یا این‌ها را به Allowed domains اضافه کن: `*.sslip.io` (یا دامنه‌ی خودت) و `*.trycloudflare.com`.

## ۳. دستورهای سرور

```
fatllm key          نمایش کلید
fatllm rotate       کلید تازه؛ کلید قبلی فوراً باطل می‌شود
fatllm off / on     خاموش و روشن کردن دسترسی
fatllm status       وضعیت و تست
fatllm log          کارهایی که Claude کرده
fatllm expire 12h   کلید بعد از ۱۲ ساعت باطل شود (never = هیچ‌وقت)
fatllm update       آپدیت، کلید می‌ماند
fatllm uninstall    حذف کامل
```

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
