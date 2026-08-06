"""Everything the bot says, in Armenian.

Only the bot's *own* wording lives here — buttons, prompts, summaries. Error
sentences come from the server in ``error.message`` and are printed verbatim, so
the two services cannot drift apart on what a failure means.
"""

from __future__ import annotations

# -- buttons ---------------------------------------------------------------

BTN_OPEN = "🟢 Բացել խանութը"
BTN_SEND_LOCATION = "📍 Ուղարկել տեղորոշումը"
BTN_SELL = "🧾 Վաճառք"
BTN_UNDO = "↩️ Չեղարկել վերջինը"
BTN_STATUS = "📊 Վիճակ"
BTN_END_SHIFT = "🚪 Ավարտել իմ հերթափոխը"
BTN_CLOSE_STORE = "🔴 Փակել խանութը"
BTN_CANCEL = "✖️ Չեղարկել"
BTN_CASH = "💵 Կանխիկ"
BTN_CARD = "💳 Քարտ"
BTN_RETAIL = "🏪 Մանրածախ"
BTN_WHOLESALE = "📦 Մեծածախ"

# -- greetings and prompts --------------------------------------------------

WELCOME = (
    "Բարև, {name}։\n\n"
    "Աշխատանքը սկսելու համար սեղմեք «{open_button}» և ուղարկեք ձեր տեղորոշումը։"
)
WELCOME_ADMIN = (
    "✅ Ձեր Telegram հաշիվը կապվեց Store Manager-ին։\n\n"
    "Այժմ կարող եք մուտք գործել կայք՝ ձեր Telegram օգտանունով։ "
    "Մուտքի կոդը կստանաք հենց այստեղ։\n\n"
    "Դուք գրանցված չեք որպես վաճառող, ուստի հերթափոխի կոճակներ չկան։"
)
WELCOME_ON_SHIFT = (
    "Բարև, {name}։\n"
    "Դուք աշխատում եք «{store}»-ում {since}-ից։"
)
ASK_LOCATION = (
    "Ուղարկեք ձեր տեղորոշումը, որպեսզի համակարգը հասկանա, թե որ խանութում եք։\n\n"
    "Սեղմեք ներքևի «{button}» կոճակը։"
)
LOCATION_ONLY_FROM_PHONE = (
    "⚠️ Տեղորոշումը չստացվեց։\n\n"
    "Համակարգչի Telegram-ը տեղորոշում ուղարկել չի կարող։ "
    "Բացեք բոտը <b>հեռախոսից</b> և այնտեղ սեղմեք «{button}» կոճակը։\n\n"
    "Եթե արդեն հեռախոսից եք՝ ստուգեք, որ Telegram-ին թույլատրված է "
    "օգտագործել տեղորոշումը (Settings → Privacy → Location)։"
)

SHIFT_OPENED = (
    "✅ Դուք հերթափոխի մեջ եք։\n"
    "Խանութ՝ <b>{store}</b>\n"
    "Հեռավորությունը՝ {distance} մ\n\n"
    "Վաճառք գրանցելու համար պարզապես գրեք ապրանքի անունը։"
)
SHIFT_ALREADY_OPEN = "Դուք արդեն աշխատում եք «{store}»-ում {since}-ից։"

# -- selling ----------------------------------------------------------------

ASK_ITEM = "Գրեք ապրանքի անունը կամ դրա մի մասը։"
NOTHING_FOUND = (
    "«{query}» — այդպիսի ապրանք չգտնվեց։\n"
    "Փորձեք անվան մեկ այլ հատված։"
)
CHOOSE_ITEM = "Ընտրեք ապրանքը՝"
OUT_OF_STOCK_HINT = "(պահեստում չկա)"
# Shown as a Telegram alert on the button itself, so it must stay short and
# carry no markup.
OUT_OF_STOCK_ALERT = (
    "«{item}» պահեստում չկա։\n\nԸնտրեք այլ ապրանք կամ համալրեք պահեստը։"
)
ASK_QUANTITY = "«{item}» — քանի՞ հատ։\nՊահեստում կա {available} հատ։"
BAD_QUANTITY = "Գրեք քանակը թվով, օրինակ՝ 2։"
NOT_ENOUGH_STOCK = (
    "«{item}» — պահեստում կա ընդամենը {available} հատ, դուք գրեցիք {requested}։\n"
    "Գրեք ավելի փոքր թիվ։"
)
QUANTITY_TOO_BIG = "Չափազանց մեծ թիվ։ Առավելագույնը {limit} հատ։"
ASK_PRICE_KIND = "«{item}» ×{quantity}\n\nՈ՞ր գնով եք վաճառում։"
ASK_PAYMENT = "«{item}» ×{quantity} = <b>{total}</b>\n\nԻնչպե՞ս վճարեց հաճախորդը։"
SALE_DONE = (
    "✅ Գրանցվեց՝ <b>{item}</b> ×{quantity} — {total} ({method})\n"
    "Մնացորդը՝ {remaining} հատ\n\n"
    "Կանխիկ՝ {cash} · Քարտ՝ {card}"
)
SALE_ALREADY_RECORDED = "Այս վաճառքն արդեն գրանցված էր։"
CANCELLED = "Չեղարկվեց։"

VOID_DONE = (
    "↩️ Չեղարկվեց վերջին վաճառքը՝ {total}։\n"
    "Ապրանքը վերադարձվեց պահեստ։\n\n"
    "Կանխիկ՝ {cash} · Քարտ՝ {card}"
)

# -- status and closing -----------------------------------------------------

STATUS = (
    "<b>{store}</b>\n"
    "Հերթափոխը՝ {since}-ից ({duration})\n\n"
    "Ձեր վաճառքը՝ {receipts} չեկ, {sold}\n"
    "Խանութի կանխիկը՝ {cash}\n"
    "Խանութի քարտը՝ {card}"
)
STATUS_NO_SHIFT = "Դուք հերթափոխի մեջ չեք։ Սեղմեք «{open_button}»։"

SHIFT_ENDED = (
    "🚪 Ձեր հերթափոխն ավարտված է։\n\n"
    "Տևողությունը՝ {duration}\n"
    "Վաճառք՝ {receipts} չեկ, {sold}\n"
    "Աշխատավարձ՝ {salary}\n"
)
STORE_STILL_OPEN = "\nԽանութը մնում է բաց՝ գործընկերները դեռ աշխատում են։"
STORE_CLOSED = (
    "\n🔴 Խանութը փակված է։\n"
    "Կանխիկ՝ {cash} · Քարտ՝ {card}"
)
CONFIRM_CLOSE_STORE = (
    "Փակե՞լ խանութը բոլորի համար։\n"
    "Բոլոր բաց հերթափոխերն ավարտվելու են, աշխատավարձերը՝ հանվելու կանխիկից։"
)
BTN_CONFIRM_CLOSE = "Այո, փակել"

# -- failures ---------------------------------------------------------------

NETWORK_TROUBLE = (
    "⚠️ Սերվերի հետ կապ չկա։ Փորձեք մի քանի վայրկյանից։\n"
    "Եթե խնդիրը կրկնվի, տեղեկացրեք ղեկավարին։"
)
UNEXPECTED = "⚠️ Անսպասելի սխալ։ Փորձեք կրկին։"
UNKNOWN_COMMAND = "Չհասկացա։ Օգտվեք ներքևի կոճակներից։"

HELP = (
    "<b>Ինչպես օգտվել</b>\n\n"
    "1. Խանութ հասնելիս սեղմեք «{open_button}» և ուղարկեք տեղորոշումը։\n"
    "2. Վաճառք գրանցելու համար գրեք ապրանքի անունը, ընտրեք ցանկից, "
    "նշեք քանակը և վճարման ձևը։\n"
    "3. Սխալվե՞լ եք — սեղմեք «{undo_button}»։\n"
    "4. Աշխատանքն ավարտելիս սեղմեք «{end_button}»։\n\n"
    "Բոլոր հաշվարկները կատարվում են ավտոմատ։"
)
