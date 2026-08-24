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
`/promote` `/demote` `/fire` — fire strips WSP membership, staff, and rank roles

**Shifts**  
`/shift menu` — public start / pause / resume / end buttons  
`/shift data` — public duty board and leaderboard  
`/shift start` `status` `leaderboard` `history` `correct`

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
