import os
import re
import io
import json
import base64
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
STORAGE_GROUP_ID = -5237650194

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory: storage message ID (survives as long as bot runs)
storage_msg_id = None

# In-memory session
user_session = {"state": "idle", "active_invoice": None, "current_sku": None, "pending_products": []}

INVOICE_PROMPT = """You are an invoice parser. Extract all product lines from this invoice.
For each product line extract SKU/Model number, product name, quantity, unit cost.
Respond ONLY with a JSON array:
[{"sku": "1308", "name": "Dress Embroidery", "qty": 12, "cost": 8.00}]
"""

COLOR_PARSE_PROMPT = """Parse color, size and quantity info from natural language.
Respond ONLY with JSON:
{"colors": {"BLACK": {"code": "BLK", "packs": 3}}, "sizes": ["S","M","L"], "units_per_size": 2}

Color codes: BLACK/siyah=BLK, WHITE/beyaz=WHT, BLUE/mavi=BLU, BEIGE/bej=BGE,
PINK/pembe=PNK, RED/kirmizi=RED, GREEN/yesil=GRN, GREY/gri=GRY, BROWN/kahve=BRN,
ORANGE/turuncu=ORG, PURPLE/mor=PRP

Examples:
"3siyah 3beyaz 2S2M2L" → BLACK=3, WHITE=3, sizes=[S,M,L], units=2
"4black 4red 4blue 2S2M2L" → BLACK=4, RED=4, BLUE=4, sizes=[S,M,L], units=2
"""

# ── STORAGE ───────────────────────────────────────────────

async def storage_load(app):
    global storage_msg_id
    try:
        chat = await app.bot.get_chat(STORAGE_GROUP_ID)
        if chat.pinned_message and chat.pinned_message.text and chat.pinned_message.text.startswith("INVDATA:"):
            storage_msg_id = chat.pinned_message.message_id
            compressed = json.loads(chat.pinned_message.text[8:])
            # Decompress: expand short keys back to full format
            invoices = {}
            for name, inv in compressed.items():
                invoices[name] = {"products": []}
                for p in inv["products"]:
                    full = {
                        "sku": p.get("s", p.get("sku", "")),
                        "name": p.get("n", p.get("name", "")),
                        "qty": p.get("q", p.get("qty", 0)),
                        "cost": p.get("c", p.get("cost", 0)),
                        "completed": p.get("done", p.get("completed", False)),
                    }
                    if p.get("col") or p.get("colors"): full["colors"] = p.get("col", p.get("colors"))
                    if p.get("sz") or p.get("sizes"): full["sizes"] = p.get("sz", p.get("sizes"))
                    if p.get("ups") or p.get("units_per_size"): full["units_per_size"] = p.get("ups", p.get("units_per_size"))
                    if p.get("sp") or p.get("sale_price"): full["sale_price"] = p.get("sp", p.get("sale_price"))
                    if p.get("skip"): full["skipped"] = True
                    invoices[name]["products"].append(full)
            return invoices
    except Exception as e:
        logger.error(f"Storage load error: {e}")
    return {}


async def storage_save(app, invoices):
    global storage_msg_id
    # Compress data to fit Telegram 4096 char limit
    compressed = {}
    for name, inv in invoices.items():
        compressed[name] = {"products": []}
        for p in inv["products"]:
            small = {"s": p["sku"], "n": p["name"][:20], "q": p["qty"], "c": p["cost"], "done": p.get("completed", False)}
            if p.get("colors"): small["col"] = p["colors"]
            if p.get("sizes"): small["sz"] = p["sizes"]
            if p.get("units_per_size"): small["ups"] = p["units_per_size"]
            if p.get("sale_price"): small["sp"] = p["sale_price"]
            if p.get("skipped"): small["skip"] = True
            compressed[name]["products"].append(small)
    text = "INVDATA:" + json.dumps(compressed, ensure_ascii=False, separators=(",", ":"))
    try:
        if storage_msg_id:
            try:
                await app.bot.edit_message_text(text, STORAGE_GROUP_ID, storage_msg_id)
                return
            except Exception:
                pass
        msg = await app.bot.send_message(STORAGE_GROUP_ID, text)
        storage_msg_id = msg.message_id
        try:
            await app.bot.pin_chat_message(STORAGE_GROUP_ID, storage_msg_id, disable_notification=True)
        except Exception as e:
            logger.warning(f"Could not pin: {e}")
    except Exception as e:
        logger.error(f"Storage save error: {e}")


# ── EXCEL ─────────────────────────────────────────────────

def create_excel(products, invoice_name):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Items"
    headers = [
        "Reference Handle", "Token", "Item Name", "Customer-facing Name",
        "Variation Name", "SKU", "Description", "Categories", "Reporting Category",
        "GTIN", "Item Type", "Weight (lb)", "Social Media Link Title",
        "Social Media Link Description", "Price", "Online Sale Price", "Archived",
        "Sellable", "Contains Alcohol", "Stockable", "Skip Detail Screen in POS",
        "Option Name 1", "Option Value 1", "Default Unit Cost", "Default Vendor Name",
        "Default Vendor Code", "Current Quantity GAVA NEW YORK", "New Quantity GAVA NEW YORK",
        "Stock Alert Enabled GAVA NEW YORK", "Stock Alert Count GAVA NEW YORK"
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", name="Arial")
        cell.fill = PatternFill("solid", start_color="1F3864")
        cell.alignment = Alignment(horizontal="center")

    for p in products:
        description = f"{p['name']} — available in multiple colors and sizes."
        for color_name, color_info in p["colors"].items():
            qty_per_size = color_info["packs"] * p["units_per_size"]
            for size in p["sizes"]:
                size_slug = size.lower().replace("/", "-")
                handle = f"#{p['sku']}-{p['name'].lower().replace(' ', '-')}-{color_name.lower()}-{size_slug}"
                sku_var = f"{p['sku']}-{color_info['code']}-{size}"
                row = {
                    "Reference Handle": handle, "Token": None,
                    "Item Name": p["name"], "Customer-facing Name": p["name"],
                    "Variation Name": f"{p['name']} {color_name} / {size}",
                    "SKU": sku_var, "Description": description,
                    "Categories": p.get("category", ""), "Reporting Category": None, "GTIN": None,
                    "Item Type": "Physical good", "Weight (lb)": None,
                    "Social Media Link Title": None, "Social Media Link Description": None,
                    "Price": p.get("sale_price", 0), "Online Sale Price": None, "Archived": "N",
                    "Sellable": None, "Contains Alcohol": "N", "Stockable": None,
                    "Skip Detail Screen in POS": "N", "Option Name 1": None, "Option Value 1": None,
                    "Default Unit Cost": p["cost"], "Default Vendor Name": p.get("vendor", ""),
                    "Default Vendor Code": None,
                    "Current Quantity GAVA NEW YORK": qty_per_size,
                    "New Quantity GAVA NEW YORK": qty_per_size,
                    "Stock Alert Enabled GAVA NEW YORK": None,
                    "Stock Alert Count GAVA NEW YORK": None,
                }
                ws.append([row.get(h) for h in headers])

    for col in ws.columns:
        max_len = max((len(str(cell.value)) if cell.value else 0) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def build_summary(products, invoice_name):
    lines = [f"✅ *{invoice_name} — Excel hazır!*\n"]
    total = 0
    for p in products:
        lines.append(f"📦 *{p['sku']} - {p['name']}*")
        for color_name, color_info in p["colors"].items():
            qty = color_info["packs"] * p["units_per_size"]
            for size in p["sizes"]:
                lines.append(f"  • {color_name}/{size} → {p['sku']}-{color_info['code']}-{size} | Qty:{qty} | ${p.get('sale_price',0)}")
                total += qty
        lines.append("")
    lines.append(f"📊 *Toplam: {total} adet*")
    return "\n".join(lines)


# ── INVOICE PARSING ───────────────────────────────────────

async def parse_invoice_image(image_bytes, media_type="image/jpeg"):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": INVOICE_PROMPT}
        ]}]
    )
    return response.content[0].text.strip()


async def parse_invoice_pdf(pdf_bytes):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    all_products = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_bytes = pix.tobytes("jpeg")
        result = await parse_invoice_image(img_bytes)
        json_match = re.search(r'\[[\s\S]*\]', result)
        if json_match:
            try:
                all_products.extend(json.loads(json_match.group()))
            except:
                pass
    doc.close()
    return json.dumps(all_products) if all_products else "[]"


# ── COMMANDS ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session.update({"state": "idle", "active_invoice": None, "current_sku": None})
    await update.message.reply_text(
        "👋 Merhaba!\n\n"
        "📄 *Invoice yükle:* PDF veya fotoğraf gönder\n"
        "📋 *Bekleyen invoice'lar:* /invoices\n"
        "📦 *Koli geldiğinde:* `/teslim [isim]`\n"
        "✅ *Tüm kutular bitti:* /done\n"
        "⏭️ *Ürün atla:* `/skip SKU`",
        parse_mode="Markdown"
    )


async def cmd_invoices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    invoices = await storage_load(context.application)
    if not invoices:
        await update.message.reply_text("📭 Kayıtlı invoice yok.")
        return
    lines = ["📋 *Kayıtlı Invoice'lar:*\n"]
    for name, inv in invoices.items():
        total = len(inv["products"])
        done = sum(1 for p in inv["products"] if p.get("completed"))
        active = " ← AKTİF" if user_session.get("active_invoice") == name else ""
        lines.append(f"• *{name}*{active} — {done}/{total} tamamlandı")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_teslim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: `/teslim vava mart`", parse_mode="Markdown")
        return

    invoice_name = " ".join(context.args).lower().strip()
    invoices = await storage_load(context.application)

    if invoice_name not in invoices:
        names = "\n".join(f"• {n}" for n in invoices.keys()) if invoices else "Hiç yok"
        await update.message.reply_text(
            f"❌ *{invoice_name}* bulunamadı.\n\nMevcut:\n{names}",
            parse_mode="Markdown"
        )
        return

    user_session["active_invoice"] = invoice_name
    user_session["state"] = "receiving_products"

    inv = invoices[invoice_name]
    remaining = [p for p in inv["products"] if not p.get("completed")]
    done_count = len(inv["products"]) - len(remaining)

    lines = [f"✅ *{invoice_name}* aktif!\n",
             f"📦 {len(remaining)} ürün bekliyor, {done_count} tamamlandı\n",
             "*Bekleyen ürünler:*"]
    for p in remaining:
        lines.append(f"• {p['sku']} - {p['name']} (${p['cost']})")
    lines.append("\n*Format:* `SKU renk/adet beden`")
    lines.append("_Örn: 1308 3siyah 3beyaz 2S2M2L_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    invoice_name = user_session.get("active_invoice")
    if not invoice_name:
        await update.message.reply_text("❌ Aktif invoice yok. `/teslim [isim]` yaz.", parse_mode="Markdown")
        return

    invoices = await storage_load(context.application)
    inv = invoices.get(invoice_name, {})
    completed = [p for p in inv.get("products", []) if p.get("completed")]
    remaining = [p for p in inv.get("products", []) if not p.get("completed")]

    if not completed:
        await update.message.reply_text("❌ Henüz hiç ürün girilmedi.")
        return

    if remaining:
        names = ", ".join(p["sku"] for p in remaining)
        await update.message.reply_text(
            f"⚠️ {len(remaining)} ürün girilmedi: *{names}*\n\nYine de devam: /done\_force",
            parse_mode="Markdown"
        )
        return

    await generate_excel(update, context, invoice_name, completed, invoices)


async def cmd_done_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    invoice_name = user_session.get("active_invoice")
    if not invoice_name:
        await update.message.reply_text("❌ Aktif invoice yok.")
        return
    invoices = await storage_load(context.application)
    inv = invoices.get(invoice_name, {})
    completed = [p for p in inv.get("products", []) if p.get("completed")]
    if not completed:
        await update.message.reply_text("❌ Hiç ürün girilmedi.")
        return
    await generate_excel(update, context, invoice_name, completed, invoices)


async def generate_excel(update, context, invoice_name, completed, invoices):
    await update.message.reply_text("⏳ Excel hazırlanıyor...")
    excel_buffer = create_excel(completed, invoice_name)
    summary = build_summary(completed, invoice_name)
    await update.message.reply_text(summary, parse_mode="Markdown")
    safe_name = invoice_name.replace(" ", "_")
    filename = f"{safe_name}_square_catalog.xlsx"
    await update.message.reply_document(
        document=excel_buffer, filename=filename,
        caption=f"📎 `{filename}`", parse_mode="Markdown"
    )
    del invoices[invoice_name]
    await storage_save(context.application, invoices)
    user_session.update({"active_invoice": None, "state": "idle", "current_sku": None})


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: `/skip 1308`", parse_mode="Markdown")
        return
    invoice_name = user_session.get("active_invoice")
    if not invoice_name:
        await update.message.reply_text("❌ Aktif invoice yok.")
        return
    sku = context.args[0].upper()
    invoices = await storage_load(context.application)
    inv = invoices.get(invoice_name, {})
    for p in inv["products"]:
        if p["sku"].upper() == sku:
            p["completed"] = True
            p["skipped"] = True
            await storage_save(context.application, invoices)
            remaining = sum(1 for x in inv["products"] if not x.get("completed"))
            await update.message.reply_text(f"⏭️ *{sku}* atlandı. {remaining} ürün kaldı.", parse_mode="Markdown")
            return
    await update.message.reply_text(f"❌ {sku} bulunamadı.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session.update({"state": "idle", "active_invoice": None, "current_sku": None})
    await update.message.reply_text("🔄 Sıfırlandı!")


# ── FILE HANDLERS ─────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document.mime_type != "application/pdf":
        await update.message.reply_text("❌ Sadece PDF veya fotoğraf.")
        return
    await update.message.reply_text("📄 Invoice okunuyor, lütfen bekle...")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        pdf_bytes = await file.download_as_bytearray()
        result = await parse_invoice_pdf(bytes(pdf_bytes))
        await process_invoice_result(update, context, result)
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text(f"❌ PDF okunamadı: {str(e)}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Invoice okunuyor...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        result = await parse_invoice_image(bytes(img_bytes))
        await process_invoice_result(update, context, result)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"❌ Fotoğraf okunamadı: {str(e)}")


async def process_invoice_result(update, context, result):
    try:
        json_match = re.search(r'\[[\s\S]*\]', result)
        if not json_match:
            await update.message.reply_text("❌ Invoice okunamadı.")
            return
        products = json.loads(json_match.group())
        for p in products:
            p["completed"] = False

        lines = [f"✅ *{len(products)} ürün tespit edildi:*\n"]
        for p in products:
            lines.append(f"• {p['sku']} - {p['name']} | {p['qty']} adet | ${p['cost']}")
        lines.append("\n*Bu invoice'a bir isim ver:*\n_(örn: vava mart, supplier abc)_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

        user_session["state"] = "waiting_invoice_name"
        user_session["pending_products"] = products
    except Exception as e:
        logger.error(f"Invoice processing error: {e}")
        await update.message.reply_text("❌ Hata oluştu.")


# ── MESSAGE HANDLER ───────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.strip()
    state = user_session.get("state", "idle")

    # Waiting for invoice name
    if state == "waiting_invoice_name":
        invoice_name = user_message.lower().strip()
        invoices = await storage_load(context.application)
        if invoice_name in invoices:
            await update.message.reply_text(f"⚠️ *{invoice_name}* zaten var. Farklı isim yaz.", parse_mode="Markdown")
            return
        products = user_session.get("pending_products", [])
        invoices[invoice_name] = {"products": products}
        await storage_save(context.application, invoices)
        user_session["state"] = "idle"
        user_session["pending_products"] = []
        await update.message.reply_text(
            f"✅ *{invoice_name}* kaydedildi! {len(products)} ürün bekleniyor.\n\n"
            f"Koli geldiğinde:\n`/teslim {invoice_name}`",
            parse_mode="Markdown"
        )
        return

    # Receiving products
    if state == "receiving_products":
        invoice_name = user_session.get("active_invoice")
        if not invoice_name:
            await update.message.reply_text("❌ Aktif invoice yok.")
            return
        invoices = await storage_load(context.application)
        inv = invoices.get(invoice_name, {})
        parts = user_message.split()
        if not parts:
            return
        sku = parts[0].upper()
        rest = " ".join(parts[1:])
        product = next((p for p in inv["products"] if p["sku"].upper() == sku), None)
        if not product:
            await update.message.reply_text(f"❌ *{sku}* bu invoice'da yok.\n`/invoices` ile listeye bak.", parse_mode="Markdown")
            return
        if product.get("completed"):
            await update.message.reply_text(f"⚠️ *{sku}* zaten girilmiş.", parse_mode="Markdown")
            return
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                system=COLOR_PARSE_PROMPT,
                messages=[{"role": "user", "content": rest}]
            )
            parsed = json.loads(response.content[0].text.strip())
            product["colors"] = parsed["colors"]
            product["sizes"] = parsed["sizes"]
            product["units_per_size"] = parsed["units_per_size"]
            user_session["state"] = "asking_price"
            user_session["current_sku"] = sku
            await storage_save(context.application, invoices)
            await update.message.reply_text(
                f"💰 *{sku} - {product['name']}*\nSatış fiyatı?",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Color parse error: {e}")
            await update.message.reply_text(
                "❌ Anlayamadım.\nFormat: `SKU renk/adet beden`\n_Örn: 1308 3siyah 3beyaz 2S2M2L_",
                parse_mode="Markdown"
            )
        return

    # Asking price
    if state == "asking_price":
        invoice_name = user_session.get("active_invoice")
        sku = user_session.get("current_sku")
        invoices = await storage_load(context.application)
        inv = invoices.get(invoice_name, {})
        product = next((p for p in inv["products"] if p["sku"].upper() == sku), None)
        try:
            price_match = re.search(r'[\d.]+', user_message)
            if not price_match:
                await update.message.reply_text("❌ Geçerli fiyat gir. (örn: 24.99)")
                return
            product["sale_price"] = float(price_match.group())
            product["completed"] = True
            await storage_save(context.application, invoices)
            remaining = [p for p in inv["products"] if not p.get("completed")]
            user_session["state"] = "receiving_products"
            user_session["current_sku"] = None
            msg = f"✅ *{sku}* kaydedildi! *{len(remaining)} ürün kaldı.*"
            if not remaining:
                msg += "\n\n🎉 Tüm ürünler bitti! /done yaz."
            await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Price error: {e}")
            await update.message.reply_text("❌ Fiyat anlaşılamadı.")
        return

    await update.message.reply_text(
        "📄 Invoice için PDF veya fotoğraf yükle.\n📋 Mevcut invoice'lar için /invoices",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("invoices", cmd_invoices))
    app.add_handler(CommandHandler("teslim", cmd_teslim))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("done_force", cmd_done_force))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
