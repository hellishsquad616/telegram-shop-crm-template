"""Order and receipt pictures using confirmed payment data."""
import io
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from customer_refunds import MOSCOW, rubles


def wrap_calendar_text(draw, value, font, width):
    lines, line = [], ""
    for word in str(value).split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) > width and line:
            lines.append(line); line = word
        else:
            line = candidate
        while draw.textlength(line, font=font) > width and len(line) > 1:
            cut = len(line)-1
            while cut > 1 and draw.textlength(line[:cut], font=font) > width:
                cut -= 1
            lines.append(line[:cut]); line = line[cut:]
    return lines + ([line] if line else [])


def font(size, bold=False):
    variant = "Bold" if bold else "Regular"
    names = [
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans-{variant}.ttf",
        f"/usr/share/fonts/truetype/liberation2/LiberationSans-{variant}.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for name in names:
        if not Path(name).is_file():
            continue
        try:
            candidate = ImageFont.truetype(name,size)
        except OSError:
            continue
        # FreeType otherwise silently draws the font's missing-glyph box.
        missing = [candidate.getmask(chr(code)) for code in (0xffff,0x10ffff,0x0378)]
        def supported(character):
            glyph = candidate.getmask(character)
            return glyph.getbbox() is not None and all(
                (glyph.size,bytes(glyph)) != (unknown.size,bytes(unknown)) for unknown in missing)
        if all(supported(character) for character in "₽Чя"):
            return candidate
    raise RuntimeError("Не установлен шрифт с поддержкой кириллицы и знака ₽ для картинки чека.")


def date_text(value):
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if stamp.tzinfo is None:
            return ""
        stamp = stamp.astimezone(MOSCOW)
    except (ValueError,TypeError):
        return ""
    months = ("января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря")
    return f"{stamp.day} {months[stamp.month-1]} {stamp.year} г. в {stamp:%H:%M}"


class ReceiptCanvas:
    width, margin = 956, 54
    ink, muted = "#222222", "#808080"

    def __init__(self):
        self.usable = self.width-self.margin*2
        self.scratch = ImageDraw.Draw(Image.new("RGB",(1,1)))
        self.blocks, self.y = [], 48

    def text(self,value,size=29,color=None,gap=0,x=None,width=None):
        x = self.margin if x is None else x
        for line in wrap_calendar_text(self.scratch,value,font(size),width or self.usable):
            self.blocks.append(("text",x,self.y,line,size,color or self.ink))
            self.y += round(size*1.32)
        self.y += gap

    def section(self,title):
        self.y += 24
        self.blocks.append(("line",self.y))
        self.y += 44
        self.text(title,35,gap=32)

    def field(self,label,value):
        self.text(label,24,self.muted,gap=7)
        self.text(str(value),29,gap=30)

    def pair(self,label,value,size=29,color=None,gap=22):
        right_width = self.scratch.textlength(str(value),font=font(size))
        right_x = self.width-self.margin-right_width
        top = self.y
        self.text(label,size,color,width=max(100,right_x-self.margin-28))
        height = self.y-top
        self.blocks.append(("text",right_x,top,str(value),size,color or self.ink))
        self.y = top+max(height,round(size*1.32))+gap

    def customer(self,data):
        customer = data.get("customer") or {}
        values = [customer.get(k) for k in ("name","email","phone") if customer.get(k)]
        if not values:
            values = [data.get("contact") or "Не указан"]
        self.section("Покупатель")
        for index,value in enumerate(dict.fromkeys(values)):
            self.text(value,29 if index==0 else 27,self.ink if index==0 else self.muted,gap=10)

    def items(self,items,title):
        if not items:
            return
        self.section(title)
        for item in items:
            total = int(item["unit_cents"])*int(item["quantity"])
            self.pair(item["name"],rubles(total),gap=6)
            self.pair("",f'{item["quantity"]} × {rubles(item["unit_cents"])}',25,self.muted,gap=30)

    def finish(self,name,minimum):
        picture = Image.new("RGB",(self.width,max(minimum,self.y+54)),"white")
        draw = ImageDraw.Draw(picture)
        for block in self.blocks:
            if block[0]=="line":
                draw.line((self.margin,block[1],self.width-self.margin,block[1]),fill="#E7E7E7",width=2)
            elif block[0]=="dot":
                _,left,top,radius,color = block
                draw.ellipse((left,top,left+radius*2,top+radius*2),fill=color)
            else:
                _,left,top,value,size,color = block
                draw.text((left,top),value,font=font(size),fill=color)
        output = io.BytesIO()
        output.name = name
        picture.save(output,format="PNG",optimize=True)
        output.seek(0)
        return output


def receipt_view(record):
    """Add only customer/items from this refund's verified snapshot."""
    data = dict(record["receipt_json"])
    snapshot = record.get("snapshot") or {}
    request = record.get("request") or {}
    customer = snapshot.get("customer") or request
    data["customer"] = {k:customer[k] for k in ("name","email","phone") if customer.get(k)}
    data["items"] = [dict(item) for item in snapshot.get("financial_items",(snapshot.get("order") or {}).get("items",[]))]
    data["order_number"] = request.get("order_number","")
    data["payment_amount_cents"] = snapshot.get("payment_amount_cents",data["amount_cents"])
    data["payment_method"] = snapshot.get("payment_method",data.get("payment_method"))
    return data


def render_refund(record):
    data = receipt_view(record)
    return render_yandex_confirmation(data) if data.get("provider")=="yandex_split" else render_receipt(data)


def render_receipt(receipt):
    page = ReceiptCanvas()
    page.margin = 44
    page.usable = page.width-page.margin*2
    page.ink,page.muted = "#102E4B","#586D84"
    page.y = 45
    title = "Чек возврата на "+rubles(receipt["amount_cents"])
    size = 48
    while size>28 and page.scratch.textlength(title,font=font(size))>page.usable:
        size -= 1
    page.text(title,size,gap=66)

    def field(label,value,separator=True):
        page.text(label,30,page.muted,gap=9)
        page.text(str(value),32,gap=24)
        if separator:
            page.blocks.append(("line",page.y))
            page.y += 29

    customer = receipt.get("customer") or {}
    field("Контакты для чека",receipt.get("contact") or customer.get("email") or customer.get("phone") or "Не указан")
    field("ID чека в ЮKassa",receipt["id"],False)
    page.y += 64
    page.text("Данные чека в облачной кассе",36,gap=38)
    fields = (
        ("ID чека в облачной кассе","fiscal_provider_id"),
        ("Номер фискального документа","fiscal_document_number"),
        ("Номер фискального накопителя","fiscal_storage_number"),
        ("Фискальный признак","fiscal_attribute"),
    )
    for index,(label,key) in enumerate(fields):
        field(label,receipt[key],index<len(fields)-1)
    return page.finish("refund-receipt.png",1360)


def render_yandex_confirmation(data):
    """Display original payment and the confirmed refund as separate facts."""
    if data.get("provider")!="yandex_split" or data.get("status") not in {"PENDING","SUCCESS","FAIL"}:
        raise ValueError("Нужно подтверждение состояния операции Яндекс Пэй.")
    page = ReceiptCanvas()
    page.text("Заказ",43,gap=14)
    page.text({"SBP":"Яндекс Пэй · СБП","CARD":"Яндекс Пэй · Карта"}.get(data.get("payment_method"),"Яндекс Пэй · Сплит"),25,page.muted,gap=36)
    page.text("Исходная оплата",23,page.muted,gap=8)
    total = data.get("payment_amount_cents",data["amount_cents"])
    amount_size = 51
    while page.scratch.textlength(rubles(total),font=font(amount_size))>540 and amount_size>30:
        amount_size -= 1
    top = page.y
    page.text(rubles(total),amount_size,gap=32)
    status_x = page.width-page.margin-page.scratch.textlength("Оплачен",font=font(29))
    page.blocks.append(("dot",status_x-30,top+17,8,"#00A65A"))
    page.blocks.append(("text",status_x,top+7,"Оплачен",29,page.ink))
    page.text(data["order_id"],29,gap=10)
    created = date_text(data.get("order_created_at"))
    if created:
        page.text(created,26,page.muted,gap=10)
    items = data.get("items") or []
    items_total = sum(int(i["quantity"])*int(i["unit_cents"]) for i in items)
    page.items(items,"Состав корзины" if items_total==total else "Состав возврата")
    page.section("Оплата")
    page.pair("Итого оплачено",rubles(total))
    if data["amount_cents"]!=total:
        page.pair("Сумма возврата",rubles(data["amount_cents"]))
    page.customer(data)
    page.section("История операций")
    status = data["status"]
    label = {"PENDING":"Возврат в процессе","SUCCESS":"Возврат оформлен","FAIL":"Возврат не выполнен"}[status]
    page.text(label,29,gap=8,x=page.margin+30,width=page.usable-30)
    page.blocks.append(("dot",page.margin,page.y-43,6,"#383842"))
    timestamp = data.get({"PENDING":"requested_at","SUCCESS":"completed_at","FAIL":"failed_at"}[status])
    page.text(date_text(timestamp),25,page.muted,gap=8,x=page.margin+30,width=page.usable-30)
    page.text(rubles(data["amount_cents"]),26,gap=18,x=page.margin+30,width=page.usable-30)
    page.text("ID операции: "+data["operation_id"],22,page.muted,gap=28)
    page.text("Оплата подтверждена",29,gap=8,x=page.margin+30,width=page.usable-30)
    paid = date_text(data.get("paid_at"))
    if paid:
        page.text(paid,25,page.muted,gap=12,x=page.margin+30,width=page.usable-30)
    return page.finish("yandex-refund-confirmation.png",1200)
