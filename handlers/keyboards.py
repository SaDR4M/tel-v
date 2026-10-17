from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import settings

MENU_BUTTONS = (
    "اکانت تست",
    "خرید اشتراک جدید",
    "سرویس‌های من",
    "آموزش استفاده",
    "پشتیبانی",
)

def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 لیست قیمت / خرید اشتراک", callback_data="menu:plans")
    b.button(text="🎁 اشتراک تست رایگان", callback_data="menu:test")
    b.button(text="📱 اشتراک من", callback_data="menu:mysub")
    b.button(text="🔄 تمدید سرویس", callback_data="menu:renew")
    b.button(text="📖 راهنمای استفاده", callback_data="menu:docs")
    b.button(text=" پشتیبانی", callback_data="menu:support")
    b.adjust(1)
    return b.as_markup()

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="اکانت تست",
                style="primary",
            ),
            KeyboardButton(
                text="خرید اشتراک جدید",
                style="primary",
            ),
        ],
        [
            KeyboardButton(
                text=" سرویس‌های من",
                style="success",
            ),
            KeyboardButton(
                text=" تمدید سرویس",
                style="success",
            ),
        ],
        [
            KeyboardButton(
                text="آموزش استفاده",
                style="",
            ),
        ],
        [
            KeyboardButton(
                text="پشتیبانی",
                style="danger",
            )
        ]
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def tiers_menu() -> InlineKeyboardMarkup:
    """Category menu: VIP (auto-provisioned), Eco and Unlimited (created
    manually by the admin, link delivered as a reply to the order)."""
    b = InlineKeyboardBuilder()
    b.button(text="حرفه‌ای — پرسرعت و پایدار", callback_data="tierplans:vip")
    b.button(text="اکونومی — کم‌خرج", callback_data="tierplans:eco")
    b.button(text="نامحدود", callback_data="tierplans:unlimited")
    b.button(text="⬅️ بازگشت", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


# Marketing copy shown above each tier's plan list. The manual-delivery
# mechanics of eco/unlimited stay invisible to buyers.
# Marketing copy shown above each tier's plan list. The manual-delivery
# mechanics of eco/unlimited stay invisible to buyers.
TIER_INFO = {
    "vip": (
        "<b>پلن حرفه‌ای</b>\n\n"
        "اگر سرعت و پایداری براتون مهمه، این همون پلنه:\n"
        "• بازی بدون لگ و قطعی\n"
        "• کار با هوش مصنوعی بدون ریست\n"
        "• تماشا و دانلود بدون انتظار\n\n"
        "حتی در شلوغ‌ترین ساعات روز، سرعت شما پایدار می‌مونه."
    ),
    "eco": (
        "<b>پلن اکونومی</b>\n\n"
        "برای کارهای روزانه، با کمترین هزینه:\n"
        "• تلگرام و اینستاگرام همیشه در دسترس\n"
        "• وب‌گردی و چت بدون قطعی\n\n"
        "کیفیت معمولی، قیمت کم. اگر گیم یا هوش مصنوعی کار می‌کنید، پلن حرفه‌ای مناسب‌تره."
    ),
    "unlimited": (
        "<b>پلن نامحدود</b>\n\n"
        "با یک خرید، چند نفر استفاده می‌کنن:\n"
        "• نامحدود: 2 کاربر همزمان با 300 گیگ حجم\n"
        "• نامحدود ویژه: 3 کاربر همزمان با 400 گیگ حجم\n\n"
        "توجه: فقط همین تعداد کاربر اعلام‌شده می‌تونن بدون افت کیفیت و سرعت استفاده کنن."
    ),
}


def plans_menu(tier: str) -> InlineKeyboardMarkup:
    """Plan list for one tier. `manual` tiers are provisioned by the admin —
    that detail never reaches the user interface."""
    b = InlineKeyboardBuilder()
    for plan in settings.PLANS:
        if plan.get("tier", "vip") != tier:
            continue
        b.button(
            text=f"{plan['name']} — {plan['price']} {settings.CURRENCY}",
            callback_data=f"buy:{plan['id']}",
        )
    b.button(text="⬅️ بازگشت", callback_data="menu:plans")
    b.adjust(1)
    return b.as_markup()


def confirm_purchase(plan_id: str, coupon_code: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if coupon_code:
        b.button(text="🔄 تغییر کد تخفیف", callback_data=f"coupon:ask:{plan_id}")
        b.button(text="❌ حذف تخفیف", callback_data=f"coupon:remove:{plan_id}")
    else:
        b.button(text="🎟 کد تخفیف دارم", callback_data=f"coupon:ask:{plan_id}")
    b.button(text="✅ پرداخت کردم، رسید را ارسال می‌کنم", callback_data=f"payflow:{plan_id}")
    b.button(text="⬅️ بازگشت", callback_data="menu:plans")
    b.adjust(1)
    return b.as_markup()


def admin_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تایید", callback_data=f"admin_approve:{order_id}")
    b.button(text="❌ رد", callback_data=f"admin_reject:{order_id}")
    b.adjust(2)
    return b.as_markup()


def back_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ بازگشت به منو", callback_data="menu:home")
    return b.as_markup()


def renewable_subs_keyboard(subs: list[dict]) -> InlineKeyboardMarkup:
    """subs: list of {'panel_username': str, 'label': str}"""
    b = InlineKeyboardBuilder()
    for sub in subs:
        b.button(text=sub["label"], callback_data=f"renew_select:{sub['panel_username']}")
    b.button(text="⬅️ بازگشت", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def no_renewable_subs_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 خرید سرویس", callback_data="menu:plans")
    b.button(text="⬅️ بازگشت", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def renewal_plans_menu() -> InlineKeyboardMarkup:
    """Renewal plan list. Eco/Unlimited are manual-tier products without a
    bot panel account, so renewals for them are handled by the admin —
    only VIP plans are offered for automated renewal."""
    b = InlineKeyboardBuilder()
    for plan in settings.PLANS:
        if plan.get("tier", "vip") != "vip":
            continue
        b.button(
            text=f"{plan['name']} — {plan['price']} {settings.CURRENCY}",
            callback_data=f"renew_plan:{plan['id']}",
        )
    b.button(text="⬅️ بازگشت", callback_data="menu:renew")
    b.adjust(1)
    return b.as_markup()


def confirm_renewal(plan_id: str, coupon_code: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if coupon_code:
        b.button(text="🔄 تغییر کد تخفیف", callback_data=f"rcoupon:ask:{plan_id}")
        b.button(text="❌ حذف تخفیف", callback_data=f"rcoupon:remove:{plan_id}")
    else:
        b.button(text="🎟 کد تخفیف دارم", callback_data=f"rcoupon:ask:{plan_id}")
    b.button(text="✅ پرداخت کردم، رسید را ارسال می‌کنم", callback_data=f"renewpayflow:{plan_id}")
    b.button(text="⬅️ بازگشت", callback_data="menu:renew")
    b.adjust(1)
    return b.as_markup()
