# Wisconsin State Patrol — Lakeville Roleplay

Discord bot and owner-only Command Center for WSP in LVRP.

Start: copy `.env.example` to `.env`, then `python main.py`. Hosted at the Render web service. Only Discord IDs in `OWNER_IDS` can sign into the website.

---

## Systems

**Setup**  
`/setupserver` `/verifysetup` `/config` `/sync` `/resetserver`

**Personnel**  
`/personnel add` `note` `transfer` `suspend` `remove` `reinstate` `history`  
`/profile`

**Rank**  
`/promote` `/demote` `/fire`  
Promote and demote keep the matching rank role plus the High / Middle / Low band role. `/fire` also strips the on-duty role and the extra WSP roles listed in config.

**Ranks**  
High: Superintendent, Colonel, Major, Captain, Lieutenant  
Middle: Sergeant  
Low: Master Trooper, Senior Trooper, Trooper, Probationary Trooper

**Shifts**  
`/shift menu` — public start / pause / resume / end buttons  
`/shift data` — public duty board and leaderboard  
`/shift start` `status` `leaderboard` `history` `correct`  
Starting a shift grants the on-duty role. Pause, end, fire, and shift reset remove it.

**Quota**  
`/quota view` `leaderboard` `admin`  
Missed quota notifies HR. It does not auto-punish. Approved LOA covers that window.

**Leave**  
`/loa menu` `request` `approve` `deny` `active`

**Command**  
`/dashboard` — overview plus **Reset shift data** (clears shifts and duty quota totals)  
`/audit` `/help`

**Command Center website**  
Owner-only. Same live database as the bot. Promote, demote, fire, roster, shifts, quota, LOA, and shift reset from the browser.
