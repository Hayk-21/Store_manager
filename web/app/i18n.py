"""Armenian is the source language; English is a translation of it.

Every string in the templates and the services is written in Armenian and looked
up here to be translated. Keeping the Armenian *as the key* has one property that
matters more than tidiness: a string nobody has translated yet renders as correct
Armenian rather than as a blank, a key, or English. Adding a page cannot break
the Armenian site, and forgetting a translation degrades to the language most of
the users read anyway.

The cost is that fixing an Armenian typo means fixing it in two places. That is
the right trade here — the Armenian is read by everybody, so it gets corrected,
and the catalogue is checked by a test that fails on any key no template uses.

Terminology, fixed here so it cannot drift between pages:

===========================  ==========================  ======================
concept                      Armenian                    English
===========================  ==========================  ======================
a store being open, once     աշխատաշրջան                 session
one worker's stint           հերթափոխ                    shift
the drawer                   դրամարկղ                    till
what was sold                հասույթ                     revenue
what was kept                շահույթ                     profit
cost to us                   ինքնարժեք                   cost price
one line of a sale           վաճառք                      sale
===========================  ==========================  ======================

«Հերթափոխ» used to mean both of the first two, which is how a *closed store* came
to say "Հերթափոխը բաց չէ" — a sentence about shifts, on a page where no shift was
involved. They are different things and now have different words.
"""

from __future__ import annotations

DEFAULT = "hy"

# Shown in the language switcher, each in its own language — a person looking for
# English should not have to read Armenian to find it.
LANGUAGES = {"hy": "Հայերեն", "en": "English"}


def normalise(language: str | None) -> str:
    """Anything unrecognised falls back to Armenian rather than raising.

    Language arrives from a form field, a database column and a bot request, and
    a bad value in any of them should show the wrong language at worst — never
    take the page down.
    """
    return language if language in LANGUAGES else DEFAULT


EN: dict[str, str] = {
    # -- chrome ---------------------------------------------------------------
    "Խանութներ": "Stores",
    "Աշխատողներ": "Staff",
    "Ծախսեր": "Expenses",
    "Հաշվետվություններ": "Reports",
    "Վիճակագրություն": "Statistics",
    "Պատմություն": "History",
    "Ելք": "Sign out",
    "Լեզու": "Language",
    "բեռնվում է…": "loading…",

    # -- signing in -----------------------------------------------------------
    "Մուտք": "Sign in",
    "Կոդի հաստատում": "Enter your code",
    "Սխալ": "Error",
    "Մուտք գործեք ձեր Telegram օգտանունով։": "Sign in with your Telegram username.",
    "Telegram օգտանուն": "Telegram username",
    "Ուղարկել կոդը": "Send me a code",
    "Կոդը Telegram-ից": "Code from Telegram",
    "Բոտը ձեզ կուղարկի 6-նիշ կոդ Telegram-ով։":
        "The bot will send you a 6-digit code on Telegram.",
    "Առաջին անգամ մուտք գործելուց առաջ բացեք":
        "Before signing in for the first time, open",
    "Telegram-ում և սեղմեք «Start» — առանց դրա բոտը ձեզ գրել չի կարող։":
        "in Telegram and press “Start” — until you do, the bot cannot message you.",
    "Կոդն ուղարկվեց": "The code was sent to",
    "Չե՞ք ստացել": "Didn’t get it",
    "փորձեք կրկին": "try again",
    "← Վերադառնալ խանութներ": "← Back to stores",

    # -- stores ---------------------------------------------------------------
    "խանութ": "store",
    "բաց": "open",
    "փակ": "closed",
    "Այսօր կանխիկ": "Cash today",
    "Այսօր քարտ": "Card today",
    "Այսօր ընդամենը": "Total today",
    "Դրամարկղում": "In the till",
    "Դրամարկղ": "Till",
    "աշխատող": "on shift",
    "բացվել է": "opened",
    "վաճառք այսօր": "sales today",
    "օրը սկսվում է": "the day starts at",
    "Դեռ ոչ մի խանութ չկա։ Ավելացրեք առաջինը ներքևում։":
        "No stores yet. Add your first one below.",
    "Նոր խանութ": "New store",
    "Անվանում": "Name",
    "Հասցե": "Address",
    "Տեղադրությունը քարտեզի վրա": "Where it is on the map",
    "📍 Իմ տեղը": "📍 My location",
    "Լայնություն": "Latitude",
    "Երկայնություն": "Longitude",
    "Շառավիղ (մ)": "Radius (m)",
    "Օրվա սկիզբ": "Day starts at",
    "Ավելացնել": "Add",
    "Սեղմեք քարտեզին կամ քաշեք նշիչը՝ կետը նշելու համար։ Եթե հենց հիմա խանութում եք, "
    "ամենաճիշտը «Իմ տեղը» կոճակն է։ Կապույտ շրջանը ցույց է տալիս այն տարածքը, որտեղից "
    "աշխատողը կարող է բացել այս խանութը։":
        "Click the map or drag the marker to place the point. If you are standing in "
        "the shop right now, “My location” is the most accurate option. The blue "
        "circle is the area a worker can open this store from.",
}
