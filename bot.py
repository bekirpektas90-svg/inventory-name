import os
import re
import io
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic
from openpyxl import load_workbook
import openpyxl

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert Inventory Management Assistant specialized in Square POS systems.

Your job is to collect product information, validate the data, and generate Square-compatible catalog Excel files.

== INPUT FORMAT ==
The user will send one or more products in this pipe-separated format (one product per line):
SKU | Product Name | Category | Total Qty | Size Assortment | Colors & Packs | Cost Price | Sale Price | Vendor Name

Example:
1450 | Floral Midi Dress | Dresses | 72 | 2S 2M 2L | 5 Black 3 White | 6$ | 19.90$ | Fashion Co.

== VALIDATION - MANDATORY BEFORE OUTPUT ==

1. QUANTITY CHECK:
   - Parse the size assortment to get units per pack (e.g. "2S 2M 2L" = 6 units per pack)
   - Sum all pack counts (e.g. 5 Black + 3 White = 8 packs)
   - Calculate expected total = packs × units per pack
   - If expected total ≠ stated total quantity → STOP and warn the user with:
     ⚠️ Quantity Mismatch!
     - Calculated from packs & sizes: X units
     - You entered: Y units
     - Difference: Z units
     Please verify: (1) Pack counts, (2) Size assortment, or (3) Total quantity — which is correct?
   - Do NOT generate the file until the user confirms.

2. REQUIRED FIELDS CHECK:
   - All 9 fields must be present. If any is missing, ask the user to complete it.

== OUTPUT FORMAT ==
When data is valid, respond with ONLY a JSON array (no other text) like this:

[
  {
    "model": "1450",
    "product_name": "Floral Midi Dress",
    "category": "Dresses",
    "vendor": "Fashion Co.",
    "price": 19.90,
    "cost": 6.00,
    "colors": {"BLACK": {"code": "BLK", "packs": 5}, "WHITE": {"code": "WHT", "packs": 3}},
    "sizes": ["S", "M", "L"],
    "units_per_size": 2
  }
]

Color code mapping:
BLACK→BLK, WHITE→WHT, BLUE→BLU, BEIGE→BGE, PINK→PNK, RED→RED, GREEN→GRN, GREY→GRY, BROWN→BRN

If there is a validation error, respond with plain text explaining the issue.
If the user is asking a general question or saying something conversational, respond naturally in their language.
"""

conversation_history = {}


def parse_sizes(size_str):
    """Parse '2S 2M 2L' into sizes=['S','M','L'] and units_per_size=2"""
    parts = size_str.strip().split()
    sizes = []
    units = []
    for part in parts:
        match = re.match(r'(\d+)([A-Za-z/]+)', part)
        if match:
            units.append(int(match.group(1)))
            sizes.append(match.group(2).upper())
    if units:
        return sizes, units[0]
    return sizes, 1


def create_excel(products_data):
    """Create Square-compatible Excel file from parsed product data"""
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

    # Style header row
    from openpyxl.styles import Font, PatternFill, Alignment
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", name="Arial")
        cell.fill = PatternFill("solid", start_color="1F3864")
        cell.alignment = Alignment(horizontal="center")

    rows = []
    for p in products_data:
        description = f"{p['product_name']} — a stylish and comfortable everyday piece. Available in multiple colors and sizes."
        for color_name, color_info in p["colors"].items():
            color_code = color_info["code"]
            packs = color_info["packs"]
            qty_per_size = packs * p["units_per_size"]
            for size in p["sizes"]:
                size_slug = size.lower().replace("/", "-")
                handle = f"#{p['model']}-{p['product_name'].lower().replace(' ', '-')}-{color_name.lower()}-{size_slug}"
                sku = f"{p['model']}-{color_code}-{size}"
                variation = f"{p['product_name']} {color_name} / {size}"

                row = {
                    "Reference Handle": handle,
                    "Token": None,
                    "Item Name": p["product_name"],
                    "Customer-facing Name": p["product_name"],
                    "Variation Name": variation,
                    "SKU": sku,
                    "Description": description,
                    "Categories": p["category"],
                    "Reporting Category": None,
                    "GTIN": None,
                    "Item Type": "Physical good",
                    "Weight (lb)": None,
                    "Social Media Link Title": None,
                    "Social Media Link Description": None,
                    "Price": p["price"],
                    "Online Sale Price": None,
                    "Archived": "N",
                    "Sellable": None,
                    "Contains Alcohol": "N",
                    "Stockable": None,
                    "Skip Detail Screen in POS": "N",
                    "Option Name 1": None,
                    "Option Value 1": None,
                    "Default Unit Cost": p["cost"],
                    "Default Vendor Name": p["vendor"],
                    "Default Vendor Code": None,
                    "Current Quantity GAVA NEW YORK": qty_per_size,
                    "New Quantity GAVA NEW YORK": qty_per_size,
                    "Stock Alert Enabled GAVA NEW YORK": None,
                    "Stock Alert Count GAVA NEW YORK": None,
                }
                rows.append(row)

    for row_data in rows:
        ws.append([row_data.get(h) for h in headers])

    # Auto-size columns
    for col in ws.columns:
        max_len = max((len(str(cell.value)) if cell.value else 0) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    # Save to buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, rows


def build_summary(products_data, rows):
    """Build a summary message"""
    lines = ["✅ *Excel hazır! İşte özet:*\n"]
    total = 0
    for p in products_data:
        lines.append(f"📦 *{p['product_name']}* (SKU: {p['model']})")
        for color_name, color_info in p["colors"].items():
            qty = color_info["packs"] * p["units_per_size"]
            for size in p["sizes"]:
                sku = f"{p['model']}-{color_info['code']}-{size}"
                lines.append(f"  • {p['product_name']} {color_name} / {size} → {sku} | Qty: {qty} | ${p['price']}")
                total += qty
        lines.append("")
    lines.append(f"📊 *Toplam: {total} adet*")
    lines.append("\nYeni ürün eklemek ister misin?")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    await update.message.reply_text(
        "👋 Merhaba! Ben Square POS Envanter Asistanınım.\n\n"
        "Ürün bilgilerini şu formatta gönder:\n\n"
        "`SKU | Ürün İsmi | Kategori | Toplam Adet | Beden Dağılımı | Renkler & Paketler | Maliyet | Satış Fiyatı | Vendor`\n\n"
        "Örnek:\n"
        "`1450 | Floral Midi Dress | Dresses | 48 | 2S 2M 2L | 5 Black 3 White | 6$ | 19.90$ | Fashion Co.`\n\n"
        "Birden fazla ürün için her ürünü yeni satıra yaz.",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({"role": "user", "content": user_message})

    await update.message.reply_text("⏳ İşleniyor...")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )

        assistant_message = response.content[0].text
        conversation_history[user_id].append({"role": "assistant", "content": assistant_message})

        # Try to parse as JSON (product data)
        try:
            cleaned = assistant_message.strip()
            if cleaned.startswith("["):
                import json
                products_data = json.loads(cleaned)

                # Generate Excel
                excel_buffer, rows = create_excel(products_data)
                summary = build_summary(products_data, rows)

                # Send summary
                await update.message.reply_text(summary, parse_mode="Markdown")

                # Send Excel file
                first_product = products_data[0]["model"]
                filename = f"{first_product}_square_catalog.xlsx"
                await update.message.reply_document(
                    document=excel_buffer,
                    filename=filename,
                    caption=f"📎 Square import dosyan hazır: `{filename}`",
                    parse_mode="Markdown"
                )
            else:
                # Plain text response (validation error or conversation)
                await update.message.reply_text(assistant_message)

        except Exception:
            await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Bir hata oluştu. Lütfen tekrar dene.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    await update.message.reply_text("🔄 Sıfırlandı! Yeni ürün bilgilerini gönderebilirsin.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
