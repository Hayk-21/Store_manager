"""Everything the bot says, in Armenian.

Only the bot's *own* wording lives here — buttons, prompts, summaries. Error
sentences come from the server in ``error.message`` and are printed verbatim, so
the two services cannot drift apart on what a failure means.
"""

from __future__ import annotations

# -- buttons ---------------------------------------------------------------

BTN_OPEN = "🟢 Բացել խանութը"
BTN_SEND_LOCATION = "📍 Ուղարկել տեղորոշումը"
BTN_STATUS = "📊 Վիճակ"
BTN_STOCK = "📦 Պահեստ"
BTN_END_SHIFT = "🚪 Ավարտել իմ հերթափոխը"
BTN_CLOSE_STORE = "🔴 Փակել խանութը"
BTN_CANCEL = "✖️ Չեղարկել"
BTN_CASH = "💵 Կանխիկ"
BTN_CARD = "💳 Քարտ"
BTN_RETAIL = "🏪 Մանրածախ"
BTN_WHOLESALE = "📦 Մեծածախ"
BTN_CO_ADD = "➕ Ավելացնել ապրանք"
BTN_CO_REMOVE = "↩️ Ջնջել վերջինը"
BTN_CO_DONE = "✅ Ավարտել ցուցակը"
BTN_CO_ABANDON = "✖️ Չեղարկել ամբողջը"
BTN_CO_SUBMIT = "✅ Հաստատել և ավարտել հերթափոխը"
BTN_CO_SUBMIT_CLOSE = "🔴 Հաստատել և փակել խանութը"

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
LOCATION_NOT_LIVE = (
    "⚠️ Անհրաժեշտ է <b>կենդանի</b> տեղորոշում (live location)։\n\n"
    "Telegram-ում սեղմեք 📎 → «Տեղորոշում» → <b>«Share My Live Location»</b>։\n"
    "Սովորական տեղորոշումը կարելի է ձեռքով նշել քարտեզին, դրա համար այն չի ընդունվում։"
)
LOCATION_FORWARDED = (
    "⚠️ Փոխանցված (forward) տեղորոշում չի ընդունվում։\n\n"
    "Սեղմեք ներքևի «{button}» կոճակը։"
)
LOCATION_STALE = (
    "⚠️ Այս տեղորոշումը հին է։\n\n"
    "Ուղարկեք ձեր ընթացիկ դիրքը՝ ներքևի «{button}» կոճակով։"
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
    "Աշխատեք հանգիստ։ Օրվա վերջում սեղմեք «🚪 Ավարտել իմ հերթափոխը» "
    "և գրեք, թե ինչ եք վաճառել։"
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

# -- end-of-shift write-up ---------------------------------------------------

CLOSEOUT_START = (
    "🧾 <b>Հերթափոխի ամփոփում</b>\n\n"
    "Գրեք, թե ինչ եք վաճառել այսօր։ Յուրաքանչյուր ապրանքի համար կնշեք "
    "քանակը, գինը և վճարման ձևը։\n\n"
    "Գրեք ապրանքի անունը կամ սեղմեք «➕ Ավելացնել ապրանք»։"
)
CLOSEOUT_ASK_ITEM = "Գրեք ապրանքի անունը կամ դրա մի մասը։"
CLOSEOUT_ASK_QUANTITY = "«{item}» — քանի՞ հատ վաճառեցիք։\nՊահեստում կա {available} հատ։"
CLOSEOUT_ASK_PRICE = (
    "«{item}» ×{quantity}\n\n"
    "Ի՞նչ գնով վաճառեցիք <b>մեկ հատը</b>։\n"
    "Սեղմեք ներքևի կոճակը կամ գրեք գինը թվով (օրինակ՝ {suggested})։"
)
CLOSEOUT_BAD_PRICE = "Գրեք գինը թվով, օրինակ՝ 3500։"
CLOSEOUT_ROW = "{index}. {name} ×{quantity} × {price} = <b>{total}</b> ({method})"
CLOSEOUT_SUMMARY = (
    "🧾 <b>Ցուցակ</b>\n\n{rows}\n\n"
    "Կանխիկ՝ <b>{cash}</b>\nՔարտ՝ <b>{card}</b>\nԸնդամենը՝ <b>{total}</b>"
)
CLOSEOUT_EMPTY_BASKET = "Ցուցակը դատարկ է։ Այսօր վաճառք չի՞ եղել։"
CLOSEOUT_CONFIRM_PROMPT = (
    "Ամեն ինչ ճի՞շտ է։ Հաստատելուց հետո փոփոխություն կարող է անել միայն ղեկավարը։"
)
CLOSEOUT_REMOVED = "Ջնջվեց՝ «{name}»։"
CLOSEOUT_NOTHING_TO_REMOVE = "Ջնջելու բան չկա։"
CLOSEOUT_TOO_MANY = "Առավելագույնը {limit} տող։ Հաստատեք եղածը և սկսեք նորը։"
CLOSEOUT_ABANDONED = (
    "Չեղարկվեց։ Ոչինչ չի գրանցվել, հերթափոխը դեռ բաց է։"
)

STOCK_HEADER = "<b>{store}</b> — պահեստ\n{lines} անվանում · {units} հատ\n"
STOCK_ROW = "• {name} — <b>{count}</b> հատ · {price}"
STOCK_EMPTY_HEADER = "\n<i>Պահեստում չկա՝</i>"
STOCK_EMPTY = "Այս խանութում ապրանք դեռ գրանցված չէ։"

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
    "2. Աշխատեք հանգիստ — բոտին դիպչելու կարիք չկա։\n"
    "3. «{stock_button}» — տեսնել, թե ինչ կա պահեստում։\n"
    "4. Աշխատանքն ավարտելիս սեղմեք «{end_button}» և գրեք, թե ինչ եք վաճառել՝ "
    "քանակը, գինը և վճարման ձևը։ Գինը կարող եք փոխել, եթե այլ գնով եք վաճառել։\n"
    "5. Ցուցակը ստուգելուց հետո հաստատեք։\n\n"
    "Բոլոր հաշվարկները կատարվում են ավտոմատ։"
)
