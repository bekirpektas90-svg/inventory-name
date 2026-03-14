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

client = Anthropic(api_key=ANTHROPIC_API_KEY)

INVOICE_PROMPT = """You are an invoice parser. Extract all product lines from this invoice image or PDF.

For each product line, extract:
- SKU/Model number (the number at the beginning)
- Product name
- Quantity (Qty column)
- Unit cost (Rate/Price column)

Respond ONLY with a JSON array, no other text:
[
  {"sku": "1308", "name": "Dress Embroidery", "qty": 12, "cost": 8.00},
  {"sku": "3701", "name": "Mini Dress", "qty": 12, "cost": 5.75}
]

If you cannot read the invoice clearly, respond with: {"error": "Cannot read invoice"}
"""

CATALOG_SYSTEM_PROMPT = """You are a Square POS catalog assistant. 
The user will provide color and size information for products.
Parse the input and respond ONLY with JSON in this exact format:
{
  "colors": {"BLACK": {"code": "BLK", "packs": 3}, "WHITE": {"code": "WHT", "packs": 3}},
  "sizes": ["S", "M", "L"],
  "units_per_size": 2
}

Color codes: BLACK=BLK, WHITE=WHT, BLUE=BLU, BEIGE=BGE, PINK=PNK, RED=RED, GREEN=GRN, GREY=GRY, BROWN=BRN, ORANGE=ORG, PURPLE=PRP

Parse natural language like:
- "2S 2M 2L | 3 Black 3 White" 
- "3 siyah 3 beyaz 2S 2M 2L"
- "black white beige hepsi 4er 2S2M2L"

Respond ONLY with the JSON object, no other text.
"""

# User session states
user_sessions = {}
# States: idle, collecting_products, asking_colors, asking_price, done


def create_excel(products):
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
            color_code = color_info["code"]
            packs = color_info["packs"]
            qty_per_size = packs * p["units_per_size"]
            for size in p["sizes"]:
                size_slug = size.lower().replace("/", "-")
                handle = f"#{p['sku']}-{p['name'].lower().replace(' ', '-')}-{color_name.lower()}-{size_slug}"
                sku = f"{p['sku']}-{color_code}-{size}"
                variation = f"{p['name']} {color_name} / {size}"
                row = {
                    "Reference Handle": handle, "Token": None,
                    "Item Name": p["name"], "Customer-facing Name": p["name"],
                    "Variation Name": variation, "SKU": sku, "Description": description,
                    "Categories": p.get("category", ""), "Reporting Category": None, "GTIN": None,
                    "Item Type": "Physical good", "Weight (lb)": None,
                    "Social Media Link Title": None, "Social Media Link Description": None,
                    "Price": p["sale_price"], "Online Sale Price": None, "Archived": "N",
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


def build_final_summary(products):
    lines = ["✅ *Excel hazır! İşte özet:*\n"]
    total_units = 0
    for p in products:
        lines.append(f"📦 *{p['name']}* (SKU: {p['sku']})")
        for color_name, color_info in p["colors"].items():
            qty = color_info["packs"] * p["units_per_size"]
            for size in p["sizes"]:
                sku_var = f"{p['sku']}-{color_info['code']}-{size}"
                lines.append(f"  • {color_name} / {size} → {sku_var} | Qty: {qty} | ${p['sale_price']}")
                total_units += qty
        lines.append("")
    lines.append(f"📊 *Toplam: {total_units} adet*")
    return "\n".join(lines)


async def parse_invoice_image(image_bytes, media_type="image/jpeg"):
    """Parse invoice using Claude Vision"""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": INVOICE_PROMPT}
            ]
        }]
    )
    return response.content[0].text.strip()


async def parse_invoice_pdf(pdf_bytes):
    """Parse PDF invoice - convert first page to image then use Vision"""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(2, 2)  # 2x zoom for better quality
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg")
    doc.close()
    return await parse_invoice_image(img_bytes, "image/jpeg")


async def ask_next_product(update, session):
    """Ask color/size info for the next pending product"""
    pending = session["pending_products"]
    if not pending:
        return False

    product = pending[0]
    await update.message.reply_text(
        f"📦 *{product['sku']} - {product['name']}*\n"
        f"Maliyet: ${product['cost']} | Adet: {product['qty']}\n\n"
        f"Renk ve beden dağılımı?\n"
        f"_(örn: 2S 2M 2L | 3 Black 3 White)_",
        parse_mode="Markdown"
    )
    session["state"] = "asking_colors"
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {"state": "idle", "pending_products": [], "completed_products": []}
    await update.message.reply_text(
        "👋 Merhaba! Ben Square POS Envanter Asistanınım.\n\n"
        "📄 *Invoice'u gönder* (PDF veya fotoğraf)\n"
        "Ben otomatik okuyup ürünleri çıkaracağım.\n"
        "Sonra her ürün için sadece renk ve beden soracağım!\n\n"
        "Ya da direkt ürün bilgisi de yazabilirsin:\n"
        "`SKU | Ürün İsmi | Kategori | Adet | Beden | Renkler | Maliyet | Satış | Vendor`",
        parse_mode="Markdown"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF invoices"""
    user_id = update.effective_user.id
    doc = update.message.document

    if not doc.mime_type == "application/pdf":
        await update.message.reply_text("❌ Sadece PDF veya fotoğraf kabul ediyorum.")
        return

    await update.message.reply_text("📄 Invoice okunuyor...")

    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()
        result = await parse_invoice_pdf(bytes(pdf_bytes))
        await process_invoice_result(update, user_id, result)
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text(f"❌ PDF okunamadı: {str(e)}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo invoices"""
    user_id = update.effective_user.id
    await update.message.reply_text("📸 Invoice fotoğrafı okunuyor...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        result = await parse_invoice_image(bytes(img_bytes))
        await process_invoice_result(update, user_id, result)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"❌ Fotoğraf okunamadı: {str(e)}")


async def process_invoice_result(update, user_id, result):
    """Process parsed invoice JSON and start asking questions"""
    try:
        json_match = re.search(r'\[[\s\S]*\]', result)
        if not json_match:
            await update.message.reply_text(
                "❌ Invoice okunamadı. Lütfen daha net bir fotoğraf deneyin."
            )
            return

        products = json.loads(json_match.group())

        if user_id not in user_sessions:
            user_sessions[user_id] = {"state": "idle", "pending_products": [], "completed_products": []}

        session = user_sessions[user_id]
        session["pending_products"] = products
        session["completed_products"] = []

        # Show what was detected
        lines = [f"✅ *{len(products)} ürün tespit edildi:*\n"]
        for p in products:
            lines.append(f"• {p['sku']} - {p['name']} | Adet: {p['qty']} | Maliyet: ${p['cost']}")
        lines.append("\nŞimdi sırayla renk ve beden bilgilerini soracağım...")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

        # Ask for first product
        await ask_next_product(update, session)

    except Exception as e:
        logger.error(f"Invoice processing error: {e}")
        await update.message.reply_text("❌ Invoice işlenirken hata oluştu.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    if user_id not in user_sessions:
        user_sessions[user_id] = {"state": "idle", "pending_products": [], "completed_products": []}

    session = user_sessions[user_id]
    state = session.get("state", "idle")

    # Asking for color/size info
    if state == "asking_colors":
        current_product = session["pending_products"][0]
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                system=CATALOG_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}]
            )
            parsed = json.loads(response.content[0].text.strip())
            current_product["colors"] = parsed["colors"]
            current_product["sizes"] = parsed["sizes"]
            current_product["units_per_size"] = parsed["units_per_size"]
            session["state"] = "asking_price"
            await update.message.reply_text(
                f"💰 *{current_product['sku']} - {current_product['name']}*\n"
                f"Satış fiyatı nedir?",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Color parse error: {e}")
            await update.message.reply_text(
                "❌ Anlayamadım. Tekrar dener misin?\n"
                "_(örn: 2S 2M 2L | 3 Black 3 White)_",
                parse_mode="Markdown"
            )

    # Asking for sale price
    elif state == "asking_price":
        current_product = session["pending_products"][0]
        try:
            price_match = re.search(r'[\d.]+', user_message)
            if not price_match:
                await update.message.reply_text("❌ Geçerli bir fiyat gir. (örn: 24.99)")
                return

            current_product["sale_price"] = float(price_match.group())
            current_product["category"] = "Items"
            current_product["vendor"] = ""

            # Move to completed
            session["completed_products"].append(current_product)
            session["pending_products"].pop(0)

            await update.message.reply_text(
                f"✅ *{current_product['sku']} - {current_product['name']}* kaydedildi!",
                parse_mode="Markdown"
            )

            # Check if more products
            if session["pending_products"]:
                await ask_next_product(update, session)
            else:
                # All done - generate Excel
                session["state"] = "idle"
                excel_buffer = create_excel(session["completed_products"])
                summary = build_final_summary(session["completed_products"])
                await update.message.reply_text(summary, parse_mode="Markdown")

                first_sku = session["completed_products"][0]["sku"]
                filename = f"{first_sku}_square_catalog.xlsx"
                await update.message.reply_document(
                    document=excel_buffer,
                    filename=filename,
                    caption=f"📎 Square import dosyan hazır: `{filename}`",
                    parse_mode="Markdown"
                )
                session["completed_products"] = []

        except Exception as e:
            logger.error(f"Price error: {e}")
            await update.message.reply_text("❌ Fiyat anlaşılamadı. Tekrar dene. (örn: 24.99)")

    # Idle state - handle manual product entry
    else:
        # Check if it's manual pipe-separated format
        if "|" in user_message:
            await update.message.reply_text("⏳ İşleniyor...")
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4000,
                    system="""Parse pipe-separated product data and return ONLY a JSON array:
[{"model":"1450","product_name":"Floral Midi Dress","category":"Dresses","vendor":"Co.","price":19.90,"cost":6.00,"colors":{"BLACK":{"code":"BLK","packs":5}},"sizes":["S","M","L"],"units_per_size":2}]
Color codes: BLACK=BLK, WHITE=WHT, BLUE=BLU, BEIGE=BGE, PINK=PNK, RED=RED, GREEN=GRN, GREY=GRY, BROWN=BRN""",
                    messages=[{"role": "user", "content": user_message}]
                )
                assistant_message = response.content[0].text.strip()
                json_match = re.search(r'\[[\s\S]*\]', assistant_message)
                if json_match:
                    products_data = json.loads(json_match.group())
                    # Convert to invoice-style format
                    converted = []
                    for p in products_data:
                        converted.append({
                            "sku": p["model"], "name": p["product_name"],
                            "qty": sum(v["packs"] for v in p["colors"].values()) * p["units_per_size"] * len(p["sizes"]),
                            "cost": p["cost"], "sale_price": p["price"],
                            "category": p["category"], "vendor": p["vendor"],
                            "colors": p["colors"], "sizes": p["sizes"],
                            "units_per_size": p["units_per_size"]
                        })
                    excel_buffer = create_excel(converted)
                    summary = build_final_summary(converted)
                    await update.message.reply_text(summary, parse_mode="Markdown")
                    filename = f"{converted[0]['sku']}_square_catalog.xlsx"
                    await update.message.reply_document(
                        document=excel_buffer,
                        filename=filename,
                        caption=f"📎 Square import dosyan hazır: `{filename}`",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(assistant_message)
            except Exception as e:
                logger.error(f"Manual entry error: {e}")
                await update.message.reply_text("❌ Bir hata oluştu. Tekrar dene.")
        else:
            await update.message.reply_text(
                "📄 Invoice göndermek için PDF veya fotoğraf yükle.\n\n"
                "Ya da manuel giriş için:\n"
                "`SKU | Ürün | Kategori | Adet | Beden | Renkler | Maliyet | Satış | Vendor`",
                parse_mode="Markdown"
            )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {"state": "idle", "pending_products": [], "completed_products": []}
    await update.message.reply_text("🔄 Sıfırlandı!")


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip current product"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        return
    session = user_sessions[user_id]
    if session["pending_products"]:
        skipped = session["pending_products"].pop(0)
        await update.message.reply_text(f"⏭️ *{skipped['sku']} - {skipped['name']}* atlandı.", parse_mode="Markdown")
        session["state"] = "idle"
        if session["pending_products"]:
            await ask_next_product(update, session)
        else:
            await update.message.reply_text("✅ Tüm ürünler işlendi.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
