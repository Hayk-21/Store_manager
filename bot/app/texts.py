"""Everything the bot says, in Armenian.

Only the bot's *own* wording lives here — buttons, prompts, summaries. Error
sentences come from the server in ``error.message`` and are printed verbatim, so
the two services cannot drift apart on what a failure means.
"""

from __future__ import annotations

# -- buttons ---------------------------------------------------------------

BTN_OPEN = "🟢 Բացել խանութը"
BTN_SEND_LOCATION = "📍 Ուղարկել տեղորոշումը"
BTN_SELL = "🧾 Վաճառել"
BTN_DEFECT = "🗑 Խոտան"
BTN_TAKE_CASH = "💸 Վերցնել դրամարկղից"
# Opens the correction screen: the whole shelf with a − and a + against each
# product. Adding a name that has never existed here is the button below.
BTN_ADD_ITEM = "➕ Ապրանք ավելացնել"
BTN_BRAND_NEW_ITEM = "🆕 Բոլորովին նոր ապրանք"
# A tickbox, not a way of paying. Tapping it sends nothing — see
# keyboards.payment_methods.
BTN_DELIVERY_OFF = "⬜ Առաքում"
BTN_DELIVERY_ON = "✅ Առաքում"
# Offered when the product has no wholesale price on it. Tapping it asks for the
# number rather than doing nothing — see keyboards.suggested_prices.
BTN_WHOLESALE_NO_PRICE = "📦 Մեծածախ — գրել գինը"
BTN_SKIP = "⏭ Բաց թողնել"
BTN_RESTOCK_SUBMIT = "✅ Հաստատել փոփոխությունները"
BTN_TRANSFERS = "🔄 Փոխանցումներ"
BTN_TRANSFER_ASK = "📥 Խնդրել ապրանք այլ խանութից"
BTN_TRANSFER_APPROVE = "✅ Հաստատել"
BTN_TRANSFER_REJECT = "❌ Մերժել"
BTN_UNDO_SALE = "↩️ Չեղարկել վաճառքը"
BTN_UNDO_STOCK = "↩️ Չեղարկել ուղղումը"
BTN_OTHER_PRICE = "✏️ Այլ գին"
BTN_STATUS = "📊 Վիճակ"
BTN_STOCK = "📦 Պահեստ"
BTN_END_SHIFT = "🚪 Ավարտել իմ հերթափոխը"
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
    "Խանութը բացելու համար ուղարկեք <b>կենդանի տեղորոշում</b> (Live Location)։\n\n"
    "1️⃣ Սեղմեք 📎 (ամրակը) ներքևում\n"
    "2️⃣ Ընտրեք «Location» / «Տեղորոշում»\n"
    "3️⃣ Սեղմեք <b>«Share My Live Location»</b>\n"
    "4️⃣ Ընտրեք <b>15 րոպե</b> և ուղարկեք\n\n"
    "Սովորական տեղորոշումը չի ընդունվում՝ այն կարելի է ձեռքով նշել քարտեզին։ "
    "Կենդանի տեղորոշումը ցույց է տալիս, որ դուք իսկապես խանութում եք։\n\n"
    "⚠️ Համակարգչի Telegram-ից չի աշխատի՝ օգտագործեք հեռախոսը։"
)
LOCATION_NOT_LIVE = (
    "⚠️ Սա սովորական տեղորոշում է։ Անհրաժեշտ է <b>կենդանի</b> (Live Location)։\n\n"
    "1️⃣ Սեղմեք 📎 → «Location»\n"
    "2️⃣ <b>«Share My Live Location»</b> (ոչ թե «Send My Current Location»)\n"
    "3️⃣ Ընտրեք 15 րոպե\n\n"
    "Սովորական տեղորոշումը կարելի է ձեռքով նշել քարտեզին, դրա համար այն "
    "չի ընդունվում։ Համակարգչից չի աշխատի՝ օգտագործեք հեռախոսը։"
)
LOCATION_FORWARDED = (
    "⚠️ Փոխանցված (forward) տեղորոշում չի ընդունվում։\n\n"
    "Ուղարկեք ձեր սեփական կենդանի տեղորոշումը՝ 📎 → «Location» → "
    "«Share My Live Location»։"
)
LOCATION_STALE = (
    "⚠️ Այս տեղորոշումը հին է։\n\n"
    "Ուղարկեք նորը՝ 📎 → «Location» → «Share My Live Location» → 15 րոպե։"
)
LOCATION_ONLY_FROM_PHONE = (
    "⚠️ Կենդանի տեղորոշումը միայն հեռախոսից է աշխատում։\n\n"
    "Համակարգչի Telegram-ը «Share My Live Location» չունի։ "
    "Բացեք բոտը <b>հեռախոսից</b> և այնտեղ սեղմեք «📎 → Location → "
    "Share My Live Location»։\n\n"
    "Եթե արդեն հեռախոսից եք՝ ստուգեք, որ Telegram-ին թույլատրված է "
    "օգտագործել տեղորոշումը (Settings → Privacy → Location)։"
)

SHIFT_OPENED = (
    "✅ Դուք հերթափոխի մեջ եք։\n"
    "Խանութ՝ <b>{store}</b>\n"
    "Հեռավորությունը՝ {distance} մ\n\n"
    "{drawer}"
    "📍 Կենդանի տեղորոշումը միացված է {minutes} րոպե։ Կարող եք թողնել այնպես՝ "
    "ձեզ ոչինչ պետք չէ անել։\n\n"
    "Աշխատեք հանգիստ։ Օրվա վերջում սեղմեք «🚪 Ավարտել իմ հերթափոխը» "
    "և գրեք, թե ինչ եք վաճառել։"
)
SHIFT_ALREADY_OPEN = "Դուք արդեն աշխատում եք «{store}»-ում {since}-ից։"
# What is in the drawer at the moment of opening up, said once, at the moment it
# matters. The float is the money the previous worker counted and left behind, and
# the arriving worker is answerable for it from now on — being told at closing time
# that the till was 1,000 heavier all along is too late to check anything.
#
# Two shapes, because joining a session somebody else opened is a different
# sentence: the drawer has been trading for six hours by then, so the float is only
# part of what is in it.
SHIFT_OPENED_DRAWER = "💰 Դրամարկղում՝ <b>{cash}</b>՝ նախորդ հերթափոխից մնացած։\n\n"
SHIFT_OPENED_DRAWER_PART = (
    "💰 Դրամարկղում՝ <b>{cash}</b>, որից <b>{carried}</b>՝ նախորդ հերթափոխից "
    "մնացած։\n\n"
)

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
    "Ձեր վաճառքը՝ կանխիկ {cash} · քարտ {card}"
)
SALE_ALREADY_RECORDED = "Այս վաճառքն արդեն գրանցված էր։"
# The fallback when the confirmation cannot be built. The sale is already in the
# books, so this has to read as success -- the alternative is a cashier entering
# the same sale a second time.
SALE_RECORDED_PLAINLY = "✅ Վաճառքը գրանցվեց։"
# The same, for an undo whose figures could not be rendered. The receipt is already
# reversed and the stock already back on the shelf, so the only question is what to
# say about it — and saying it plainly beats printing a number that may be wrong.
VOID_DONE_PLAINLY = "↩️ Վաճառքը չեղարկվեց։ Ապրանքը վերադարձվեց պահեստ։"
SELL_INTERRUPTED = (
    "Սկսած վաճառքը չեղարկվեց՝ ոչինչ չի գրանցվել։\n"
    "Սեղմեք կոճակը կրկին։"
)
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

# -- the shift, read back before it ends -------------------------------------
#
# One message, in sections, with the empty sections left out. Everything the shift
# accounts for: what went out, what was thrown away, what was corrected on the
# shelf, what came out of the drawer, what the drawer holds and what the shift
# pays. This is the last moment the worker can fix anything without the owner.

REVIEW_HEADER = "📋 <b>Հերթափոխի ստուգում</b> — {store}\n"
REVIEW_SOLD = (
    "🧾 <b>Գրանցված վաճառք</b>\n{rows}\n"
    "Չեկեր՝ {receipts} · Կանխիկ՝ {cash} · Քարտ՝ {card}\n"
    "Ընդամենը՝ <b>{total}</b>\n"
)
REVIEW_SOLD_ROW = "• {name} ×{quantity} — {total}"
REVIEW_SOLD_NOTHING = (
    "🧾 <b>Գրանցված վաճառք</b>\nԴեռ ոչինչ գրանցված չէ։\n"
)
# Its own section, under its own heading. An order that came by phone is money the
# shop took, but the worker did not sell it over the counter — adding it to «you
# sold» credited them with orders they had no hand in, and the same figure decided
# their bonus. Shown rather than hidden: they entered it, and the stock left because
# of it, so leaving it out of the write-up would make the shelf look wrong.
REVIEW_DELIVERED = (
    "🚚 <b>Առաքում</b>\n{rows}\n"
    "Պատվեր՝ {receipts}\n"
    "<i>Առաքումը ձեր վաճառքի մեջ չի հաշվվում։</i>\n"
)
# Which products went out and how many — and no money. The worker did not sell
# these and is not measured on them, so what they came to is the owner's business.
# What the worker needs from this section is that the stock left, so that the shelf
# they are about to be asked about adds up.
REVIEW_DELIVERED_ROW = "• {name} ×{quantity}"
REVIEW_WRITTEN_OFF = "🗑 <b>Խոտան</b>\n{rows}\n"
REVIEW_WRITTEN_OFF_ROW = "• {name} ×{quantity} — {reason}"
REVIEW_STOCK_FIXED = "📦 <b>Պահեստի ուղղումներ</b>\n{rows}\n"
REVIEW_STOCK_FIXED_ROW = "• {name}՝ {delta} → {count} հատ"
REVIEW_TAKEN_OUT = "💸 <b>Վերցված դրամարկղից</b>\n{rows}\n"
REVIEW_TAKEN_OUT_ROW = "• {amount} — {purpose}"
REVIEW_TRANSFERS = "🔄 <b>Փոխանցումներ</b>\n{rows}\n"
# The arrow points at this shop or away from it, so the direction is readable at a
# glance rather than worked out from the shop name.
REVIEW_TRANSFER_IN = "• ⬅️ {name} ×{quantity} — «{store}»-ից"
REVIEW_TRANSFER_OUT = "• ➡️ {name} ×{quantity} — «{store}»-ին"
REVIEW_NO_REASON = "առանց նշման"
# Said when a section was too long to print in full. Never left silent: a list that
# quietly stops looks like a complete list.
REVIEW_AND_MORE = "<i>… և ևս {more} տող։ Ամբողջը տեսանելի է ղեկավարի էջում։</i>"
# The drawer, and only the drawer. The card figure that used to sit beside it was
# the whole shop's card income for the session, deliveries included — money this
# worker did not take and is not measured on, printed under their own name at the
# one moment they are reading their day back. Nothing is lost by dropping it: card
# money is not in any drawer, and the worker's own card sales are in «Գրանցված
# վաճառք» a few lines above.
REVIEW_TILL = (
    "💰 <b>Դրամարկղը հիմա</b>\n"
    "Կանխիկ՝ <b>{cash}</b>\n"
    "Խանութի մնացորդը վերջին հաշվարկով՝ {float_}\n"
)
REVIEW_SALARY = "👤 <b>Ձեր աշխատավարձը</b>՝ {salary}\n"
REVIEW_SALARY_HALVED = (
    "<i>Դեռ {hours} ժամ չեք աշխատել, ուստի հաշվարկված է կիսով չափ։</i>\n"
)
REVIEW_FOOTER = (
    "\n<i>Ստուգեք ամեն ինչ։ Ներքևում գրեք միայն այն վաճառքը, որը վերևի "
    "ցուցակում չկա։</i>"
)

CLOSEOUT_ROW = "{index}. {name} ×{quantity} × {price} = <b>{total}</b> ({method}{kind})"
# Appended to a write-up row when the line did not go out at the shelf price.
KIND_WHOLESALE = ", մեծածախ"
KIND_CUSTOM = ", փոփոխված գին"
KIND_DELIVERY = ", առաքում"
CLOSEOUT_SUMMARY = (
    "🧾 <b>Ցուցակ</b>\n\n{rows}\n\n"
    "Կանխիկ՝ <b>{cash}</b>\nՔարտ՝ <b>{card}</b>\nԸնդամենը՝ <b>{total}</b>{delivery}"
)
# Appended when the list has an «առաքում» line in it. The three totals above are the
# counter alone, so a worker who types four phone orders is not shown a day they did
# not have; the rows themselves still carry their own amounts, because a list you
# cannot check is not a list you can be asked to confirm.
CLOSEOUT_SUMMARY_DELIVERY = (
    "\n<i>Առաքումով տողերը այս հաշվարկի մեջ չեն՝ դրանք ձեր վաճառքը չեն։</i>"
)
CLOSEOUT_EMPTY_BASKET = "Ցուցակը դատարկ է։ Այսօր վաճառք չի՞ եղել։"
# The same empty list, when the day is already in the books. Asking "was there no
# sale today?" over a screenful of receipts rung up an hour ago reads as the bot
# having lost them.
CLOSEOUT_NOTHING_TO_ADD = (
    "Նոր վաճառք չեք ավելացրել։\n"
    "Այս հերթափոխին արդեն գրանցված է {receipts} չեկ՝ <b>{total}</b>։"
)
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
    "Ձեր վաճառքը՝ կանխիկ {cash} · քարտ {card}"
)

# -- status and closing -----------------------------------------------------

STATUS = (
    "<b>{store}</b>\n"
    "Հերթափոխը՝ {since}-ից ({duration})\n\n"
    "Ձեր վաճառքը՝ {receipts} չեկ, {sold}\n"
    "{deliveries}\n"
    "Դրամարկղում կանխիկ՝ {cash}{carried}"
)
# Slotted under the drawer, and empty when the shop opened on an empty one. The
# drawer is not all takings: a shop keeps a float overnight — whatever the last
# person to lock up said they were leaving — and it is in there from the first
# minute of the shift. A worker who sold 5,000 in cash and is shown 6,000 has no
# way to tell where the other 1,000 came from, and every one of them assumes the
# difference is theirs until it is not.
STATUS_CARRIED = "\n<i>Որից {amount}՝ նախորդ հերթափոխից մնացած։</i>"
# Slotted into STATUS, and empty on a shift that has taken none. «Ձեր վաճառքը»
# above is this worker's own selling, and the figure their bonus is measured
# against; this is the shop's takings through the other door.
STATUS_DELIVERIES = "Առաքում՝ {receipts} պատվեր\n"
STATUS_NO_SHIFT = "Դուք հերթափոխի մեջ չեք։ Սեղմեք «{open_button}»։"

SHIFT_ENDED = (
    "🚪 Ձեր հերթափոխն ավարտված է։\n\n"
    "Տևողությունը՝ {duration}\n"
    "Վաճառք՝ {receipts} չեկ, {sold}\n"
    "Աշխատավարձ՝ {salary}\n"
)
# Appended when the shift also handled deliveries. Its own line, under the sales
# rather than inside them: the shop took that money, the worker did not sell it,
# and «Վաճառք» above is the figure their bonus is measured against.
SHIFT_ENDED_DELIVERIES = (
    "Առաքում՝ {receipts} պատվեր\n"
    "<i>Առաքումը վաճառքի մեջ չի հաշվվում։</i>\n"
)
# Appended when the wage came to half. A worker paid less than they expected
# will ask why, and the answer should already be on the screen.
SALARY_HALVED = (
    "\n<i>Հերթափոխը տևել է {hours} ժամից պակաս, ուստի աշխատավարձը հաշվվել է "
    "կիսով չափ։</i>\n"
)
# Said when the drawer was too thin to pay the whole wage — a day taken mostly on
# card. Without it the wage above looks like a mistake or a deduction; it is
# neither, and the worker should not have to work that out.
WAGE_STILL_OWED = (
    "\n⚠️ Դրամարկղում բավարար կանխիկ չկար։ Ձեռքին ստացել եք ավելի քիչ, "
    "մնացած <b>{owed}</b>-ը ղեկավարը կվճարի։\n"
)
STORE_STILL_OPEN = "\nԽանութը մնում է բաց՝ գործընկերները դեռ աշխատում են։"
# «Քարտով մուտք» is gone from both of these. It was the shop's card income for the
# whole session — every delivery in it — shown to the one worker who happened to be
# last out of the door, as though it were the takings of their day. What they sold on
# card is stated above, under their own name, and it is the only card figure a
# cashier has any use for.
STORE_CLOSED = (
    "\n🔴 Խանութը փակված է։\n"
    "Դրամարկղում կանխիկ՝ {cash}"
)
# The same line with the cash left out too. A drawer cannot hold less than nothing, so
# a negative figure there is a bookkeeping artefact and not something a cashier can act
# on — «Կանխիկ՝ -4,500 ֏» is the one a worker saw and reported.
STORE_CLOSED_NO_CASH = "\n🔴 Խանութը փակված է։"

# -- failures ---------------------------------------------------------------

NETWORK_TROUBLE = (
    "⚠️ Սերվերի հետ կապ չկա։ Փորձեք մի քանի վայրկյանից։\n"
    "Եթե խնդիրը կրկնվի, տեղեկացրեք ղեկավարին։"
)
UNEXPECTED = "⚠️ Անսպասելի սխալ։ Փորձեք կրկին։"
UNKNOWN_COMMAND = "Չհասկացա։ Օգտվեք ներքևի կոճակներից։"
# Said when a flow's state has gone — a restart, or a deploy — and the worker is
# still answering its questions. Names the cause, because "I don't understand"
# after they typed exactly what was asked reads as the bot calling them wrong.
LOST_THE_THREAD = (
    "Չհասկացա։ Հնարավոր է՝ գործողությունը ընդհատվել է։\n"
    "Սկսեք նորից ներքևի կոճակներից։"
)

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

# -- write-offs --------------------------------------------------------------

ASK_OTHER_PRICE = "Գրեք գինը թվով, օրինակ՝ 3200։"
# Asked when «Մեծածախ» was tapped on a product with no wholesale price set. The
# line is still recorded as a wholesale one — that is the point of asking here
# rather than sending the cashier to «Այլ գին».
ASK_WHOLESALE_PRICE = (
    "Այս ապրանքի մեծածախ գինը նշված չէ։\nԳրեք մեծածախ գինը թվով, օրինակ՝ 3000։"
)
UNDO_HINT = "Սխա՞լ գրանցեցիք։"

DEFECT_ASK_ITEM = (
    "🗑 <b>Խոտան ապրանք</b>\n\n"
    "Գրեք ապրանքի անունը կամ դրա մի մասը։ Այն կհանվի պահեստից, "
    "բայց վաճառք չի գրանցվի։"
)
DEFECT_ASK_QUANTITY = "«{item}» — քանի՞ հատ է խոտան։\nՊահեստում կա {available} հատ։"
# No cost figure. What the shop paid for the vape is the owner's business, and
# the owner sees the loss on the report; the cashier needs to know it was recorded
# and what is left on the shelf.
DEFECT_DONE = (
    "🗑 Դուրս գրվեց՝ <b>{item}</b> ×{quantity}\n"
    "Պահեստի մնացորդը՝ {remaining} հատ\n\n"
    "Վաճառք չի գրանցվել։"
)
DEFECT_DONE_PLAINLY = "🗑 Խոտանը գրանցվեց և հանվեց պահեստից։"

# -- taking money out of the till --------------------------------------------

# The reason comes first, and it is picked rather than typed. Two things follow
# from that: the ceiling is known before the number is asked for, so the question
# can say what it is; and the report reads back one spelling of «Ճաշ» instead of
# nine. What is written on the row is chosen by the server — these are the same
# strings, and a test holds them to it.
CASH_PURPOSE_LUNCH = "Ճաշ"
CASH_PURPOSE_DELIVERY = "Հայփոստ (առաքման վճար)"
BTN_CASH_LUNCH = f"🍽 {CASH_PURPOSE_LUNCH}"
BTN_CASH_DELIVERY = f"📦 {CASH_PURPOSE_DELIVERY}"
CASH_ASK_REASON = (
    "💸 <b>Վերցնել դրամարկղից</b>\n\n"
    "Ինչի՞ համար եք վերցնում։"
)
# The example is deliberately below the limit: an amount the server is about to
# refuse is a bad thing to suggest.
CASH_ASK_AMOUNT_LUNCH = (
    "🍽 <b>Ճաշ</b>\n\n"
    "Որքա՞ն եք վերցնում։ Գրեք գումարը թվով, օրինակ՝ 500։\n"
    "Մեկ հերթափոխի ընթացքում՝ առավելագույնը 1,000 ֏։"
)
# No ceiling named here, because there is none. The parcel costs what the post
# office charges; the only thing that can refuse it is an empty drawer.
CASH_ASK_AMOUNT_DELIVERY = (
    "📦 <b>Հայփոստ (առաքման վճար)</b>\n\n"
    "Որքա՞ն եք վերցնում։ Գրեք գումարը թվով, օրինակ՝ 2500։\n"
    "Սահմանաչափ չկա՝ որքան որ վճարել եք։"
)
CASH_BAD_AMOUNT = "Գրեք գումարը թվով, օրինակ՝ 500։"
CASH_OVER_LIMIT = (
    "❌ Շատ է։ Ճաշի համար մեկ հերթափոխի ընթացքում կարելի է վերցնել առավելագույնը "
    "<b>{limit}</b>։\n\n"
    "Գրեք ավելի փոքր գումար, կամ ավելիի համար դիմեք ղեկավարին։"
)
CASH_DONE = (
    "💸 Գրանցվեց՝ <b>{amount}</b>\n"
    "Նպատակը՝ {purpose}\n\n"
    "Դրամարկղում մնաց՝ {cash}"
)
CASH_DONE_PLAINLY = "💸 Գումարի վերցնումը գրանցվեց։"

# -- counting the drawer -----------------------------------------------------

# One count, at the end, by the person who was at the drawer. Counting at the start
# of a shift was dropped: it asked a worker to answer for a drawer somebody else had
# filled.
BTN_TILL_COUNT = "💰 Դրամարկղի մնացորդ"
BTN_TILL_CLOSE = "💰 Գրել դրամարկղի մնացորդը"
TILL_ASK_CLOSE = (
    "💰 <b>Դրամարկղի մնացորդը</b>\n\n"
    "Հաշվեք դրամարկղում մնացած կանխիկը և գրեք գումարը թվով։\n"
    "Այդքանը մնում է խանութում՝ հաջորդ հերթափոխի համար, "
    "մնացածը հանձնվում է ղեկավարին։"
)
TILL_BAD_AMOUNT = "Գրեք գումարը թվով, օրինակ՝ 40000։"
TILL_DONE_CLOSE = "💰 Գրանցվեց՝ դրամարկղում մնում է <b>{counted}</b>։\n"
# The figure the worker is about to act on — they are holding that money — so it is
# stated rather than left to be worked out from the other two.
TILL_HANDED_OVER = "Ղեկավարին հանձնվում է՝ <b>{handed}</b>։\n"
TILL_NOTHING_TO_HAND = "Ղեկավարին հանձնելու գումար չկա։\n"
TILL_DONE_PLAINLY = "💰 Դրամարկղի մնացորդը գրանցվեց։"
TILL_SKIPPED = (
    "Դրամարկղի հաշվարկը բաց թողնվեց։ Խանութի մնացորդը մնում է նույնը։"
)
# The invitation, carrying the button. Short on purpose: the full explanation
# belongs on the screen that asks for the number, and saying it twice in a row made
# the second message look like the bot repeating itself.
TILL_HANDOVER_PROMPT = (
    "Մնաց մեկ բան՝ գրեք, թե որքան կանխիկ եք թողնում խանութի դրամարկղում։"
)

# -- moving stock between shops ----------------------------------------------

TRANSFER_NOTHING_PENDING = (
    "🔄 <b>Փոխանցումներ</b>\n\n"
    "Այս պահին ձեր խանութից ապրանք չեն խնդրում։\n"
    "Կարող եք ինքներդ ապրանք խնդրել այլ խանութից։"
)
TRANSFER_INCOMING = (
    "🔄 <b>Փոխանցումներ</b>\n\n"
    "Ձեր խանութից խնդրում են՝\n{rows}\n\n"
    "Հաստատելուց հետո ապրանքը կհանվի ձեր պահեստից։"
)
TRANSFER_INCOMING_ROW = "• <b>{quantity}</b> հատ «{item}» → {store}"
TRANSFER_NO_OTHER_STORES = "Այլ բաց խանութ չկա, որտեղից ապրանք խնդրել։"
TRANSFER_PICK_STORE = "📥 <b>Ո՞ր խանութից</b>\n\nԸնտրեք խանութը։"
TRANSFER_PICK_ITEM = "«{store}» — ընտրեք ապրանքը կամ գրեք անվան մի մասը։"
TRANSFER_STORE_EMPTY = "«{store}»-ի պահեստում այս պահին ապրանք չկա։"
TRANSFER_ASK_QUANTITY = "«{item}» — քանի՞ հատ եք խնդրում։\nԱյդ խանութում կա {available} հատ։"
TRANSFER_TOO_MANY = "Այդ խանութում կա ընդամենը {available} հատ։"
TRANSFER_REQUESTED_OK = (
    "📤 Հարցումն ուղարկվեց՝ <b>{quantity}</b> հատ «{item}»\n"
    "Խանութը՝ {store}\n\n"
    "Ապրանքը կավելանա ձեր պահեստին այն բանից հետո, երբ այդ խանութի "
    "աշխատողը հաստատի։"
)
TRANSFER_REQUESTED_PLAINLY = "📤 Հարցումն ուղարկվեց։"
TRANSFER_APPROVED = (
    "✅ Հաստատվեց՝ <b>{quantity}</b> հատ «{item}» ուղարկվեց «{store}»։\n"
    "Ձեր պահեստից այդ քանակը հանված է։"
)
TRANSFER_REJECTED = "❌ Մերժվեց՝ {quantity} հատ «{item}» ({store})։\nՊահեստը չի փոխվել։"
TRANSFER_DECIDED_PLAINLY = "Հարցումը պատասխանվեց։"

# -- correcting the shelf ----------------------------------------------------

RESTOCK_INTRO = (
    "📦 <b>Պահեստի ուղղում</b> — {store}\n\n"
    "Ապրանքի կողքի ➖ և ➕ կոճակներով նշեք, թե քանիսն եք ավելացնում կամ հանում։\n"
    "Ոչինչ չի գրանցվի, մինչև չսեղմեք «Հաստատել»։"
)
RESTOCK_SCREEN = "📦 <b>Պահեստի ուղղում</b>\nԷջ {page}/{pages}"
RESTOCK_NO_CHANGES = "\n\n<i>Դեռ ոչինչ նշված չէ։</i>"
RESTOCK_CHANGED_HEADER = "\n\n<b>Փոփոխությունները՝</b>\n"
RESTOCK_CHANGED_ROW = "• {name}՝ {before} → <b>{after}</b> ({delta})"
RESTOCK_AT_ZERO = "Պահեստում այլևս չկա։"
RESTOCK_USE_THE_BUTTONS = (
    "Օգտվեք ➖ և ➕ կոճակներից՝ քանակը փոխելու համար։\n"
    "Եթե ապրանքը ցուցակում չկա, սեղմեք «🆕 Բոլորովին նոր ապրանք»։"
)
RESTOCK_NOTHING_YET = (
    "📦 <b>Պահեստը դատարկ է</b>\n\n"
    "Այս խանութում դեռ ապրանք գրանցված չէ։ Ավելացրեք առաջինը։"
)
RESTOCK_DONE = "✅ <b>Պահեստը թարմացվեց</b>\n\n{rows}"
RESTOCK_DONE_ROW = "• {name}՝ {delta} → {count} հատ"
RESTOCK_DONE_PLAINLY = "✅ Պահեստը թարմացվեց։"

# Offered on the confirmation itself. A cashier who typed 39 where they meant 37
# had no way back: the only fix was another correction, which needs the number the
# shelf had *before* — and the bot had just replaced it with the new one on screen.
UNDO_STOCK_HINT = "Սխա՞լ ուղղեցիք։"
RESTOCK_UNDONE = "↩️ <b>Ուղղումը չեղարկվեց</b>\n\n{rows}"
RESTOCK_UNDONE_PLAINLY = "↩️ Ուղղումը չեղարկվեց։"

# -- a new product, added at the counter -------------------------------------

NEW_ITEM_ASK_NAME = (
    "➕ <b>Նոր ապրանք</b>\n\n"
    "Գրեք ապրանքի անունը՝ այնպես, ինչպես կփնտրեք այն վաճառելիս։"
)
NEW_ITEM_ASK_NAME_AGAIN = "Գրեք ապրանքի անունը։"
NEW_ITEM_ASK_COUNT = "«{item}» — քանի՞ հատ եք ավելացնում պահեստին։"
NEW_ITEM_ASK_COST = "💰 <b>Ինքնարժեք</b>\n\nԻ՞նչ գնով եք գնել մեկ հատը։"
NEW_ITEM_ASK_PRICE = (
    "🏷 <b>Մանրածախ գին</b>\n\n"
    "Ի՞նչ գնով եք վաճառելու մեկ հատը սովորական գնորդին։"
)
# Blank is a real answer, not a gap: the owner's price sheet has a dash where a
# model is not sold wholesale, and storing 0 for it would read as "free".
NEW_ITEM_ASK_WHOLESALE = (
    "📦 <b>Մեծածախ գին</b>\n\n"
    "Ի՞նչ գնով եք վաճառելու մեծածախ։\n"
    "Եթե այս ապրանքը մեծածախ չեք վաճառում, սեղմեք «Բաց թողնել»։"
)
NEW_ITEM_DONE = (
    "✅ Ավելացվեց՝ <b>{item}</b>\n"
    "Պահեստում՝ {count} հատ · մանրածախ՝ {price}{wholesale}\n\n"
    "Այժմ կարող եք վաճառել այն։"
)
# Appended to the line above only when there is one.
NEW_ITEM_DONE_WHOLESALE = " · մեծածախ՝ {price}"
NEW_ITEM_DONE_PLAINLY = "✅ Ապրանքն ավելացվեց պահեստին։"
