# Wisconsin State Patrol — Lakeville Roleplay

Discord bot and owner-only Command Center for WSP in LVRP.

Start: copy `.env.example` to `.env`, then `python main.py`. Hosted at the Render web service. Only Discord IDs in `OWNER_IDS` can sign into the website.

Commands work as slash (`/shift menu`) and as text with `?` (`?shift menu`). Enable **Message Content Intent** on the Discord application so `?` commands work in the server.

---

## Systems

**Setup (owner)**  
`/setupserver` `/verifysetup` `/config` `/sync`

**Rank (HR and website)**  
`/promote` `/demote` `/fire`  
Promote and demote keep the matching rank role plus the High / Middle / Low band role. `/fire` also strips the on-duty role and extra WSP roles.

**Ranks**  
High: Superintendent, Colonel, Major, Captain, Lieutenant — 30 min weekly quota  
Middle: Sergeant — 75 min weekly quota  
Low: Master Trooper, Senior Trooper, Trooper, Probationary Trooper — 90 min weekly quota

**Shifts**  
`/shift menu` — start / pause / resume / end (certified patrol role required to start)  
`/shift data` — duty board and leaderboard  
`/shift status` `leaderboard` `history`  
`/shift admin start|end|edit|delete` — Middle Rank and above, for a member. Edit uses hours, minutes, and seconds.

**Quota**  
`/quota view` `leaderboard` `admin`  
Quota is taken from the High / Middle / Low rank roles. Missed quota notifies HR. It does not auto-punish. Approved LOA covers that window.

**Leave**  
`/loa menu` `request`  
`/loa active` — HR only  
Requests post in the LOA channel with Accept / Deny buttons (HR only).

**Command**  
`/dashboard` — overview plus **Reset shift data**  
`/help`

**Command Center website**  
Owner-only. Same live database as the bot. Promote, demote, fire, roster, shifts, quota, LOA, and shift reset from the browser.
