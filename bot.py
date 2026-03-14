import os
import re
import io
import json
import logging
import tempfile
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
DRIVE_FOLDER_ID = "1v7YJv9lxPblfrtIz3YBlIU4KkEZ31brT"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Google Drive setup
def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def upload_photo_to_drive(photo_bytes, filename):
    """Upload photo to Google Drive and return public link"""
    try:
        service = get_drive_service()
        file_metadata = {
            "name": filename,
            "parents": [DRIVE_FOLDER_ID]
        }
        media = MediaIoBaseUpload(io.BytesIO(photo_bytes), mimetype="image/jpeg")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()

        file_id = file.get("id")

        # Make file public
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"}
        ).execute()

        # Return direct view link
        public_link = f"https://drive.google.com/uc?id={file_id}"
        return public_link
    except Exception as e:
        logger.error(f"Drive upload error: {e}")
        return None

SYSTEM_PROMPT = """You are an expert Inventory Management Assistant specialized in Square POS systems.

Your job is to collect product information, validate the data, and generate Square-compatible catalog Excel files.

== INPUT FORMAT ==
The user will send one or more products in this pipe-separated format (one product per line):
SKU | Product Name | Category | Total Qty | Size Assortment | Colors & Packs | Cost Price | Sale Price | Vendor Name

Example:
1450 | Floral Midi Dress | Dresses | 48 | 2S 2M 2L | 5 Black 3 White | 6$ | 19.90$ | Fashion Co.

== VALIDATION ==

QUANTITY CHECK - Follow exactly:
  Step 1: Sum all numbers in size assortment to get units per pack.
          Example: "2S 2M 2L" = 2+2+2 = 6 units per pack
  Step 2: Sum all pack counts from colors.
          Example: "5 Black 3 White" = 5+3 = 8 packs total
  Step 3: Expected total = total packs x units per pack = 8 x 6 = 48
  Step 4: Compare with stated total.
          - If they MATCH: proceed directly to JSON output. Do NOT mention the calculation at all.
          - If they DO NOT match: warn the user only then.

  Mismatch warning format:
  Quantity Mismatch!
  - Calculated: X units (Y packs x Z units per pack)
  - You entered: W units
  - Difference: V units
  Please verify: (1) Pack counts, (2) Size assortment, or (3) Total quantity.

REQUIRED FIELDS: All 9 fields must be present. If any is missing, ask before proceeding.

== OUTPUT FORMAT ==
CRITICAL RULE: When all data is valid, your ENTIRE response must be ONLY the JSON array.
- Start your response with [
- End your response with ]
- No text before the [
- No text after the ]
- No explanations, no greetings, no markdown code blocks

Valid output example:
[{"model":"1450","product_name":"Floral Midi Dress","category":"Dresses","vendor":"Fashion Co.","price":19.90,"cost":6.00,"colors":{"BLACK":{"code":"BLK","packs":5},"WHITE":{"code":"WHT","packs":3}},"sizes":["S","M","L"],"units_per_size":2}]

Color codes: BLACK=BLK, WHITE=WHT, BLUE=BLU, BEIGE=BGE, PINK=PNK, RED=RED, GREEN=GRN, GREY=GRY, BROWN=BRN, ORANGE=ORG, PURPLE=PRP

For validation errors: respond in plain text.
For general questions: respond naturally in the user's language.
"""

conversation_history = {}
pending_photos = {}  # user_id -> list of (filename, url)


def create_excel(products_data, photo_links=None):
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
        "Stock Alert Enabled GAVA NEW YORK", "Stock Alert Count GAVA NEW YORK", "Image URL"
    ]

    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", name="Arial")
        cell.fill = PatternFill("solid", start_color="1F3864")
        cell.alignment = Alignment(horizontal="center")

    rows = []
    photo_index = 0

    for p in products_data:
        description = f"{p['product_name']} — a stylish and comfortable everyday piece. Available in multiple colors and sizes."
        for color_name, color_info in p["colors"].items():
            color_code = color_info["code"]
            packs = color_info["packs"]
            qty_per_size = packs * p["units_per_size"]

            # Get photo link for this color if available
            image_url = None
            if photo_links and photo_index < len(photo_links):
                image_url = photo_links[photo_index]
                photo_index += 1

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
                    "Image URL": image_url,
                }
                rows.append(row)

    for row_data in rows:
        ws.append([row_data.get(h) for h in headers])

    for col in ws.columns:
        max_len = max((len(str(cell.value)) if cell.value else 0) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, rows


def build_summary(products_data, has_photos=False):
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
    if has_photos:
        lines.append("🖼️ *Fotoğraflar Google Drive'a yüklendi ve Excel'e eklendi!*")
    lines.append("\nYeni ürün eklemek ister misin?")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    pending_photos[user_id] = []
    await update.message.reply_text(
        "👋 Merhaba! Ben Square POS Envanter Asistanınım.\n\n"
        "📸 *Fotoğraf göndermek istersen:*\n"
        "Önce fotoğrafları gönder, sonra ürün bilgilerini yaz.\n\n"
        "📋 *Ürün bilgisi formatı:*\n"
        "`SKU | Ürün İsmi | Kategori | Toplam Adet | Beden Dağılımı | Renkler & Paketler | Maliyet | Satış Fiyatı | Vendor`\n\n"
        "Örnek:\n"
        "`1450 | Floral Midi Dress | Dresses | 48 | 2S 2M 2L | 5 Black 3 White | 6$ | 19.90$ | Fashion Co.`",
        parse_mode="Markdown"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in pending_photos:
        pending_photos[user_id] = []

    await update.message.reply_text("📸 Fotoğraf alındı, yükleniyor...")

    try:
        # Get highest resolution photo
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await photo_file.download_as_bytearray()

        filename = f"product_{photo.file_id}.jpg"
        public_url = upload_photo_to_drive(bytes(photo_bytes), filename)

        if public_url:
            pending_photos[user_id].append(public_url)
            count = len(pending_photos[user_id])
            await update.message.reply_text(
                f"✅ Fotoğraf {count} Google Drive'a yüklendi!\n"
                f"Şimdi ürün bilgilerini gönderebilirsin."
            )
        else:
            await update.message.reply_text("❌ Fotoğraf yüklenemedi. Tekrar dene.")

    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text("❌ Fotoğraf işlenirken hata oluştu.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    if user_id not in conversation_history:
        conversation_history[user_id] = []
    if user_id not in pending_photos:
        pending_photos[user_id] = []

    conversation_history[user_id].append({"role": "user", "content": user_message})

    await update.message.reply_text("⏳ İşleniyor...")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )

        assistant_message = response.content[0].text.strip()
        conversation_history[user_id].append({"role": "assistant", "content": assistant_message})

        json_match = re.search(r'\[[\s\S]*\]', assistant_message)
        if json_match:
            try:
                products_data = json.loads(json_match.group())
                photo_links = pending_photos.get(user_id, [])
                excel_buffer, rows = create_excel(products_data, photo_links if photo_links else None)
                has_photos = len(photo_links) > 0
                summary = build_summary(products_data, has_photos)

                # Clear photos after use
                pending_photos[user_id] = []

                await update.message.reply_text(summary, parse_mode="Markdown")

                first_product = products_data[0]["model"]
                filename = f"{first_product}_square_catalog.xlsx"
                await update.message.reply_document(
                    document=excel_buffer,
                    filename=filename,
                    caption=f"📎 Square import dosyan hazır: `{filename}`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Excel generation error: {e}")
                await update.message.reply_text(assistant_message)
        else:
            await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Bir hata oluştu. Lütfen tekrar dene.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    pending_photos[user_id] = []
    await update.message.reply_text("🔄 Sıfırlandı! Yeni ürün bilgilerini gönderebilirsin.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
