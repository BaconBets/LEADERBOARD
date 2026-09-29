# Monthly giveaway leaderboard bot

## 1. Create the bot in Discord
1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy it (this is `DISCORD_TOKEN`). Never share it.
3. On the same tab, turn on **Server Members Intent** and **Message Content Intent**.
4. **OAuth2 → URL Generator**: tick `bot` and `applications.commands`. Under bot permissions tick
   View Channels, Send Messages, Embed Links, Read Message History, Add Reactions, and **Manage Server**
   (needed to see which invite someone used), **Manage Roles** (to move the champion role). Open the link and add the bot to your server.

## 2. Set up your server
Create `#leaderboard`, `#winners`, and a private staff channel, plus a `VIP` role and a `Monthly Champion` role.
For the champion role, turn on **Display role members separately** and give it a color so the winner stands out in the member list. In Server Settings → Roles, drag the bot's role **above** Monthly Champion, or it can't hand it out.
Turn on Developer Mode (User Settings → Advanced), then right-click the server, channels,
and role → **Copy ID**.

Let the bot send messages in `#leaderboard` and `#winners`, but make them read-only for
members so the board stays at the top.

## 3. Deploy on Railway
1. Put these files in a GitHub repo (private is fine).
2. On https://railway.com → **New Project → Deploy from GitHub repo** → pick it.
3. In the project, click **+ New → Database → PostgreSQL**.
4. Open your bot service → **Variables** and add everything from `.env.example`.
   For `DATABASE_URL`, use `${{Postgres.DATABASE_URL}}` so Railway links it automatically.
5. It deploys automatically. The logs should say `Logged in as ...`.

| Variable | What it is |
|---|---|
| `GUILD_ID` | Your server ID |
| `VIP_ROLE_ID` | The VIP role ID |
| `CHAMPION_ROLE_ID` | The Monthly Champion role ID |
| `LOYALTY_3_ROLE_ID` / `LOYALTY_6_ROLE_ID` / `LOYALTY_12_ROLE_ID` | Badge roles for 3, 6 and 12 months of VIP in a row (leave any blank to skip it) |
| `VIP_LOUNGE_CHANNEL_ID` | Optional: where the bot congratulates VIPs on new badges |
| `LEADERBOARD_CHANNEL_ID` | Where the live board is posted |
| `WINNERS_CHANNEL_ID` | Where monthly winners are announced |
| `STAFF_CHANNEL_ID` | Private channel for the review before payout |
| `WINS_CHANNEL_ID` | Your winning-slips channel. Image posts there earn +15 (max 3/day) |
| `ALLOWED_CHANNEL_IDS` | Comma-separated chat channels that earn points. Leave empty to count all |
| `EXCLUDED_USER_IDS` | Comma-separated user IDs kept out of the race (e.g. the owner) |
| `EXCLUDED_ROLE_IDS` | Comma-separated role IDs kept out of the race (e.g. Mod) |
| `TIMEZONE` | When days and months roll over, e.g. `America/Chicago` |
| `PRIZE_TEXT` / `VIP_PRIZE_TEXT` | Shown on the board and in announcements |

## What happens automatically
- Every 10 minutes the board in `#leaderboard` updates.
- On the 1st of each month the bot announces the top 3 and the top VIP in `#winners`,
  gives the #1 the Monthly Champion role (removing it from last month's champion), posts a point
  breakdown with red flags to your staff channel, and starts a fresh board that shows the champion at the top.
  Old months stay in the database. Nothing is deleted.

## Staff commands
- `/points_adjust member amount reason`: add or remove points (use a negative amount to remove).
- `/disqualify member [month]` and `/undisqualify member [month]`.
- `/vip_since member YYYY-MM-DD`: set when an existing VIP really started, so their loyalty badge is right.
- `/recrown month`: after disqualifying a winner, redo that month's winners and move the champion role.

## VIP extras
- **Loyalty badges:** the bot tracks how long each person has had the VIP role in a row and gives them the 3, 6 or 12-month badge role (only the highest one). If VIP lapses and comes back within 7 days, the streak is kept.
- **Recap DMs:** on the 1st, every VIP who earned points gets a private recap of their month. People with DMs closed are skipped.

## Member commands
- Daily check-in is automatic: a member's first message or reaction each day gives +10.
- `/daily`: optional, shows your streak (and checks you in if you haven't been active yet today).
- `/leaderboard`: private view of the board.
- Tabs on the board: This month, VIP race, Champions, My stats (each opens a private copy only you can see), plus How to earn.

## Changing the rules
All point values and limits are at the top of `bot.py` (e.g. `MSG_DAILY_CAP = 50`).
Change a number, push to GitHub, and Railway redeploys.
